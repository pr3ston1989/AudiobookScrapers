"""Exercise real downloads, cover files, queues and writes in isolated source schemas."""

import json
import hashlib
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import main
from database import Database
from main import ROOT, load_settings, run_site, write_json


def verify():
    manifest = json.loads((ROOT / "tests/fixtures/live_manifest.json").read_text(encoding="utf-8"))
    settings = load_settings(ROOT / "config.json")
    output_root = ROOT
    settings["backup"] = False
    settings["browser_headless"] = False
    report = {}
    with tempfile.TemporaryDirectory(prefix="pipeline_", dir=ROOT / "state") as directory:
        temporary = Path(directory)
        main.ROOT = temporary
        settings["browser_profile"] = temporary / "browser_profile"
        settings["coverage_database"] = temporary / "coverage.db"
        for site, samples in manifest["services"].items():
            path = temporary / f"{site}.db"
            schema_sql = (output_root / f"tests/fixtures/schema_{site}.sql").read_text(encoding="utf-8")
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(schema_sql)
                original_schema = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            urls = [sample["requested_url"] for sample in samples]
            db = Database(site, path)
            try:
                db.enqueue(urls)
            finally:
                db.close()
            settings["databases"][site] = path
            args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                                   no_backup=True, refresh_limit=0, limit=0, no_covers=False)
            result = run_site(site, settings, args, {})
            with closing(sqlite3.connect(path)) as conn:
                conn.row_factory = sqlite3.Row
                result["stored_books"] = [dict(row) for row in conn.execute("SELECT * FROM books")]
                result["stored_authors"] = conn.execute("SELECT COUNT(*) FROM book_authors").fetchone()[0]
                result["stored_reviews"] = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
                schema = [tuple(row) for row in conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
                result["schema_unchanged"] = schema == original_schema
                if site == "lubimyczytac":
                    covers = [dict(row) for row in conn.execute("SELECT * FROM covers")]
                else:
                    covers = [{"local_path": row["cover_local_path"]} for row in result["stored_books"]]
                result["cover_files_exist"] = all(cover["local_path"] and Path(cover["local_path"]).is_file() for cover in covers) and len(covers) == len(urls)
                result["cover_hashes_match"] = all(hashlib.sha256(Path(cover["local_path"]).read_bytes()).hexdigest() == cover["sha256"] for cover in covers if cover.get("sha256"))
            with closing(sqlite3.connect(settings["coverage_database"])) as conn:
                result["full_metadata"] = [json.loads(row[0]) for row in conn.execute("SELECT data FROM metadata WHERE site=?", (site,))]
            assert result["schema_unchanged"], site
            assert result["after"]["books"] == len(urls), (site, result)
            assert len(result["full_metadata"]) == len(urls), site
            checked_fields = set()
            from urls import identity
            for data in result["full_metadata"]:
                row = next(row for row in result["stored_books"] if identity(site, row["url"]) == identity(site, data["url"]))
                for field, value in data.items():
                    if field in row and field not in ("id", "url", "created_at") and value is not None and not isinstance(value, (list, dict)):
                        assert row[field] == value, (site, field, row[field], value)
                        checked_fields.add(field)
            result["scalar_fields_compared"] = sorted(checked_fields)
            assert result["cover_files_exist"], (site, "covers", result["errors"])
            assert result["cover_hashes_match"], (site, "cover checksum")
            report[site] = result
            write_json(output_root / "reports/pipeline_live.json", report)
            print(site, "saved", result["after"]["books"], "incomplete", result["incomplete"], "errors", len(result["errors"]), flush=True)
        main.ROOT = output_root
    return report


if __name__ == "__main__":
    verify()
