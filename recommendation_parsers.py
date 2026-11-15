"""Extract explicitly labelled product relationships without guessing target identities."""

import re
from urllib.parse import urljoin, urlsplit

from urls import BASES, canonical, identity, same_site


def normalized_link(site, item):
    """Keep exact product URLs or LC review URLs whose book identity is unknown."""
    if not isinstance(item, dict) or not isinstance(item.get("url"), str):
        return None
    url = urljoin(BASES[site], item["url"])
    if not same_site(site, url):
        return None
    key = identity(site, url)
    if key:
        url = canonical(site, url)
    elif site == "lubimyczytac" and re.fullmatch(r"/ksiazka/[^/]+/opinia/\d+/?", urlsplit(url).path):
        parts = urlsplit(url)
        url = BASES[site] + parts.path
    else:
        return None
    kind = item.get("relation_kind", "similar")
    if kind not in {"similar", "read_also", "also_liked"}:
        return None
    audio = item.get("audio_available")
    return {"url": url, "target_key": key, "relation_kind": kind,
            "title": item.get("title") if isinstance(item.get("title"), str) else None,
            "source_heading": item.get("source_heading") if isinstance(item.get("source_heading"), str) else None,
            "audio_available": int(audio) if isinstance(audio, (bool, int)) and audio in (0, 1) else None}


def related_signals(site, soup, url):
    """Read only a title's explicit similar/read-also section, excluding global carousels."""
    sections = []
    if site == "storytel":
        for section in soup.select("section[data-card-list-section]"):
            heading = section.select_one("h2")
            if heading and heading.get_text(" ", strip=True).casefold().startswith("książki podobne"):
                sections.append((section, heading.get_text(" ", strip=True), "similar", 'ol li a[href*="/pl/books/"]'))
    elif site == "empik":
        for section in soup.select('section[data-ta-section="SimilarProducts"]'):
            heading = section.select_one("h2")
            sections.append((section, heading.get_text(" ", strip=True) if heading else "Podobne produkty",
                             "similar", 'a[data-ta="product-link"][href]'))
    elif site == "woblink":
        for heading in soup.select("section.products-carousel > h2"):
            if heading.get_text(" ", strip=True).casefold().startswith("podobne do "):
                sections.append((heading.parent, heading.get_text(" ", strip=True), "similar", 'a[href*="/audiobook/"]'))
    elif site == "legimi":
        for heading in soup.select('h3[data-test="lp-collection-name"]'):
            if heading.get_text(" ", strip=True).casefold() == "podobne":
                section = heading.find_parent("div", class_="scroll-content")
                if section:
                    sections.append((section, "Podobne", "similar", 'a.book-title[href]'))
    elif site == "lubimyczytac":
        for section in soup.select("#ksiazki-rekomendacje"):
            heading = section.find_previous(["h2", "h3"])
            label = heading.get_text(" ", strip=True) if heading else ""
            if label.casefold().startswith("czytelnicy ") and "przeczytali również" in label.casefold():
                sections.append((section, label, "read_also", 'a.book-recommendation-card[href]'))
    if not sections:
        return {}
    result, seen = [], set()
    for section, heading, kind, selector in sections:
        for link in section.select(selector):
            title = link.get("aria-label") or link.get_text(" ", strip=True) or None
            audio = None
            if site == "empik":
                card = link.find_parent(attrs={"data-ta": "product"})
                title = card.get("data-product-name") if card else title
                if title and re.search(r"-\s*audiobook\s*$", title, re.I):
                    audio = True
                elif title and re.search(r"-\s*ebook\s*$", title, re.I):
                    audio = False
            elif site == "legimi":
                card = link.find_parent("div", class_="book-list-item")
                formats = card.select_one(".book-formats") if card else None
                if formats and formats.select('i[class*="icon-book-format-"]'):
                    audio = bool(formats.select_one(".icon-book-format-audiobook"))
            elif site == "woblink":
                audio = True
            elif site == "lubimyczytac":
                label = link.select_one(".book-recommendation-card__title")
                title = label.get_text(" ", strip=True) if label else None
            item = normalized_link(site, {"url": link["href"], "relation_kind": kind,
                                          "source_heading": heading, "title": title, "audio_available": audio})
            if not item or item["target_key"] == identity(site, url):
                continue
            key = item["target_key"] or item["url"]
            if key not in seen:
                result.append(item)
                seen.add(key)
    return {"related_links": result, "related_urls": [item["url"] for item in result if item["target_key"]],
            "recommendation_coverage": {"source_url": url, "scope": "page_section", "complete": None,
                                        "collected": len(result), "next_url": None}}
