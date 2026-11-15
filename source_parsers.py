"""Normalize additional public audiobook catalogs into the existing book model."""

import json
import math
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from urls import canonical, identity


def json_roots(soup):
    """Decode JSON and serialized framework data without executing site JavaScript."""
    decoder = json.JSONDecoder()
    flight = []
    for tag in soup.select("script"):
        raw = tag.get_text().strip()
        if tag.get("type") in ("application/ld+json", "application/json"):
            try:
                yield json.loads(raw)
            except ValueError:
                pass
        if 'initialReduxState' in raw and 'JSON.parse(' in raw:
            try:
                literal, _ = decoder.raw_decode(raw.split("JSON.parse(", 1)[1])
                yield json.loads(literal)
            except (ValueError, TypeError):
                pass
        marker = "self.__next_f.push("
        if marker in raw:
            try:
                values, _ = decoder.raw_decode(raw.split(marker, 1)[1])
                if len(values) > 1 and isinstance(values[1], str):
                    flight.append(values[1])
            except (ValueError, TypeError):
                pass
    payload = "".join(flight).encode("utf-8")
    cursor = 0
    while cursor < len(payload):
        match = re.search(rb"[0-9a-f]+:", payload[cursor:])
        if not match:
            break
        cursor += match.end()
        sized = re.match(rb"T([0-9a-f]+),", payload[cursor:])
        if sized:
            cursor += sized.end()
            end = cursor + int(sized[1], 16)
            value = payload[cursor:end].decode("utf-8", errors="replace")
            try:
                yield json.loads(value)
            except ValueError:
                pass
            cursor = end
            continue
        if payload[cursor:cursor+1] == b"I":
            cursor += 1
        try:
            tail = payload[cursor:].decode("utf-8")
            value, end = decoder.raw_decode(tail)
            yield value
            cursor += len(tail[:end].encode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            end = payload.find(b"\n", cursor)
            cursor = end + 1 if end >= 0 else len(payload)


def objects(value):
    """Walk decoded data including nested serialized JSON strings."""
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))
        elif isinstance(current, str) and current.startswith(("{", "[")):
            try:
                stack.append(json.loads(current))
            except ValueError:
                pass


def redux(soup):
    """Return Legimi's public SSR state, excluding any need for private APIs."""
    return next((root for root in json_roots(soup) if isinstance(root, dict) and "bookContainer" in root), {})


def text(node):
    """Extract visible text without script payloads."""
    return node.get_text(" ", strip=True) if node else ""


def clean_cover(site, value):
    """Use documented/source image paths instead of known resized variants."""
    if not value:
        return None
    from sources import SOURCES
    value = urljoin(SOURCES[site]["base"], value)
    parsed = urlsplit(value)
    if site == "nexto" and parsed.path == "/converter":
        image = parse_qs(parsed.query).get("img", [""])[0]
        if image.startswith("/upload/"):
            return urljoin(SOURCES[site]["base"], image)
    if site == "bookbeat":
        return urlunsplit((*parsed[:3], "", ""))
    return value


def largest_image(images):
    """Select the largest image actually advertised by the source."""
    if not isinstance(images, dict):
        return None
    return next((images[key] for key in sorted(images, key=lambda key: int(key) if str(key).isdigit() else 0, reverse=True) if images[key]), None)


def parse_extra(site, html, url):
    """Parse one identified audiobook and retain source-specific provenance."""
    from parse_books import BookValidationError, NotAudiobook, apply_structured, book_tags, duration, jsonld, names, structured_product
    if site == "wolnelektury":
        return parse_wolnelektury(json.loads(html), url)
    soup = BeautifulSoup(html, "lxml")
    key = identity(site, url)
    link = soup.select_one('link[rel="canonical"][href]')
    if link:
        target = urljoin(url, link["href"])
        if identity(site, target) != key:
            raise BookValidationError(f"Strona nie odpowiada żądanemu audiobookowi: {url}")
        url = target
    data = {"url": canonical(site, url), "type": "audiobook", "format": "MP3", "authors": [],
            "narrators": [], "publisher": None, "description": None, "title": None,
            "cover_url": None, "duration_minutes": None, "language": None, "reviews": []}
    source_objects = list(jsonld(soup))
    own = structured_product(soup, site, url)
    apply_structured(data, soup, site)
    title = soup.select_one("h1")
    if title and not data.get("title"):
        data["title"] = text(title)
    if site == "virtualo":
        from virtualo_parser import parse_virtualo
        parse_virtualo(soup, data)
    elif site == "nexto":
        if not own:
            raise NotAudiobook("Nexto: brak archiwalnej karty audiobooka")
        data["title"] = re.sub(r"\s*[–-]\s*audiobooki?$", "", data.get("title") or "", flags=re.I)
        data["publisher"] = (names(own.get("brand")) or [None])[0]
        data["isbn"] = own.get("sku")
        meta = soup.select_one('meta[name="og:description"],meta[property="og:description"]')
        if meta:
            description = meta.get("content", "")
            match = re.search(r", autor (.+?), lektor (.+?) w formacie", description)
            if match:
                data["authors"], data["narrators"] = [match[1]], [match[2]]
        for review in own.get("review", []):
            body = review.get("reviewBody") or review.get("description")
            if body:
                data["reviews"].append({"username": (names(review.get("author")) or ["Anonim"])[0], "text": body,
                                        "rating": (review.get("reviewRating") or {}).get("ratingValue"), "date": review.get("datePublished")})
        data["source_archive"] = True
        data["archive_notice"] = "Dystrybucja audio zakończona 30.06.2026; dostępne metadane historyczne"
    elif site == "publio":
        if not re.search(r"audiobook", text(soup.title), re.I):
            raise NotAudiobook("Publio: karta nie potwierdza formatu audiobooka")
        data['source_details'] = {}
        for row in soup.select(".details-element"):
            label, node = row.select_one(".detail-title"), row.select_one(".detail-content")
            label, value = text(label).lower(), text(node)
            if label and value:
                data['source_details'][label] = value
            linked = [text(a).replace(" (lektor)", "") for a in node.select("a")] if node else []
            if label == "autor":
                data["authors"] = linked or [value]
            elif label == "współtwórca":
                data["narrators"] = [text(a).replace(" (lektor)", "") for a in node.select("a") if "lektor" in text(a).lower()]
                data["contributors"] = linked
                translators = [text(a).replace(' (tłumacz)', '').replace(' (tłumaczenie)', '') for a in node.select('a') if 'tłumacz' in text(a).lower()]
                if translators:
                    data['translators'] = translators
                    data['translator'] = ', '.join(translators)
            elif label == "wydawca":
                data["publisher"] = value
            elif label == "opis":
                data["description"] = text(node.select_one(".detail-label")) or value
            elif label == "czas nagrania":
                data["length_str"] = value
                data["duration_minutes"] = duration(value) or data.get("duration_minutes")
            elif label == "format" and not data["duration_minutes"]:
                data["duration_minutes"] = duration(value)
            elif label == "isbn":
                data["isbn"] = value
            elif label == "język publikacji":
                data["language"] = value
            elif label == "miejsce i rok wydania":
                match = re.search(r"\b(\d{4})\b", value)
                data["release_year"] = int(match[1]) if match else None
            elif label == "publikacja z kategorii":
                data["categories"] = linked
                data["category"] = linked[-1] if linked else value
            elif label in ('seria', 'cykl'):
                data['collections'] = linked or [value]
                data['series'] = (data['collections'] or [None])[0]
            elif label == 'rodzaj publikacji':
                data['publication_kind'] = value
        long_description = soup.select_one(".product-description")
        if long_description:
            value = long_description.get_text("\n", strip=True)
            if len(value) > len(data.get("description") or ""):
                data["description"] = value
        image = soup.select_one('meta[property="og:image"][content]')
        data["cover_url"] = image["content"] if image else None
    elif site == "woblink":
        data["tags"] = names(names(data.get("tags")) + book_tags(soup, site))
        if own.get("material") != "audiobook" and not urlsplit(url).path.startswith("/audiobook/"):
            raise NotAudiobook("Woblink: produkt nie jest audiobookiem")
        data['source_details'] = {}
        for row in soup.select("table tr"):
            cells = row.select("td")
            if len(cells) < 2:
                continue
            label, node = text(cells[0]).lower(), cells[1]
            value = text(node)
            if label and value:
                data['source_details'][label.rstrip(':')] = value
            linked = [text(a) for a in node.select("a")]
            if label == "autor:":
                data["authors"] = linked or [value]
            elif label == "czyta:":
                data["narrators"] = linked or [value]
            elif label == "wydawca:":
                data["publisher"] = linked[0] if linked else value.split("Nazwa handlowa")[0].strip()
            elif label.startswith("czas słuchania"):
                data["length_str"], data["duration_minutes"] = value, duration(value)
            elif label.startswith("język audiobooka"):
                data["language"] = value
            elif label.rstrip(':') in ('tłumacz', 'tłumaczenie'):
                data['translators'] = linked or [value]
                data['translator'] = ', '.join(data['translators'])
            elif label.rstrip(':') in ('seria', 'cykl'):
                data['collections'] = linked or [value]
                data['series'] = (data['collections'] or [None])[0]
        breadcrumbs = next((obj for obj in source_objects if isinstance(obj, dict) and obj.get('@type') == 'BreadcrumbList'), {})
        categories = []
        for item in breadcrumbs.get('itemListElement', []):
            target = item.get('item') or {}
            href = item.get('url') or (target.get('@id') if isinstance(target, dict) else target) or ''
            label = item.get('name') or (target.get('name') if isinstance(target, dict) else None)
            if label and '/katalog/audiobooki/' in href:
                categories.append(label)
        data['source_metadata'] = {'category_breadcrumbs': categories}
        if not data.get('categories'):
            data['categories'] = names(categories)
            if data['categories']:
                data['category'] = data['categories'][-1]
        data["isbn"] = str(own.get("productID", "")).removeprefix("isbn:") or None
        data["release_date"] = own.get("releaseDate")
        data["rating_scale"] = 10
    elif site == "bookbeat":
        audio = own.get("workExample", [])
        audio = next((item for item in audio if isinstance(item, dict) and item.get("@type") == "Audiobook"), {})
        if not audio:
            raise NotAudiobook("BookBeat: brak wydania audio (produkt może być ebookiem)")
        product = next((obj for root in json_roots(soup) for obj in objects(root)
                        if str(obj.get("bookid", obj.get("id"))) == key and "audiobooklength" in obj), {})
        seconds = product.get("audiobooklength")
        if seconds:
            data["duration_seconds"] = seconds
            data["duration_minutes"] = math.ceil(float(seconds) / 60)
        for field, source in (("description", "summary"), ("language", "language"), ("translator", "translator")):
            if product.get(source):
                data[field] = BeautifulSoup(str(product[source]), "lxml").get_text("\n", strip=True)
        data["length_str"] = f"PT{seconds}S" if seconds else None
        data["original_title"] = product.get("originaltitle")
        genres = names(product.get("genres"))
        if genres:
            data["categories"] = genres
            data["category"] = genres[0]
        data["rating_count"] = (own.get("aggregateRating") or {}).get("reviewCount", data.get("rating_count"))
        data["audio_edition_id"] = audio.get("@id")
        data["isbn"] = audio.get("isbn")
        data["rating_scale"] = 5
        series = product.get('series') or {}
        if series.get('name'):
            data['series'] = series
            data['collections'] = [series['name']]
            part = series.get('displaypartnumber', series.get('partnumber'))
            if part is not None:
                data['volume_number'] = str(part)
        narration = product.get('narratingRating') or {}
        data['narration_rating'] = narration.get('ratingValue')
        data['narration_rating_count'] = narration.get('numberOfRatings')
        data['content_types'] = names(product.get('contenttypetags'))
        data['related_readings_url'] = product.get('relatedreadingsurl')
        data['source_metadata'] = {field: product.get(field) for field in (
            'contenttypetags', 'relatedreadingsurl', 'subtitle', 'originalpublishyear', 'editions', 'rating', 'narratingRating', 'swiftWords', 'narratingSwiftWords') if product.get(field) is not None}
    elif site == "legimi":
        state = redux(soup)
        response = state.get("bookContainer", {}).get("response") or {}
        product = response.get("book") or {}
        if not product:
            raise BookValidationError("Legimi: brak danych własnej karty w stanie aplikacji; nie uznaję tego za brak audiobooka")
        audio = product.get("audiobook") or {}
        if not audio or audio.get("isArtificialAudio"):
            raise NotAudiobook("Legimi: brak nagrania audiobooka lub dostępny tylko TTS")
        if str(product.get("id")) != key:
            raise BookValidationError("Legimi: dane dotyczą innego produktu")
        data.update(title=product.get("title"), authors=names(product.get("authors")),
                    narrators=names(product.get("lectors")) or names(audio.get("lectorName")),
                    publisher=audio.get("publisherName"), cover_url=largest_image(audio.get("images")),
                    description=BeautifulSoup(product.get("description") or "", "lxml").get_text("\n", strip=True),
                    length_str=audio.get("duration"), duration_minutes=duration(audio.get("duration")),
                    language=product.get("language"), category=(product.get("primaryCategory") or {}).get("name"),
                    audio_edition_id=audio.get("id"), source_ids=list({str(product.get("id")), str(audio.get("id"))}),
                    isbn=audio.get("isbn") or (product.get("isbn") if not product.get("ebook") else None))
        rating = product.get("rating") or {}
        data.update(avg_rating=rating.get("averageRate"), rating_count=rating.get("totalVotes"), rating_scale=5)
        data["source_metadata"] = {"audio_id": audio.get("id"), "synchrobook": response.get("isSynchrobook"), "release_year": product.get("publicationYear")}
        cycle = product.get('cycle') or {}
        if cycle.get('name'):
            data['series'] = cycle
            data['collections'] = [cycle['name']]
        if product.get('publicationYear') and not product.get('ebook'):
            data['release_year'] = product['publicationYear']
        extra_categories = names(product.get('additionalCategories'))
        data['categories'] = list(dict.fromkeys(([data['category']] if data.get('category') else []) + extra_categories))
        data['source_metadata'].update({field: product.get(field) for field in ('languageShort', 'formats', 'readerCount', 'rating', 'isAvailable') if product.get(field) is not None})
        if audio.get('pages') is not None:
            data['source_metadata']['source_page_count'] = audio['pages']
    if not data.get("title"):
        raise BookValidationError(f"Brak tytułu produktu: {url}")
    data["authors"] = list(dict.fromkeys(data.get("authors") or []))
    data["narrators"] = list(dict.fromkeys(data.get("narrators") or []))
    data["cover_url"] = clean_cover(site, data.get("cover_url"))
    for field in ("description",):
        if data.get(field):
            data[field] = re.sub(r"\s*Czytaj więcej\.{0,3}\s*$", "", data[field])
    if data.get("release_date"):
        value = str(data["release_date"])
        match = re.fullmatch(r"(\d{2})[-.](\d{2})[-.](\d{4})", value)
        if match:
            data["release_date"] = f"{match[3]}-{match[2]}-{match[1]}"
        match = re.match(r"(\d{4})-", data["release_date"])
        if match:
            data["release_year"] = int(match[1])
    from recommendation_parsers import related_signals
    data.update(related_signals(site, soup, url))
    return data


def parse_wolnelektury(value, requested_url):
    """Read public audiobook metadata; two-part audio_length means minutes:seconds."""
    from parse_books import BookValidationError, NotAudiobook, names
    if not isinstance(value, dict) or identity("wolnelektury", value.get("url", "")) != identity("wolnelektury", requested_url):
        raise BookValidationError("Wolne Lektury: API zwróciło inny utwór")
    media = [item for item in value.get("media", []) if item.get("type") in ("mp3", "ogg")]
    if not media:
        raise NotAudiobook("Wolne Lektury: brak nagrania")
    parts = [int(part) for part in (value.get("audio_length") or "").split(":") if part.isdigit()]
    seconds = sum(number * 60 ** index for index, number in enumerate(reversed(parts)))
    narrators = list(dict.fromkeys(item["artist"] for item in media if item.get("artist")))
    fragment = value.get("fragment_data") or {}
    description = fragment.get("html", "") if isinstance(fragment, dict) else ""
    if not description:
        description = value.get("preview") if isinstance(value.get("preview"), str) else ""
    return {"url": canonical("wolnelektury", value["url"]), "title": value.get("title"), "type": "audiobook",
            "authors": names(value.get("authors")), "narrators": narrators,
            "description": BeautifulSoup(str(description), "lxml").get_text("\n", strip=True),
            "cover_url": value.get("cover"), "duration_minutes": math.ceil(seconds / 60) if seconds else None,
            "description_kind": "excerpt", "duration_seconds": seconds or None, "length_str": value.get("audio_length"),
            "language": value.get("language"), "category": (names(value.get("genres")) or [None])[0],
            "categories": names(value.get("genres")), "epochs": names(value.get("epochs")),
            "literary_kinds": names(value.get("kinds")),
            "translators": names(value.get("translators")), "recordings": media,
            "directors": list(dict.fromkeys(item["director"] for item in media if item.get("director"))),
            "parent": value.get("parent"), "children": value.get("children"), "format": "MP3", "reviews": []}
