"""Finite background maintenance, aggregate reporting and unchanged cover handling."""

import json
import io
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main
from background import other_workers, plan
from crawl_state import Coverage
from database import Database
from discovery import Discovery
from http_client import Client, Response
from worker import execute
from urls import identity


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = main.load_settings(ROOT / "config.json")
        self.settings["databases"] = {site: path for site, path in self.settings["databases"].items() if site in ("audioteka", "storytel", "empik", "lubimyczytac")}
        self.settings.update(coverage_database=self.root / "coverage.db", backup=False, repair_limit=2)
        for site in self.settings["databases"]:
            path = self.root / f"{site}.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript((ROOT / f"tests/fixtures/schema_{site}.sql").read_text(encoding="utf-8"))
            self.settings["databases"][site] = path
        self.args = SimpleNamespace(command="update", url=None, resume=False, full_catalog=False,
                                    fast=False, no_backup=True, refresh_limit=0, limit=0, no_covers=True)
        self.peers = self.root / "peers"
        self.peers.mkdir()
        (self.peers / "ready").touch()
        for site in ("audioteka", "storytel"):
            (self.peers / f"{site}.running").touch()
        self.job = dict(site="audioteka", root=str(self.root), settings=self.settings,
                        args=vars(self.args), state={}, result=str(self.root / "result.json"), peers=str(self.peers))

    def seed(self, count=7):
        db = Database("audioteka", self.settings["databases"]["audioteka"])
        try:
            for n in range(count):
                db.save(self.data(f"https://audioteka.com/pl/audiobook/book-{n}/", publisher=None))
        finally:
            db.close()

    @staticmethod
    def data(url, **extra):
        data = dict(url=url, title="Book", description="Description", authors=["Author"],
                    cover_url="https://example.test/cover.jpg", duration_minutes=60,
                    category="Fiction", categories=["Fiction"], **extra)
        if "lubimyczytac.pl" in url:
            data["external_id"] = int(identity("lubimyczytac", url))
            data["slug"] = "a"
            data["type"] = "book"
        return data

    def run_job(self, fetch=None):
        with patch("main.ROOT", self.root), patch("main.discover", return_value=Discovery()), patch("main.fetch_book", side_effect=fetch or (lambda client, site, url: self.data(url, publisher="Publisher"))) as getter:
            code = execute(self.job)
        return code, json.loads((self.root / "result.json").read_text(encoding="utf-8")), getter

    def test_repairs_continue_and_aggregate_without_duplicates_or_schema_changes(self):
        self.seed()
        with closing(sqlite3.connect(self.settings["databases"]["audioteka"])) as conn:
            before = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        code, result, getter = self.run_job()
        report = result["report"]
        self.assertEqual(code, 0)
        self.assertEqual(getter.call_count, 7)
        self.assertEqual(report["updated"], 7)
        self.assertEqual(report["inserted"], 0)
        self.assertEqual(report["background_batches"], 3)
        self.assertEqual(report["metadata_gaps"]["incomplete_books"], 0)
        self.assertEqual(report["before"]["books"], report["after"]["books"])
        self.assertFalse((self.peers / "audioteka.running").exists())
        with closing(sqlite3.connect(self.settings["databases"]["audioteka"])) as conn:
            self.assertEqual(before, conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall())
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_early_finisher_resumes_maintenance_without_repeating_discovery(self):
        self.seed(5)
        (self.peers / "ready").unlink()
        _, first, getter = self.run_job()
        self.assertTrue(first["initial_pass_only"])
        self.assertEqual(getter.call_count, 2)
        (self.peers / "ready").touch()
        (self.peers / "audioteka.running").touch()
        self.job.update(previous_report=first["report"], attempted_tasks=first["attempted_tasks"], cycle_started=time.time())
        with patch("main.ROOT", self.root), patch("main.discover", side_effect=AssertionError("Nie wolno powtarzać discovery")), patch("main.fetch_book", side_effect=lambda client, site, url: self.data(url, publisher="Publisher")) as fetch:
            code = execute(self.job)
        result = json.loads((self.root / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(result["report"]["updated"], 5)
        self.assertEqual(result["report"]["before"]["books"], 5)

    def test_incomplete_rows_are_attempted_once_even_after_retry_deadlines_pass(self):
        self.seed(5)
        def fetch(client, site, url):
            return self.data(url, publisher=None)
        with patch("crawl_state.time.time", return_value=time.time() + 7200):
            code, result, getter = self.run_job(fetch)
        self.assertEqual(code, 0)
        self.assertEqual(getter.call_count, 5)
        self.assertEqual(result["report"]["incomplete"], 5)
        self.assertEqual(result["report"]["background_batches"], 2)

    def test_continues_own_work_when_last_peer_finishes(self):
        self.seed(7)
        calls = []
        def fetch(client, site, url):
            calls.append(url)
            if len(calls) == 3:
                (self.peers / "storytel.running").unlink()
            return self.data(url, publisher="Publisher")
        _, result, _ = self.run_job(fetch)
        self.assertEqual(len(calls), 7)
        self.assertEqual(result["report"]["background_batches"], 3)
        self.assertEqual(result["report"]["metadata_gaps"]["incomplete_books"], 0)

    def test_single_service_finishes_all_due_work(self):
        self.seed(7)
        (self.peers / "storytel.running").unlink()
        _, result, getter = self.run_job()
        self.assertEqual(getter.call_count, 7)
        self.assertEqual(result['report']['background_batches'], 3)

    def test_waiting_initial_services_take_priority(self):
        (self.peers / "ready").unlink()
        self.assertFalse(other_workers(self.job))

    def test_explicit_limit_is_not_expanded(self):
        self.seed(7)
        self.args.limit = 1
        _, result, getter = self.run_job()
        self.assertEqual(getter.call_count, 1)
        self.assertNotIn("background_batches", result["report"])

    def test_resume_does_not_add_inherited_repairs_or_catalogs(self):
        self.seed(7)
        self.args.resume = True
        ledger = Coverage(self.settings["coverage_database"], "audioteka", self.settings["databases"]["audioteka"])
        ledger.seed_pages(["https://audioteka.com/pl/katalog/"])
        ledger.close()
        _, result, getter = self.run_job()
        getter.assert_not_called()
        self.assertNotIn("background_batches", result["report"])

    def test_catalog_continuation_does_not_reread_sitemaps(self):
        ledger = Coverage(self.settings["coverage_database"], "audioteka", self.settings["databases"]["audioteka"])
        ledger.seed_pages(["https://audioteka.com/pl/katalog/test/"])
        ledger.close()
        batch = plan("audioteka", self.settings, self.args, time.time(), {})
        self.assertTrue(batch.background_catalog)
        batch.attempted_tasks = set()
        with patch("main.discover") as discover, patch("main.discover_catalog") as catalog:
            main.run_site("audioteka", self.settings, batch, {})
        discover.assert_not_called()
        catalog.assert_called_once()

    def test_deferred_catalog_pages_do_not_cause_background_loop(self):
        ledger = Coverage(self.settings["coverage_database"], "audioteka", self.settings["databases"]["audioteka"])
        url = "https://audioteka.com/pl/katalog/"
        ledger.seed_pages([url])
        ledger.fail_page(url, "timeout")
        ledger.close()
        self.assertIsNone(plan("audioteka", self.settings, self.args, time.time(), {}))

    def test_background_downloads_covers_through_existing_downloader(self):
        self.seed(3)
        self.args.no_covers = False
        from PIL import Image
        image = io.BytesIO()
        Image.new("RGB", (1024, 1024), "blue").save(image, format="PNG")
        source = "https://atkcdn.audioteka.com/cover.jpg"
        def fetch(client, site, url):
            data = self.data(url, publisher="Publisher")
            data["cover_url"] = source + "?w=300"
            return data
        with patch.object(Client, "get", return_value=Response(source, image.getvalue())) as cover:
            code, result, getter = self.run_job(fetch)
        self.assertEqual(code, 0)
        self.assertEqual(result["report"]["errors"], [])
        self.assertEqual(getter.call_count, 3)
        self.assertEqual(cover.call_count, 3)
        self.assertTrue(all(call.args[0] == source for call in cover.call_args_list))
        self.assertEqual(result["report"]["background_batches"], 1)
        with closing(sqlite3.connect(self.settings["databases"]["audioteka"])) as conn:
            paths = conn.execute("SELECT cover_local_path FROM books").fetchall()
        for (path,) in paths:
            with Image.open(path) as stored:
                self.assertEqual(stored.size, (1024, 1024))

    def test_interrupt_preserves_previous_batch_counts_and_checkpoint(self):
        self.seed(5)
        calls = []
        def fetch(client, site, url):
            calls.append(url)
            if len(calls) == 4:
                raise KeyboardInterrupt
            return self.data(url, publisher="Publisher")
        code, result, _ = self.run_job(fetch)
        self.assertEqual(code, 130)
        self.assertTrue(result["interrupted"])
        self.assertEqual(result["report"]["updated"], 3)
        self.assertTrue((self.root / "state/audioteka/run.json").exists())
        db = Database("audioteka", self.settings["databases"]["audioteka"])
        try:
            self.assertTrue(db.pending())
        finally:
            db.close()

    def test_same_planner_fills_missing_data_for_every_service(self):
        templates = {"audioteka": "https://audioteka.com/pl/audiobook/book-{n}/",
                     "storytel": "https://www.storytel.com/pl/books/book-{n}",
                     "empik": "https://www.empik.com/book,p{n},ebooki-i-mp3-p",
                     "lubimyczytac": "https://lubimyczytac.pl/ksiazka/{n}/a"}
        for site, template in templates.items():
            with self.subTest(site=site):
                self.settings["coverage_database"] = self.root / f"{site}.coverage.db"
                db = Database(site, self.settings["databases"][site])
                try:
                    for n in (100, 101, 102):
                        data = self.data(template.format(n=n), publisher="Publisher")
                        data["description"] = None
                        cover = dict(source_url=data["cover_url"], local_path="fixture.jpg", sha256="fixture") if site == "lubimyczytac" else None
                        db.save(data, cover)
                finally:
                    db.close()
                (self.peers / f"{site}.running").touch()
                self.job.update(site=site)
                _, result, getter = self.run_job()
                self.assertEqual(getter.call_count, 3)
                self.assertEqual(result["report"]["updated"], 3)
                self.assertEqual(result["report"]["background_batches"], 1)
                self.assertEqual(result["report"]["errors"], [])
                self.assertEqual(result["report"]["metadata_gaps"]["incomplete_books"], 0)
                (self.peers / "audioteka.running").touch()
                (self.peers / "storytel.running").touch()

    def test_lc_continues_saved_id_range(self):
        site = "lubimyczytac"
        ledger = Coverage(self.settings["coverage_database"], site, self.settings["databases"][site])
        ledger.set_checkpoint("lc_id_cursor", 100)
        ledger.set_checkpoint("lc_id_target", 103)
        ledger.close()
        batch = plan(site, self.settings, self.args, time.time(), {})
        batch.attempted_tasks = set()
        with patch("main.fetch_book", side_effect=lambda client, site, url: self.data(url, publisher="P")) as fetch:
            report = main.run_site(site, self.settings, batch, {})
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["inserted"], 3)
        self.assertEqual(report["id_scan"]["remaining_ids"], 0)
        self.assertIsNone(plan(site, self.settings, self.args, time.time(), {"repairs_done": True}))

    def test_background_stops_after_five_consecutive_transport_errors(self):
        from http_client import HTTPError
        self.seed(10)
        self.settings["repair_limit"] = 10
        calls = []
        def fetch(client, site, url):
            calls.append(url)
            raise HTTPError(url, 503)
        code, result, _ = self.run_job(fetch)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 5)
        self.assertTrue(result["report"]["stopped_on_errors"])
        self.assertNotIn("background_batches", result["report"])


if __name__ == "__main__":
    unittest.main()
