"""Regression cases found in the October 13 production reports."""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database import Database
from discovery import catalog_links, discover_catalog, Discovery
from crawl_state import Coverage
from http_client import Client, HTTPError, Response
from main import load_settings, run_site, fetch_book
from parse_books import parse
from urls import audioteka_product_redirect
from test_scraper import fixture, URLS


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = load_settings(ROOT / "config.json")
        self.settings.update(backup=False, coverage_database=self.root / "coverage.db")

    def database(self, site):
        path = self.root / f"{site}.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript((ROOT / f"tests/fixtures/schema_{site}.sql").read_text(encoding="utf-8"))
        self.settings["databases"][site] = path
        db = Database(site, path)
        self.addCleanup(db.close)
        return db

    def test_lc_other_edition_canonical_requires_two_matching_page_sources(self):
        url = URLS["lubimyczytac"]
        html = fixture("lubimyczytac")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        tag = soup.select_one('link[rel="canonical"]')
        tag["href"] = "https://lubimyczytac.pl/ksiazka/123/other-edition"
        # JSON-LD fixture już potwierdza właściwe ID; og:url dodajemy jawnie.
        og = soup.new_tag("meta", attrs={"property": "og:url", "content": url})
        old_og = soup.select_one('meta[property="og:url"]')
        if old_og:
            old_og.decompose()
        soup.head.append(og)
        data = parse("lubimyczytac", str(soup), url)
        self.assertEqual(data["external_id"], 5187984)
        og["content"] = "https://lubimyczytac.pl/ksiazka/123/wrong-book"
        with self.assertRaises(ValueError):
            parse("lubimyczytac", str(soup), url)

    def test_lc_existing_external_id_with_bad_url_updates_without_duplicate(self):
        db = self.database("lubimyczytac")
        data = parse("lubimyczytac", fixture("lubimyczytac"), URLS["lubimyczytac"])
        db.save(data)
        with closing(sqlite3.connect(db.path)) as conn:
            conn.execute("UPDATE books SET url=?", ("https://lubimyczytac.pl/ksiazka/123/old-wrong-url",))
            conn.commit()
        db.close()
        restarted = Database("lubimyczytac", db.path)
        self.addCleanup(restarted.close)
        self.assertIn("5187984", restarted.known)
        self.assertNotIn("123", restarted.known)
        self.assertEqual(restarted.refresh_urls(0, 1)[0], [URLS["lubimyczytac"]])
        self.assertEqual(restarted.save(data), "updated")
        self.assertEqual(restarted.stats()["books"], 1)
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute("SELECT url FROM books").fetchone()[0], data["url"])

    def test_short_jsonld_description_does_not_replace_full_product_description(self):
        from parse_books import apply_structured
        from bs4 import BeautifulSoup
        data = {"url": URLS["audioteka"], "description": "Complete description " * 30}
        html = '<script type="application/ld+json">{"@type":"Audiobook","description":"Short SEO description"}</script>'
        apply_structured(data, BeautifulSoup(html, "lxml"), "audioteka")
        self.assertEqual(data["description"], "Complete description " * 30)

    def test_empty_duration_never_overwrites_existing_duration(self):
        db = self.database("empik")
        data = parse("empik", fixture("empik"), URLS["empik"])
        data["duration_minutes"] = 498
        db.save(data)
        data["duration_minutes"] = 0
        db.save(data)
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute("SELECT duration_minutes FROM books").fetchone()[0], 498)

    def test_empik_placeholder_is_not_a_cover_or_duration(self):
        html = '<h1 data-ta="title">Test - audiobook</h1><meta property="og:image" content="https://www.empik.com/b/mp/img/defaults/b.gif">'
        html += '<tr data-ta="row"><th data-ta="attribute-name">Czas trwania</th><td data-ta="attribute-value">Brak informacji</td></tr>'
        data = parse("empik", html, URLS["empik"])
        self.assertIsNone(data["cover_url"])
        self.assertIsNone(data["duration_minutes"])
        self.assertIsNone(data["length_str"])

    def test_existing_empik_placeholder_is_eligible_for_cover_replacement(self):
        db = self.database("empik")
        data = parse("empik", fixture("empik"), URLS["empik"])
        data["cover_url"] = "https://www.empik.com/b/mp/img/defaults/b.gif"
        path = self.root / "placeholder.jpg"
        path.write_bytes(b"placeholder")
        db.save(data, {"local_path": str(path)})
        self.assertTrue(db.needs_cover(data["url"]))

    def test_lc_catalog_filters_reviews_and_malformed_categories(self):
        html = '<a href="/kategoria/beletrystyka/ksiazka/a/opinia/123">Review</a><a href="/ksiazki/k//-">Broken</a><a href="/ksiazki/k/63/informatyka-matematyka">Category</a>'
        self.assertEqual(catalog_links("lubimyczytac", html, URLS["lubimyczytac"]),
                         {"https://lubimyczytac.pl/ksiazki/k/63/informatyka-matematyka"})

    def test_lc_old_invalid_catalog_task_is_retired_without_request(self):
        db = self.database("lubimyczytac")
        ledger = Coverage(self.settings["coverage_database"], "lubimyczytac", db.path)
        self.addCleanup(ledger.close)
        ledger.seed_pages(["https://lubimyczytac.pl/kategoria/beletrystyka/ksiazka/a/opinia/123"])
        client = SimpleNamespace(get=unittest.mock.Mock())
        result = Discovery()
        discover_catalog("lubimyczytac", client, result, ledger, {"catalog_page_budget": 5})
        client.get.assert_not_called()
        self.assertFalse(ledger.has_pending_pages())

    def test_audioteka_verified_typo_alias_still_rejects_other_targets(self):
        source = "https://audioteka.com/pl/audiobook/projet-hail-mary/"
        target = "https://audioteka.com/pl/audiobook/projekt-hail-mary/"
        client = SimpleNamespace(get=lambda *args, **kwargs: Response(target, fixture("audioteka").encode()))
        with patch("main.parse", return_value={"url": target, "title": "Projekt Hail Mary"}):
            self.assertEqual(fetch_book(client, "audioteka", source)["url"], target)
        self.assertFalse(audioteka_product_redirect(source, "https://audioteka.com/pl/audiobook/other/"))
        self.assertFalse(audioteka_product_redirect("https://bad.test/", "https://bad.test/"))

    def test_browser_429_retries_with_cooldown(self):
        client = Client(self.settings)
        self.addCleanup(client.close)
        url = URLS["empik"]
        with patch.object(client, "browser_get", side_effect=[HTTPError(url, 429), Response(url, b"ok")]) as get, patch("http_client.time.sleep") as sleep:
            self.assertEqual(client.get(url, render=True).text, "ok")
            self.assertEqual(get.call_count, 2)
            self.assertEqual(sleep.call_count, 30)

    def test_rate_limit_stops_service_without_requesting_four_more_books(self):
        db = self.database("storytel")
        db.enqueue([f"https://www.storytel.com/pl/books/book-{n}" for n in range(100, 106)])
        args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                               no_backup=True, refresh_limit=0, limit=0, no_covers=True)
        with patch("main.fetch_book", side_effect=HTTPError(URLS["storytel"], 429)) as get:
            report = run_site("storytel", self.settings, args, {})
        self.assertEqual(get.call_count, 1)
        self.assertTrue(report["stopped_on_rate_limit"])
        self.assertTrue(db.pending())


if __name__ == "__main__":
    unittest.main()
