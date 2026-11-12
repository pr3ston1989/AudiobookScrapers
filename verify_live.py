"""Audit current public product pages and real LC AJAX pagination without DB writes."""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from discovery import catalog_response, page_links
from http_client import Client
from main import ROOT, fetch_book, load_settings, write_json
from quality import assess

EXAMPLES = {
    "audioteka": "https://audioteka.com/pl/audiobook/gos-c-zuzia-antecka-jak-radzic-sobie-z-niejadkiem-odc-155-mama-lama/",
    "storytel": "https://www.storytel.com/pl/books/the-wealth-gap-is-a-knowledge-gap-14740522",
    "empik": "https://www.empik.com/the-truth-about-pyecraft-a-humorous-science-fiction-story-wells-herbert-george,p1764931904,ebooki-i-mp3-p",
    "lubimyczytac": "https://lubimyczytac.pl/ksiazka/5187984/a",
}


def audit():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = load_settings(ROOT / "config.json")
    settings["browser_headless"] = False
    report = {"services": {}, "pagination": {}}
    fixtures = ROOT / "tests" / "fixtures"
    for site, example in EXAMPLES.items():
        with closing(sqlite3.connect(settings["databases"][site].as_uri() + "?mode=ro", uri=True)) as conn:
            urls = [row[0] for row in conn.execute("SELECT url FROM books WHERE description IS NOT NULL AND avg_rating > 0 ORDER BY id LIMIT 2")]
        samples = []
        client = Client(settings)
        try:
            for number, url in enumerate(dict.fromkeys([example, *urls]), 1):
                try:
                    response = client.get(url, render=site == "empik")
                    (fixtures / f"live_{site}_{number}.html").write_text(response.text, encoding="utf-8")
                    from parse_books import parse
                    data = parse(site, response.text, response.url)
                    result = {"requested_url": url, "data": data, "quality": assess(site, data),
                              "cookies": client.cookie_status if site == "empik" else None}
                    samples.append(result)
                    print(site, number, data["title"], result["quality"]["missing_required"], flush=True)
                except Exception as exc:
                    samples.append({"requested_url": url, "error": str(exc)})
                    print(site, number, "ERROR", exc, flush=True)
            report["services"][site] = samples
            if site == "lubimyczytac":
                response = client.get("https://lubimyczytac.pl/katalog/ksiazki")
                first, pages = page_links(site, response.text, response.url)
                ajax = next(url for url in pages if "standardFilteredDataPageContent" in url)
                second = catalog_response(site, client, ajax)
                (fixtures / "lc_catalog_page2.html").write_text(second.text, encoding="utf-8")
                products, next_pages = page_links(site, second.text, second.url)
                report["pagination"][site] = {"page1_products": len(first), "page2_products": len(products),
                    "different_products": len(products - first), "next_pages": sorted(next_pages), "tested_url": ajax}
                print("LC pagination", report["pagination"][site], flush=True)
        finally:
            client.close()
            write_json(ROOT / "reports" / "live_field_audit.json", report)
    write_json(fixtures / "live_manifest.json", {"services": report["services"]})
    return report


if __name__ == "__main__":
    audit()
