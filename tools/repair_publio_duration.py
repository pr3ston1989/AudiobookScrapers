"""Repair missing Publio durations from retained metadata without network access."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import run_lock
from parse_books import duration
from quality import assess


def repair(root, apply=False):
    """Back up both databases and update only recoverable missing durations."""
    root = Path(root)
    primary = root / 'data/publio.db'
    coverage = root / 'state/publio/coverage.db'
    if not primary.is_file() or not coverage.is_file():
        raise FileNotFoundError('Brak istniejącej bazy Publio lub zapisanych metadanych')
    with run_lock(root / 'scraper.lock'), closing(sqlite3.connect(primary)) as conn:
        conn.execute('ATTACH DATABASE :path AS coverage', {'path': str(coverage)})
        rows = conn.execute("""SELECT b.id,b.url,b.cover_local_path,m.product_key,m.data,p.status
            FROM books b JOIN coverage.products p ON p.site='publio' AND p.url=b.url
            JOIN coverage.metadata m ON m.site=p.site AND m.product_key=p.product_key
            WHERE COALESCE(b.duration_minutes,0)<=0""").fetchall()
        changes = []
        for book_id, url, cover_path, key, raw, status in rows:
            data = json.loads(raw)
            minutes = duration(data.get('length_str')) or duration((data.get('source_details') or {}).get('format'))
            if not minutes:
                continue
            data['duration_minutes'] = minutes
            quality = assess('publio', data)
            if not cover_path or not Path(cover_path).is_file():
                quality['missing_required'].append('cover_local_file')
                quality['complete'] = False
            changes.append((book_id, url, key, data, quality, status))
        result = {'repairable': len(changes), 'applied': 0, 'completed': 0}
        if not apply or not changes:
            return result
        backup = root / 'reports/publio_woblink_fix' / datetime.now().strftime('backup_%Y%m%d_%H%M%S_%f')
        backup.mkdir(parents=True)
        for source in (primary, coverage):
            with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(backup / source.name)) as dst:
                src.backup(dst)
        result['backup'] = str(backup)
        with conn:
            for book_id, url, key, data, quality, status in changes:
                conn.execute('UPDATE books SET duration_minutes=:minutes WHERE id=:id AND COALESCE(duration_minutes,0)<=0',
                             {'minutes': data['duration_minutes'], 'id': book_id})
                conn.execute("UPDATE coverage.metadata SET data=:data WHERE site='publio' AND product_key=:key",
                             {'data': json.dumps(data, ensure_ascii=False), 'key': key})
                # Innych błędów i zaplanowanych odświeżeń nie zamykamy przy okazji.
                if status == 'incomplete':
                    conn.execute("""UPDATE coverage.products SET status=:status,details=:details,
                        attempts=CASE WHEN :complete THEN 0 ELSE attempts END,
                        retry_at=CASE WHEN :complete THEN 0 ELSE retry_at END
                        WHERE site='publio' AND product_key=:key""",
                        {'status': 'verified' if quality['complete'] else 'incomplete',
                         'details': json.dumps(quality, ensure_ascii=False), 'complete': quality['complete'], 'key': key})
                    if quality['complete']:
                        conn.execute("UPDATE scrape_queue SET status='completed' WHERE url=:url AND status='incomplete'", {'url': url})
                        result['completed'] += 1
                result['applied'] += 1
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair(ROOT, args.apply), ensure_ascii=False, indent=2))
