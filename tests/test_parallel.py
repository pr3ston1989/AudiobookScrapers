"""Process isolation, legacy state migration and cooperative parallel stopping."""

import json
import io
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawl_state import Coverage
from main import load_settings
from parallel import run_parallel, service_settings
from sources import SOURCES, create_database


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = load_settings(ROOT / "config.json")
        self.settings["databases"] = {site: path for site, path in self.settings["databases"].items() if site in ("audioteka", "storytel", "empik", "lubimyczytac")}
        self.settings["coverage_database"] = self.root / "state" / "coverage.db"
        self.settings["backup"] = False
        for site in self.settings["databases"]:
            path = self.root / f"{site}.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript((ROOT / f"tests/fixtures/schema_{site}.sql").read_text(encoding="utf-8"))
            self.settings["databases"][site] = path
        self.args = SimpleNamespace(command="stats", services=list(self.settings["databases"]),
                                    url=None, resume=True, full_catalog=False, no_backup=True,
                                    refresh_limit=0, limit=0, no_covers=False)

    def test_four_real_workers_finish_with_independent_processes(self):
        reports, interrupted = run_parallel(self.settings, self.args, {}, self.root, 4)
        self.assertFalse(interrupted)
        self.assertEqual(len(reports), 4)
        self.assertEqual(len({report["worker_pid"] for report in reports}), 4)
        self.assertTrue(all(report["before"]["books"] == 0 and not report["errors"] for report in reports))

    def test_all_services_finish_with_four_worker_slots(self):
        for site in SOURCES:
            path = self.root / f"{site}.db"
            create_database(site, path)
            self.settings["databases"][site] = path
        self.args.services = list(self.settings["databases"])
        reports, interrupted = run_parallel(self.settings, self.args, {}, self.root, 4)
        self.assertFalse(interrupted)
        self.assertEqual(len(reports), len(self.settings["databases"]))
        self.assertEqual(len({report["worker_pid"] for report in reports}), len(self.settings["databases"]))
        self.assertTrue(all(report["before"]["books"] == 0 and not report["errors"] for report in reports))

    def test_six_services_can_all_receive_worker_slots(self):
        for site in ("publio", "bookbeat"):
            path = self.root / f"{site}.db"
            create_database(site, path)
            self.settings["databases"][site] = path
        self.args.services = list(self.settings["databases"])
        with patch("parallel.emit") as events:
            reports, interrupted = run_parallel(self.settings, self.args, {}, self.root, 6)
        self.assertFalse(interrupted)
        self.assertEqual(len(reports), 6)
        self.assertEqual(len({report["worker_pid"] for report in reports}), 6)
        self.assertTrue(all(not report["errors"] for report in reports))
        for site in self.args.services:
            events.assert_any_call("phase", site=site, phase="Oczekuje na wolny proces")
            events.assert_any_call("phase", site=site, phase="Uruchamianie")

    def test_default_parallel_selection_excludes_inactive_archive(self):
        self.settings["databases"] = {}
        for site in ("nexto", "publio"):
            path = self.root / f"{site}.db"
            create_database(site, path)
            self.settings["databases"][site] = path
        self.args.services = None
        reports, interrupted = run_parallel(self.settings, self.args, {}, self.root, 2)
        self.assertFalse(interrupted)
        self.assertEqual([report["site"] for report in reports], ["publio"])

    def test_parent_restarts_early_finisher_after_initial_services_received_slots(self):
        self.args.command = "update"
        self.args.services = ["audioteka", "storytel", "empik"]
        launched = []

        class Process:
            def __init__(process, command, **kwargs):
                job = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                site = job["site"]
                maintenance = bool(job.get("previous_report"))
                launched.append((site, maintenance))
                process.remaining = 12 if site == "storytel" else 0
                process.stdout = io.StringIO("")
                process.returncode = 0
                report = {"site": site, "errors": [], "warnings": [], "after": {"books": 1},
                          "inserted": 1, "updated": int(maintenance)}
                Path(job["result"]).write_text(json.dumps({"report": report, "state": {},
                    "attempted_tasks": [123], "initial_pass_only": site == "audioteka" and not maintenance}), encoding="utf-8")
                if maintenance:
                    self.assertEqual(job["attempted_tasks"], [123])

            def poll(process):
                process.remaining -= 1
                return None if process.remaining >= 0 else 0

        with patch("parallel.subprocess.Popen", Process):
            reports, interrupted = run_parallel(self.settings, self.args, {}, self.root, 2)
        self.assertFalse(interrupted)
        self.assertEqual(launched, [("audioteka", False), ("storytel", False), ("empik", False), ("audioteka", True)])
        self.assertEqual(next(report for report in reports if report["site"] == "audioteka")["updated"], 1)

    def test_one_failed_worker_does_not_cancel_other_services(self):
        self.settings["databases"]["audioteka"] = self.root / "missing.db"
        reports, interrupted = run_parallel(self.settings, self.args, {}, self.root, 4)
        self.assertFalse(interrupted)
        by_site = {report["site"]: report for report in reports}
        self.assertTrue(by_site["audioteka"]["errors"])
        self.assertTrue(all(not report["errors"] for site, report in by_site.items() if site != "audioteka"))

    def test_duplicate_database_paths_are_rejected_before_launch(self):
        self.settings["databases"]["storytel"] = self.settings["databases"]["audioteka"]
        with self.assertRaises(ValueError):
            run_parallel(self.settings, self.args, {}, self.root, 4)

    def test_shared_state_is_split_without_losing_metadata_or_retries(self):
        legacy = self.settings["coverage_database"]
        for site, url in (("storytel", "https://www.storytel.com/pl/books/title-123"),
                          ("audioteka", "https://audioteka.com/pl/audiobook/title/")):
            ledger = Coverage(legacy, site, self.settings["databases"][site])
            ledger.mark(url, "incomplete", {"missing": "publisher"})
            ledger.save_metadata({"url": url, "title": site})
            ledger.set_checkpoint("repair_cursor", 42)
            ledger.close()
        paths = []
        for site in ("storytel", "audioteka"):
            isolated = service_settings(self.settings, site)
            paths.append(isolated["coverage_database"])
            with closing(sqlite3.connect(paths[-1])) as conn:
                self.assertEqual(conn.execute("SELECT site,status FROM products").fetchall(), [(site, "incomplete")])
                self.assertEqual(json.loads(conn.execute("SELECT data FROM metadata").fetchone()[0])["title"], site)
                self.assertEqual(json.loads(conn.execute("SELECT value FROM checkpoints").fetchone()[0]), 42)
        self.assertNotEqual(paths[0], paths[1])

    def test_stop_request_stops_started_workers_and_saves_checkpoints(self):
        self.args.command = "update"
        timer = threading.Timer(0.3, lambda: (self.root / "stop.request").write_text("stop", encoding="ascii"))
        timer.start()
        try:
            reports, interrupted = run_parallel(self.settings, self.args, {}, self.root, 4)
        finally:
            timer.join()
        self.assertTrue(interrupted)
        self.assertTrue(reports)
        self.assertTrue(all(report.get("interrupted") for report in reports))
        for report in reports:
            self.assertTrue((self.root / "state" / report["site"] / "run.json").exists())


if __name__ == "__main__":
    unittest.main()
