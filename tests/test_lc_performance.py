"""Review lookup persistence, audiobook identity and independent record failures."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database import Database
from discovery import Discovery
from main import load_settings, run_site
from parse_books import BookValidationError, parse
from test_scraper import fixture, URLS, schema


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "lc.db"
        conn = sqlite3.connect(self.path)
        conn.executescript((ROOT / "tests/fixtures/schema_lubimyczytac.sql").read_text(encoding="utf-8"))
        conn.close()
        self.data = parse("lubimyczytac", fixture("lubimyczytac"), URLS["lubimyczytac"])
        self.data["reviews"] = []
        self.settings = load_settings(ROOT / "config.json")
        self.settings.update(backup=False, coverage_database=self.root / "coverage.db")
        self.settings["databases"]["lubimyczytac"] = self.path
        self.args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                                    no_backup=True, refresh_limit=0, limit=0, no_covers=True)

    def database(self, cached=False):
        db = Database("lubimyczytac", self.path, review_index=self.root / "reviews.db" if cached else None)
        self.addCleanup(db.close)
        return db

    def test_reviews_are_indexed_once_and_restart_avoids_full_scans(self):
        original_schema = schema(self.path)
        db = self.database()
        db.save(self.data)
        book_id = db.known["5187984"]
        with db.engine.begin() as conn:
            conn.execute(text("INSERT INTO reviews(book_id,username,full_text,is_featured,rating) VALUES (:book,:user,:body,0,3)"),
                         [{"book": book_id, "user": f"User {n}", "body": f"Review {n}"} for n in range(500)])
        db.close()
        data = {**self.data, "reviews": [{"username": "User 1", "text": "Review 1", "rating": 5},
                                          {"username": "New", "text": "New review", "rating": 4}]}
        cached = self.database(True)
        cached.save(data)
        cached.close()
        restarted = self.database(True)
        queries = []
        event.listen(restarted.engine, "before_cursor_execute", lambda conn, cursor, statement, params, context, many: queries.append(statement))
        restarted.save(data)
        with restarted.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM reviews")).scalar_one(), 501)
            self.assertEqual(conn.execute(text("SELECT rating FROM reviews WHERE id=2")).scalar_one(), 5)
        self.assertFalse(any("FROM reviews WHERE book_id=" in query for query in queries))
        self.assertEqual(original_schema, schema(self.path))

    def test_stale_cache_entry_is_verified_against_primary(self):
        cached = self.database(True)
        data = {**self.data, "reviews": [{"username": "A", "text": "Body", "rating": 3}]}
        cached.save(data)
        with cached.engine.begin() as conn:
            conn.execute(text("DELETE FROM reviews"))
        cached.save(data)
        with cached.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM reviews")).scalar_one(), 1)

    def test_wrong_product_pages_do_not_stop_independent_books(self):
        db = self.database()
        urls = [f"https://lubimyczytac.pl/ksiazka/{n}/a" for n in range(100, 107)]
        db.enqueue(urls)
        def fetch(client, site, url):
            if url != urls[-1]:
                raise BookValidationError("Wrong product")
            return {**self.data, "external_id": 106, "url": url}
        with patch("main.fetch_book", side_effect=fetch) as get:
            report = run_site("lubimyczytac", self.settings, self.args, {})
        self.assertEqual(get.call_count, 7)
        self.assertEqual(report["inserted"], 1)
        self.assertFalse(report.get("stopped_on_errors"))

    def test_lc_audiobook_can_point_canonical_to_print_edition(self):
        html = fixture("lubimyczytac")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        soup.select_one('link[rel="canonical"]')["href"] = "https://lubimyczytac.pl/ksiazka/123/print"
        old = soup.select_one('meta[property="og:url"]')
        if old:
            old.decompose()
        soup.head.append(soup.new_tag("meta", attrs={"property": "og:url", "content": URLS["lubimyczytac"]}))
        for tag in soup.select('script[type="application/ld+json"]'):
            tag.decompose()
        script = soup.new_tag("script", attrs={"type": "application/ld+json"})
        script.string = '{"@type":"Audiobook","url":"https://lubimyczytac.pl/audiobook/5187984/audio"}'
        soup.head.append(script)
        data = parse("lubimyczytac", str(soup), URLS["lubimyczytac"])
        self.assertEqual(data["external_id"], 5187984)
        self.assertEqual(data["type"], "audiobook")

    def test_new_only_does_not_fetch_known_pending_or_inherited_gaps(self):
        db = self.database()
        known = {**self.data, "description": None}
        db.save(known)
        db.enqueue([known["url"]], refresh=True)
        new_url = "https://lubimyczytac.pl/ksiazka/777/new"
        found = Discovery()
        found.add("lubimyczytac", known["url"])
        found.add("lubimyczytac", new_url)
        self.args.resume = False
        self.args.new_only = True
        self.args.refresh_limit = 50
        with patch("main.discover", return_value=found), patch("main.fetch_book", return_value={**self.data, "url": new_url, "external_id": 777}) as get:
            report = run_site("lubimyczytac", self.settings, self.args, {})
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.args[2], new_url)
        self.assertEqual(report["inserted"], 1)
        self.assertEqual(report["queued_refresh"], 0)
        self.assertNotIn("queued_inherited_repair", report)


if __name__ == "__main__":
    unittest.main()
