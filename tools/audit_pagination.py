"""Exercise real first and continuation pages for every active catalog adapter."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawl_state import Coverage
from discovery import Discovery, EMPIK_CATEGORIES, SITEMAPS, catalog_response, catalog_links, page_links, sitemap_entries
from http_client import Client
from main import load_settings, write_json
from source_discovery import catalog_roots, inspect_page
from sources import SOURCES

OUT = ROOT / 'reports/full_source_audit'
STARTS = {
    'audioteka': 'https://audioteka.com/pl/cykl/nowosci/',
    'storytel': 'https://www.storytel.com/pl/categories',
    'lubimyczytac': 'https://lubimyczytac.pl/katalog/ksiazki',
    'empik': 'https://www.empik.com/audiobooki-i-ebooki/audiobooki/literatura-obyczajowa,350216,s?sort=publishDesc',
}


def inspect(pair):
    """Do not mistake landing pages, ignored page parameters or blocks for catalog ends."""
    site, start = pair
    settings = load_settings(ROOT / 'config.json')
    settings.update(browser_headless=True, browser_profile=OUT / (site + '_catalog_profile'), http_retries=1, timeout=20)
    client = Client(settings)
    record = {'start': start, 'pages': []}
    try:
        if site == 'wolnelektury':
            response = client.get('https://wolnelektury.pl/api/audiobooks/?format=json')
            values = json.loads(response.text)
            record.update(api_items=len(values), audio_items=sum(bool(value.get('has_audio')) for value in values), mechanism='public API list')
        elif site == 'bookbeat':
            response = client.get(start)
            kind, entries = sitemap_entries(response.content, response.url)
            record['pages'].append({'url': response.url, 'kind': kind, 'entries': len(entries)})
            response = client.get(entries[0][0])
            kind, entries = sitemap_entries(response.content, response.url)
            record['pages'].append({'url': response.url, 'kind': kind, 'entries': len(entries)})
        else:
            response = catalog_response(site, client, start)
            for number in range(3):
                (OUT / f'{site}.catalog_{number + 1}.html').write_text(response.text, encoding='utf-8')
                products, pages = page_links(site, response.text, response.url)
                record['pages'].append({'url': response.url, 'products': sorted(products), 'next': sorted(pages)})
                if pages:
                    target = sorted(pages)[0]
                    if site == 'virtualo' and not products:
                        target = next((url for url in pages if 'biografie-' in url), target)
                    response = catalog_response(site, client, target)
                elif number == 0 and site not in SOURCES:
                    branches = catalog_links(site, response.text, response.url)
                    if not branches:
                        break
                    response = catalog_response(site, client, sorted(branches)[0])
                else:
                    break
            record['distinct_continuation'] = any(set(record['pages'][index]['products']) - set(record['pages'][index - 1]['products']) for index in range(1, len(record['pages'])))
        if site in SITEMAPS:
            response = client.get(SITEMAPS[site][0])
            kind, entries = sitemap_entries(response.content, response.url)
            from urls import identity
            if kind == 'sitemapindex' and entries:
                response = client.get(entries[0][0])
                kind, entries = sitemap_entries(response.content, response.url)
            record['sitemap'] = {'url': response.url, 'entries': len(entries), 'recognized_products': sum(identity(site, url) is not None for url, _ in entries)}
    except Exception as exc:
        record['error'] = str(exc)
    finally:
        client.close()
    write_json(OUT / f'{site}.pagination.json', record)
    print(site, record.get('error') or ('pages=' + str(len(record['pages']))), flush=True)
    return site, record


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    starts = dict(STARTS)
    starts.update({site: catalog_roots(site)[0] for site, source in SOURCES.items() if source['active']})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(pool.map(inspect, starts.items()))
    write_json(OUT / 'pagination_results.json', results)
