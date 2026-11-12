"""Checkpoint corruption, durable writes, independent workers and folder relocation."""

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
import main
from state_files import read_state, write_json
from tools.recover_moved_project import recover


class StateFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_zero_filled_checkpoint_recovers_parent_and_archives_original(self):
        path = self.root / 'run.json'
        path.write_bytes(bytes(185))
        fallback = {'audioteka': {'refresh_cursor': 250}}
        self.assertEqual(read_state(path, fallback), fallback)
        self.assertEqual(read_state(path), fallback)
        self.assertEqual(next(self.root.glob('*.corrupt.*')).read_bytes(), bytes(185))

    def test_last_good_backup_recovers_truncated_checkpoint(self):
        path = self.root / 'state.json'
        write_json(path, {'audioteka': {'refresh_cursor': 100}})
        write_json(path, {'audioteka': {'refresh_cursor': 150}})
        path.write_text('{', encoding='utf-8')
        self.assertEqual(read_state(path)['audioteka']['refresh_cursor'], 100)

    def test_invalid_schema_and_utf8_recover_without_mutating_fallback(self):
        for raw in (b'[]', b'{"audioteka": null}', b'\xff'):
            path = self.root / 'run.json'
            path.write_bytes(raw)
            fallback = {'audioteka': {'refresh_cursor': 50}}
            state = read_state(path, fallback)
            state['audioteka']['refresh_cursor'] = 60
            self.assertEqual(fallback['audioteka']['refresh_cursor'], 50)

    def test_failed_replace_preserves_original_and_cleans_temporary_file(self):
        path = self.root / 'report.json'
        write_json(path, {'previous': True})
        with patch('state_files.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                write_json(path, {'next': True})
        self.assertEqual(json.loads(path.read_text()), {'previous': True})
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_corrupt_site_does_not_prevent_four_worker_launch(self):
        state = {'audioteka': {'refresh_cursor': 250}, 'storytel': {'refresh_cursor': 100}}
        write_json(self.root / 'state.json', state)
        broken = self.root / 'state/audioteka/run.json'
        broken.parent.mkdir(parents=True)
        broken.write_bytes(bytes(185))
        sites = ['audioteka', 'empik', 'storytel', 'lubimyczytac']
        settings = {'databases': dict.fromkeys(sites), 'parallel_services': 4}
        args = SimpleNamespace(command='update', services=sites, workers=4)
        with patch.object(main, 'ROOT', self.root), patch('parallel.run_parallel', return_value=([], False)) as run:
            self.assertEqual(main.run_cycle(settings, args), 0)
        self.assertEqual(run.call_args.args[2]['audioteka']['refresh_cursor'], 250)
        self.assertEqual(run.call_args.args[1].services, sites)

    def test_relocation_keeps_metadata_and_rebases_existing_cover(self):
        old = self.root / 'missing_old_root'
        (self.root / 'data').mkdir()
        write_json(self.root / 'config.json', {'databases': {'audioteka': 'data/audioteka.db'},
            'requests_per_second': 1, 'http_retries': 1, 'timeout': 1, 'max_listing_pages': 1, 'refresh_limit': 0})
        cover = self.root / 'media/audioteka/test.jpg'
        cover.parent.mkdir(parents=True)
        cover.write_bytes(b'original-cover')
        with closing(sqlite3.connect(self.root / 'data/audioteka.db')) as conn, conn:
            conn.executescript('CREATE TABLE books(id INTEGER PRIMARY KEY, cover_local_path TEXT);')
            conn.execute('INSERT INTO books VALUES (1,?)', (str(old / 'media/audioteka/test.jpg'),))
        directory = self.root / 'state/audioteka'
        directory.mkdir(parents=True)
        with closing(sqlite3.connect(directory / 'coverage.db')) as conn, conn:
            conn.executescript('CREATE TABLE sources(site TEXT, database_path TEXT); CREATE TABLE products(id INTEGER); INSERT INTO products VALUES (42);')
            conn.execute('INSERT INTO sources VALUES (?,?)', ('audioteka', str(old / 'data/audioteka.db')))
        write_json(self.root / 'state.json', {'audioteka': {'database': str(old / 'data/audioteka.db'), 'refresh_cursor': 250}})
        (directory / 'run.json').write_bytes(bytes(185))
        result = recover(self.root, old, self.root / 'report')
        self.assertEqual(result['audioteka']['covers'], 1)
        self.assertEqual(read_state(directory / 'run.json')['audioteka']['refresh_cursor'], 250)
        with closing(sqlite3.connect(directory / 'coverage.db')) as conn:
            self.assertEqual(conn.execute('SELECT id FROM products').fetchone()[0], 42)
        with closing(sqlite3.connect(self.root / 'data/audioteka.db')) as conn:
            self.assertEqual(conn.execute('SELECT cover_local_path FROM books').fetchone()[0], str(cover))
        self.assertEqual(recover(self.root, old, self.root / 'report')['audioteka']['covers'], 0)


if __name__ == '__main__':
    unittest.main()
