"""Legacy parsers with schema.org and Apollo metadata fallbacks."""

import json
import math
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parsers.audioteka import parse_audioteka_page, clean_at_cover
from parsers.empik import EmpikParser
from parsers.lc import extract_book_info, extract_reviews
from parsers.storytel import parse_storytel_page
from urls import identity, same_site
from recommendation_parsers import related_signals


TAG_SELECTORS = {
    "woblink": '#product-card-tags a[href*="/tag/"]',
    "empik": 'span[class*="productTag"]',
    "lubimyczytac": 'div.tags a.tag[href*="/ksiazki/t/"]',
    "storytel": 'ul[class*="_tagList"] a[href*="/pl/tags/"]',
}
BOOK_TITLE_SELECTORS = {
    "woblink": "h1#book-title",
    "empik": 'h1[data-ta="title"]',
    "lubimyczytac": "h1.book__title",
    "storytel": 'h1[class*="_styledTitle"]',
}


class BookValidationError(ValueError):
    """Reject an individual mismatched product without treating it as a network outage."""


class NotAudiobook(ValueError):
    pass


def names(value):
    """Normalize schema.org named objects, lists and strings."""
    items = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(n.strip() for item in items
                             if isinstance(n := item.get("name") if isinstance(item, dict) else item, str) and n.strip()))


def duration(value):
    """Parse ISO 8601, Polish duration strings and hh:mm:ss."""
    if not value:
        return None
    value = str(value)
    iso = re.fullmatch(r"P(?:(\d+(?:\.\d+)?)D)?T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", value, re.I)
    if iso:
        d, h, m, s = (float(v or 0) for v in iso.groups())
        return math.ceil(d * 1440 + h * 60 + m + s / 60)
    clock = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", value.strip())
    if clock:
        h, m, s = (int(v or 0) for v in clock.groups())
        return math.ceil(h * 60 + m + s / 60) if m < 60 and s < 60 else None
    h = re.search(r"(\d+)\s*(?:godz|hour|hr|h\b)", value, re.I)
    m = re.search(r"(\d+)\s*(?:min|m\b)", value, re.I)
    return int(h[1] if h else 0) * 60 + int(m[1] if m else 0) if h or m else None


def jsonld(soup):
    """Yield top-level schema objects and @graph without traversing recommendations."""
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.get_text())
        except (ValueError, TypeError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict):
                yield from obj.get("@graph", [obj])


def structured_product(soup, site, url):
    """Prefer a verified product identity and reject ambiguous unbound schema objects."""
    key = identity(site, url)
    matched, unbound = [], []
    for obj in jsonld(soup):
        if not isinstance(obj, dict):
            continue
        types = obj.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if not set(types) & {"Book", "Audiobook", "Product"}:
            continue
        target = obj.get("url")
        if isinstance(target, dict):
            target = target.get("@id")
        if not target and isinstance(obj.get("@id"), str):
            value = obj["@id"]
            if value.startswith(("http://", "https://", "/", "#")):
                target = urljoin(url, value)
            elif site == "bookbeat" and value.isdigit():
                if value == key:
                    matched.append(obj)
                continue
        if isinstance(target, str):
            if identity(site, urljoin(url, target)) == key:
                matched.append(obj)
        else:
            unbound.append(obj)
    if matched:
        return matched[0]
    if len(unbound) == 1:
        heading = soup.select_one("h1")
        if heading and isinstance(unbound[0].get("name"), str):
            import unicodedata
            def title_key(value):
                value = re.sub(r"\s*[-–—]\s*(?:audiobook|ebook)\s*$", "", value, flags=re.I)
                return unicodedata.normalize("NFC", " ".join(value.split())).casefold()
            direct = " ".join(part.strip() for part in heading.find_all(string=True, recursive=False) if part.strip())
            if title_key(unbound[0]["name"]) in {title_key(direct), title_key(heading.get_text(" ", strip=True))}:
                return unbound[0]
    return {}


def apply_structured(data, soup, site):
    for obj in [structured_product(soup, site, data["url"])]:
        types = obj.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if not set(types) & {"Book", "Audiobook", "Product"}:
            continue
        if obj.get("name"):
            data["title"] = obj["name"]
        examples = obj.get("workExample", [])
        examples = examples if isinstance(examples, list) else [examples]
        audio = next((e for e in examples if isinstance(e, dict) and "Audiobook" in e.get("@type", "")), {})
        if audio.get("name") and obj.get("name"):
            from product_validation import title_key
            if title_key(audio["name"]) != title_key(obj["name"]):
                raise BookValidationError("Wydanie audio w JSON-LD dotyczy innego tytułu")
        details = {**obj, **audio}
        for field, schema_field in (("authors", "author"), ("narrators", "readBy"), ("publisher", "publisher")):
            extracted = names(details.get(schema_field))
            if not extracted and field == "publisher" and site == "audioteka":
                extracted = names(details.get("brand"))
            if extracted:
                data[field] = [{"name": n} for n in extracted] if site == "lubimyczytac" and field == "authors" else extracted
                if site == "lubimyczytac" and field == "publisher":
                    data[field] = {"name": extracted[0]}
        for field, schema_field in (("description", "description"), ("language", "inLanguage"), ("isbn", "isbn"), ("release_date", "datePublished")):
            value = details.get(schema_field)
            if value:
                parsed = BeautifulSoup(str(value), "lxml").get_text("\n", strip=True) if field == "description" else value
                # Krótszy opis SEO nie zastępuje pełnego opisu ze strony produktu.
                if field != "description" or len(str(parsed)) > len(str(data.get(field) or "")):
                    data[field] = parsed
        image = details.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        if isinstance(image, str):
            data["cover_url"] = image
        minutes = duration(details.get("duration"))
        if minutes is not None:
            data["duration_minutes"] = minutes
            data["length_str"] = details["duration"]
        if site != "lubimyczytac" and (genres := names(details.get("genre"))):
            data["category"] = data.get("category") or genres[0]
            data["categories"] = names(names(data.get("categories")) + genres)
        keywords = details.get("keywords")
        if keywords:
            labels = keywords.split(",") if isinstance(keywords, str) else keywords
            data["tags"] = names(names(data.get("tags")) + names(labels))
        rating = details.get("aggregateRating") or {}
        for field, schema_field, convert in (("avg_rating", "ratingValue", float), ("rating_count", "ratingCount", int)):
            if rating.get(schema_field) is not None:
                try:
                    data[field] = convert(str(rating[schema_field]).replace(",", "."))
                except (TypeError, ValueError):
                    pass
        if site == "storytel":
            data["type"] = "audiobook" if audio or "Audiobook" in types else "ebook"
        reviews = details.get("review", [])
        for rev in reviews if isinstance(reviews, list) else []:
            if isinstance(rev, dict) and rev.get("reviewBody"):
                data.setdefault("reviews", []).append({
                    "username": (names(rev.get("author")) or ["Anonim"])[0],
                    "text": rev["reviewBody"], "date": rev.get("datePublished"),
                    "rating": (rev.get("reviewRating") or {}).get("ratingValue"),
                })
        break


def empik_apollo(soup, url, data):
    """Read the matching SSR Apollo product without evaluating JavaScript."""
    for script in soup.find_all("script"):
        raw = script.get_text()
        match = re.search(r"window\.__APOLLO_STATE__\s*=\s*", raw)
        if not match:
            continue
        try:
            state, _ = json.JSONDecoder().raw_decode(raw[match.end():])
        except ValueError:
            continue
        product = next((obj for obj in state.values() if isinstance(obj, dict) and obj.get("__typename") == "Product"
                        and str(obj.get("id")) == "p" + identity("empik", url)), None)
        if not product:
            continue
        base = product.get("baseInformation") or {}
        subtype = base.get("subtype")
        if subtype and subtype.upper() != "MP3":
            raise NotAudiobook(f"Empik: produkt {subtype} zamiast audiobooka")
        creator = base.get("creatorAttributes") or {}
        data["authors"] = creator.get("author") or data["authors"]
        data["publisher"] = (creator.get("publisher") or [data.get("publisher")])[0]
        rating = base.get("rating") or {}
        data["avg_rating"] = rating.get("score", data.get("avg_rating"))
        data["rating_count"] = rating.get("count", data.get("rating_count"))
        data["cover_url"] = (base.get("cover") or {}).get("large") or data.get("cover_url")
        data["description"] = (product.get("descriptionData") or {}).get("cleanDescription") or data.get("description")
        data["format"] = (product.get("detailsInformation") or {}).get("digitalFormat") or "mp3"
        return subtype == "MP3"
    return False


def validated_book_url(soup, site, url):
    canonical_link = soup.select_one('link[rel="canonical"][href]')
    if canonical_link:
        candidate = urljoin(url, canonical_link["href"])
        if not same_site(site, candidate) or identity(site, candidate) != identity(site, url):
            og = soup.select_one('meta[property="og:url"][content]')
            matching_book = next((obj for obj in jsonld(soup) if isinstance(obj, dict) and obj.get("@type") in ("Book", "Audiobook")
                                  and identity(site, obj.get("url", "")) == identity(site, url)), None)
            # LC wskazuje w canonical inne wydanie; dwa źródła muszą potwierdzać żądane ID.
            if site != "lubimyczytac" or not og or identity(site, og["content"]) != identity(site, url) or not matching_book:
                raise BookValidationError(f"Strona nie odpowiada żądanej książce: {url}")
            candidate = urljoin(url, matching_book["url"])
        url = candidate
    if not identity(site, url):
        raise ValueError(f"Nieprawidłowy adres książki: {url}")
    return url


def book_tags(soup, site):
    return list(dict.fromkeys(
        label for link in soup.select(TAG_SELECTORS[site])
        if (label := " ".join(link.stripped_strings)) and label not in ("...", "…")
    ))


def parse_tags(site, html, url):
    """Validate a title card before treating an empty tag list as checked."""
    soup = BeautifulSoup(html, "lxml")
    url = validated_book_url(soup, site, url)
    title = soup.select_one(BOOK_TITLE_SELECTORS[site])
    if not title or not title.get_text(strip=True):
        raise BookValidationError(f"Brak nagłówka karty książki: {url}")
    return book_tags(soup, site)


def parse(site, html, url):
    """Combine proven legacy selectors with current structured metadata."""
    from sources import SOURCES
    if site in SOURCES:
        from source_parsers import parse_extra
        return parse_extra(site, html, url)
    soup = BeautifulSoup(html, "lxml")
    url = validated_book_url(soup, site, url)
    if site == "lubimyczytac":
        data = extract_book_info(html, url)
        if data is None:
            raise ValueError(f"Nie udało się sparsować książki: {url}")
        data["reviews"] = extract_reviews(html)
    elif site == "audioteka":
        data = parse_audioteka_page(html, url)["data"]
    elif site == "storytel":
        data = parse_storytel_page(html, url)["data"]
    else:
        data = EmpikParser(html, url).parse()
        is_audio = empik_apollo(soup, url, data)
        title = soup.select_one('h1[data-ta="title"]')
        if not is_audio and not (title and "audiobook" in title.get_text().lower()):
            raise NotAudiobook(f"Brak potwierdzenia formatu audiobooka: {url}")
        data["type"] = "audiobook"
    if site in TAG_SELECTORS:
        data["tags"] = book_tags(soup, site)
    apply_structured(data, soup, site)
    data.update(related_signals(site, soup, url))
    source_details = {}
    for label, value in zip(soup.select('dl dt'), soup.select('dl dd')):
        label_text = label.get_text(' ', strip=True).rstrip(':')
        value_text = value.get_text(' ', strip=True)
        if label_text and value_text:
            source_details[label_text] = value_text
        if 'tłumacz' in label_text.lower() and value_text:
            data['translators'] = names([a.get_text(' ', strip=True) for a in value.select('a')] or value_text)
            data['translator'] = ', '.join(data['translators'])
    if source_details:
        data['source_details'] = source_details
    if site == 'audioteka':
        from source_parsers import json_roots, objects
        slug = url.rstrip('/').rsplit('/', 1)[-1]
        product = next((obj for root in json_roots(soup) for obj in objects(root)
                        if obj.get('slug') == slug and obj.get('reference_id') and 'tracks_duration_in_ms' in obj), {})
        if product:
            if product.get('isbn'):
                data['isbn'] = product['isbn']
            if product.get('published_at'):
                data['release_date'] = product['published_at']
                data['release_year'] = int(product['published_at'][:4])
            if product.get('external_published_at'):
                data['premiere_date'] = product['external_published_at']
            if product.get('tracks_duration_in_ms'):
                data['duration_seconds'] = product['tracks_duration_in_ms'] / 1000
            data['source_metadata'] = {field: product.get(field) for field in
                ('id', 'reference_id', 'created_at', 'changed_at', 'published_at', 'external_published_at', 'main_category_id', 'sample_url', 'ai_review')
                if product.get(field) is not None}
    if not data.get("title"):
        raise ValueError(f"Brak tytułu książki — możliwa zmiana HTML: {url}")
    if data.get("cover_url"):
        data["cover_url"] = urljoin(url, data["cover_url"])
        if site == "audioteka":
            # JSON-LD również zawiera miniaturkę; czyszczenie musi być ostatnim krokiem.
            data["cover_url"] = clean_at_cover(data["cover_url"])
    if site == "empik":
        if "/img/defaults/" in (data.get("cover_url") or ""):
            data["cover_url"] = None
        if not data.get("duration_minutes"):
            data["duration_minutes"] = None
            if "brak informacji" in (data.get("length_str") or "").lower():
                data["length_str"] = None
    return data
