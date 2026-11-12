"""Production regressions: startup cost, catalog batching, GUI inventory and access errors."""

import gc
import json
import sqlite3
import sys
import tempfile
import time
import tkinter as tk
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawl_state import Coverage
from database import Database
from discovery import Discovery, discover_sitemaps
from gui import ScraperGUI, SERVICES
from http_client import Client, HTTPError, Response
from main import load_settings, run_site
import main
from quality import assess
from source_discovery import discover_extra
from sources import create_database
from bs4 import BeautifulSoup
from virtualo_parser import parse_virtualo


class RuntimeImprovements(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = load_settings(ROOT / 'config.json')
        self.settings.update(backup=False, coverage_database=self.root / 'coverage.db')

    def db(self, site):
        path = self.root / f'{site}.db'
        if site in ('audioteka', 'storytel', 'empik', 'lubimyczytac'):
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript((ROOT / f'tests/fixtures/schema_{site}.sql').read_text(encoding='utf-8'))
        else:
            create_database(site, path)
        self.settings['databases'][site] = path
        db = Database(site, path)
        self.addCleanup(db.close)
        return db

    def args(self, **extra):
        return SimpleNamespace(**dict(command='update', url=None, resume=True, full_catalog=False,
                                      no_backup=False, refresh_limit=0, limit=0, no_covers=True, **extra))

    def test_lc_identity_query_uses_covering_index_without_url_column(self):
        db = self.db('lubimyczytac')
        with db.engine.connect() as conn:
            plan = str(conn.exec_driver_sql('EXPLAIN QUERY PLAN SELECT id,external_id FROM books').all())
        self.assertIn('COVERING INDEX', plan)
        with patch('database.identity', side_effect=AssertionError('LC does not need URL parsing')):
            restarted = Database('lubimyczytac', db.path)
            restarted.close()

    def test_backup_requires_explicit_flag_even_with_old_config_enabled(self):
        self.db('publio')
        self.settings['backup'] = True
        with patch.object(Database, 'backup') as backup:
            run_site('publio', self.settings, self.args(), {})
        backup.assert_not_called()
        with patch.object(Database, 'backup') as backup:
            run_site('publio', self.settings, self.args(backup=True), {})
        backup.assert_called_once()

    def test_single_unlimited_service_uses_same_continuation_worker(self):
        args = self.args()
        args.services = ['publio']
        args.workers = 1
        with patch.object(main, 'ROOT', self.root), patch('parallel.run_parallel', return_value=([], False)) as run:
            self.assertEqual(main.run_cycle(self.settings, args), 0)
        self.assertEqual(run.call_args.args[-1], 1)

    def test_zero_batch_budget_is_rejected_instead_of_creating_a_loop(self):
        config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
        config['sitemap_page_budget'] = 0
        path = self.root / 'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'sitemap_page_budget'):
            load_settings(path)

    def test_five_broken_woblink_cards_do_not_prevent_next_valid_book(self):
        db = self.db('woblink')
        broken = [f'https://woblink.com/audiobook/--{n}' for n in range(1, 6)]
        good = 'https://woblink.com/audiobook/book-6'
        db.enqueue([*broken, good])
        def fetch(client, site, url):
            if url in broken:
                raise HTTPError(url, 500)
            return dict(url=url, title='Book', authors=['Author'], publisher='P', description='D',
                        cover_url='https://example.test/cover.jpg', duration_minutes=60)
        with patch('main.fetch_book', side_effect=fetch):
            report = run_site('woblink', self.settings, self.args(), {})
        self.assertEqual(report['inserted'], 1)
        self.assertEqual(report['errors'], [])
        self.assertEqual(len(report['deferred_errors']), 5)
        self.assertFalse(report.get('stopped_on_errors'))

    def test_http403_stops_after_one_book_without_losing_queue(self):
        db = self.db('publio')
        db.enqueue([f'https://publio.pl/book,p{n}.html' for n in range(1, 8)])
        with patch('main.fetch_book', side_effect=HTTPError('https://publio.pl/', 403)) as fetch:
            report = run_site('publio', self.settings, self.args(), {})
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(report['stopped_on_access_limit'])
        self.assertTrue(db.pending())

    def test_broken_placeholder500_is_not_retried_three_times(self):
        client = Client(self.settings)
        self.addCleanup(client.close)
        response = SimpleNamespace(status_code=500, headers={})
        with patch.object(client.session, 'get', return_value=response) as get:
            with self.assertRaises(HTTPError):
                client.get('https://woblink.com/audiobook/--123')
        self.assertEqual(get.call_count, 1)

    def test_optional_review_timeout_is_bounded_and_no_retry(self):
        client = Client(self.settings)
        self.addCleanup(client.close)
        with patch.object(client.session, 'get', side_effect=TimeoutError) as get:
            with self.assertRaises(TimeoutError):
                client.get_optional('https://api.bookbeat.com/api/reviews/pl/book/1')
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs['timeout'], 8)

    def test_cached_access_denial_does_not_repeat_network_requests(self):
        client = Client(self.settings)
        self.addCleanup(client.close)
        client.access_denied_hosts.add('publio.pl')
        with patch.object(client.session, 'get') as get:
            with self.assertRaises(HTTPError):
                client.get('https://publio.pl/audiobooki.html')
        get.assert_not_called()

    def test_wl_without_editorial_description_can_be_complete_but_other_sources_cannot(self):
        data = dict(title='Wiersz', authors=['Autor'], cover_url='https://example.test/cover.jpg', duration_minutes=2, publisher='P')
        self.assertTrue(assess('wolnelektury', data)['complete'])
        self.assertIn('description', assess('publio', data)['missing_required'])

    def test_virtualo_empty_description_retains_available_seo_excerpt_with_provenance(self):
        soup = BeautifulSoup('<h1 itemprop="name">Book - audiobook</h1><meta itemprop="bookFormat" content="Audiobook">'
                             '<meta name="description" content="Dostępny skrót opisu..."><div class="product-description-wrapper"><div class="description"></div></div>', 'lxml')
        data = {}
        parse_virtualo(soup, data)
        self.assertEqual(data['description'], 'Dostępny skrót opisu...')
        self.assertEqual(data['description_kind'], 'seo_excerpt')

    def test_sitemap_budget_resumes_remaining_shards_without_repeating_finished_map(self):
        ledger = Coverage(self.root / 'ledger.db', 'audioteka', self.root / 'books.db')
        self.addCleanup(ledger.close)
        index = 'https://audioteka.com/pl/sitemap/audiobooks.xml'
        shard = 'https://audioteka.com/pl/sitemap/new.xml'
        product = 'https://audioteka.com/pl/audiobook/new-book/'
        bodies = {index: f'<sitemapindex><sitemap><loc>{shard}</loc></sitemap></sitemapindex>',
                  shard: f'<urlset><url><loc>{product}</loc></url></urlset>'}
        calls = []
        def get(url):
            calls.append(url)
            return Response(url, bodies.get(url, '').encode())
        client = SimpleNamespace(get=get)
        first = Discovery(observer=ledger.discover)
        discover_sitemaps('audioteka', client, first, max_maps=1, ledger=ledger)
        self.assertEqual(ledger.checkpoint('sitemaps_pending'), [shard])
        self.assertEqual(first.errors, [])
        calls.clear()
        second = Discovery(observer=ledger.discover)
        discover_sitemaps('audioteka', client, second, max_maps=1, ledger=ledger)
        self.assertNotIn(index, calls)
        self.assertEqual(ledger.checkpoint('sitemaps_pending'), [])
        self.assertEqual(len(second.urls), 1)

    def test_virtualo_news_remainder_is_saved_in_catalog_queue(self):
        ledger = Coverage(self.root / 'ledger.db', 'virtualo', self.root / 'books.db')
        self.addCleanup(ledger.close)
        url = 'https://virtualo.pl/audiobooki/nowosci/?list-grid=1'
        html = '<a href="/audiobook/test-i123/">Book</a><a rel="next" href="?list-grid=1&page=2">Next</a>'
        client = SimpleNamespace(get=lambda value: Response(value, html.encode()))
        result = discover_extra('virtualo', client, {'news_page_budget': 1}, {}, False, ledger=ledger)
        self.assertEqual(result.pages, 1)
        pending = [row[0] for row in ledger.conn.execute("SELECT url FROM catalog_pages WHERE status='pending'")]
        self.assertTrue(any('page=2' in page for page in pending))

    def test_gui_reads_all_counts_and_backup_is_opt_in(self):
        config = {'databases': {}}
        for site in SERVICES:
            db = self.db(site)
            config['databases'][site] = str(db.path)
        (self.root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        root = tk.Tk()
        root.withdraw()
        with patch('gui.ROOT', self.root):
            gui = ScraperGUI(root)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and any(gui.table.item(site, 'values')[0] == '—' for site in SERVICES):
                    root.update()
                    time.sleep(0.01)
                self.assertTrue(all(gui.table.item(site, 'values')[0] == '0' for site in SERVICES))
                self.assertIn('--no-backup', gui.command())
                gui.backup.set(True)
                self.assertIn('--backup', gui.command())
                gui.event({'event': 'progress', 'site': 'bookbeat', 'books': 12, 'done': 25, 'total': 100,
                           'inserted': 1, 'updated': 0, 'incomplete': 0, 'skipped': 24, 'url': 'https://www.bookbeat.com'})
                self.assertEqual(gui.table.item('bookbeat', 'values')[0], '12')
                self.assertIn('pominięto 24', gui.table.item('bookbeat', 'values')[4])
                gui.event({'event': 'summary', 'site': 'publio', 'errors': ['HTTP 403'], 'stopped_on_access_limit': True})
                gui.messages.put(('exit', 1))
                gui.poll()
                self.assertIn('Publio', gui.status.get())
                self.assertIn('HTTP 403', gui.table.item('publio', 'values')[4])
            finally:
                root.destroy()
                del gui
                gc.collect()


if __name__ == '__main__':
    unittest.main()
