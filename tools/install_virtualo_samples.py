"""Publish a complete validated Virtualo database without replacing existing files."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
from crawl_state import Coverage
from database import Database
from main import load_settings, run_lock, write_json
from quality import assess
from sources import create_database


def install(merge_existing=False):
    """Stage three checked records and atomically publish only a new Virtualo file."""
    validation = json.loads((ROOT / "reports/new_sources/latest_virtualo_validation.json").read_text(encoding="utf-8"))
    if validation["errors"] or len(validation["services"]["virtualo"]["samples"]) != 3:
        raise RuntimeError("Brak poprawnej walidacji trzech audiobooków Virtualo")
    origin = Path(validation["output"])
    target = load_settings(ROOT / "config.json")["databases"]["virtualo"]
    target.parent.mkdir(parents=True, exist_ok=True)
    with run_lock(ROOT / "state/virtualo/install.lock"):
        existing = target.exists()
        if existing and not merge_existing:
            raise FileExistsError(f"Istniejąca baza pozostaje bez zmian: {target}")
        if existing and list((ROOT / "state/jobs").glob("cycle_*/peers/virtualo.running")):
            raise RuntimeError("Proces Virtualo nadal pracuje; nie można uzupełnić bazy startowej.")
        with closing(sqlite3.connect(origin / "state/virtualo/coverage.db")) as conn:
            samples = [json.loads(row[0]) for row in conn.execute("SELECT data FROM metadata WHERE site='virtualo'")]
        with closing(sqlite3.connect(origin / "data/virtualo.db")) as conn:
            cover_paths = dict(conn.execute("SELECT url,cover_local_path FROM books"))
        with tempfile.TemporaryDirectory(prefix="virtualo_seed_", dir=target.parent) as folder:
            staged = target if existing else Path(folder) / "virtualo.db"
            if not existing:
                create_database("virtualo", staged)
            db = Database("virtualo", staged)
            try:
                for data in samples:
                    source = Path(cover_paths[data["url"]])
                    destination = ROOT / "media/virtualo" / source.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():
                        shutil.copy2(source, destination)
                    with Image.open(destination) as image:
                        width, height = image.size
                        image.verify()
                    db.save(data, dict(source_url=data["cover_url"], local_path=str(destination), width=width, height=height,
                                       size_bytes=destination.stat().st_size, sha256=hashlib.sha256(destination.read_bytes()).hexdigest()))
            finally:
                db.close()
            with closing(sqlite3.connect(staged)) as conn:
                integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
                total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
                if integrity != "ok" or total < 3:
                    raise RuntimeError("Niepoprawna baza startowa Virtualo")
            # Publikacja nie nadpisze bazy, jeżeli w międzyczasie utworzył ją scraper.
            if not existing:
                os.link(staged, target)
        ledger = Coverage(ROOT / "state/virtualo/coverage.db", "virtualo", target)
        try:
            for data in samples:
                ledger.save_metadata(data)
                ledger.mark(data["url"], "verified", assess("virtualo", data))
        finally:
            ledger.close()
        result = dict(database=str(target), books=total, quick_check=integrity, covers=3, sample_only=total == 3,
                      preserved_existing_queue=existing)
        write_json(ROOT / "reports/virtualo/installed.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    install()
