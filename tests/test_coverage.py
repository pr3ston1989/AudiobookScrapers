"""Tests for durable coverage, AJAX pagination, field quality and GUI commands."""

import json
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawl_state import Coverage
from discovery import Discovery, catalog_links, catalog_response, discover_catalog, page_links
from http_client import Response
from quality import assess
from gui import ScraperGUI
from parse_books import parse


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.db"

    def ledger(self):
        return Coverage(self.path, "storytel", Path(self.temp.name) / "books.db")

    def test_saved_incomplete_book_is_retried_after_restart(self):
        url = "https://www.storytel.com/pl/books/book-123"
        ledger = self.ledger()
        ledger.discover(url, "2026-10-09")
        ledger.reconcile({"123": 1})
        self.assertEqual(ledger.summary()["inherited_not_verified"], 1)
        ledger.mark(url, "incomplete", {"missing": ["duration_minutes"]})
        self.assertFalse(ledger.due(url))
        ledger.close()
        ledger = self.ledger()
        self.assertEqual(ledger.work({"123": 1}), [])
        ledger.conn.execute("UPDATE products SET retry_at=0")
        ledger.conn.commit()
        self.assertEqual(ledger.work({"123": 1}), [url])
        ledger.mark(url, "verified")
        ledger.discover(url, "2026-10-10")
        self.assertEqual(ledger.work({"123": 1}), [url])
        ledger.close()

    def test_catalog_resumes_after_page_budget(self):
        class Client:
            def get(self, url, **kwargs):
                if url.endswith("categories"):
                    return Response(url, b'<a href="/pl/categories/crime">Crime</a>')
                return Response(url, b'<a href="/pl/books/book-123">Book</a>')
        ledger = self.ledger()
        first = Discovery(observer=ledger.discover)
        discover_catalog("storytel", Client(), first, ledger, {"catalog_page_budget": 1})
        self.assertFalse(first.catalog_complete)
        self.assertIsNotNone(ledger.pending_page())
        ledger.close()

        ledger = self.ledger()
        second = Discovery(observer=ledger.discover)
        discover_catalog("storytel", Client(), second, ledger, {"catalog_page_budget": 1})
        self.assertTrue(second.catalog_complete)
        self.assertIn("123", second.urls)
        ledger.close()

    def test_new_empik_category_is_discovered_without_filter_recursion(self):
        html = '<a href="/audiobooki-i-ebooki/audiobooki/nowa,999999,s?sort=popular">New</a><a href="/audiobooki-i-ebooki/audiobooki/nowa,999999,s,61?price=10">Filter</a>'
        self.assertEqual(catalog_links("empik", html, "https://www.empik.com/"),
                         {"https://www.empik.com/audiobooki-i-ebooki/audiobooki/nowa,999999,s?sort=publishDesc"})

    def test_lc_ajax_preserves_filters_and_next_page(self):
        html = (ROOT / "tests/fixtures/lc_catalog.html").read_text(encoding="utf-8")
        _, pages = page_links("lubimyczytac", html, "https://lubimyczytac.pl/katalog/ksiazki")
        self.assertTrue(any("standardFilteredDataPageContent" in page for page in pages))
        class Client:
            def post_catalog(self, url, data):
                self.data = dict(data)
                return Response(url, json.dumps({"data": {"left": 1, "content": '<a href="/ksiazka/123/a">Book</a>'}}).encode())
        client = Client()
        ajax = next(page for page in pages if "standardFilteredDataPageContent" in page)
        response = catalog_response("lubimyczytac", client, ajax)
        products, next_pages = page_links("lubimyczytac", response.text, response.url)
        self.assertEqual(client.data["page"], "2")
        self.assertEqual(client.data["onlyPublished"], "1")
        self.assertEqual(len(products), 1)
        self.assertTrue(any("page=3" in page for page in next_pages))

    def test_optional_absence_does_not_mask_missing_duration(self):
        data = dict(title="Book", authors=["A"], publisher="P", description="D", cover_url="C")
        self.assertFalse(assess("empik", data)["complete"])
        data["duration_minutes"] = 22
        self.assertTrue(assess("empik", data)["complete"])
        self.assertIn("narrators", assess("empik", data)["optional_absent"])
        self.assertTrue(assess("lubimyczytac", {k: v for k, v in data.items() if k != "duration_minutes"})["complete"])
        data["duration_minutes"] = 0
        self.assertFalse(assess("empik", data)["complete"])

    def test_extra_fields_survive_restart_and_partial_response(self):
        ledger = self.ledger()
        ledger.save_metadata({"url": "https://www.storytel.com/pl/books/book-123", "isbn": "9781234567890", "release_date": "2026-01-01"})
        ledger.close()
        ledger = self.ledger()
        ledger.save_metadata({"url": "https://www.storytel.com/pl/books/book-123", "isbn": None, "release_date": ""})
        data = json.loads(ledger.conn.execute("SELECT data FROM metadata").fetchone()[0])
        self.assertEqual(data["isbn"], "9781234567890")
        self.assertEqual(data["release_date"], "2026-01-01")
        ledger.close()

    def test_broken_catalog_branch_does_not_block_healthy_branch(self):
        class Client:
            def get(self, url, **kwargs):
                if url.endswith("categories"):
                    return Response(url, b'<a href="/pl/categories/a-broken">Bad</a><a href="/pl/categories/z-good">Good</a>')
                if "a-broken" in url:
                    raise RuntimeError("HTTP 503")
                return Response(url, b'<a href="/pl/books/book-123">Book</a>')
        ledger = self.ledger()
        try:
            result = Discovery(observer=ledger.discover)
            discover_catalog("storytel", Client(), result, ledger, {"catalog_page_budget": 3})
            self.assertIn("123", result.urls)
            self.assertFalse(result.catalog_complete)
            self.assertTrue(ledger.has_pending_pages())
            self.assertIsNone(ledger.pending_page())
            retry = Discovery()
            discover_catalog("storytel", Client(), retry, ledger, {"catalog_page_budget": 3})
            self.assertEqual(retry.pages, 0)
            self.assertFalse(retry.catalog_complete)
        finally:
            ledger.close()

    def test_success_resets_error_backoff(self):
        ledger = self.ledger()
        url = "https://www.storytel.com/pl/books/book-123"
        ledger.mark(url, "error")
        ledger.mark(url, "verified")
        self.assertEqual(ledger.conn.execute("SELECT attempts FROM products").fetchone()[0], 0)
        ledger.mark(url, "error")
        self.assertEqual(ledger.conn.execute("SELECT attempts FROM products").fetchone()[0], 1)
        ledger.close()

    def test_real_lc_second_page_and_no_duplicate_next_urls(self):
        html = (ROOT / "tests/fixtures/lc_catalog_page2.html").read_text(encoding="utf-8")
        products, pages = page_links("lubimyczytac", html, "https://lubimyczytac.pl/book/standardFilteredDataPageContent?page=2")
        self.assertEqual(len(products), 30)
        self.assertEqual(len(pages), 1)
        self.assertIn("page=3", next(iter(pages)))

    def test_twelve_live_examples_required_and_extended_fields(self):
        report = json.loads((ROOT / "tests/fixtures/live_manifest.json").read_text(encoding="utf-8"))
        for site, samples in report["services"].items():
            valid = [sample for sample in samples if "data" in sample]
            self.assertEqual(len(valid), 3)
            for number, sample in enumerate(valid, 1):
                html = (ROOT / f"tests/fixtures/live_{site}_{number}.html").read_text(encoding="utf-8")
                data = parse(site, html, sample["requested_url"])
                self.assertTrue(assess(site, data)["complete"], (site, number))
                if site in ("audioteka", "empik", "lubimyczytac") and number == 2:
                    self.assertTrue(data["reviews"], site)
                if site == "lubimyczytac" and number == 2:
                    self.assertEqual(data["series"]["name"], "Karkonoska seria kryminalna")
                    self.assertEqual(data["volume_number"], "1")
                if site == "empik" and number == 2:
                    self.assertEqual(data["duration_minutes"], 504)
                    self.assertEqual(data["narrators"], ["Liberek Barbara"])
                    self.assertTrue(data["tags"])

    def test_current_storytel_series_label_and_volume(self):
        html = (ROOT / "tests/fixtures/storytel_series_live.html").read_text(encoding="utf-8")
        data = parse("storytel", html, "https://www.storytel.com/pl/books/harry-potter-i-kamie%C5%84-filozoficzny-254416")
        self.assertIn("Harry Potter", data["collections"])
        self.assertEqual(data["volume_number"], "1")
        self.assertEqual(data["series_position"], "1 z 7")
        self.assertEqual(data["narrators"], ["Piotr Fronczewski"])


class GuiTests(unittest.TestCase):
    def test_widgets_command_and_report_events(self):
        root = tk.Tk()
        root.withdraw()
        try:
            gui = ScraperGUI(root)
            root.update_idletasks()
            command = gui.command()
            self.assertEqual(len(gui.table.get_children()), 10)
            self.assertNotIn("nexto", gui.table.get_children())
            self.assertNotIn("nexto", gui.selected)
            self.assertNotIn("nexto", command)
            self.assertTrue(all(site in command for site in ("publio", "bookbeat", "woblink", "legimi", "wolnelektury", "virtualo")))
            self.assertIn("--services", command)
            self.assertNotIn("--headless", command)
            gui.workers.set("6")
            parallel_command = gui.command()
            self.assertEqual(parallel_command[parallel_command.index("--workers") + 1], "6")
            gui.parallel.set(False)
            sequential_command = gui.command()
            self.assertEqual(sequential_command[sequential_command.index("--workers") + 1], "1")
            gui.parallel.set(True)
            gui.workers.set("4")
            selected = ["audioteka", "storytel", "empik", "publio", "legimi", "wolnelektury"]
            gui.event({"event": "schedule", "sites": selected, "workers": 4})
            self.assertIn("Wybrano 6", gui.status.get())
            self.assertIn("oczekuje 2", gui.status.get())
            self.assertEqual(gui.table.item("wolnelektury", "values")[4], "Oczekuje na wolny proces")
            self.assertEqual(gui.table.item("bookbeat", "values")[4], "Nie wybrano")
            gui.event({"event": "phase", "site": "wolnelektury", "phase": "Uruchamianie"})
            self.assertEqual(gui.table.item("wolnelektury", "values")[4], "Uruchamianie")
            gui.event({"event": "progress", "site": "empik", "done": 2, "total": 10, "inserted": 1,
                       "updated": 1, "incomplete": 0, "url": "https://www.empik.com/"})
            self.assertEqual(gui.table.item("empik", "values")[1], "1")
            gui.event({"event": "summary", "site": "empik", "after": {"books": 28505}, "errors": [],
                       "coverage": {"catalog_pages_pending": 18}})
            self.assertIn("18", gui.table.item("empik", "values")[4])
            gui.event({"event": "summary", "site": "empik", "after": {"books": 28506}, "errors": [],
                       "incomplete": 6, "inserted": 7, "coverage": {"unresolved": 6}})
            self.assertEqual(gui.table.item("empik", "values")[3], "6")
            self.assertIn("ponowimy później", gui.table.item("empik", "values")[4])
            gui.event({"event": "summary", "site": "empik", "errors": ["HTTP 503"]})
            self.assertEqual(gui.table.item("empik", "values")[4], "Błąd: HTTP 503")
            gui.event({"event": "summary", "site": "audioteka", "after": {"books": 75600},
                       "inserted": 322, "updated": 627, "errors": ["HTTP 502"]})
            self.assertIn("Przebieg zakończony; błędy: 1", gui.table.item("audioteka", "values")[4])
            gui.event({"event": "summary", "site": "audioteka", "after": {"books": 75600},
                       "errors": ["HTTP 503"], "stopped_on_errors": True})
            self.assertIn("Zatrzymano po 5", gui.table.item("audioteka", "values")[4])
            gui.mode.set("Sprawdź ponownie wszystkie tagi Empiku")
            tag_command = gui.command()
            self.assertIn("--tags-only", tag_command)
            self.assertIn("--recheck", tag_command)
            selected = tag_command[tag_command.index("--services")+1:tag_command.index("--limit")]
            self.assertEqual(selected, ["empik"])
            gui.mode.set("Napraw miniaturki Audioteki")
            repair_command = gui.command()
            self.assertIn("repair-covers", repair_command)
            selected = repair_command[repair_command.index("--services")+1:repair_command.index("--limit")]
            self.assertEqual(selected, ["audioteka"])
            gui.limit.set("-1")
            with self.assertRaises(ValueError):
                gui.command()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
