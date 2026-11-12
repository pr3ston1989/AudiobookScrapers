"""Bounded and resumable discovery for additional audiobook catalogs."""

import json
import re
import time
from collections import deque
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from events import check_stop, emit
from source_parsers import redux
from sources import SOURCES
from urls import canonical, identity, listing_url, same_site


def page_links_extra(site, html, base):
    """Extract explicit products and source-specific next-page controls."""
    soup = BeautifulSoup(html, "lxml")
    products, pages = set(), set()
    if site == "legimi":
        state = redux(soup)
        response = (state.get("catalogueContainer") or {}).get("response") or {}
        book_list = response.get("bookList") or {}
        pagination = book_list.get("pagination") or {}
        if not isinstance(book_list.get("books"), list) or not pagination:
            raise ValueError("Legimi: strona polecanych kolekcji zamiast paginowanego katalogu audiobooków")
        current, total = int(pagination.get("currentPage", 0)), int(pagination.get("totalPages", 0))
        query = dict(parse_qsl(urlsplit(base).query))
        if current < 1 or total < current or current != int(query.get("page", 1)):
            raise ValueError("Legimi: niespójny numer strony katalogu")
        books = book_list["books"]
        for book in books:
            url = urljoin(base, book.get("catalogUrl") or book.get("url") or "")
            if book.get("audiobookFormat") and identity(site, url):
                products.add(canonical(site, url))
        next_url = (state.get("changeMetatag") or {}).get("nextUrl")
        if current < total:
            page = listing_url(site, next_url or base, base)
            if not page:
                raise ValueError("Legimi: nieprawidłowy adres następnej strony")
            target = urlsplit(page)
            target_query = dict(parse_qsl(target.query))
            for field in ("filters", "sort"):
                target_query.setdefault(field, query.get(field, ""))
                if query.get(field) and target_query[field] != query[field]:
                    raise ValueError("Legimi: następna strona zmienia filtr lub kolejność katalogu")
            if not next_url:
                target_query["page"] = str(current + 1)
            if target.path != urlsplit(base).path or int(target_query.get("page", 0)) != current + 1 or "audiobooks" not in target_query.get("filters", ""):
                raise ValueError("Legimi: następna strona nie kontynuuje katalogu audiobooków")
            pages.add(urlunsplit((*target[:3], urlencode(sorted(target_query.items())), "")))
        return products, pages
    for anchor in soup.select("a[href]"):
        url = urljoin(base, anchor["href"])
        if identity(site, url):
            products.add(canonical(site, url))
        if "next" in anchor.get("rel", []) or "next" in " ".join(anchor.get("class", [])).lower():
            page = listing_url(site, url, base)
            if page and page != listing_url(site, base, base):
                pages.add(page)
    for link in soup.select('link[rel="next"][href]'):
        page = listing_url(site, link["href"], base)
        if page:
            pages.add(page)
    if site == "virtualo" and urlsplit(base).path.rstrip("/") == "/audiobooki":
        # Landing audio nie zawiera książek; wszystkie gatunki są osobnymi listami.
        for anchor in soup.select('a[href]'):
            url = urljoin(base, anchor['href'])
            if same_site(site, url) and re.fullmatch(r"/audiobooki/[^?]+-c\d+/?", urlsplit(url).path):
                parsed = urlsplit(url)
                pages.add(urlunsplit((*parsed[:3], "list-grid=1", "")))
    if site == "publio":
        active = soup.select_one(".paginate-container .active")
        current = int(active.get_text(strip=True)) if active and active.get_text(strip=True).isdigit() else 1
        if soup.select_one(".paginate-container .next:not(.disabled)"):
            raw = "\n".join(tag.get_text() for tag in soup.select("script"))
            match = re.search(r'pageUrlTemplate\s*[:=]\s*("(?:[^"\\]|\\.)*")', raw)
            if match:
                template = json.loads(match[1])
                pages.add(listing_url(site, template.replace("{pageNumber}", str(current + 1)), base))
    if site == "woblink":
        parsed = urlsplit(base)
        query = dict(parse_qsl(parsed.query))
        current = int(query.get("strona", 1))
        candidates = []
        for anchor in soup.select('a[href*="strona="]'):
            page = listing_url(site, anchor["href"], base)
            target = urlsplit(page) if page else None
            if target and target.path == parsed.path:
                number = dict(parse_qsl(target.query)).get("strona", "")
                if number.isdigit() and int(number) > current:
                    candidates.append(int(number))
        if candidates:
            query["strona"] = str(min(candidates))
            pages = {urlunsplit((*parsed[:3], urlencode(sorted(query.items())), ""))}
        else:
            pages = set()
    return products, {page for page in pages if page and same_site(site, page)}


def catalog_roots(site):
    """Use BookBeat's Polish sitemap because category ?page is ignored by that site."""
    source = SOURCES[site]
    if site == "bookbeat":
        return [source["base"] + "/sitemaps/book/pl/sitemap-index.xml"]
    return [source["base"] + source.get("catalog", source["starts"][0])]


def inspect_page(site, client, url, result):
    """Read one listing or sitemap and return only explicit crawl continuations."""
    from discovery import sitemap_entries
    response = client.get(url)
    if urlsplit(url).path.endswith((".xml", ".xml.gz")) and site in ("bookbeat", "virtualo"):
        kind, entries = sitemap_entries(response.content, response.url)
        result.maps += 1
        if kind == "sitemapindex":
            pages = {target for target, _ in entries if same_site(site, target) and (site != "bookbeat" or "/pl/" in target)}
            if not pages:
                raise ValueError(f"{site}: indeks nie wskazuje obsługiwanych map katalogu")
            return set(), pages
        products = set()
        for target, lastmod in entries:
            if identity(site, target):
                result.add(site, target, lastmod)
                products.add(canonical(site, target))
        if kind != "urlset":
            raise ValueError(f"{site}: odpowiedź nie jest mapą produktów")
        return products, set()
    products, pages = page_links_extra(site, response.text, response.url)
    audio_landing = site == "virtualo" and urlsplit(url).path.rstrip("/") == "/audiobooki" and pages
    if not products and not audio_landing:
        raise ValueError(f"Brak audiobooków na stronie katalogu: {url}")
    for product in products:
        result.add(site, product)
    result.pages += 1
    return products, pages


def prepare_catalog(site, ledger):
    """Invalidate obsolete listing adapters without losing discovered products or books."""
    version = {"virtualo": 2, "legimi": 1}.get(site)
    if ledger and version and ledger.checkpoint(f"{site}_catalog_version", 0) != version:
        # Wymieniamy tylko strony błędnego katalogu, bez naruszania produktów i książek.
        ledger.restart_catalog()
        ledger.set_checkpoint("last_full_catalog", 0)
        ledger.seed_pages(catalog_roots(site))
        ledger.set_checkpoint(f"{site}_catalog_version", version)


def discover_extra_catalog(site, client, result, ledger, settings):
    """Advance a durable bounded catalog queue without marking budget exhaustion complete."""
    prepare_catalog(site, ledger)
    if not ledger.has_pending_pages():
        ledger.restart_catalog()
        ledger.seed_pages(catalog_roots(site))
    errors = 0
    for _ in range(settings.get("catalog_page_budget", 100)):
        check_stop()
        url = ledger.pending_page(settings.get("background_retry_cutoff"))
        if url is None:
            break
        try:
            _, pages = inspect_page(site, client, url, result)
            ledger.seed_pages(sorted(pages))
            ledger.finish_page(url)
            emit("discovery", site=site, pages=result.pages, maps=result.maps, discovered=len(result.urls), url=url)
        except Exception as exc:
            ledger.fail_page(url, exc)
            result.errors.append(f"Katalog {url}: {exc}")
            errors += 1
            if errors >= 3:
                break
    result.catalog_complete = not ledger.has_pending_pages() and not errors
    if result.catalog_complete:
        ledger.set_checkpoint("last_full_catalog", time.time())
    else:
        result.warnings.append("Pełny przegląd katalogu jest w toku; zapisano punkt wznowienia")


def discover_extra(site, client, settings, known, full_catalog, observer=None, ledger=None):
    """Combine current releases with durable full discovery and the public WL API."""
    from discovery import Discovery
    result = Discovery(observer=observer)
    prepare_catalog(site, ledger)
    source = SOURCES[site]
    if site == "nexto":
        result.warnings.append("Nexto zakończyło dystrybucję audio 30.06.2026. Archiwalne karty obsługiwane przez adres książki; brak aktywnego katalogu nowości.")
        return result
    if site == "wolnelektury":
        response = client.get(source["base"] + source["starts"][0])
        values = json.loads(response.text)
        if not isinstance(values, list) or not values:
            raise ValueError("Wolne Lektury: API nie zwróciło listy audiobooków")
        for value in values:
            if value.get("has_audio") and value.get("url"):
                result.add(site, value["url"])
        result.pages = 1
        result.catalog_complete = True
        if ledger:
            ledger.set_checkpoint("last_full_catalog", time.time())
        return result
    starts = [source["base"] + path for path in source["starts"]]
    if site == "bookbeat":
        # Mapa obejmuje cały rynek PL; page=2 kategorii powtarzało pierwszą stronę.
        starts += catalog_roots(site)
    todo, visited, known_pages = deque(starts), set(), 0
    budget = min(settings.get("max_listing_pages", 2000), 20 if not full_catalog else 5)
    if site == "virtualo":
        # Nowości są domyślnie sortowane sprzedażą, a sort_id=4 zwraca HTTP 500.
        # Znane pierwsze strony nie mogą więc zakończyć wyszukiwania nowości.
        budget = min(settings.get("max_listing_pages", 2000), 100)
    if ledger:
        budget = min(budget, settings.get('news_page_budget', 5))
    while todo and len(visited) < budget:
        check_stop()
        url = todo.popleft()
        if url in visited:
            continue
        visited.add(url)
        try:
            products, pages = inspect_page(site, client, url, result)
            fresh = any(identity(site, product) not in (known or {}) for product in products)
            known_pages = 0 if fresh else known_pages + 1
            if site in ("bookbeat", "virtualo") or known_pages < 3:
                todo.extend(sorted(page for page in pages if page not in visited))
            emit("discovery", site=site, pages=result.pages, maps=result.maps, discovered=len(result.urls), url=url)
        except Exception as exc:
            result.errors.append(f"Nowości {url}: {exc}")
    if todo:
        if ledger:
            # Dalsze strony nowości trafiają do tej samej trwałej kolejki co katalog.
            ledger.seed_pages([page for page in todo if page not in visited])
        result.warnings.append("Szybki skan osiągnął budżet; pełne pokrycie wymaga kontynuacji katalogu")
    if full_catalog and ledger:
        discover_extra_catalog(site, client, result, ledger, settings)
    if not result.urls:
        result.errors.append("Nie znaleziono żadnego audiobooka; katalog nie został uznany za kompletny")
    return result
