"""Reject Legimi landing collections and verify durable full-list pagination."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawl_state import Coverage
from discovery import Discovery, page_links
from http_client import Response
from source_discovery import catalog_roots, discover_extra_catalog, prepare_catalog
from source_parsers import redux

FIXTURES = ROOT / 'tests/fixtures/new_sources'
BASE = 'https://www.legimi.pl/ebooki/?filters=audiobooks&sort=latest'


class LegimiCatalogTests(unittest.TestCase):
    def test_full_catalog_root_is_paginated_audio_listing(self):
        self.assertEqual(catalog_roots('legimi'), [BASE])
        html = (FIXTURES / 'legimi_curated.html').read_text(encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'kolekcji'):
            page_links('legimi', html, BASE)

    def test_real_tenth_and_last_pages(self):
        products, pages = page_links('legimi', (FIXTURES / 'legimi_page10.html').read_text(encoding='utf-8'), BASE + '&page=10')
        self.assertEqual(len(products), 20)
        self.assertEqual(len(pages), 1)
        self.assertIn('page=11', next(iter(pages)))
        products, pages = page_links('legimi', (FIXTURES / 'legimi_last.html').read_text(encoding='utf-8'), BASE + '&page=1290')
        self.assertEqual(len(products), 9)
        self.assertFalse(pages)

    def test_wrong_page_response_is_rejected(self):
        html = (FIXTURES / 'legimi_filtered.html').read_text(encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'numer'):
            page_links('legimi', html, BASE + '&page=10')

    def test_missing_next_tag_is_recovered_from_pagination(self):
        state = redux(BeautifulSoup((FIXTURES / 'legimi_filtered.html').read_text(encoding='utf-8'), 'lxml'))
        state['changeMetatag']['nextUrl'] = None
        html = '<script type="application/json">' + json.dumps(state) + '</script>'
        _, pages = page_links('legimi', html, BASE)
        self.assertEqual(len(pages), 1)
        self.assertIn('page=2', next(iter(pages)))
        self.assertIn('filters=audiobooks', next(iter(pages)))

    def test_migration_keeps_products_and_invalidates_false_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Coverage(Path(folder) / 'state.db', 'legimi', Path(folder) / 'books.db')
            try:
                url = 'https://www.legimi.pl/ebook-test,b123.html'
                ledger.mark(url, 'verified')
                ledger.seed_pages(['https://www.legimi.pl/ebooki/?filters=audiobooks'])
                ledger.finish_page('https://www.legimi.pl/ebooki/?filters=audiobooks')
                ledger.set_checkpoint('last_full_catalog', 9999999999)
                prepare_catalog('legimi', ledger)
                self.assertEqual(ledger.pending_page(), BASE)
                self.assertEqual(ledger.checkpoint('last_full_catalog'), 0)
                self.assertEqual(ledger.summary()['products'], {'verified': 1})
                prepare_catalog('legimi', ledger)
                self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM catalog_pages').fetchone()[0], 1)
            finally:
                ledger.close()

    def test_budget_resumes_to_confirmed_last_page(self):
        state = redux(BeautifulSoup((FIXTURES / 'legimi_filtered.html').read_text(encoding='utf-8'), 'lxml'))
        state['catalogueContainer']['response']['bookList']['pagination']['totalPages'] = 2
        def get(url):
            page = 2 if 'page=2' in url else 1
            state['catalogueContainer']['response']['bookList']['pagination']['currentPage'] = page
            state['changeMetatag']['nextUrl'] = BASE + '&page=2' if page == 1 else None
            return Response(url, ('<script type="application/json">' + json.dumps(state) + '</script>').encode())
        with tempfile.TemporaryDirectory() as folder:
            ledger = Coverage(Path(folder) / 'state.db', 'legimi', Path(folder) / 'books.db')
            try:
                first = Discovery(observer=ledger.discover)
                discover_extra_catalog('legimi', SimpleNamespace(get=get), first, ledger, {'catalog_page_budget': 1})
                self.assertFalse(first.catalog_complete)
                self.assertTrue(ledger.has_pending_pages())
                second = Discovery(observer=ledger.discover)
                discover_extra_catalog('legimi', SimpleNamespace(get=get), second, ledger, {'catalog_page_budget': 1})
                self.assertTrue(second.catalog_complete)
                self.assertFalse(ledger.has_pending_pages())
            finally:
                ledger.close()


if __name__ == '__main__':
    unittest.main()
