"""Install validated samples into new databases without changing existing databases."""

import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
from crawl_state import Coverage
from database import Database
from main import load_settings, run_lock, write_json
from quality import assess
from sources import SOURCES, create_database


def install():
    """Copy only successfully validated sample records and their actual cover files."""
    validation = json.loads((ROOT / "reports/new_sources/latest_validation.json").read_text(encoding="utf-8"))
    if validation["errors"]:
        raise RuntimeError("Najpierw usuń błędy walidacji integracji.")
    origin = Path(validation["output"])
    settings = load_settings(ROOT / "config.json")
    result = {}
    with run_lock(ROOT / "scraper.lock"):
        for site in SOURCES:
            target = settings["databases"][site]
            if not create_database(site, target):
                result[site] = {"skipped_existing": True, "database": str(target)}
                continue
            db = Database(site, target)
            ledger = Coverage(ROOT / "state" / site / "coverage.db", site, target)
            try:
                with closing(sqlite3.connect(origin / "state" / site / "coverage.db")) as source:
                    samples = [json.loads(row[0]) for row in source.execute("SELECT data FROM metadata WHERE site=?", (site,))]
                with closing(sqlite3.connect(origin / "data" / f"{site}.db")) as source:
                    covers = dict(source.execute("SELECT url,cover_local_path FROM books"))
                if len(samples) != 3:
                    raise RuntimeError(f"{site}: oczekiwano trzech potwierdzonych próbek")
                for data in samples:
                    cover = None
                    path = covers.get(data["url"])
                    if path:
                        destination = ROOT / "media" / site / Path(path).name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, destination)
                        with Image.open(destination) as image:
                            width, height = image.size
                            image.verify()
                        cover = dict(source_url=data["cover_url"], local_path=str(destination), width=width, height=height,
                                     size_bytes=destination.stat().st_size, sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
                    if SOURCES[site]["active"] and not cover:
                        raise RuntimeError(f"{site}: brak zweryfikowanej okładki")
                    db.save(data, cover)
                    ledger.save_metadata(data)
                    quality = assess(site, data)
                    ledger.mark(data["url"], "verified" if quality["complete"] and cover else "incomplete", quality)
                with closing(sqlite3.connect(target)) as conn:
                    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
                result[site] = dict(database=str(target), books=db.stats()["books"], quick_check=integrity,
                                    active=SOURCES[site]["active"], sample_only=True)
            finally:
                db.close()
                ledger.close()
    write_json(ROOT / "reports/new_sources/installed_databases.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    install()
