"""Canonical URLs and stable product identities for supported services."""

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from sources import SOURCES

BASES = {
    "audioteka": "https://audioteka.com",
    "storytel": "https://www.storytel.com",
    "empik": "https://www.empik.com",
    "lubimyczytac": "https://lubimyczytac.pl",
}
BASES.update({site: source["base"] for site, source in SOURCES.items()})


def same_site(site, url):
    """Accept only the exact service host and its www alias."""
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.") == urlsplit(BASES[site]).hostname.removeprefix("www.")


def canonical(site, url, base=None):
    """Remove tracking parameters and normalize escaped Unicode and slashes."""
    parsed = urlsplit(urljoin(base or BASES[site], url.strip()))
    if parsed.scheme not in ("http", "https") or not same_site(site, parsed.geturl()):
        return None
    path = quote(unquote(parsed.path), safe="/,:@-._~!$&'()*+;=").rstrip("/")
    if site == "audioteka":
        path += "/"
    return BASES[site] + path


def identity(site, url):
    """Return a product key independent of slug, encoding and query string."""
    normalized = canonical(site, url)
    if not normalized:
        return None
    path = urlsplit(normalized).path
    patterns = {
        "storytel": r"^/pl/books/[^/]+-(\d+)$",
        "empik": r",p(\d+),ebooki-i-mp3-p$",
        "lubimyczytac": r"^/(?:ksiazka|audiobook)/(\d+)(?:/[^/]+)?$",
    }
    if site == "audioteka":
        if "[object" in unquote(path).lower():
            return None
        return normalized if re.fullmatch(r"/pl/audiobook/[^/]+/", path) else None
    pattern = SOURCES[site]["pattern"] if site in SOURCES else patterns[site]
    match = re.search(pattern, path)
    return match.group(1) if match else None


def listing_url(site, href, base):
    """Keep pagination parameters while rejecting foreign and product links."""
    url = urljoin(base, href.strip())
    if not same_site(site, url):
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http"):
        return None
    return urlunsplit(("https", urlsplit(BASES[site]).netloc, parsed.path, urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))), ""))


def audioteka_uuid_redirect(source, target):
    """Accept legacy UUID product aliases only within Audioteka's product routes."""
    if not identity("audioteka", target):
        return False
    normalized = canonical("audioteka", source)
    return bool(normalized and re.fullmatch(r"/pl/audiobook/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/", urlsplit(normalized).path, re.IGNORECASE))


def audioteka_product_redirect(source, target):
    """Accept UUID aliases and individually verified legacy product redirects."""
    verified = {"https://audioteka.com/pl/audiobook/projet-hail-mary/":
                "https://audioteka.com/pl/audiobook/projekt-hail-mary/"}
    return audioteka_uuid_redirect(source, target) or bool(identity("audioteka", target) and verified.get(canonical("audioteka", source)) == canonical("audioteka", target))
