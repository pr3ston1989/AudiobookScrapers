"""Reproduce reported metadata gaps on live pages in isolated databases."""

import json
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import events
import main
from database import Database
from main import ROOT, load_settings, run_site, write_json


def verify():
    source = json.loads((ROOT / "reports/update_20261010_130045.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "tests/fixtures/live_manifest.json").read_text(encoding="utf-8"))
    output = {}
    settings = load_settings(ROOT / "config.json")
    settings.update(backup=False, browser_headless=True)
    with tempfile.TemporaryDirectory(prefix="incomplete_", dir=ROOT / "state") as directory:
        temporary = Path(directory)
        settings["browser_profile"] = temporary / "browser_profile"
        settings["coverage_database"] = temporary / "coverage.db"
        with patch("main.ROOT", temporary), patch("events.STOP_FILE", temporary / "stop.request"):
            for previous in source["services"]:
                site = previous["site"]
                if site == "lubimyczytac":
                    continue
                urls = [re.search(r"Niepełne dane (https://[^ ]+):", error).group(1)
                        for error in previous["errors"] if error.startswith("Niepełne dane ")]
                urls.append(manifest["services"][site][0]["requested_url"])
                path = temporary / f"{site}.db"
                with closing(sqlite3.connect(path)) as connection:
                    connection.executescript((ROOT / f"tests/fixtures/schema_{site}.sql").read_text(encoding="utf-8"))
                    schema = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
                settings["databases"][site] = path
                db = Database(site, path)
                try:
                    db.enable_writes()
                    # Kolejność jest istotna: problematyczne strony przed poprawną książką.
                    for number, url in enumerate(urls):
                        db.enqueue([url], priority=100-number)
                finally:
                    db.close()
                args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                                       no_backup=True, refresh_limit=0, limit=0, no_covers=False)
                result = run_site(site, settings, args, {})
                with closing(sqlite3.connect(path)) as connection:
                    connection.row_factory = sqlite3.Row
                    rows = [dict(row) for row in connection.execute("SELECT * FROM books")]
                    result["schema_unchanged"] = schema == [tuple(row) for row in connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
                    result["cover_files_exist"] = all(row["cover_local_path"] and Path(row["cover_local_path"]).is_file() for row in rows)
                with closing(sqlite3.connect(settings["coverage_database"])) as connection:
                    result["metadata"] = [json.loads(row[0]) for row in connection.execute("SELECT data FROM metadata WHERE site=?", (site,))]
                result["tested_urls"] = urls
                output[site] = result
                write_json(ROOT / "reports/incomplete_queue_live.json", output)
                assert result["inserted"] == len(urls), (site, result["errors"])
                assert not result["errors"], (site, result["errors"])
                assert result["schema_unchanged"] and result["cover_files_exist"], site
                assert rows[-1]["url"] == urls[-1], (site, "healthy book not reached")
                print(site, "saved", result["inserted"], "incomplete", result["incomplete"], "covers", result["cover_files_exist"], flush=True)
    return output


if __name__ == "__main__":
    verify()
