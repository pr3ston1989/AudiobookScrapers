"""Exercise real discovery, parsing, cover downloads and idempotent isolated writes."""

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import main
from crawl_state import Coverage
from discovery import discover
from http_client import Client
from quality import assess
from sources import SOURCES, create_database


def verify(sites=None):
    """Leave durable evidence and inspectable sample databases without touching old data."""
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "tests/fixtures/new_sources/manifest.json").read_text(encoding="utf-8"))
    output = root / "reports/new_sources" / f"validation_{datetime.now():%Y%m%d_%H%M%S}"
    output.mkdir(parents=True)
    settings = main.load_settings(root / "config.json")
    settings["databases"] = {site: output / "data" / f"{site}.db" for site in SOURCES}
    settings.update(backup=False, http_retries=2, max_listing_pages=3, catalog_page_budget=2)
    results = {"output": str(output), "services": {}, "errors": []}
    original_root = main.ROOT
    main.ROOT = output
    try:
        for site in sites or SOURCES:
            record = {"samples": [], "errors": [], "ready_for_updates": SOURCES[site]["active"]}
            if not SOURCES[site]["active"]:
                record["limitations"] = ["Dystrybucja zakończona 30.06.2026; archiwalne metadane bez czasu i dostępnych okładek. Brak aktualizacji nowości."]
            settings["coverage_database"] = output / "state" / site / "coverage.db"
            create_database(site, settings["databases"][site])
            state = {}
            if site != "nexto":
                ledger = Coverage(settings["coverage_database"], site, settings["databases"][site])
                client = Client(settings)
                try:
                    found = discover(site, client, settings, {}, site in ("bookbeat", "virtualo"), ledger.discover, ledger)
                    record["discovery"] = {"products": len(found.urls), "pages": found.pages, "maps": found.maps,
                                           "errors": found.errors, "warnings": found.warnings,
                                           "catalog_complete": found.catalog_complete}
                    if not found.urls or found.errors:
                        record["errors"].extend(found.errors or ["Brak odkrytych audiobooków"])
                except Exception as exc:
                    record["errors"].append(f"Discovery: {exc}")
                finally:
                    ledger.close()
                    client.close()
            for stem, url, minutes in manifest[site]:
                try:
                    args = SimpleNamespace(command="update", url=url, resume=False, full_catalog=False,
                                           no_backup=True, refresh_limit=0, limit=1, no_covers=False)
                    first = main.run_site(site, settings, args, state)
                    second = main.run_site(site, settings, args, state)
                    if first["errors"] or second["errors"]:
                        raise RuntimeError(str(first["errors"] + second["errors"]))
                    if second["inserted"] or second["after"]["books"] != first["after"]["books"]:
                        raise RuntimeError("Ponowienie utworzyło duplikat")
                    with closing(sqlite3.connect(settings["databases"][site])) as conn:
                        row = conn.execute("SELECT id,title,duration_minutes,cover_local_path FROM books ORDER BY id DESC LIMIT 1").fetchone()
                    if not row:
                        raise RuntimeError("Brak zapisanego rekordu")
                    dimensions = None
                    if SOURCES[site]["active"] and (not row[3] or not Path(row[3]).is_file()):
                        raise RuntimeError("Brak pobranej okładki")
                    if row[3] and Path(row[3]).is_file():
                        with Image.open(row[3]) as image:
                            dimensions = image.size
                            image.verify()
                    if row[2] != minutes:
                        raise RuntimeError(f"Czas trwania {row[2]} zamiast {minutes}")
                    ledger = Coverage(settings["coverage_database"], site, settings["databases"][site])
                    try:
                        from urls import identity
                        snapshot = ledger.conn.execute("SELECT data FROM metadata WHERE site=? AND product_key=?", (site, identity(site, url))).fetchone()
                        if snapshot is None and site == "legimi":
                            snapshot = ledger.conn.execute("SELECT data FROM metadata WHERE site=? ORDER BY fetched_at DESC LIMIT 1", (site,)).fetchone()
                        parsed = json.loads(snapshot[0])
                        quality = assess(site, parsed)
                        expected = ["duration_minutes"] if site == "nexto" else []
                        if quality["missing_required"] != expected:
                            raise RuntimeError(f"Nieoczekiwane braki: {quality['missing_required']}")
                    finally:
                        ledger.close()
                    record["samples"].append({"url": url, "title": row[1], "duration_minutes": row[2], "dimensions": dimensions,
                                               "cover_path": row[3], "quality": quality, "idempotent": True})
                    print(f"{site}: OK {row[1]} ({dimensions or 'archiwum, brak okładki'})", flush=True)
                except Exception as exc:
                    record["errors"].append(f"{url}: {exc}")
                    print(f"{site}: ERROR {exc}", flush=True)
            with closing(sqlite3.connect(settings["databases"][site])) as conn:
                record["books"] = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
                record["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
            results["services"][site] = record
            results["errors"].extend([f"{site}: {error}" for error in record["errors"]])
            main.write_json(output / "validation.json", results)
    finally:
        main.ROOT = original_root
    filename = "latest_validation.json" if sites is None else "latest_" + "_".join(sites) + "_validation.json"
    main.write_json(root / "reports/new_sources" / filename, results)
    print(f"Raport: {output / 'validation.json'}", flush=True)
    return bool(results["errors"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(verify())
