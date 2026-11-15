"""Adversarial identity and full-GUI-run regressions."""

import json
import sqlite3
import tempfile
import tkinter as tk
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

import test_scraper as original
from database import Database
from dynamic_metadata import bookbeat_related
from gui import ScraperGUI, FULL_MODE
from http_client import Response
from main import fetch_book, run_site
from parse_books import BookValidationError, parse
from product_validation import validate_download
from recommendation_parsers import related_signals
from recommendations import Recommendations, recommendations_path


class IdentityTests(unittest.TestCase):
    def test_original_and_additional_real_samples_pass_identity_verification(self):
        count = 0
        for site, url in original.URLS.items():
            html = original.fixture(site)
            data = parse(site, html, url)
            validate_download(site, html, url, url, data)
            self.assertEqual(data["source_identity"]["site"], site)
            count += 1
        manifest = json.loads((original.FIXTURES / "new_sources/manifest.json").read_text(encoding="utf-8"))
        for site, records in manifest.items():
            for stem, url, _ in records:
                with self.subTest(site=site, sample=stem):
                    html = (original.FIXTURES / f"new_sources/{stem}.html").read_text(encoding="utf-8")
                    data = parse(site, html, url)
                    validate_download(site, html, url, url, data)
                    self.assertTrue(data["source_identity"]["evidence"])
                    count += 1
        self.assertEqual(count, 25)

    def test_missing_identity_is_rejected_even_with_a_valid_heading(self):
        url = "https://www.storytel.com/pl/books/test-123"
        with self.assertRaisesRegex(BookValidationError, "Brak potwierdzenia"):
            validate_download("storytel", '<h1>Test</h1>', url, url, {"url": url, "title": "Test"})

    def test_conflicting_og_url_is_rejected(self):
        url = "https://www.storytel.com/pl/books/test-123"
        html = f'<link rel="canonical" href="{url}"><meta property="og:url" content="https://www.storytel.com/pl/books/inny-999"><h1>Test</h1>'
        with self.assertRaisesRegex(BookValidationError, "og_url"):
            validate_download("storytel", html, url, url, {"url": url, "title": "Test"})

    def test_payload_title_must_match_visible_heading(self):
        url = "https://www.storytel.com/pl/books/test-123"
        html = f'<link rel="canonical" href="{url}"><h1>Inna książka</h1>'
        with self.assertRaisesRegex(BookValidationError, "Tytuł danych"):
            validate_download("storytel", html, url, url, {"url": url, "title": "Test"})

    def test_uuid_redirect_requires_explicit_matching_reference(self):
        source = "https://audioteka.com/pl/audiobook/7a8a297f-e998-4f6d-8521-e9deb9992b68/"
        target = "https://audioteka.com/pl/audiobook/inna/"
        html = f'<link rel="canonical" href="{target}"><h1 class="product-top_title">Inna</h1>'
        client = SimpleNamespace(get=lambda *args, **kwargs: Response(target, html.encode()))
        with self.assertRaisesRegex(BookValidationError, "alias UUID"):
            fetch_book(client, "audioteka", source)

    def test_audio_work_example_cannot_supply_another_books_metadata(self):
        url = "https://www.storytel.com/pl/books/test-123"
        html = '<h1 class="x_styledTitle">Test</h1><script type="application/ld+json">' + json.dumps({
            "@type": "Book", "url": url, "name": "Test", "workExample": {
                "@type": "Audiobook", "name": "Inna książka", "readBy": [{"name": "Obcy lektor"}], "duration": "PT9H"}
        }) + '</script>'
        with self.assertRaisesRegex(BookValidationError, "Wydanie audio"):
            parse("storytel", html, url)

    def test_recommendation_card_id_must_match_its_target_link(self):
        html = '<section data-ta-section="SimilarProducts"><h2>Podobne produkty</h2><div data-ta="product" data-product-id="p999" data-product-name="Obca - audiobook"><a data-ta="product-link" href="/inna,p2,ebooki-i-mp3-p"></a></div></section>'
        with self.assertRaisesRegex(BookValidationError, "ID karty"):
            related_signals("empik", BeautifulSoup(html, "lxml"), "https://www.empik.com/test,p1,ebooki-i-mp3-p")

    def test_named_recommendation_section_must_belong_to_the_visible_product(self):
        html = '<h1>Właściwy tytuł</h1><section data-card-list-section><h2>Książki podobne do Inna książka</h2><ol><li><a href="/pl/books/cel-2">Cel</a></li></ol></section>'
        with self.assertRaisesRegex(BookValidationError, "Sekcja podobnych"):
            related_signals("storytel", BeautifulSoup(html, "lxml"), "https://www.storytel.com/pl/books/test-1")

    def test_lc_never_borrows_authors_or_publisher_from_foreign_page_sections(self):
        url = "https://lubimyczytac.pl/ksiazka/123/test"
        html = '<h1 class="book__title">Test</h1><script type="application/ld+json">' + json.dumps([
            {"@type": "Book", "url": "https://lubimyczytac.pl/ksiazka/999/other", "name": "Other", "author": {"name": "Obcy autor"}},
            {"@type": "Book", "url": url, "name": "Test"}
        ]) + '</script><footer><a class="link-name" href="/autor/1/obcy">Obcy autor</a><a href="/wydawnictwo/1/obce">Obce wydawnictwo</a></footer>'
        data = parse("lubimyczytac", html, url)
        self.assertEqual(data["authors"], [])
        self.assertIsNone(data["publisher"])

    def test_woblink_does_not_borrow_categories_from_foreign_breadcrumbs(self):
        url = "https://woblink.com/audiobook/test-123"
        html = '<h1 id="book-title">Test</h1><script type="application/ld+json">' + json.dumps([
            {"@type": "Product", "url": url, "name": "Test"},
            {"@type": "BreadcrumbList", "itemListElement": [
                {"name": "Obca kategoria", "url": "https://woblink.com/katalog/audiobooki/obca"},
                {"name": "Inna", "url": "https://woblink.com/audiobook/other-999"}]}
        ]) + '</script>'
        data = parse("woblink", html, url)
        self.assertEqual(data.get("categories", []), [])

    def test_mismatched_proof_cannot_reach_recommendation_storage(self):
        with tempfile.TemporaryDirectory() as folder, closing(Recommendations(Path(folder) / "signals.db")) as store:
            with self.assertRaises(BookValidationError), store.conn:
                store.save("empik", {"url": original.URLS["empik"], "tags": ["Obcy tag"],
                                     "source_identity": {"site": "empik", "product_key": "999"}})
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0], 0)


class DatabaseIdentityTests(unittest.TestCase):
    setUp = original.DatabaseTests.setUp
    database = original.DatabaseTests.database
    backfill_settings = original.DatabaseTests.backfill_settings
    backfill_args = original.DatabaseTests.backfill_args

    def test_wrong_fill_target_and_category_target_leave_all_rows_unchanged(self):
        db = self.database("storytel")
        first, other = "https://www.storytel.com/pl/books/first-1", "https://www.storytel.com/pl/books/other-2"
        db.save({"url": first, "title": "Pierwsza"})
        for operation in (lambda: db.save({"url": other, "title": "Obca", "category": "Obca kategoria"}, fill_book_id=1),
                          lambda: db.save_categories(1, {"url": other, "categories": ["Obca kategoria"]})):
            with self.assertRaises(BookValidationError):
                operation()
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute("SELECT title,category_id FROM books").fetchall(), [("Pierwsza", None)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0], 0)

    def test_verified_payload_with_a_different_stored_title_requires_review(self):
        db = self.database("storytel")
        url = "https://www.storytel.com/pl/books/test-1"
        db.save({"url": url, "title": "Tytuł zapisany w bazie"})
        data = {"url": url, "title": "Inny tytuł zwrócony przez serwis", "categories": ["Obca kategoria"],
                "source_identity": {"site": "storytel", "product_key": "1", "evidence": ["canonical"]}}
        for operation in (lambda: db.save(data, fill_book_id=1), lambda: db.save_categories(1, data)):
            with self.assertRaisesRegex(BookValidationError, "wymagane sprawdzenie"):
                operation()
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute("SELECT category_id FROM books").fetchone()[0], None)

    def test_corrupted_in_memory_identity_map_cannot_overwrite_another_book(self):
        db = self.database("storytel")
        db.save({"url": "https://www.storytel.com/pl/books/first-1", "title": "Pierwsza"})
        db.known["2"] = 1
        with self.assertRaises(BookValidationError):
            db.save({"url": "https://www.storytel.com/pl/books/other-2", "title": "Obca"})
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute("SELECT title FROM books").fetchall(), [("Pierwsza",)])

    def test_full_mode_fills_gaps_and_replaces_categories_using_fresh_data_only(self):
        db = self.database("storytel")
        url = original.URLS["storytel"]
        db.save({"url": url, "title": "Zachowany tytuł", "category": "Stara kategoria"})
        settings = self.backfill_settings(db)
        fresh = parse("storytel", original.fixture("storytel"), url)
        args = self.backfill_args(tags_only=False, recommendations=True, recheck=True, sync_categories=True, complete=True)
        with patch("main.fetch_book", return_value=fresh) as fetch:
            report = run_site("storytel", settings, args, {})
        fetch.assert_called_once_with(unittest.mock.ANY, "storytel", url, all_recommendations=True)
        self.assertEqual(report["attempted"], 1)
        self.assertTrue(report["complete_mode"])
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute("SELECT title FROM books").fetchone()[0], "Zachowany tytuł")
            self.assertTrue(conn.execute("SELECT description FROM books").fetchone()[0])
        with closing(sqlite3.connect(recommendations_path(settings))) as conn:
            self.assertNotIn(("Stara kategoria",), conn.execute("SELECT label FROM labels WHERE kind='category'").fetchall())

    def test_verified_edition_alias_uses_the_canonical_identity_in_source_and_signals(self):
        from sources import create_database
        path = Path(self.temp.name) / "legimi.db"
        create_database("legimi", path)
        with closing(Database("legimi", path)) as db:
            alias = "https://www.legimi.pl/ebook-test,b456.html"
            target = "https://www.legimi.pl/ebook-test,b123.html"
            db.save({"url": alias, "title": "Test"})
            settings = self.backfill_settings(db)
            fresh = {"url": target, "title": "Test", "source_ids": ["123", "456"], "categories": ["Kryminał"],
                     "source_identity": {"site": "legimi", "product_key": "123", "requested_key": "456", "evidence": ["product_state"]}}
            args = self.backfill_args(tags_only=False, recommendations=True, recheck=True, sync_categories=True, complete=True)
            with patch("main.fetch_book", return_value=fresh):
                report = run_site("legimi", settings, args, {})
            self.assertEqual(report["errors"], [])
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT url FROM books").fetchone()[0], target)
            with closing(sqlite3.connect(recommendations_path(settings))) as conn:
                self.assertEqual(conn.execute("SELECT product_key FROM titles").fetchall(), [("123",)])

    def test_full_mode_records_rejected_identity_without_saving_signals(self):
        db = self.database("storytel")
        url = original.URLS["storytel"]
        db.save({"url": url, "title": "Zachowany tytuł"})
        settings = self.backfill_settings(db)
        wrong = {"url": "https://www.storytel.com/pl/books/other-999", "title": "Obca", "tags": ["Obcy tag"]}
        args = self.backfill_args(tags_only=False, recommendations=True, recheck=True, sync_categories=True, complete=True)
        with patch("main.fetch_book", return_value=wrong):
            report = run_site("storytel", settings, args, {})
        self.assertEqual(len(report["identity_rejections"]), 1)
        with closing(sqlite3.connect(recommendations_path(settings))) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0], 0)


    def test_invalid_legacy_url_is_reported_instead_of_silently_skipped(self):
        db = self.database("storytel")
        db.save({"url": "https://www.storytel.com/pl/books/test-1", "title": "Zachowany tytuł"})
        with closing(sqlite3.connect(db.path)) as conn, conn:
            conn.execute("UPDATE books SET url='https://foreign.example/wrong'")
        args = self.backfill_args(tags_only=False, recommendations=True, recheck=True, sync_categories=True, complete=True)
        with patch("main.fetch_book") as fetch:
            report = run_site("storytel", self.backfill_settings(db), args, {})
        fetch.assert_not_called()
        self.assertEqual(len(report["identity_rejections"]), 1)
        self.assertTrue(report["errors"])

    def test_full_mode_stops_a_service_after_repeated_identity_conflicts(self):
        db = self.database("storytel")
        for number in range(1, 8):
            db.save({"url": f"https://www.storytel.com/pl/books/test-{number}", "title": "Zachowany tytuł"})
        settings = self.backfill_settings(db)
        args = self.backfill_args(tags_only=False, recommendations=True, recheck=True, sync_categories=True, complete=True)
        with patch("main.fetch_book", side_effect=BookValidationError("Niepotwierdzone ID")) as fetch:
            report = run_site("storytel", settings, args, {})
        self.assertEqual(fetch.call_count, 5)
        self.assertTrue(report["stopped_on_identity_conflicts"])
        self.assertEqual(len(report["identity_rejections"]), 5)
        self.assertFalse(report.get("scan_finished", False))

    def test_archived_metadata_from_another_identity_cannot_leak_into_current_book(self):
        from crawl_state import Coverage
        settings = self.backfill_settings(self.database("storytel"))
        path = settings["coverage_database"]
        with closing(Coverage(path, "storytel", settings["databases"]["storytel"])) as ledger:
            ledger.save_metadata({"url": "https://www.storytel.com/pl/books/test-1", "tags": ["Poprawny"]})
            ledger.conn.execute("UPDATE metadata SET data=?", (json.dumps({"url": "https://www.storytel.com/pl/books/other-999", "tags": ["Obcy tag"]}),))
            ledger.conn.commit()
            ledger.save_metadata({"url": "https://www.storytel.com/pl/books/test-1", "title": "Właściwa książka"})
            stored = json.loads(ledger.conn.execute("SELECT data FROM metadata").fetchone()[0])
            self.assertNotIn("tags", stored)


    def test_archive_reader_rejects_payload_whose_identity_differs_from_its_key(self):
        from crawl_state import Coverage
        from recommendations import ArchivedSignals
        settings = self.backfill_settings(self.database("storytel"))
        with closing(Coverage(settings["coverage_database"], "storytel", settings["databases"]["storytel"])) as ledger:
            ledger.save_metadata({"url": "https://www.storytel.com/pl/books/test-1", "tags": ["Poprawny"]})
            ledger.conn.execute("UPDATE metadata SET data=?", (json.dumps({"url": "https://www.storytel.com/pl/books/other-999", "tags": ["Obcy tag"]}),))
            ledger.conn.commit()
        with closing(ArchivedSignals(settings["coverage_database"], "storytel")) as archive:
            self.assertEqual(archive.get(["1"]), {})


class FullRecommendationTests(unittest.TestCase):
    def test_full_bookbeat_pagination_checks_each_page_and_collects_all_targets(self):
        data = {"url": "https://www.bookbeat.com/pl/book/test-123", "related_readings_url": "https://api.bookbeat.com/api/booklists/relatedreadings/Poland/123?offset=0&limit=25"}
        calls = []
        def get(url):
            calls.append(url)
            second = "offset=25" in url
            value = {"id": 123, "_embedded": {"books": [{"id": 3 if second else 2, "title": "Cel", "audio": True}]}}
            if not second:
                value["_links"] = {"next": {"href": "https://api.bookbeat.com/api/booklists/relatedreadings/Poland/123?offset=25&limit=25"}}
            return Response(url, json.dumps(value).encode())
        bookbeat_related(SimpleNamespace(get=get), data, all_pages=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual([x["target_key"] for x in data["related_links"]], ["2", "3"])
        self.assertTrue(data["recommendation_coverage"]["complete"])
        self.assertEqual(data["recommendation_coverage"]["scope"], "all_api_pages")

    def test_wrong_second_page_does_not_expose_a_partial_success(self):
        data = {"url": "https://www.bookbeat.com/pl/book/test-123", "related_readings_url": "https://api.bookbeat.com/api/booklists/relatedreadings/Poland/123?offset=0&limit=25"}
        def get(url):
            value = {"id": 999 if "offset=25" in url else 123, "_embedded": {"books": [{"id": 2}]},
                     "_links": {"next": {"href": "https://api.bookbeat.com/api/booklists/relatedreadings/Poland/123?offset=25&limit=25"}}}
            return Response(url, json.dumps(value).encode())
        bookbeat_related(SimpleNamespace(get=get), data, all_pages=True)
        self.assertIn("error", data["recommendation_coverage"])
        self.assertNotIn("related_links", data)

    def test_gui_defaults_to_a_single_full_verification_mode_and_shows_rejections(self):
        root = tk.Tk()
        root.withdraw()
        try:
            with patch.object(ScraperGUI, "load_counts"):
                gui = ScraperGUI(root)
            self.assertEqual(gui.mode.get(), FULL_MODE)
            self.assertIn("--complete", gui.command())
            self.assertIn("nie zostaną zapisane", gui.mode_description.get())
            gui.event({"event": "summary", "site": "empik", "errors": ["Niezgodne ID"],
                       "identity_rejections": [{"url": "u", "reason": "Niezgodne ID"}]})
            self.assertIn("Nie zapisano niepotwierdzonych", gui.table.item("empik", "values")[4])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
