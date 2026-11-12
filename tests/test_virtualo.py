"""Virtualo fields, audio validation, gzip maps and unordered release pagination."""
import gzip
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from discovery import Discovery, discover, page_links
from http_client import Response
from parse_books import NotAudiobook, BookValidationError, parse
from source_discovery import inspect_page
from source_discovery import catalog_roots, discover_extra_catalog
from crawl_state import Coverage
from virtualo_parser import audio_length

FIXTURES = ROOT / "tests" / "fixtures" / "new_sources"
URL = "https://virtualo.pl/audiobook/morelowy-las-i631575/"
NEWS = "https://virtualo.pl/audiobooki/nowosci/?list-grid=1"


class VirtualoTests(unittest.TestCase):
    def test_full_catalog_starts_from_audio_categories_not_ebook_maps(self):
        root = catalog_roots("virtualo")[0]
        self.assertEqual(root, "https://virtualo.pl/audiobooki/")
        html = (FIXTURES / "virtualo_catalog.html").read_text(encoding="utf-8")
        products, pages = page_links("virtualo", html, root)
        self.assertFalse(products)
        self.assertGreater(len(pages), 30)
        self.assertTrue(all('/audiobooki/' in page and 'list-grid=1' in page for page in pages))
        client = SimpleNamespace(get=lambda url: Response(url, html.encode()))
        products, actual_pages = inspect_page("virtualo", client, root, Discovery())
        self.assertEqual(actual_pages, pages)

    def test_real_category_contains_audio_and_a_next_page(self):
        url = "https://virtualo.pl/audiobooki/biografie-c288/?list-grid=1"
        products, pages = page_links("virtualo", (FIXTURES / "virtualo_category.html").read_text(encoding="utf-8"), url)
        self.assertEqual(len(products), 20)
        self.assertEqual(pages, {url + '&page=2'})

    def test_catalog_adapter_migration_preserves_discovered_products(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Coverage(Path(folder) / 'coverage.db', 'virtualo', Path(folder) / 'books.db')
            try:
                ledger.discover(URL)
                ledger.seed_pages(['https://virtualo.pl/sitemap.xml'])
                html = (FIXTURES / "virtualo_catalog.html").read_bytes()
                client = SimpleNamespace(get=lambda url: Response(url, html))
                result = Discovery(observer=ledger.discover)
                discover_extra_catalog('virtualo', client, result, ledger, {'catalog_page_budget': 1})
                self.assertEqual(ledger.checkpoint('virtualo_catalog_version'), 2)
                self.assertFalse(result.catalog_complete)
                self.assertIn('/audiobooki/', ledger.pending_page())
                self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM catalog_pages WHERE url LIKE '%sitemap%'").fetchone()[0], 0)
                self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)
            finally:
                ledger.close()

    def test_audio_edition_fields_and_largest_advertised_cover(self):
        data = parse("virtualo", (FIXTURES / "virtualo_book1.html").read_text(encoding="utf-8"), URL)
        self.assertEqual(data["title"], "Morelowy Las")
        self.assertEqual(data["authors"], ["Patryk Michniewicz"])
        self.assertEqual(data["narrators"], ["Łukasz Giętkowski"])
        self.assertEqual(data["isbn"], "9788397679917")
        self.assertEqual(data["release_date"], "2026-01-17")
        self.assertEqual(data["duration_seconds"], 2580)
        self.assertIn("/covers/large/", data["cover_url"])
        self.assertGreater(len(data["description"]), 500)

    def test_ambiguous_duration_remains_missing_instead_of_becoming_hours(self):
        url = "https://virtualo.pl/audiobook/bajka-o-rodzicach-i652242/"
        data = parse("virtualo", (FIXTURES / "virtualo_short.html").read_text(encoding="utf-8"), url)
        self.assertIsNone(data["duration_minutes"])
        self.assertTrue(data["duration_ambiguous"])
        self.assertIn("9:44", data["length_str"])

    def test_duration_formats_and_narrative_numbers(self):
        for value, seconds in {"43 minuty": 2580, "4 godz. 25 min.": 15900, "1h48'": 6480,
                               "1h48min": 6480, "0:09:44": 584, "9:30 minut": 570,
                               "1 dzień 7 godz. 28 min.": 113280, "2 dni": 172800,
                               "9:44": None, "bitwy (114)": None}.items():
            with self.subTest(value=value):
                self.assertEqual(audio_length(value), seconds)

    def test_real_second_page_has_other_products_and_preserves_view(self):
        first, pages = page_links("virtualo", (FIXTURES / "virtualo_news_grid.html").read_text(encoding="utf-8"), NEWS)
        second, more = page_links("virtualo", (FIXTURES / "virtualo_page2.html").read_text(encoding="utf-8"), NEWS + "&page=2")
        self.assertEqual(len(first), 20)
        self.assertTrue(second - first)
        self.assertEqual(pages, {NEWS + "&page=2"})
        self.assertEqual(more, {NEWS + "&page=3"})

    def test_known_sales_sorted_pages_do_not_hide_later_new_books(self):
        urls = [URL, URL.replace("631575", "631576"), URL.replace("631575", "631577"), URL.replace("631575", "631578")]
        def get(url):
            index = int(url.split("page=")[-1]) - 1 if "page=" in url else 0
            html = f'<a href="{urls[index]}">Book</a>'
            if index < 3:
                html += f'<link rel="next" href="{NEWS}&page={index + 2}">'
            return Response(url, html.encode())
        from urls import identity
        result = discover("virtualo", SimpleNamespace(get=get), {"max_listing_pages": 10},
                          {identity("virtualo", url): 1 for url in urls[:3]}, False)
        self.assertEqual(len(result.urls), 4)
        self.assertEqual(result.errors, [])

    def test_compressed_maps_ignore_ebooks_and_keep_audio(self):
        xml = f'<urlset><url><loc>{URL}</loc><lastmod>2026-10-15</lastmod></url><url><loc>https://virtualo.pl/ebook/test-i99/</loc></url></urlset>'
        client = SimpleNamespace(get=lambda url: Response(url, gzip.compress(xml.encode())))
        result = Discovery()
        products, pages = inspect_page("virtualo", client, "https://virtualo.pl/sitemap-category-test.xml.gz", result)
        self.assertEqual(len(products), 1)
        self.assertEqual(len(result.urls), 1)
        self.assertFalse(pages)
        real = (FIXTURES / "virtualo_map_products.html").read_bytes()
        client.get = lambda url: Response(url, real)
        inspect_page("virtualo", client, "https://virtualo.pl/sitemap-category-test.xml.gz", Discovery())

    def test_wrong_product_ebook_and_empty_index_are_rejected(self):
        html = (FIXTURES / "virtualo_book1.html").read_text(encoding="utf-8")
        with self.assertRaises(BookValidationError):
            parse("virtualo", html, URL.replace("631575", "999999"))
        with self.assertRaises(NotAudiobook):
            parse("virtualo", html.replace(" - audiobook", " - ebook").replace("<div class=\"data\">MP3</div>", "<div class=\"data\">EPUB</div>"), URL)
        client = SimpleNamespace(get=lambda url: Response(url, b'<sitemapindex/>'))
        with self.assertRaises(ValueError):
            inspect_page("virtualo", client, "https://virtualo.pl/sitemap.xml", Discovery())


if __name__ == "__main__":
    unittest.main()
