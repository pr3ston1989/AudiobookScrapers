"""Compare real HTTP metadata, rendered pages and catalog continuations in isolation."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bs4 import BeautifulSoup
from main import load_settings, write_json
from http_client import Client
from parse_books import parse
from quality import assess
from sources import SOURCES
from urls import identity

OUT = ROOT / 'reports/full_source_audit'


def sample_urls():
    """Reuse previously checked URLs without depending on populated production databases."""
    old = json.loads((ROOT / 'tests/fixtures/live_manifest.json').read_text(encoding='utf-8'))
    result = {site: [item['requested_url'] for item in entries if 'data' in item][:3]
              for site, entries in old['services'].items()}
    newer = json.loads((ROOT / 'tests/fixtures/new_sources/manifest.json').read_text(encoding='utf-8'))
    result.update({site: [entry[1] for entry in entries] for site, entries in newer.items() if SOURCES[site]['active']})
    return result


def http_samples(pair):
    """Keep HTTP payloads and parsed field coverage for three different publications."""
    site, urls = pair
    client = Client(dict(load_settings(ROOT / 'config.json'), http_retries=1, timeout=20))
    records = []
    try:
        for index, url in enumerate(urls, 1):
            item = dict(url=url)
            try:
                request = 'https://wolnelektury.pl/api/books/' + identity(site, url) + '/?format=json' if site == 'wolnelektury' else url
                response = client.get(request)
                (OUT / f'{site}_{index}.http.html').write_text(response.text, encoding='utf-8')
                data = parse(site, response.text, response.url)
                write_json(OUT / f'{site}_{index}.http.json', data)
                item.update(final_url=response.url, data=data, quality=assess(site, data), size=len(response.content))
            except Exception as exc:
                item['error'] = str(exc)
            records.append(item)
            print(f'HTTP {site} {index}: {item.get("error") or item["quality"]["missing_required"]}', flush=True)
    finally:
        client.close()
    write_json(OUT / f'{site}.http_results.json', records)
    return site, records


def http_audit():
    """Run independent source clients, keeping per-source request pacing intact."""
    OUT.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(pool.map(http_samples, sample_urls().items()))
    write_json(OUT / 'http_results.json', results)


def browser_audit():
    """Compare rendered product metadata and public lazy-loaded request paths."""
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel='chromium')
        try:
            for site, urls in sample_urls().items():
                url = urls[1]
                context = browser.new_context(locale='pl-PL', viewport={'width': 1440, 'height': 1000})
                page = context.new_page()
                item = dict(url=url, requests=[])
                def capture(response):
                    if response.request.resource_type in ('xhr', 'fetch'):
                        host = response.url.split('/')[2].lower()
                        domain = ('lubimyczytac.pl' if site == 'lubimyczytac' else 'bookbeat.com' if site == 'bookbeat' else 'audioteka.com' if site == 'audioteka' else 'wolnelektury.pl' if site == 'wolnelektury' else site + '.pl' if site != 'storytel' else 'storytel.com')
                        if host == domain or host.endswith('.' + domain):
                            item['requests'].append({'url': response.url.split('?')[0], 'status': response.status})
                page.on('response', capture)
                try:
                    response = page.goto(url, wait_until='domcontentloaded', timeout=30000)
                    item['status'] = response.status if response else None
                    page.wait_for_timeout(2200)
                    for selector in ('button[data-ta="cookie-btn-accept-all"]', '#onetrust-accept-btn-handler', '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll'):
                        button = page.locator(selector)
                        if button.count() and button.first.is_visible():
                            button.first.click(timeout=2000)
                    for _ in range(6):
                        page.evaluate('window.scrollBy(0, 800)')
                        page.wait_for_timeout(400)
                    for selector in ('button[data-ta="expand-details-btn"]', 'button[data-ta="expand-description-btn"]'):
                        button = page.locator(selector)
                        if button.count() and button.first.is_visible():
                            button.first.click(timeout=2000)
                    page.wait_for_timeout(1200)
                    html = page.content()
                    (OUT / f'{site}.browser.html').write_text(html, encoding='utf-8')
                    text = page.locator('body').inner_text()
                    (OUT / f'{site}.browser.txt').write_text(text, encoding='utf-8')
                    item['title'] = page.title()
                    if site == 'wolnelektury':
                        item['note'] = 'Parser uses the public structured API rather than website HTML.'
                    else:
                        data = parse(site, html, page.url)
                        item.update(data=data, quality=assess(site, data))
                        write_json(OUT / f'{site}.browser.json', data)
                except Exception as exc:
                    item['error'] = str(exc)
                finally:
                    context.close()
                results[site] = item
                write_json(OUT / 'browser_results.json', results)
                print(f'BROWSER {site}: {item.get("error") or item.get("quality", {}).get("missing_required", [])}', flush=True)
        finally:
            browser.close()


if __name__ == '__main__':
    if '--browser' in sys.argv:
        browser_audit()
    else:
        http_audit()
