"""Confirmed Publio similar-list API and Virtualo bestseller isolation."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from dynamic_metadata import publio_related
from http_client import Client, HTTPError, Response
from main import fetch_book, load_settings
from parse_books import BookValidationError, parse
from product_validation import validate_download
from recommendation_parsers import related_signals
from recommendations import Recommendations

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/publio_virtualo_signals"
PRIMARY = ROOT / "tests/fixtures/new_sources"


def response(key, value, *, url=None, status=200):
    return Response(url or f"https://publio.pl/rest/v3/catalog/product/{key}/similar", json.dumps(value).encode(), status)


def own(key="911672"):
    return {"url": f"https://publio.pl/test,p{key}.html", "title": "Własna książka", "categories": ["Własna kategoria"], "authors": ["Własny autor"]}


class PublioConfirmedTests(unittest.TestCase):
    def test_three_captured_api_lists_validate_ids_without_copying_target_metadata(self):
        for key, count in (("911672", 19), ("2775312", 27), ("247092", 15)):
            with self.subTest(key=key):
                value = json.loads((FIXTURES / f"publio_similar_{key}.json").read_text(encoding="utf-8"))
                data = own(key)
                publio_related(SimpleNamespace(get=lambda url: response(key, value)), data)
                self.assertEqual(len(data["related_links"]), count)
                self.assertTrue(all(item["audio_available"] == 1 for item in data["related_links"]))
                self.assertEqual(data["authors"], ["Własny autor"])
                self.assertEqual(data["categories"], ["Własna kategoria"])
                self.assertEqual(data["recommendation_coverage"]["scope"], "api_response")
                self.assertIsNone(data["recommendation_coverage"]["complete"])
                self.assertEqual({x["target_key"] for x in data["related_links"]}, {str(x["id"]) for x in value})

    def test_redirected_or_foreign_response_cannot_be_attached_to_the_requested_book(self):
        for url in ("https://publio.pl/rest/v3/catalog/product/999/similar", "https://foreign.example/rest/v3/catalog/product/911672/similar"):
            with self.subTest(url=url):
                data = own()
                publio_related(SimpleNamespace(get=lambda target: response("911672", [], url=url)), data)
                self.assertIn("error", data["recommendation_coverage"])
                self.assertNotIn("related_links", data)

    def test_bad_product_identity_or_format_rejects_the_whole_list(self):
        good = {"id": 2, "productOrIssueId": 2, "productUrl": "/target,p2.html", "title": "Cel", "section": "AUDIOBOOK", "formatList": ["MP3"]}
        for changes in ({"productUrl": "/wrong,p999.html"}, {"productOrIssueId": 999}, {"formatList": ["EPUB"]},
                        {"section": "EBOOK"}, {"title": ""}, {"formatList": None}, {"id": None}):
            with self.subTest(changes=changes):
                data = own()
                publio_related(SimpleNamespace(get=lambda url: response("911672", [good, {**good, **changes}])), data)
                self.assertIn("error", data["recommendation_coverage"])
                self.assertNotIn("related_links", data)

    def test_ebook_targets_are_marked_explicitly_and_duplicates_and_self_are_removed(self):
        records = [{"id": 2, "productUrl": "/ebook,p2.html", "title": "Ebook", "section": "EBOOK", "formatList": ["EPUB"]},
                   {"id": 911672, "productUrl": "/self,p911672.html", "title": "Własna", "section": "AUDIOBOOK", "formatList": ["MP3"]}]
        data = own()
        publio_related(SimpleNamespace(get=lambda url: response("911672", records + records)), data)
        self.assertEqual([(x["target_key"], x["audio_available"]) for x in data["related_links"]], [("2", 0)])

    def test_only_a_successful_empty_array_is_recorded_as_an_empty_list(self):
        for value, status in (({}, 200), ([], 403), ([], 429), ("challenge", 200)):
            with self.subTest(value=value, status=status):
                data = own()
                publio_related(SimpleNamespace(get=lambda url: response("911672", value, status=status)), data)
                self.assertIn("error", data["recommendation_coverage"])
                self.assertNotIn("related_links", data)
        data = own()
        publio_related(SimpleNamespace(get=lambda url: response("911672", [])), data)
        self.assertEqual(data["related_links"], [])
        self.assertNotIn("error", data["recommendation_coverage"])

    def test_optional_403_does_not_blacklist_primary_product_pages_or_launch_a_browser(self):
        client = Client(load_settings(ROOT / "config.json"))
        try:
            with patch.object(client, "throttle"), patch.object(client, "browser_get") as browser, patch.object(client.session, "get", return_value=SimpleNamespace(status_code=403)):
                with self.assertRaises(HTTPError):
                    client.get_optional_json("https://publio.pl/rest/v3/catalog/product/911672/similar")
                browser.assert_not_called()
                self.assertEqual(client.access_denied_hosts, set())
        finally:
            client.close()

    def test_full_fetch_collects_relations_after_primary_product_validation(self):
        value = json.loads((FIXTURES / "publio_similar_911672.json").read_text(encoding="utf-8"))
        html = (PRIMARY / "publio_book1.html").read_bytes()
        url = "https://publio.pl/mitologia-grecja-jan-parandowski,p911672.html"
        optional = Mock(side_effect=lambda target: response("911672", value))
        client = SimpleNamespace(get=lambda *args, **kwargs: Response(url, html), get_optional_json=optional)
        data = fetch_book(client, "publio", url, include_reviews=False)
        self.assertEqual(len(data["related_links"]), 19)
        optional.assert_called_once()
        self.assertEqual(data["source_identity"]["product_key"], "911672")
        with tempfile.TemporaryDirectory() as directory, closing(Recommendations(Path(directory) / "signals.db")) as store:
            with store.conn:
                store.save("publio", data, recommendations_checked=True, replace_labels=True)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM related_links WHERE product_key='911672'").fetchone()[0], 19)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM similar_titles WHERE product_key='911672'").fetchone()[0], 19)
            self.assertEqual(store.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_invalid_primary_page_prevents_optional_api_calls(self):
        url = "https://publio.pl/test,p911672.html"
        optional = Mock()
        html = b'<link rel="canonical" href="https://publio.pl/other,p999.html"><h1>Other</h1>'
        client = SimpleNamespace(get=lambda *args, **kwargs: Response(url, html), get_optional_json=optional)
        with self.assertRaises(BookValidationError):
            fetch_book(client, "publio", url)
        optional.assert_not_called()


class VirtualoConfirmedTests(unittest.TestCase):
    def test_three_rendered_bestseller_widgets_cannot_change_primary_book_metadata(self):
        rows = json.loads((FIXTURES / "virtualo_bestsellers.json").read_text(encoding="utf-8"))
        expected = {"virtualo_book1": ("Morelowy Las", ["Patryk Michniewicz"], ["Fantasy i realizm magiczny"]),
                    "virtualo_book2": ("Café Museum", ["Robert Makłowicz"], ["Wakacje i podróże"]),
                    "virtualo_book3": ("W poszukiwaniu światła. Opowieść o Marii Skłodowskiej-Curie", ["Anna Czerwińska-Rydel"], ["Nauka i wiedza"])}
        for row in rows:
            with self.subTest(fixture=row["fixture"]):
                soup = BeautifulSoup((PRIMARY / (row["fixture"] + ".html")).read_text(encoding="utf-8"), "lxml")
                old = soup.select_one("#products_top_view")
                replacement = BeautifulSoup(row["widget"], "lxml").select_one("#products_top_view")
                old.replace_with(replacement)
                data = parse("virtualo", str(soup), row["url"])
                validate_download("virtualo", str(soup), row["url"], row["url"], data)
                self.assertEqual((data["title"], data["authors"], data["categories"]), expected[row["fixture"]])
                self.assertIn("product_widget_context", data["source_identity"]["evidence"])
                self.assertFalse(data.get("tags"))
                self.assertEqual(related_signals("virtualo", soup, row["url"]), {})

    def test_wrong_virtualo_widget_product_context_is_rejected(self):
        url = "https://virtualo.pl/audiobook/morelowy-las-i631575/"
        html = (PRIMARY / "virtualo_book1.html").read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "lxml")
        soup.select_one("#products_top_view")["data-product-id"] = "999"
        data = parse("virtualo", str(soup), url)
        with self.assertRaisesRegex(BookValidationError, "kontekst listy"):
            validate_download("virtualo", str(soup), url, url, data)


if __name__ == "__main__":
    unittest.main()
