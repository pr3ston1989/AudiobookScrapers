"""Additional audiobook sources and schema-compatible database initialization."""

import os
import sqlite3
from pathlib import Path

TAG_SITES = ("lubimyczytac", "storytel", "empik", "woblink")

SOURCES = {
    "nexto": {"label": "Nexto (archiwum)", "base": "https://www.nexto.pl", "pattern": r"^/audiobooki/[^/]+_p(\d+)\.xml$", "starts": ["/audiobooki_c1033.xml"], "active": False},
    "publio": {"label": "Publio", "base": "https://publio.pl", "pattern": r"^/[^/]+,p(\d+)\.html$", "starts": ["/audiobooki,nowosc.html"], "catalog": "/audiobooki.html", "active": True},
    "bookbeat": {"label": "BookBeat", "base": "https://www.bookbeat.com", "pattern": r"^/pl/book/(?:[^/]+-)?(\d+)$", "starts": ["/pl/audiobooks"], "catalog": "/pl/categories", "active": True},
    "woblink": {"label": "Woblink", "base": "https://woblink.com", "pattern": r"^/audiobook/[^/]+-(\d+)$", "starts": ["/katalog/audiobooki/nowosci"], "catalog": "/katalog/audiobooki", "active": True},
    "legimi": {"label": "Legimi", "base": "https://www.legimi.pl", "pattern": r"^/ebook-[^/]+,b(\d+)\.html$", "starts": ["/ebooki/?filters=audiobooks&sort=latest"], "catalog": "/ebooki/?filters=audiobooks&sort=latest", "active": True},
    "wolnelektury": {"label": "Wolne Lektury", "base": "https://wolnelektury.pl", "pattern": r"^/(?:katalog/lektura|api/books)/([^/]+)$", "starts": ["/api/audiobooks/?format=json"], "active": True},
    "virtualo": {"label": "Virtualo", "base": "https://virtualo.pl", "pattern": r"^/audiobook/[^/]+-i(\d+)$", "starts": ["/audiobooki/nowosci/?list-grid=1"], "catalog": "/audiobooki/", "active": True},
}


def create_database(site, path):
    """Create only a registered new database; never replace an existing file."""
    if site not in SOURCES:
        raise ValueError(f"Nie wolno tworzyć zastępczej bazy istniejącego serwisu: {site}")
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".initializing.db")
    temporary.unlink(missing_ok=True)
    conn = sqlite3.connect(temporary)
    try:
        conn.executescript((Path(__file__).parent / "schemas" / "audiobooks.sql").read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    # Utworzenie atomowe: istniejąca baza nie może zostać nadpisana.
    try:
        os.link(temporary, path)
        temporary.unlink()
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        return False
    return True
