"""Relocate verified SQLite snapshots without losing committed WAL data."""

import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path

from main import ROOT, run_lock, write_json


def signature(conn):
    """Return schema and row counts for every existing table."""
    schema = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    tables = [name for kind, name, _ in schema if kind == "table"]
    counts = {name: conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
    return {"schema": schema, "counts": counts}


def migrate():
    config_path = ROOT / "config.json"
    settings = json.loads(config_path.read_text(encoding="utf-8"))
    if all((ROOT / relative).resolve() == (ROOT / "data" / f"{site}.db").resolve()
           for site, relative in settings["databases"].items()):
        print("Bazy są już przeniesione; zachowuję raport migracji.")
        return
    data_dir = ROOT / "data"
    archive = ROOT / "legacy_backup"
    data_dir.mkdir(exist_ok=True)
    archive.mkdir(exist_ok=True)
    report = {}
    moves = []
    workspace = ROOT.parent.resolve()
    with run_lock(ROOT / "scraper.lock"), ExitStack() as stack:
        for site, relative in settings["databases"].items():
            source = (ROOT / relative).resolve()
            target = (data_dir / f"{site}.db").resolve()
            if source == target:
                continue
            if not source.is_relative_to(workspace) or not target.is_relative_to(ROOT.resolve()):
                raise ValueError(f"Ścieżka poza workspace: {source}")
            if target.exists():
                raise FileExistsError(f"Cel już istnieje, nie nadpisuję: {target}")
            print(f"{site}: blokuję zapis do {source}", flush=True)
            guard = stack.enter_context(closing(sqlite3.connect(source, timeout=5)))
            guard.execute("BEGIN IMMEDIATE")
            reader = stack.enter_context(closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)))
            expected = signature(reader)
            if expected["counts"].get("books", 0) == 0:
                raise ValueError(f"Odmowa przeniesienia pustej bazy: {source}")
            temporary = target.with_suffix(".db.tmp")
            last_bucket = [-1]
            def progress(status, remaining, total):
                bucket = (total - remaining) // 65536
                if bucket != last_bucket[0]:
                    print(f"{site}: kopia {total - remaining}/{total} stron SQLite", flush=True)
                    last_bucket[0] = bucket
            with closing(sqlite3.connect(temporary)) as destination:
                reader.backup(destination, pages=2048, progress=progress, sleep=0.05)
                checks = [row[0] for row in destination.execute("PRAGMA quick_check")]
                if checks != ["ok"] or signature(destination) != expected:
                    raise RuntimeError(f"Weryfikacja kopii {site} nie przeszła: {checks[:3]}")
            temporary.replace(target)
            archived = (archive / f"{site}_{source.name}").resolve()
            if archived.exists():
                raise FileExistsError(f"Archiwum już istnieje: {archived}")
            moves.append((source, archived))
            settings["databases"][site] = f"data\\{site}.db"
            report[site] = {"source": str(source), "target": str(target), "archived_source": str(archived),
                            "counts": expected["counts"], "schema_unchanged": True, "quick_check": "ok"}
            print(f"{site}: zweryfikowano {expected['counts']['books']} książek, schemat bez zmian", flush=True)
        # Konfigurację przełączamy dopiero po weryfikacji wszystkich czterech kopii.
        write_json(config_path, settings)
    # Po zamknięciu połączeń pliki źródłowe trafiają do odwracalnego archiwum.
    for source, archived in moves:
        for suffix in ("", "-wal", "-shm"):
            original = Path(str(source) + suffix)
            destination = Path(str(archived) + suffix)
            if original.exists():
                original.rename(destination)
    write_json(ROOT / "reports" / "database_migration.json", report)
    print("Gotowe: wszystkie aktywne bazy są w scraper_glowny\\data.", flush=True)


if __name__ == "__main__":
    migrate()
