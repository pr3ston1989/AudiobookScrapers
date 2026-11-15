"""Accuracy regressions for source labels, verified relationships and safe category repair."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

from dynamic_metadata import bookbeat_related
from http_client import HTTPError, Response
from main import fetch_book, run_site
from parse_books import parse, structured_product
from recommendation_parsers import normalized_link, related_signals
from recommendations import Recommendations, merge_signals, recommendations_path
import test_scraper as scraper_tests
from test_scraper import FIXTURES, URLS, fixture


class LabelAccuracyTests(unittest.TestCase):
    def test_lc_keeps_exact_comma_containing_category_and_ignores_global_navigation(self):
        soup = BeautifulSoup(fixture("lubimyczytac"), "lxml")
        soup.select_one(".book__desc a.book__category").string = "kryminał, sensacja, thriller"
        html = str(soup) + '<footer><a class="book__category">Obca kategoria</a></footer>'
        data = parse("lubimyczytac", html, URLS["lubimyczytac"])
        self.assertIn({"name": "kryminał, sensacja, thriller"}, data["categories"])
        self.assertNotIn({"name": "sensacja"}, data["categories"])
        self.assertNotIn({"name": "Obca kategoria"}, data["categories"])

    def test_verified_schema_identity_beats_earlier_unbound_recommendation(self):
        url = "https://woblink.com/audiobook/test-123"
        soup = BeautifulSoup('<script type="application/ld+json">' + json.dumps([
            {"@type": "Product", "name": "Inna książka", "genre": "Obca kategoria"},
            {"@type": "Product", "url": url, "name": "Test", "genre": ["Thriller", "Sensacja"]},
        ]) + '</script>', "lxml")
        self.assertEqual(structured_product(soup, "woblink", url)["name"], "Test")
        data = parse("woblink", str(soup), url)
        self.assertEqual(data["categories"], ["Thriller", "Sensacja"])
        self.assertNotIn("Obca kategoria", data["categories"])

    def test_ambiguous_schema_does_not_guess_a_product(self):
        soup = BeautifulSoup('<script type="application/ld+json">' + json.dumps([
            {"@type": "Product", "name": "A", "genre": "A"},
            {"@type": "Product", "name": "B", "genre": "B"},
        ]) + '</script>', "lxml")
        self.assertEqual(structured_product(soup, "woblink", "https://woblink.com/audiobook/test-123"), {})

    def test_single_unbound_schema_must_match_the_visible_product_title(self):
        soup = BeautifulSoup('<h1>Właściwy tytuł</h1><script type="application/ld+json">' +
                             json.dumps({"@type": "Product", "name": "Inna książka", "genre": "Obca kategoria"}) + '</script>', "lxml")
        self.assertEqual(structured_product(soup, "woblink", "https://woblink.com/audiobook/test-123"), {})

    def test_recheck_replaces_old_labels_and_offline_export_cannot_restore_them(self):
        with tempfile.TemporaryDirectory() as directory, closing(Recommendations(Path(directory) / "signals.db")) as store:
            url = "https://lubimyczytac.pl/ksiazka/123/test"
            old = {"url": url, "categories": ["kryminał", "sensacja", "thriller"], "tags": ["Usunięty tag"]}
            with store.conn:
                store.save("lubimyczytac", old)
                fresh = merge_signals(old, {"url": url, "categories": ["kryminał, sensacja, thriller"], "tags": []}, fresh=True)
                store.save("lubimyczytac", fresh, tags_checked=True, replace_labels=True)
                store.save("lubimyczytac", old, only_missing=True)
            self.assertEqual(store.conn.execute("SELECT kind,label FROM labels").fetchall(),
                             [("category", "kryminał, sensacja, thriller")])

    def test_empty_unavailable_category_does_not_erase_existing_data(self):
        with tempfile.TemporaryDirectory() as directory, closing(Recommendations(Path(directory) / "signals.db")) as store:
            with store.conn:
                store.save("empik", {"url": URLS["empik"], "categories": ["Thriller"]})
                store.save("empik", {"url": URLS["empik"], "categories": []}, replace_labels=True)
            self.assertEqual(store.conn.execute("SELECT label FROM labels").fetchall(), [("Thriller",)])


class RelationshipAccuracyTests(unittest.TestCase):
    def test_all_three_real_examples_for_woblink_legimi_and_storytel(self):
        manifest = json.loads((FIXTURES / "new_sources/manifest.json").read_text(encoding="utf-8"))
        for site in ("woblink", "legimi"):
            for stem, url, _ in manifest[site]:
                with self.subTest(site=site, sample=stem):
                    data = parse(site, (FIXTURES / f"new_sources/{stem}.html").read_text(encoding="utf-8"), url)
                    if site == "legimi" or stem == "woblink_book1":
                        self.assertGreater(len(data.get("related_links", [])), 5)
                    else:
                        self.assertEqual(data.get("related_links", []), [])
                    self.assertEqual(len({item["target_key"] for item in data.get("related_links", [])}), len(data.get("related_links", [])))
                    self.assertTrue(all(item["relation_kind"] == "similar" for item in data.get("related_links", [])))
        manifest = json.loads((FIXTURES / "live_manifest.json").read_text(encoding="utf-8"))
        for index, sample in enumerate(manifest["services"]["storytel"], 1):
            if "data" not in sample:
                continue
            data = parse("storytel", (FIXTURES / f"live_storytel_{index}.html").read_text(encoding="utf-8"), sample["requested_url"])
            self.assertEqual(len(data.get("related_urls", [])), len(data.get("related_links", [])))

    def test_fifteen_current_recorded_sections_have_expected_targets_and_scope(self):
        records = json.loads((FIXTURES / "recommendation_sections.json").read_text(encoding="utf-8"))
        self.assertEqual(len(records), 15)
        for record in records:
            with self.subTest(site=record["site"], url=record["source_url"]):
                data = related_signals(record["site"], BeautifulSoup(record["html"], "lxml"), record["source_url"])
                links = data.get("related_links", [])
                self.assertEqual(len(links), record["expected_count"])
                if links:
                    self.assertEqual(data["recommendation_coverage"]["scope"], "page_section")
                    self.assertIsNone(data["recommendation_coverage"]["complete"])
                if record["site"] == "lubimyczytac":
                    self.assertTrue(all(item["target_key"] is None for item in links))
                else:
                    self.assertTrue(all(item["target_key"] for item in links))

    def test_empik_uses_only_similar_products_and_preserves_format_evidence(self):
        url = "https://www.empik.com/test,p1,ebooki-i-mp3-p"
        html = """<section data-ta-section="SimilarProducts"><h2>Podobne produkty</h2>
            <div data-ta="product" data-product-name="Druga - audiobook"><a data-ta="product-link" href="/druga,p2,ebooki-i-mp3-p"></a></div>
            <div data-ta="product" data-product-name="Trzecia - ebook"><a data-ta="product-link" href="/trzecia,p3,ebooki-i-mp3-p"></a></div>
            <a data-ta="product-link" href="/duplikat,p2,ebooki-i-mp3-p">Alias</a>
            <a data-ta="product-link" href="https://evil.test/test,p4,ebooki-i-mp3-p">Obcy</a></section>
            <section><h2>Bestsellery</h2><a data-ta="product-link" href="/bestseller,p5,ebooki-i-mp3-p">Inna lista</a></section>"""
        data = related_signals("empik", BeautifulSoup(html, "lxml"), url)
        self.assertEqual([(item["target_key"], item["audio_available"]) for item in data["related_links"]], [("2", 1), ("3", 0)])

    def test_lc_review_links_are_not_misidentified_as_books_or_inferred_from_covers(self):
        url = "https://lubimyczytac.pl/ksiazka/123/test"
        html = """<h2>Czytelnicy Test przeczytali również</h2><section id="ksiazki-rekomendacje">
            <a class="book-recommendation-card" href="/ksiazka/osada/opinia/102194390">
            <img src="https://s.lubimyczytac.pl/upload/books/5087000/5087715/cover.jpg">
            <div class="book-recommendation-card__title">Osada</div></a></section>"""
        data = related_signals("lubimyczytac", BeautifulSoup(html, "lxml"), url)
        item = data["related_links"][0]
        self.assertIsNone(item["target_key"])
        self.assertEqual(item["title"], "Osada")
        self.assertEqual(item["relation_kind"], "read_also")
        self.assertEqual(data["related_urls"], [])
        with tempfile.TemporaryDirectory() as directory, closing(Recommendations(Path(directory) / "signals.db")) as store:
            with store.conn:
                store.save("lubimyczytac", {"url": url, **data}, recommendations_checked=True)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM similar_titles").fetchone()[0], 0)
            self.assertEqual(store.conn.execute("SELECT target_key,title,relation_kind FROM related_links").fetchone(), (None, "Osada", "read_also"))
            self.assertEqual(store.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_generic_audioteka_promotions_are_not_claimed_to_be_similar(self):
        soup = BeautifulSoup('<section aria-label="Polecane"><h3>Polecane</h3><a href="/pl/audiobook/inny/">Inny</a></section>', "lxml")
        self.assertEqual(related_signals("audioteka", soup, "https://audioteka.com/pl/audiobook/test/"), {})

    def test_existing_recommendation_schema_migrates_without_losing_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.db"
            with closing(Recommendations(path)) as store, store.conn:
                store.save("woblink", {"url": "https://woblink.com/audiobook/test-1", "related_urls": ["/audiobook/other-2"]})
                store.conn.execute("DROP TABLE related_links")
                store.conn.execute("DROP TABLE recommendation_coverage")
            with closing(Recommendations(path)) as store:
                self.assertEqual(store.conn.execute("SELECT similar_key FROM similar_titles").fetchall(), [("2",)])
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM related_links").fetchone()[0], 0)

    def test_relationship_replacement_and_empty_checked_list(self):
        with tempfile.TemporaryDirectory() as directory, closing(Recommendations(Path(directory) / "signals.db")) as store:
            url = "https://woblink.com/audiobook/test-1"
            with store.conn:
                store.save("woblink", {"url": url, "related_links": [{"url": "/audiobook/other-2", "relation_kind": "similar"}]})
                store.save("woblink", {"url": url, "related_links": []}, recommendations_checked=True)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM related_links").fetchone()[0], 0)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM similar_titles").fetchone()[0], 0)


class BookBeatRelationshipTests(unittest.TestCase):
    def test_api_identity_audio_flags_and_pagination_state(self):
        data = {"url": "https://www.bookbeat.com/pl/book/test-123", "related_readings_url": "https://api.bookbeat.com/api/booklists/relatedreadings/Poland/123?offset=0&limit=25"}
        payload = {"id": 123, "title": "Inni też lubią", "_embedded": {"books": [
            {"id": 456, "title": "Audio", "audio": True, "shareurl": "https://www.bookbeat.com/pl/book/audio-456"},
            {"id": 789, "title": "Ebook", "audio": False},
        ]}, "_links": {"next": {"href": "https://api.bookbeat.com/api/booklists/relatedreadings/Poland/123?offset=25&limit=25"}}}
        calls = []
        def get(url):
            calls.append(url)
            return Response(url, json.dumps(payload).encode())
        bookbeat_related(SimpleNamespace(get=get), data)
        self.assertEqual([(x["target_key"], x["audio_available"]) for x in data["related_links"]], [("456", 1), ("789", 0)])
        self.assertEqual(data["recommendation_coverage"]["collected"], 2)
        self.assertFalse(data["recommendation_coverage"]["complete"])
        self.assertEqual(len(calls), 1)

    def test_failed_or_mismatched_api_does_not_become_a_checked_empty_list(self):
        base = {"url": "https://www.bookbeat.com/pl/book/test-123", "related_readings_url": "https://api.bookbeat.com/api/booklists/relatedreadings/Poland/123?offset=0&limit=25"}
        for payload in ({"id": 999, "_embedded": {"books": []}}, {"id": 123, "_embedded": {}},
                        {"id": 123, "_embedded": {"books": [{"id": 456, "shareurl": "https://www.bookbeat.com/pl/book/wrong-999"}]}}):
            with self.subTest(payload=payload):
                data = dict(base)
                bookbeat_related(SimpleNamespace(get=lambda url: Response(url, json.dumps(payload).encode())), data)
                self.assertIn("error", data["recommendation_coverage"])
                self.assertNotIn("related_links", data)
        data = dict(base)
        def fail(url):
            raise HTTPError(url, 429)
        bookbeat_related(SimpleNamespace(get=fail), data)
        self.assertIn("error", data["recommendation_coverage"])
        self.assertNotIn("related_links", data)

    def test_full_fetch_without_reviews_still_collects_related_books(self):
        manifest = json.loads((FIXTURES / "new_sources/manifest.json").read_text(encoding="utf-8"))
        stem, url, _ = manifest["bookbeat"][0]
        html = (FIXTURES / f"new_sources/{stem}.html").read_bytes()
        calls = []
        def optional(target):
            calls.append(target)
            return Response(target, json.dumps({"id": 2086055, "_embedded": {"books": []}}).encode())
        data = fetch_book(SimpleNamespace(get=lambda *args, **kwargs: Response(url, html), get_optional=optional), "bookbeat", url, include_reviews=False)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("/reviews/", calls[0])
        self.assertEqual(data["related_links"], [])


class CategoryRepairTests(unittest.TestCase):
    setUp = scraper_tests.DatabaseTests.setUp
    database = scraper_tests.DatabaseTests.database
    backfill_settings = scraper_tests.DatabaseTests.backfill_settings
    backfill_args = scraper_tests.DatabaseTests.backfill_args
    # Testy bazowe korzystają z tych samych schematów i katalogu tymczasowego.
    def test_sync_categories_repairs_existing_labels_without_rewriting_metadata(self):
        for site in ("empik", "lubimyczytac"):
            with self.subTest(site=site):
                db = self.database(site)
                url = URLS[site]
                db.save({**parse(site, fixture(site), url), "url": url, "title": "Zachowany tytuł", "authors": ["Zachowany autor"],
                         "category": "Błędna kategoria", "categories": ["Błędna kategoria"]})
                settings = self.backfill_settings(db)
                fresh = {"url": url, "title": "Nowy tytuł", "authors": ["Nowy autor"],
                         "category": "kryminał, sensacja, thriller", "categories": ["kryminał, sensacja, thriller"], "tags": []}
                with closing(Recommendations(recommendations_path(settings))) as store, store.conn:
                    store.save(site, {"url": url, "categories": ["Błędna kategoria"]}, recommendations_checked=True)
                args = self.backfill_args(tags_only=False, recommendations=True, sync_categories=True)
                with patch("main.fetch_book", return_value=fresh) as fetch:
                    report = run_site(site, settings, args, {})
                    fetch.assert_called_once()
                    self.assertEqual(report["attempted"], 1)
                stored = next(iter(db.stored_signals([1]).values()))
                self.assertEqual(stored.get("category") or stored["categories"][0], "kryminał, sensacja, thriller")
                with closing(sqlite3.connect(db.path)) as conn:
                    self.assertEqual(conn.execute("SELECT title FROM books").fetchone()[0], "Zachowany tytuł")
                    self.assertEqual(conn.execute("SELECT name FROM authors").fetchall(), [("Zachowany autor",)])
                    if site == "lubimyczytac":
                        self.assertEqual(conn.execute("SELECT c.name FROM categories c JOIN book_categories bc ON bc.category_id=c.id").fetchall(), [("kryminał, sensacja, thriller",)])
                with closing(sqlite3.connect(recommendations_path(settings))) as conn:
                    self.assertEqual(conn.execute("SELECT label FROM labels WHERE kind='category' AND site=?", (site,)).fetchall(), [("kryminał, sensacja, thriller",)])


if __name__ == "__main__":
    unittest.main()
