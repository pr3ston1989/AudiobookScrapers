"""Sitemap traversal and bounded catalog pagination with explicit diagnostics."""

import gzip
import html as html_module
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from events import emit

from urls import BASES, canonical, identity, listing_url, same_site
from http_client import Response

log = logging.getLogger(__name__)
SITEMAPS = {
    "audioteka": ["https://audioteka.com/pl/sitemap/audiobooks.xml"],
    "storytel": ["https://www.storytel.com/sitemaps/sitemaps_full_poland.xml"],
    "lubimyczytac": ["https://lubimyczytac.pl/_sitemap_books/_books_sitemap.xml"],
}
EMPIK_CATEGORIES = [
    ("literatura-obyczajowa", 350216), ("kryminal-sensacja-thriller", 350217),
    ("dla-dzieci", 350231), ("fantastyka-horror", 350219),
    ("literatura-piekna-polska", 350214), ("literatura-faktu-reportaz", 350218),
    ("rozwoj-osobisty", 350228), ("biografie-i-wspomnienia", 350220),
    ("dla-mlodziezy", 350232), ("lektury-pomoce-szkolne", 350230),
    ("historia", 350226), ("biznes-ekonomia", 350221), ("young-adult", 350241),
    ("nauki-spoleczne-i-humanistyczne", 350224), ("religie-i-wyznania", 350225),
    ("zdrowie-rodzina", 350227), ("poradniki", 350222), ("poezja-aforyzm-dramat", 350215),
]


@dataclass
class Discovery:
    urls: dict = field(default_factory=dict)
    maps: int = 0
    pages: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    observer: object = None
    catalog_complete: bool = False

    def add(self, site, url, lastmod=None):
        key = identity(site, url)
        if key:
            previous = self.urls.get(key)
            self.urls[key] = {"url": canonical(site, url), "lastmod": lastmod or (previous or {}).get("lastmod")}
            if self.observer:
                self.observer(canonical(site, url), lastmod)


def sitemap_allowed(site, url):
    """Ignore foreign locales without relying on a fixed sitemap filename."""
    if not same_site(site, url):
        return False
    path = urlsplit(url).path.lower()
    if site == "storytel":
        return "poland" in path or path in ("/sitemap.xml", "/sitemap_index.xml") or path.startswith("/pl/")
    if site == "audioteka":
        return not re.match(r"/(cz|sk|de|lt|es)(/|$)", path) and ("/sitemap/" not in path or "/pl/" in path or "pl.xml" in path)
    if site == "lubimyczytac":
        return "_sitemap_books" in path
    return True


def sitemap_entries(content, base):
    """Read XML namespaces, escaped entities, whitespace and gzip by magic bytes."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = ET.fromstring(content)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError(f"Nieprawidłowa mapa XML: {kind}")
    entries = []
    for child in root:
        values = {node.tag.rsplit("}", 1)[-1]: (node.text or "").strip() for node in child}
        if values.get("loc"):
            entries.append((urljoin(base, values["loc"]), values.get("lastmod")))
    return kind, entries


def discover_sitemaps(site, client, result, max_maps=1000, ledger=None):
    """Follow current indices, including newly added shards, on every invocation."""
    roots = list(SITEMAPS.get(site, []))
    try:
        robots = client.get(BASES[site] + "/robots.txt").text
        roots.extend(url.strip() for url in re.findall(r"^Sitemap:\s*(.+)$", robots, re.I | re.M) if sitemap_allowed(site, url.strip()))
    except Exception as exc:
        result.warnings.append(f"robots.txt: {exc}; korzystam ze znanych indeksów")
    pending = ledger.checkpoint('sitemaps_pending', []) if ledger else []
    todo = deque(dict.fromkeys(pending or roots))
    visited = set(ledger.checkpoint('sitemaps_seen', [])) if ledger and pending else set()
    processed = 0
    while todo:
        url = todo.popleft()
        if url in visited or not sitemap_allowed(site, url):
            continue
        if processed >= max_maps:
            todo.appendleft(url)
            if not ledger:
                result.errors.append(f"Osiągnięto limit {max_maps} sitemap; skan jest niekompletny")
            break
        visited.add(url)
        processed += 1
        try:
            response = client.get(url)
            kind, entries = sitemap_entries(response.content, response.url)
            result.maps += 1
            emit("discovery", site=site, maps=result.maps, pages=result.pages, discovered=len(result.urls), url=url)
            log.info("[%s] Mapa %s: %s wpisów", site, url, len(entries))
            if kind == "sitemapindex":
                todo.extend(u for u, _ in entries if u not in visited)
            else:
                for product, lastmod in entries:
                    result.add(site, product, lastmod)
        except Exception as exc:
            result.errors.append(f"{url}: {exc}")
        if ledger:
            ledger.set_checkpoint('sitemaps_pending', list(todo))
            ledger.set_checkpoint('sitemaps_seen', sorted(visited))
    if ledger:
        ledger.set_checkpoint('sitemaps_pending', list(todo))
        ledger.set_checkpoint('sitemaps_seen', sorted(visited) if todo else [])
        if todo:
            result.warnings.append('Mapy katalogu w toku; zapisano punkt wznowienia')


def page_links(site, html, base):
    """Extract products and explicit pagination without crawling recommendations."""
    from sources import SOURCES
    if site in SOURCES:
        from source_discovery import page_links_extra
        return page_links_extra(site, html, base)
    soup = BeautifulSoup(html, "lxml")
    products, pages = set(), set()
    product_scope = soup.select_one(".search-content") if site == "empik" else None
    for anchor in soup.select("a[href]"):
        url = urljoin(base, anchor["href"])
        if identity(site, url):
            if product_scope is None or product_scope in anchor.parents:
                products.add(canonical(site, url))
        else:
            classes = " ".join(anchor.get("class", []))
            pagination = ("next" in anchor.get("rel", []) or
                          "next" in classes.lower() or
                          anchor.get("aria-label", "").lower() in ("następna", "następna strona", "next", "next page") or
                          anchor.get_text(strip=True).lower() in ("następna", "następna strona", "dalej", "next"))
            if pagination:
                page = listing_url(site, anchor["href"], base)
                if page:
                    pages.add(page)
    for link in soup.select('link[rel="next"][href]'):
        page = listing_url(site, link["href"], base)
        if page:
            pages.add(page)
    if site == "lubimyczytac":
        # LC ma href="#"; właściwy adres i filtry są w konfiguracji paginatora JS.
        scripts = "\n".join(tag.get_text() for tag in soup.select("script"))
        action = re.search(r"\.action\s*=\s*['\"]([^'\"]+)['\"]", scripts)
        filters = re.search(r"\.params\s*=\s*['\"]([^'\"]*)['\"]", scripts)
        list_id = re.search(r"\.listId\s*=\s*['\"]([^'\"]+)['\"]", scripts)
        if action and action[1] == "/book/standardFilteredDataPageContent" and list_id:
            for button in soup.select('.next-page:not(.disabled) a[data-page], a[aria-label="Next"][data-page]'):
                page = button.get("data-page", "")
                if page.isdigit():
                    query = parse_qsl(filters[1] if filters else "") + [("page", page), ("listId", list_id[1]), ("paginatorType", "Standard")]
                    pages.add(BASES[site] + action[1] + "?" + urlencode(query))
            pages.discard(listing_url(site, "#", base))
    return products, {listing_url(site, page, base) for page in pages}


def catalog_response(site, client, url):
    """Resolve AJAX pagination as HTML while retaining a durable URL checkpoint."""
    parsed = urlsplit(url)
    if site != "lubimyczytac" or parsed.path != "/book/standardFilteredDataPageContent":
        return client.get(url, **({"render": True} if site == "empik" else {}))
    pairs = parse_qsl(parsed.query)
    response = client.post_catalog(BASES[site] + parsed.path, pairs)
    payload = json.loads(response.text)
    data = payload.get("data") or {}
    content = data.get("content")
    if not isinstance(content, str):
        raise ValueError("LC: brak HTML katalogu w odpowiedzi paginacji")
    current_page = int(dict(pairs).get("page", 1))
    soup = BeautifulSoup(content, "lxml")
    next_button = soup.select_one('.next-page:not(.disabled) a[data-page]')
    has_next = data.get("left", 0) > 0 or (next_button and next_button.get("data-page", "").isdigit() and int(next_button["data-page"]) > current_page)
    if has_next:
        next_pairs = [(k, v) for k, v in pairs if k != "page"] + [("page", str(current_page + 1))]
        next_url = BASES[site] + parsed.path + "?" + urlencode(next_pairs)
        content += '<a rel="next" href="' + html_module.escape(next_url, quote=True) + '">Next</a>'
    return Response(url, content.encode("utf-8"))


def catalog_links(site, html, base):
    """Discover only catalog branches, avoiding social pages and recommendation recursion."""
    soup = BeautifulSoup(html, "lxml")
    patterns = {
        "audioteka": r"^/pl/(katalog|cykl)/",
        "storytel": r"^/pl/(categories|toplists)/",
        "lubimyczytac": r"^/(?:kategoria/[\w-]+|ksiazki/k/\d+/[\w-]+|katalog/ksiazki)/?$",
        "empik": r"^/audiobooki-i-ebooki/audiobooki/",
    }
    links = set()
    for anchor in soup.select("a[href]"):
        url = listing_url(site, anchor["href"], base)
        if url and re.search(patterns[site], urlsplit(url).path):
            if site == "empik":
                path = urlsplit(url).path
                if not re.fullmatch(r"/audiobooki-i-ebooki/audiobooki/[^/]+,\d+,s", path):
                    continue
                url = BASES[site] + path + "?sort=publishDesc"
            links.add(url)
    return links


def discover_catalog(site, client, result, ledger, settings):
    """Persist a bounded catalog walk and resume unfinished pages on the next cycle."""
    from sources import SOURCES
    if site in SOURCES:
        from source_discovery import discover_extra_catalog
        return discover_extra_catalog(site, client, result, ledger, settings)
    roots = {
        "audioteka": [BASES[site] + "/pl/katalog/"],
        "storytel": [BASES[site] + "/pl/categories"],
        "lubimyczytac": [BASES[site] + "/katalog", BASES[site] + "/katalog/ksiazki"],
        "empik": [BASES[site] + f"/audiobooki-i-ebooki/audiobooki/{slug},{number},s?sort=publishDesc" for slug, number in EMPIK_CATEGORIES],
    }[site]
    if not ledger.has_pending_pages():
        ledger.restart_catalog()
        ledger.seed_pages(roots)
    errors = 0
    for _ in range(settings.get("catalog_page_budget", 100)):
        url = ledger.pending_page(settings.get("background_retry_cutoff"))
        if url is None:
            break
        if site == "lubimyczytac" and not re.fullmatch(r"/(?:katalog(?:/ksiazki)?|kategoria/[\w-]+|ksiazki/k/\d+/[\w-]+|book/standardFilteredDataPageContent)/?", urlsplit(url).path):
            # Usuwamy ze wznowienia dawne linki opinii i uszkodzone adresy kategorii.
            ledger.finish_page(url)
            result.warnings.append(f"Pominięto adres spoza katalogu: {url}")
            continue
        try:
            response = catalog_response(site, client, url)
            products, pages = page_links(site, response.text, response.url)
            branches = catalog_links(site, response.text, response.url)
            if not products and not branches:
                raise ValueError("Katalog nie zawiera książek ani podkategorii")
            for product in products:
                result.add(site, product)
            if site == "empik":
                normalized = set()
                for page in pages:
                    parsed = urlsplit(page)
                    query = dict(parse_qsl(parsed.query))
                    query["sort"] = "publishDesc"
                    normalized.add(urlunsplit((*parsed[:3], urlencode(query), "")))
                pages = normalized
            ledger.seed_pages(sorted(pages | branches))
            ledger.finish_page(url)
            result.pages += 1
            emit("discovery", site=site, maps=result.maps, pages=result.pages, discovered=len(result.urls), url=url)
        except Exception as exc:
            result.errors.append(f"Pełny katalog {url}: {exc}")
            errors += 1
            ledger.fail_page(url, exc)
            # Pozostałe gałęzie katalogu działają niezależnie od uszkodzonej strony.
            if errors >= 3:
                break
    result.catalog_complete = not ledger.has_pending_pages() and not errors
    if result.catalog_complete:
        ledger.set_checkpoint("last_full_catalog", time.time())
    else:
        result.warnings.append("Pełny przegląd katalogu jest w toku; zapisano punkt wznowienia")


def discover_listings(site, client, result, starts, max_pages, known=None, full_catalog=False, ledger=None):
    """Visit all explicit next pages; fail loudly on loops, blocks and empty catalogs."""
    todo = deque(starts)
    visited = set()
    consecutive_errors = 0
    known_pages = {}
    while todo:
        url = todo.popleft()
        if url in visited:
            continue
        if len(visited) >= max_pages:
            if ledger:
                ledger.seed_pages([url, *todo])
                result.warnings.append('Dalsze strony list zapisano do kontynuacji katalogu')
            else:
                result.errors.append(f"Limit {max_pages} stron katalogu; skan jest niekompletny")
            break
        visited.add(url)
        try:
            response = client.get(url, browser_fallback=site == "empik", **({"render": True} if site == "empik" else {}))
            products, pages = page_links(site, response.text, response.url)
            result.pages += 1
            emit("discovery", site=site, maps=result.maps, pages=result.pages, discovered=len(result.urls), url=url)
            if not products:
                raise ValueError("Brak linków książek na liście; możliwa zmiana HTML lub blokada")
            for product in products:
                result.add(site, product)
            if site == "empik":
                # Sortowanie od najnowszych pozwala zakończyć codzienny skan po 3
                # stronach wyłącznie znanych produktów; pełny skan pozostaje dostępny.
                category = re.sub(r",s(?:,\d+)?$", ",s", urlsplit(url).path.strip())
                fresh = any(identity(site, product) not in (known or {}) for product in products)
                known_pages[category] = 0 if fresh else known_pages.get(category, 0) + 1
                if not full_catalog and known_pages[category] >= 3:
                    pages = set()
                normalized_pages = set()
                for page in pages:
                    parsed = urlsplit(page)
                    query = dict(parse_qsl(parsed.query))
                    query["sort"] = "publishDesc"
                    normalized_pages.add(urlunsplit((*parsed[:3], urlencode(query), "")))
                pages = normalized_pages
            todo.extend(p for p in sorted(pages) if p not in visited)
            log.info("[%s] Lista %s: %s książek", site, url, len(products))
            consecutive_errors = 0
        except Exception as exc:
            result.errors.append(f"{url}: {exc}")
            consecutive_errors += 1
            if consecutive_errors >= 3:
                result.errors.append("Przerwano skan list po 3 kolejnych błędach")
                break


def discover(site, client, settings, known=None, full_catalog=False, observer=None, ledger=None):
    from sources import SOURCES
    if site in SOURCES:
        from source_discovery import discover_extra
        return discover_extra(site, client, settings, known, full_catalog, observer, ledger)
    result = Discovery(observer=observer)
    if site != "empik":
        discover_sitemaps(site, client, result, max_maps=settings.get('sitemap_page_budget', 2) if ledger else 1000, ledger=ledger)
    starts = {
        "audioteka": [BASES[site] + "/pl/cykl/nowosci/"],
        "storytel": [BASES[site] + "/pl"],
        "lubimyczytac": [BASES[site] + "/ksiazki/nowosci"],
        "empik": [BASES[site] + f"/audiobooki-i-ebooki/audiobooki/{slug},{number},s?sort=publishDesc" for slug, number in EMPIK_CATEGORIES],
    }[site]
    if not (site == "empik" and full_catalog and ledger):
        discover_listings(site, client, result, starts, min(settings["max_listing_pages"], settings.get('news_page_budget', 5)) if ledger else settings['max_listing_pages'], known, full_catalog, ledger)
    if full_catalog and ledger:
        discover_catalog(site, client, result, ledger, settings)
    if not result.urls:
        result.errors.append("Nie znaleziono żadnego linku książki; nie uznaję aktualizacji za udaną")
    return result
