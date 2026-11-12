"""Production regressions: Publio's unpadded clocks and interrupted GUI summaries."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawl_state import Coverage
from database import Database
from gui import ScraperGUI
from parse_books import duration, parse
from sources import create_database
from tools.repair_publio_duration import repair


class PublioRegressionTests(unittest.TestCase):
    def test_actual_unpadded_duration_variants_and_invalid_values(self):
        for value, expected in [('8:6:18', 487), ('15:0:34', 901), ('12:17:3', 738),
                                ('0:6:59', 7), ('10:25:51', 626), ('0:0:1', 1),
                                ('8:60:00', None), ('8:06:60', None)]:
            with self.subTest(value=value):
                self.assertEqual(duration(value), expected)

    def test_invalid_detail_cannot_erase_valid_format_duration(self):
        url = 'https://publio.pl/test,p123.html'
        html = '''<title>Test - audiobook</title><h1>Test</h1>
            <div class="details-element"><div class="detail-title">Format</div>
            <div class="detail-content">MP3 (długość: 8 godz. 7 min.)</div></div>
            <div class="details-element"><div class="detail-title">Czas nagrania</div>
            <div class="detail-content">brak danych</div></div>'''
        self.assertEqual(parse('publio', html, url)['duration_minutes'], 487)

    def test_offline_repair_is_idempotent_and_keeps_other_gaps_and_covers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'data').mkdir()
            path = root / 'data/publio.db'
            create_database('publio', path)
            cover = root / 'cover.jpg'
            cover.write_bytes(b'existing-cover')
            db = Database('publio', path)
            ledger = Coverage(root / 'state/publio/coverage.db', 'publio', path)
            for key, description in [(1, 'Opis'), (2, None)]:
                data = {'url': f'https://publio.pl/test,p{key}.html', 'title': 'Test', 'authors': ['Autor'],
                        'publisher': 'Wydawca', 'cover_url': 'https://publio.pl/cover.jpg',
                        'length_str': '8:6:18', 'description': description, 'type': 'audiobook'}
                db.save(data, {'local_path': str(cover), 'source_url': data['cover_url']})
                db.enqueue([data['url']], refresh=True)
                ledger.save_metadata(data)
                ledger.mark(data['url'], 'incomplete', {'missing_required': ['duration_minutes']})
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("UPDATE scrape_queue SET status='incomplete'")
            ledger.close()
            db.close()
            self.assertEqual(repair(root)['repairable'], 2)
            result = repair(root, apply=True)
            self.assertEqual((result['applied'], result['completed']), (2, 1))
            self.assertTrue((Path(result['backup']) / 'publio.db').is_file())
            self.assertEqual(repair(root, apply=True)['applied'], 0)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT duration_minutes FROM books').fetchall(), [(487,), (487,)])
                self.assertEqual(dict(conn.execute('SELECT status,count(*) FROM scrape_queue GROUP BY status')), {'completed': 1, 'incomplete': 1})
                self.assertEqual(conn.execute('SELECT count(*) FROM books').fetchone()[0], 2)
            with closing(sqlite3.connect(root / 'state/publio/coverage.db')) as conn:
                details = json.loads(conn.execute("SELECT details FROM products WHERE product_key='2'").fetchone()[0])
                self.assertEqual(details['missing_required'], ['description'])
            self.assertEqual(cover.read_bytes(), b'existing-cover')


class GuiSummaryTests(unittest.TestCase):
    def status(self, event):
        values = ['—', 0, 0, 0, '']
        def item(site, option=None, **kwargs):
            if kwargs:
                values[:] = kwargs['values']
            return values
        gui = SimpleNamespace(table=SimpleNamespace(item=item))
        ScraperGUI.event(gui, dict(event, event='summary', site='publio'))
        return values[4]

    def test_interrupted_with_errors_is_resumable(self):
        self.assertIn('można wznowić', self.status({'interrupted': True, 'errors': ['Błąd karty']}))

    def test_fatal_summary_displays_actual_reason(self):
        self.assertEqual(self.status({'errors': ['database is locked']}), 'Błąd: database is locked')


if __name__ == '__main__':
    unittest.main()
