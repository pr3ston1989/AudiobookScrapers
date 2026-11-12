"""Regression tests for original Audioteka URLs and safe thumbnail replacement."""

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database import Database, download_cover
from http_client import Response
from main import load_settings
from parse_books import parse
from parsers.audioteka import clean_at_cover
from repair_audioteka_covers import repair


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stopper = patch("events.STOP_FILE", self.root / "stop.request")
        stopper.start()
        self.addCleanup(stopper.stop)
        self.url = "https://audioteka.com/pl/audiobook/test/"
        self.small_url = "https://atkcdn.audioteka.com/test/4.jpg?w=300&auto=format"
        self.large_url = "https://atkcdn.audioteka.com/test/4.jpg"

    @staticmethod
    def image_bytes(size):
        buffer = io.BytesIO()
        Image.new("RGB", size, "red").save(buffer, "JPEG")
        return buffer.getvalue()

    def setup_database(self):
        self.path = self.root / "audioteka.db"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript((ROOT / "tests/fixtures/schema_audioteka.sql").read_text(encoding="utf-8"))
        self.local = self.root / "media/audioteka/0123456789abcdef_0123456789ab.jpg"
        self.local.parent.mkdir(parents=True)
        self.local.write_bytes(self.image_bytes((300, 300)))
        self.original_bytes = self.local.read_bytes()
        db = Database("audioteka", self.path)
        try:
            db.save({"url": self.url, "title": "Test", "cover_url": self.small_url}, {"local_path": str(self.local)})
        finally:
            db.close()
        self.settings = load_settings(ROOT / "config.json")
        self.settings["databases"]["audioteka"] = self.path
        self.settings["coverage_database"] = self.root / "coverage.db"
        self.args = SimpleNamespace(limit=0, backup=True)

    def test_cdn_variants_and_final_jsonld_override_are_normalized(self):
        for url in (self.small_url, self.large_url+"/300x300", self.large_url+"@300w.webp", self.large_url):
            self.assertEqual(clean_at_cover(url), self.large_url)
        html = '<h1 class="product-top_title">Test</h1><script type="application/ld+json">' + json.dumps({"@type": "Product", "name": "Test", "image": self.small_url}) + '</script>'
        self.assertEqual(parse("audioteka", html, self.url)["cover_url"], self.large_url)

    def test_downloader_defensively_requests_original_and_keeps_dimensions(self):
        payload = self.image_bytes((1400, 1400))
        class Client:
            def get(inner, url):
                self.assertEqual(url, self.large_url)
                return Response(url, payload)
        metadata = download_cover(Client(), self.small_url, self.root, "test")
        self.assertEqual(metadata["source_url"], self.large_url)
        self.assertEqual((metadata["width"], metadata["height"]), (1400, 1400))
        with Image.open(metadata["local_path"]) as image:
            self.assertEqual(image.size, (1400, 1400))

    def test_existing_thumbnail_is_replaced_with_backup_and_same_path(self):
        self.setup_database()
        with closing(sqlite3.connect(self.path)) as connection:
            schema = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        with patch("repair_audioteka_covers.Client.get", return_value=Response(self.large_url, self.image_bytes((1200, 1200)))):
            result = repair(self.settings, self.args, self.root)
        self.assertEqual(result["updated"], 1)
        self.assertTrue(result["complete"])
        with Image.open(self.local) as image:
            self.assertEqual(image.size, (1200, 1200))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT cover_url,cover_local_path FROM books").fetchone(), (self.large_url, str(self.local)))
            self.assertEqual(schema, connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall())
        previous = list(Path(result["backup_directory"]).glob("original_files/*/*.jpg"))
        self.assertEqual(len(previous), 1)
        self.assertEqual(previous[0].read_bytes(), self.original_bytes)
        with closing(sqlite3.connect(self.root / "state/audioteka/coverage.db")) as connection:
            self.assertEqual(json.loads(connection.execute("SELECT data FROM metadata").fetchone()[0])["cover_url"], self.large_url)
        with patch("repair_audioteka_covers.Client.get") as getter:
            again = repair(self.settings, self.args, self.root)
            getter.assert_not_called()
        self.assertEqual(again["updated"], 0)
        self.assertEqual(len(list(self.local.parent.glob("*.jpg"))), 1)

    def test_failed_download_preserves_existing_file_and_database(self):
        self.setup_database()
        with patch("repair_audioteka_covers.Client.get", side_effect=OSError("offline")):
            result = repair(self.settings, self.args, self.root)
        self.assertEqual(result["updated"], 0)
        self.assertTrue(result["errors"])
        self.assertFalse(result["complete"])
        self.assertEqual(self.local.read_bytes(), self.original_bytes)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT cover_url FROM books").fetchone()[0], self.small_url)

    def test_optional_backup_disabled_uses_only_cleaned_temporary_staging(self):
        self.setup_database()
        self.args.backup = False
        with patch('repair_audioteka_covers.Client.get', return_value=Response(self.large_url, self.image_bytes((1200, 1200)))):
            result = repair(self.settings, self.args, self.root)
        self.assertEqual(result['updated'], 1)
        self.assertIsNone(result['backup_directory'])
        self.assertFalse((self.root / 'backups').exists())
        self.assertEqual(list((self.root / 'state').glob('cover_repair_*')), [])
        with Image.open(self.local) as image:
            self.assertEqual(image.size, (1200, 1200))

    def test_original_with_same_dimensions_does_not_destroy_thumbnail(self):
        self.setup_database()
        with patch("repair_audioteka_covers.Client.get", return_value=Response(self.large_url, self.image_bytes((300, 300)))):
            result = repair(self.settings, self.args, self.root)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["original_small"], 1)
        self.assertTrue(result["complete"])
        with Image.open(self.local) as image:
            self.assertEqual(image.size, (300, 300))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT cover_url FROM books").fetchone()[0], self.large_url)

    def test_original_small_cover_is_not_downloaded_again_after_repair(self):
        self.setup_database()
        with patch("repair_audioteka_covers.Client.get", return_value=Response(self.large_url, self.image_bytes((300, 300)))):
            first = repair(self.settings, self.args, self.root)
        self.assertEqual(first["original_small"], 1)
        with patch("repair_audioteka_covers.Client.get") as getter:
            second = repair(self.settings, self.args, self.root)
            getter.assert_not_called()
        self.assertEqual(second["candidates"], 0)
        self.assertTrue(second["complete"])

    def test_interrupt_preserves_file_and_writes_resumable_report(self):
        self.setup_database()
        with patch("repair_audioteka_covers.Client.get", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                repair(self.settings, self.args, self.root)
        self.assertEqual(self.local.read_bytes(), self.original_bytes)
        report = json.loads((self.root / "reports/audioteka_cover_repair.json").read_text(encoding="utf-8"))
        self.assertTrue(report["interrupted"])
        self.assertFalse(report["complete"])
        with patch("repair_audioteka_covers.Client.get", return_value=Response(self.large_url, self.image_bytes((1200, 1200)))):
            result = repair(self.settings, self.args, self.root)
        self.assertEqual(result["updated"], 1)

    def test_failed_thumbnail_upgrade_is_not_marked_verified(self):
        from main import run_site
        self.setup_database()
        self.settings["backup"] = False
        args = SimpleNamespace(command="update", url=self.url, resume=False, full_catalog=False,
                               no_backup=True, refresh_limit=0, limit=1, no_covers=False)
        data = {"url": self.url, "title": "Test", "cover_url": self.large_url,
                "authors": ["A"], "publisher": "P", "description": "D", "duration_minutes": 60}
        with patch("main.fetch_book", return_value=data), patch("main.download_cover", return_value=None):
            report = run_site("audioteka", self.settings, args, {})
        self.assertEqual(report["incomplete"], 1)
        self.assertIn("cover_local_file", report["incomplete_samples"][0]["missing_required"])
        self.assertEqual(self.local.read_bytes(), self.original_bytes)
        db = Database("audioteka", self.path)
        try:
            self.assertTrue(db.needs_cover(self.url))
        finally:
            db.close()

    def test_existing_large_file_with_thumbnail_url_gets_original_address(self):
        self.setup_database()
        self.local.write_bytes(self.image_bytes((1200, 1200)))
        with patch("repair_audioteka_covers.Client.get", return_value=Response(self.large_url, self.image_bytes((1200, 1200)))):
            result = repair(self.settings, self.args, self.root)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["original_small"], 0)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT cover_url FROM books").fetchone()[0], self.large_url)

    def test_smaller_original_is_rejected_without_overwriting_file(self):
        self.setup_database()
        with patch("repair_audioteka_covers.Client.get", return_value=Response(self.large_url, self.image_bytes((200, 200)))):
            result = repair(self.settings, self.args, self.root)
        self.assertEqual(result["updated"], 0)
        self.assertTrue(result["errors"])
        self.assertEqual(self.local.read_bytes(), self.original_bytes)

    def test_refresh_recognizes_old_resized_cover_url(self):
        self.setup_database()
        db = Database("audioteka", self.path)
        try:
            self.assertTrue(db.needs_cover(self.url))
            db.save({"url": self.url, "title": "Test", "cover_url": self.large_url})
            self.assertTrue(db.needs_cover(self.url))
            self.local.write_bytes(self.image_bytes((1200, 1200)))
            self.assertFalse(db.needs_cover(self.url))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
