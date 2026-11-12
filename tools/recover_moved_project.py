"""Rebase retained state and cover paths after moving the whole scraper folder."""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import load_settings, run_lock
from state_files import read_state, write_json


def recover(root, old_root, report_folder):
    """Preserve queues and rebase only files verified at their new location."""
    root, old_root, report_folder = Path(root).resolve(), Path(old_root).resolve(), Path(report_folder)
    if old_root.exists() or not root.is_dir():
        raise ValueError('Stara lokalizacja musi być nieobecna, a nowa musi istnieć')
    settings = load_settings(root / 'config.json')
    changes = {}
    with run_lock(root / 'scraper.lock'):
        state = read_state(root / 'state.json')
        write_json(report_folder / 'state_before.json', state)
        for site, primary in settings['databases'].items():
            if not primary.is_file():
                continue
            old_database = str(old_root / 'data' / primary.name)
            checkpoint = root / 'state' / site / 'run.json'
            local = read_state(checkpoint, {site: state.get(site, {})})
            for mapping in (state, local):
                if mapping.get(site, {}).get('database') == old_database:
                    mapping[site]['database'] = str(primary)
            if checkpoint.exists():
                write_json(checkpoint, local)
            updates = {'sources': [], 'covers': [], 'missing_covers': 0}
            for relative, table, column in ((f'state/{site}/coverage.db', 'sources', 'database_path'),
                                             (f'state/{site}/{site}_reviews.db', 'source', 'path')):
                path = root / relative
                if not path.is_file():
                    continue
                with closing(sqlite3.connect(path)) as conn:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    if not conn.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (old_database,)).fetchone():
                        continue
                    entry = {'file': str(path), 'table': table, 'column': column, 'old': old_database, 'new': str(primary)}
                    updates['sources'].append(entry)
                    write_json(report_folder / f'{site}_undo.json', updates)
                    with conn:
                        conn.execute(f'UPDATE {table} SET {column}=:new WHERE {column}=:old', {'new': str(primary), 'old': old_database})
            with closing(sqlite3.connect(primary, timeout=60)) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                table, column = ('covers', 'local_path') if 'covers' in tables else ('books', 'cover_local_path')
                prefix = str(old_root) + '\\'
                rows = conn.execute(f'SELECT id,{column} FROM {table} WHERE {column} LIKE :prefix', {'prefix': prefix + '%'}).fetchall()
                for row_id, old in rows:
                    if not old.startswith(prefix):
                        continue
                    new = root / old[len(prefix):]
                    if new.is_file():
                        updates['covers'].append({'id': row_id, 'old': old, 'new': str(new)})
                    else:
                        updates['missing_covers'] += 1
                write_json(report_folder / f'{site}_undo.json', updates)
                with conn:
                    conn.executemany(f'UPDATE {table} SET {column}=:new WHERE id=:id AND {column}=:old', updates['covers'])
            changes[site] = {'sources': len(updates['sources']), 'covers': len(updates['covers']), 'missing_covers': updates['missing_covers']}
        write_json(root / 'state.json', state)
        write_json(report_folder / 'recovery.json', changes)
    return changes


if __name__ == '__main__':
    print(json.dumps(recover(ROOT, ROOT / 'scraper_glowny', ROOT / 'reports/startup_recovery_20261017'), indent=2))
