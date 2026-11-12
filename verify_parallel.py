"""Verify four concurrent live updates with cover downloads in isolated databases."""

import json
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from database import Database
from main import ROOT, load_settings, write_json
from parallel import run_parallel


def verify():
    settings = load_settings(ROOT / "config.json")
    manifest = json.loads((ROOT / "tests/fixtures/live_manifest.json").read_text(encoding="utf-8"))
    settings["backup"] = False
    with tempfile.TemporaryDirectory(prefix="parallel_live_", dir=ROOT / "state") as directory:
        root = Path(directory)
        settings["coverage_database"] = root / "state" / "coverage.db"
        settings["browser_profile"] = root / "browser_profile"
        for site in settings["databases"]:
            path = root / f"{site}.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript((ROOT / f"tests/fixtures/schema_{site}.sql").read_text(encoding="utf-8"))
            settings["databases"][site] = path
            db = Database(site, path)
            try:
                db.enqueue([manifest["services"][site][1]["requested_url"]])
            finally:
                db.close()
        args = SimpleNamespace(command="update", services=list(settings["databases"]), url=None,
                               resume=True, full_catalog=False, no_backup=True, refresh_limit=0,
                               limit=0, no_covers=False)
        start = time.monotonic()
        state = {}
        reports, interrupted = run_parallel(settings, args, state, root, 4)
        wall_seconds = time.monotonic() - start
        assert not interrupted and len(reports) == 4
        for report in reports:
            site = report["site"]
            assert not report["errors"] and report["inserted"] == 1, report
            with closing(sqlite3.connect(settings["databases"][site])) as conn:
                sql = "SELECT local_path FROM covers" if site == "lubimyczytac" else "SELECT cover_local_path FROM books"
                path = conn.execute(sql).fetchone()[0]
                report["cover_file_exists"] = bool(path) and Path(path).is_file()
                assert report["cover_file_exists"], site
                expected = (ROOT / f"tests/fixtures/schema_{site}.sql").read_text(encoding="utf-8")
                with closing(sqlite3.connect(":memory:")) as reference:
                    reference.executescript(expected)
                    sql = "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                    assert reference.execute(sql).fetchall() == conn.execute(sql).fetchall(), site
            assert (root / "state" / site / "run.json").exists(), site
        overlap = max(report["started_at"] for report in reports) < min(report["finished_at"] for report in reports)
        assert overlap, "Workers did not overlap"
        result = {"wall_seconds": round(wall_seconds, 3), "all_four_overlapped": overlap,
                  "worker_seconds_sum": round(sum(report["elapsed_seconds"] for report in reports), 3), "services": reports}
        write_json(ROOT / "reports/parallel_live.json", result)
        print(f"Cztery serwisy jednocześnie: {wall_seconds:.1f}s, wszystkie okładki pobrane", flush=True)


if __name__ == "__main__":
    verify()
