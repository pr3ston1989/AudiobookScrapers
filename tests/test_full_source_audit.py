"""Regression checks for metadata found in JS state and delayed browser consent."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dynamic_metadata import bookbeat_reviews
from http_client import Client, Response, blocked
from main import load_settings
from parse_books import parse, BookValidationError

FIXTURES = ROOT / 'tests/fixtures'


class HiddenMetadataTests(unittest.TestCase):
    def test_audioteka_translation_isbn_and_publication_from_server_state(self):
        url = 'https://audioteka.com/pl/audiobook/obdarowana/'
        data = parse('audioteka', (FIXTURES / 'live_audioteka_2.html').read_text(encoding='utf-8'), url)
        self.assertEqual(data['translators'], ['Dariusz Latoś'])
        self.assertEqual(data['isbn'], '9788323377931')
        self.assertEqual(data['release_year'], 2026)
        self.assertIn('published_at', data['source_metadata'])
        self.assertNotIn('?w=', data['cover_url'])

    def test_bookbeat_series_and_narration_rating(self):
        manifest = json.loads((FIXTURES / 'new_sources/manifest.json').read_text(encoding='utf-8'))
        stem, url, _ = manifest['bookbeat'][1]
        data = parse('bookbeat', (FIXTURES / f'new_sources/{stem}.html').read_text(encoding='utf-8'), url)
        self.assertEqual(data['collections'], ['Bezimienny'])
        self.assertEqual(data['volume_number'], '5')
        self.assertEqual(data['series']['id'], 87733)
        self.assertIsNotNone(data['narration_rating'])

    def test_legimi_cycle_and_audio_only_year(self):
        manifest = json.loads((FIXTURES / 'new_sources/manifest.json').read_text(encoding='utf-8'))
        for index in (1, 2):
            stem, url, _ = manifest['legimi'][index]
            data = parse('legimi', (FIXTURES / f'new_sources/{stem}.html').read_text(encoding='utf-8'), url)
            if index == 1:
                self.assertEqual(data['release_year'], 2017)
            else:
                self.assertEqual(data['collections'], ['Rozważna i Romantyczna'])

    def test_missing_legimi_state_is_not_classified_as_ebook(self):
        with self.assertRaises(BookValidationError):
            parse('legimi', '<h1>Audiobook</h1>', 'https://www.legimi.pl/ebook-test,b123.html')

    def test_woblink_genres_are_not_lost_in_breadcrumbs(self):
        manifest = json.loads((FIXTURES / 'new_sources/manifest.json').read_text(encoding='utf-8'))
        stem, url, _ = manifest['woblink'][1]
        data = parse('woblink', (FIXTURES / f'new_sources/{stem}.html').read_text(encoding='utf-8'), url)
        self.assertTrue(data['categories'])
        self.assertIn('Literatura piękna', data['categories'])
        self.assertTrue(data['source_details']['czas słuchania'])

    def test_public_review_batch_keeps_identity_and_remaining_pages(self):
        data = {'url': 'https://www.bookbeat.com/pl/book/test-123', 'reviews': []}
        value = {'_embedded': {'reviews': [{'id': 'abc', 'bookid': 123, 'reviewername': 'Reader', 'reviewtext': 'Full review', 'bookrating': 4}]},
                 '_links': {'next': {'href': 'https://api.bookbeat.com/api/reviews/0/book/123?offset=4'}}}
        client = SimpleNamespace(get=lambda url: Response(url, json.dumps(value).encode()))
        bookbeat_reviews(client, data)
        self.assertEqual(data['reviews'][0]['text'], 'Full review')
        self.assertEqual(data['reviews'][0]['source_review_id'], 'abc')
        self.assertFalse(data['review_coverage']['complete'])
        value['_embedded']['reviews'][0]['bookid'] = 456
        data = {'url': data['url'], 'reviews': []}
        bookbeat_reviews(client, data)
        self.assertEqual(data['reviews'], [])
        self.assertIn('innej książki', data['review_coverage']['error'])

    def test_polish_challenge_is_detected_without_rejecting_a_book_title(self):
        self.assertTrue(blocked('<h1>Przeprowadzanie weryfikacji zabezpieczeń</h1>'))
        self.assertFalse(blocked('<title>Cierpliwości! - audiobook</title><h1>Opis książki</h1>'))


class ConsentTests(unittest.TestCase):
    def run_consent(self, remaining, checked=False, sticky=False):
        class Buttons:
            first = property(lambda self: self)
            def wait_for(self, **kwargs):
                pass
            def count(self):
                return page.remaining
            def click(self, **kwargs):
                page.clicked += 1
                if not sticky:
                    page.remaining -= 1
        page = SimpleNamespace(remaining=remaining, clicked=0, locator=lambda selector: Buttons(), wait_for_timeout=lambda milliseconds: None)
        client = Client(load_settings(ROOT / 'config.json'))
        client.page = page
        client.cookies_checked = checked
        try:
            client.dismiss_consent()
            return page.clicked, client.cookie_status
        finally:
            client.page = None
            client.close()

    def test_two_successive_consent_steps_are_dismissed(self):
        self.assertEqual(self.run_consent(2), (2, 'accepted'))

    def test_late_dialog_is_checked_after_initial_page_load(self):
        self.assertEqual(self.run_consent(1, checked=True), (1, 'accepted'))

    def test_unclosed_consent_is_reported_as_error(self):
        with self.assertRaisesRegex(RuntimeError, 'okna zgody'):
            self.run_consent(1, sticky=True)


if __name__ == '__main__':
    unittest.main()
