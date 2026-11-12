"""Inspect public Virtualo samples without writing application databases."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bs4 import BeautifulSoup
from http_client import Client
from main import load_settings

OUT = ROOT / "reports" / "virtualo"
URLS = {
    "news": "https://virtualo.pl/audiobooki/nowosci/",
    "catalog": "https://virtualo.pl/audiobooki/",
    "book1": "https://virtualo.pl/audiobook/morelowy-las-i631575/",
    "book2": "https://virtualo.pl/audiobook/cafe-museum-i155270/",
    "book3": "https://virtualo.pl/audiobook/w-poszukiwaniu-swiatla-opowiesc-o-marii-sklodowskiej-curie-i303852/",
    "short": "https://virtualo.pl/audiobook/bajka-o-rodzicach-i652242/",
}


def probe():
    """Keep source HTML and a compact account of metadata and pagination."""
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings(ROOT / "config.json")
    client = Client(dict(settings, http_retries=1))
    result = {}
    try:
        for name, url in URLS.items():
            try:
                response = client.get(url)
                (OUT / f"{name}.html").write_text(response.text, encoding="utf-8")
                soup = BeautifulSoup(response.text, "lxml")
                record = dict(url=response.url, title=soup.title.get_text() if soup.title else None, size=len(response.content))
                record["structured"] = [tag.get_text() for tag in soup.select('script[type="application/ld+json"]')]
                record["images"] = [str(tag)[:900] for tag in soup.select('img') if any(word in str(tag).lower() for word in ("cover", "product", "large"))][:8]
                record["pagination"] = [str(tag)[:650] for tag in soup.select('a[href]') if any(word in tag['href'] for word in ("page=", "strona", "p="))][:12]
                record["details"] = soup.get_text(" ", strip=True)[-14000:] if name not in ("news", "catalog") else ""
                result[name] = record
                print(f"{name}: {response.url} ({len(response.content)} bytes)", flush=True)
            except Exception as exc:
                result[name] = {"error": str(exc)}
                print(f"{name}: {exc}", flush=True)
    finally:
        client.close()
    (OUT / "probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    probe()
