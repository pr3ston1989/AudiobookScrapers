"""Real-page fixtures and transactional integration for six additional sources."""

import json
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
from crawl_state import Coverage
from database import Database
from discovery import Discovery, discover, discover_catalog, page_links, sitemap_entries
from http_client import Response
from main import fetch_book, load_settings, run_site
from parse_books import BookValidationError, NotAudiobook, parse
from quality import assess
from source_discovery import catalog_roots, discover_extra_catalog
from source_parsers import json_roots, objects, redux
from sources import SOURCES, create_database
from urls import canonical, identity

FIXTURES = ROOT / "tests/fixtures/new_sources"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def sample(site, index=0):
    stem, url, minutes = MANIFEST[site][index]
    return (FIXTURES / f"{stem}.html").read_text(encoding="utf-8"), url, minutes


class SourceParserTests(unittest.TestCase):
    def test_eighteen_public_samples_and_source_specific_expected_durations(self):
        for site in SOURCES:
            for index in range(3):
                with self.subTest(site=site, index=index):
                    html, url, minutes = sample(site, index)
                    data = parse(site, html, url)
                    self.assertTrue(data["title"])
                    self.assertTrue(data["authors"])
                    self.assertTrue(data["narrators"])
                    self.assertTrue(data["cover_url"].startswith("https://"))
                    self.assertEqual(data["duration_minutes"], minutes)
                    quality = assess(site, data)
                    self.assertEqual(quality["missing_required"], ["duration_minutes"] if site == "nexto" else [])

    def test_legimi_uses_audio_publisher_cover_and_never_ebook_isbn(self):
        html, url, _ = sample("legimi")
        data = parse("legimi", html, url)
        self.assertEqual(data["publisher"], "Wydawnictwo Otwarte")
        self.assertIn("a3784a29", data["cover_url"])
        self.assertEqual(data["audio_edition_id"], 1702194)
        self.assertIsNone(data["isbn"])
        self.assertEqual(len(parse("legimi", sample("legimi", 2)[0], sample("legimi", 2)[1])["narrators"]), 18)

    def test_bookbeat_selects_audio_isbn_and_full_description(self):
        data = parse("bookbeat", *sample("bookbeat")[:2])
        self.assertEqual(data["isbn"], "9788368952643")
        self.assertEqual(data["duration_seconds"], 33757)
        self.assertGreater(len(data["description"]), 500)
        self.assertNotIn("?w=", data["cover_url"])

    def test_rejects_ebook_and_unrelated_product_canonical(self):
        html = (FIXTURES / "legimi_ebook.html").read_text(encoding="utf-8")
        with self.assertRaises(NotAudiobook):
            parse("legimi", html, "https://www.legimi.pl/ebook-co-za-balagan-lynn-painter,b1705777.html")
        html, url, _ = sample("publio")
        with self.assertRaises(BookValidationError):
            parse("publio", html, "https://publio.pl/wrong,p9999999.html")

    def test_wolnelektury_short_duration_is_seconds_not_forty_one_minutes(self):
        data = parse("wolnelektury", *sample("wolnelektury", 1)[:2])
        self.assertEqual(data["duration_seconds"], 41)
        self.assertEqual(data["duration_minutes"], 1)
        self.assertIsNone(data.get("isbn"))
        self.assertIsNone(data.get("publisher"))

    def test_query_encoding_slug_aliases_and_foreign_hosts(self):
        for site in SOURCES:
            _, url, _ = sample(site)
            self.assertEqual(identity(site, url), identity(site, url + "?tracking=1#cover"))
            self.assertIsNone(identity(site, "https://example.test" + url[url.find("/", 8):]))
        self.assertEqual(identity("bookbeat", "https://www.bookbeat.com/pl/book/2086055"), "2086055")
        self.assertIsNone(identity("woblink", "https://woblink.com/ebook/example-123"))

    def test_legimi_verified_group_redirect_and_wrong_redirect(self):
        html, target, _ = sample("legimi")
        client = SimpleNamespace(get=lambda *a, **k: Response(target, html.encode()))
        source = target.replace("b1703371", "b1702194")
        self.assertEqual(fetch_book(client, "legimi", source)["audio_edition_id"], 1702194)
        with self.assertRaises(BookValidationError):
            fetch_book(client, "legimi", source.replace("1702194", "9999999"))

    def test_serialized_json_is_decoded_without_executing_javascript(self):
        from bs4 import BeautifulSoup
        value = {"title": "Żółw", "id": 123}
        payload = json.dumps(value, ensure_ascii=False)
        record = f"1:T{len(payload.encode()):x}," + payload
        script = 'self.__next_f.push(' + json.dumps([1, record], ensure_ascii=False) + ')'
        soup = BeautifulSoup(f"<script>{script}</script><script>raiseException()</script>", "lxml")
        self.assertIn(value, list(json_roots(soup)))


class DiscoveryTests(unittest.TestCase):
    def test_publio_legimi_and_woblink_have_real_next_pages(self):
        cases = [("publio", "publio_catalog", "https://publio.pl/audiobooki.html", "strona2.html"),
                 ("legimi", "legimi_filtered", "https://www.legimi.pl/ebooki/?filters=audiobooks&sort=latest", "page=2"),
                 ("woblink", "woblink_catalog", "https://woblink.com/katalog/audiobooki", "strona=2")]
        for site, stem, url, next_part in cases:
            with self.subTest(site=site):
                products, pages = page_links(site, (FIXTURES / f"{stem}.html").read_text(encoding="utf-8"), url)
                self.assertGreater(len(products), 10)
                self.assertEqual(len(pages), 1)
                self.assertIn(next_part, next(iter(pages)))

    def test_second_listing_pages_are_not_copies_of_first_page(self):
        for site, first, second, url in [("publio", "publio_catalog", "publio_page2", "https://publio.pl/audiobooki.html"),
                                          ("legimi", "legimi_filtered", "legimi_page2", "https://www.legimi.pl/ebooki/?filters=audiobooks&sort=latest")]:
            a, pages = page_links(site, (FIXTURES / f"{first}.html").read_text(encoding="utf-8"), url)
            b, _ = page_links(site, (FIXTURES / f"{second}.html").read_text(encoding="utf-8"), next(iter(pages)))
            self.assertTrue(b - a)

    def test_bookbeat_catalog_is_product_sitemap_not_ignored_page_parameter(self):
        root = catalog_roots("bookbeat")[0]
        self.assertIn("/sitemaps/book/pl/", root)
        kind, entries = sitemap_entries((FIXTURES / "bookbeat_products.html").read_bytes(), root)
        self.assertEqual(kind, "urlset")
        self.assertGreater(sum(identity("bookbeat", url) is not None for url, _ in entries), 10000)

    def test_catalog_checkpoint_survives_budget_and_resumes_to_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Coverage(Path(folder) / "state.db", "bookbeat", Path(folder) / "books.db")
            root = catalog_roots("bookbeat")[0]
            index = (FIXTURES / "bookbeat_product_index.html").read_bytes()
            products = (FIXTURES / "bookbeat_products.html").read_bytes()
            client = SimpleNamespace(get=lambda url: Response(url, index if url == root else products))
            first = Discovery(observer=ledger.discover)
            discover_extra_catalog("bookbeat", client, first, ledger, {"catalog_page_budget": 1})
            self.assertFalse(first.catalog_complete)
            self.assertTrue(ledger.has_pending_pages())
            second = Discovery(observer=ledger.discover)
            discover_extra_catalog("bookbeat", client, second, ledger, {"catalog_page_budget": 1})
            self.assertTrue(second.catalog_complete)
            self.assertGreater(len(second.urls), 10000)
            ledger.close()


class DatabaseIntegrationTests(unittest.TestCase):
    def test_each_source_recovers_interrupted_processing_without_duplicates(self):
        for site in SOURCES:
            with self.subTest(site=site), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                settings = load_settings(ROOT / "config.json")
                settings.update(databases={site: root / "books.db"}, coverage_database=root / "coverage.db", backup=False)
                create_database(site, settings["databases"][site])
                samples = [parse(site, *sample(site, index)[:2]) for index in range(3)]
                db = Database(site, settings["databases"][site])
                db.enqueue([data["url"] for data in samples])
                db.close()
                args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                                       no_backup=True, refresh_limit=0, limit=0, no_covers=True)
                calls = []
                def fetch(client, requested_site, url):
                    calls.append(url)
                    if len(calls) == 2:
                        raise KeyboardInterrupt
                    return next(data for data in samples if identity(site, data["url"]) == identity(site, url))
                with patch("main.ROOT", root), patch("main.fetch_book", side_effect=fetch):
                    with self.assertRaises(KeyboardInterrupt):
                        run_site(site, settings, args, {})
                db = Database(site, settings["databases"][site])
                try:
                    self.assertEqual(db.stats()["books"], 1)
                    # Stan processing symuluje również awarię procesu bez obsługi wyjątku.
                    db.mark(db.pending()[0]["id"], "processing")
                finally:
                    db.close()
                with patch("main.ROOT", root), patch("main.fetch_book", side_effect=lambda client, requested_site, url: next(data for data in samples if identity(site, data["url"]) == identity(site, url))):
                    report = run_site(site, settings, args, {})
                self.assertEqual(report["errors"], [])
                self.assertEqual(report["after"]["books"], 3)
                self.assertEqual(report["inserted"], 2)

    def test_manual_url_does_not_consume_or_enqueue_other_discovered_products(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            site = "bookbeat"
            settings = load_settings(ROOT / "config.json")
            settings.update(databases={site: root / "books.db"}, coverage_database=root / "coverage.db", backup=False)
            create_database(site, settings["databases"][site])
            html, url, _ = sample(site)
            other = "https://www.bookbeat.com/pl/book/another-999999999"
            ledger = Coverage(settings["coverage_database"], site, settings["databases"][site])
            ledger.discover(other)
            ledger.close()
            db = Database(site, settings["databases"][site])
            db.enqueue([other])
            db.close()
            args = SimpleNamespace(command="update", url=url, resume=False, full_catalog=False,
                                   no_backup=True, refresh_limit=0, limit=0, no_covers=True)
            with patch("main.ROOT", root), patch("main.fetch_book", return_value=parse(site, html, url)) as fetch:
                report = run_site(site, settings, args, {})
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[2], url)
            self.assertEqual(report["inserted"], 1)
            self.assertEqual(report["queued_repair"], 0)
            db = Database(site, settings["databases"][site])
            try:
                self.assertEqual([item["url"] for item in db.pending()], [other])
            finally:
                db.close()

    def test_all_six_schemas_are_compatible_and_upserts_are_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            for site in SOURCES:
                with self.subTest(site=site):
                    path = Path(folder) / f"{site}.db"
                    self.assertTrue(create_database(site, path))
                    self.assertFalse(create_database(site, path))
                    with closing(sqlite3.connect(path)) as conn:
                        before = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
                    db = Database(site, path)
                    try:
                        for index in range(3):
                            html, url, _ = sample(site, index)
                            data = parse(site, html, url)
                            self.assertEqual(db.save(data), "inserted")
                            self.assertEqual(db.save(data), "updated")
                        self.assertEqual(db.stats()["books"], 3)
                        with closing(sqlite3.connect(path)) as conn:
                            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                            self.assertEqual(before, conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall())
                    finally:
                        db.close()

    def test_cannot_create_empty_replacement_of_original_database(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                create_database("audioteka", Path(folder) / "missing.db")

    def test_skipped_ebooks_are_deferred_not_fetched_each_run(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Coverage(Path(folder) / "state.db", "bookbeat", Path(folder) / "books.db")
            try:
                url = "https://www.bookbeat.com/pl/book/ebook-123"
                ledger.mark(url, "skipped")
                self.assertFalse(ledger.due(url))
                self.assertEqual(ledger.work({}), [])
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
