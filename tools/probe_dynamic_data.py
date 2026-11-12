"""Inspect public consent steps and review responses in a fresh anonymous browser."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from tools.audit_all_sources import sample_urls
from main import write_json
from parse_books import parse

OUT = ROOT / 'reports/full_source_audit'


def probe():
    """Keep exact public review request parameters and visible metadata after consent."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel='chromium')
        try:
            for site in ('empik', 'bookbeat', 'storytel'):
                context = browser.new_context(locale='pl-PL', viewport={'width': 1440, 'height': 1000})
                page = context.new_page()
                record = {'consent_steps': [], 'responses': []}
                def capture(response):
                    if any(part in response.url for part in ('/api/reviews/', '/api/graphql/review-list', '/api/graphql/review-overview')):
                        try:
                            payload = response.json()
                            index = len(record['responses'])
                            write_json(OUT / f'{site}.public_reviews_{index}.json', payload)
                            record['responses'].append({'url': response.url, 'post': response.request.post_data, 'status': response.status})
                        except Exception as exc:
                            record['responses'].append({'error': str(exc)})
                page.on('response', capture)
                try:
                    response = page.goto(sample_urls()[site][1], wait_until='domcontentloaded', timeout=30000)
                    record['status'] = response.status if response else None
                    page.wait_for_timeout(2500)
                    if site == 'empik':
                        for _ in range(5):
                            dialogs = page.locator('[role="dialog"]')
                            buttons = [{'text': button.inner_text(), 'ta': button.get_attribute('data-ta')} for button in dialogs.locator('button').all() if button.is_visible()]
                            record['consent_steps'].append(buttons)
                            found = next((button for button in dialogs.locator('button').all() if button.is_visible() and re.search(r'akceptuj|zgadzam', button.inner_text(), re.I)), None)
                            if found is None:
                                break
                            found.click(timeout=3000)
                            page.wait_for_timeout(1000)
                    for _ in range(8):
                        page.evaluate('window.scrollBy(0, 850)')
                        page.wait_for_timeout(500)
                    if site == 'empik':
                        for selector in ('button[data-ta="expand-details-btn"]', 'button[data-ta="expand-description-btn"]'):
                            button = page.locator(selector)
                            if button.count() and button.first.is_visible():
                                button.first.click(timeout=2000)
                    page.wait_for_timeout(2000)
                    html = page.content()
                    (OUT / f'{site}.dynamic.html').write_text(html, encoding='utf-8')
                    (OUT / f'{site}.dynamic.txt').write_text(page.locator('body').inner_text(), encoding='utf-8')
                    write_json(OUT / f'{site}.dynamic.json', parse(site, html, page.url))
                except Exception as exc:
                    record['error'] = str(exc)
                    (OUT / f'{site}.dynamic_error.html').write_text(page.content(), encoding='utf-8')
                finally:
                    context.close()
                write_json(OUT / f'{site}.dynamic_probe.json', record)
                print(site, json.dumps(record, ensure_ascii=True)[:1800], flush=True)
        finally:
            browser.close()


if __name__ == '__main__':
    probe()
