"""Parse Virtualo audio metadata without borrowing ebook or preview data."""
import math
import re
from datetime import date
from bs4 import BeautifulSoup, Comment


def audio_length(value):
    """Return seconds for explicit units or an unambiguous three-part clock."""
    value = str(value or "").strip().lower()
    days = re.match(r"^(\d+)\s*(?:dzień|dni|doby|doba|dób)\s*", value)
    if days:
        remainder = value[days.end():].strip()
        seconds = audio_length(remainder) if remainder else 0
        return int(days[1]) * 86400 + seconds if seconds is not None else None
    compact = re.match(r"^(\d+)\s*h\s*(\d{1,2})\s*['′’]", value)
    if compact:
        h, m = map(int, compact.groups())
        return h * 3600 + m * 60 if m < 60 else None
    clock = re.match(r"^(\d+):(\d{2}):(\d{2})(?!\d)", value)
    if clock:
        h, m, s = map(int, clock.groups())
        return h * 3600 + m * 60 + s if m < 60 and s < 60 else None
    short = re.match(r"^(\d+):(\d{2})\s*min", value)
    if short:
        m, s = map(int, short.groups())
        return m * 60 + s if s < 60 else None
    units = re.match(r"(?:(\d+)\s*(?:godz\w*\.?|h(?=\s|\d|$))\s*)?(?:(\d+)\s*(?:min\w*\.?|m\b)\s*)?(?:(\d+)\s*(?:sek\w*\.?|s\b)\s*)?", value)
    if units and any(units.groups()):
        h, m, s = (int(part or 0) for part in units.groups())
        return h * 3600 + m * 60 + s
    return None


def parse_virtualo(soup, data):
    """Read audio microdata, details and explicitly labelled duration."""
    from parse_books import NotAudiobook
    heading = soup.select_one('h1[itemprop="name"]')
    book_format = soup.select_one('meta[itemprop="bookFormat"]')
    audio_heading = heading and re.search(r"[-–]\s*audiobook\s*$", heading.get_text(" ", strip=True), re.I)
    audio_format = any(row.select_one('.designation') and row.select_one('.data')
                       and row.select_one('.designation').get_text(strip=True).rstrip(":").lower() == "format"
                       and "MP3" in row.select_one('.data').get_text().upper()
                       for row in soup.select('.details.row.table .table-row'))
    if not heading or not (audio_heading and audio_format or book_format and "audiobook" in book_format.get("content", "").lower()):
        raise NotAudiobook("Virtualo: brak potwierdzonego wydania audiobooka")
    data["title"] = re.sub(r"\s*[-–]\s*audiobook\s*$", "", heading.get_text(" ", strip=True), flags=re.I)
    data["authors"] = [tag["content"].strip() for tag in soup.select('meta[itemprop="author"][content]') if tag["content"].strip()]
    descriptions = soup.select('#product-description .desc')
    data["description"] = "\n\n".join(node.get_text("\n", strip=True) for node in descriptions) or None
    if not data['description']:
        alternative = soup.select_one('.product-description-wrapper .description')
        if alternative and alternative.get_text(strip=True):
            data['description'] = alternative.get_text('\n', strip=True)
    if not data['description']:
        excerpt = soup.select_one('meta[name="description"][content]')
        if excerpt and excerpt['content'].strip():
            data['description'] = excerpt['content'].strip()
            data['description_kind'] = 'seo_excerpt'
    image = soup.select_one('img[itemprop="image"][src]')
    data["cover_url"] = image.get("src") if image else None
    length = None
    details = soup.select_one('.details.row.table')
    if details:
        # Serwis pozostawia czas w komentarzu własnej karty produktu.
        rows = list(details.select('.table-row'))
        for comment in details.find_all(string=lambda node: isinstance(node, Comment)):
            rows.extend(BeautifulSoup(str(comment), "lxml").select('.table-row'))
        for row in rows:
            label, node = row.select_one('.designation'), row.select_one('.data')
            if not label or not node:
                continue
            label = label.get_text(" ", strip=True).rstrip(":").lower()
            value = node.get_text(" ", strip=True)
            linked = [tag.get_text(" ", strip=True) for tag in node.select('a')]
            if label == "wydawnictwo":
                data["publisher"] = value
            elif label == "lektor":
                data["narrators"] = linked or ([value] if value else [])
            elif label == "seria":
                data["collections"] = linked or ([value] if value else [])
                data["series"] = (data["collections"] or [None])[0]
            elif label in ("tłumacz", "tłumaczenie"):
                data["translators"] = linked or ([value] if value else [])
            elif label == "format":
                data["format"] = value
            elif label in ("język", "język publikacji"):
                data["language"] = value
            elif label in ("czas", "czas trwania") and value:
                length = value
    for row in soup.select('table.product-details tr'):
        cells = row.select('td')
        if len(cells) < 2:
            continue
        label, node = cells[0].get_text(" ", strip=True).rstrip(":").lower(), cells[1]
        if label == "isbn":
            data["isbn"] = node.get_text(" ", strip=True)
        elif label == "kategoria":
            data["categories"] = [tag.get_text(" ", strip=True) for tag in node.select('a')]
            data["category"] = (data["categories"] or [node.get_text(" ", strip=True)])[0]
        elif label == "język":
            data["language"] = node.get_text(" ", strip=True)
    published = soup.select_one('meta[itemprop="datePublished"][content]')
    if published:
        try:
            data["release_date"] = date(*map(int, published["content"].split("-"))).isoformat()
        except (TypeError, ValueError):
            pass
    if not length:
        match = re.search(r"(?:czas\s+trwania|łączny\s+czas)\s*[:–-]?\s*(.{1,80})", data["description"] or "", re.I)
        length = match[1] if match else None
    seconds = audio_length(length)
    data.update(length_str=length, duration_seconds=seconds,
                duration_minutes=math.ceil(seconds / 60) if seconds else None)
    if length and seconds is None:
        data["duration_ambiguous"] = True
    available = soup.select_one('link[itemprop="availability"][href]')
    if available:
        data["source_availability"] = available["href"].rsplit("/", 1)[-1]
