"""Regression tests for discovery, parsing, transactions and unchanged schemas."""

import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database
from discovery import Discovery, discover_sitemaps, discover_listings, page_links, sitemap_entries
from http_client import Client, HTTPError, Response
from main import load_settings, run_cycle, run_lock, run_site, fetch_book
from parse_books import BookValidationError, NotAudiobook, duration, parse, parse_tags
from recommendations import Recommendations, recommendations_path
from urls import canonical, identity

FIXTURES = ROOT / "tests" / "fixtures"
URLS = {
    "audioteka": "https://audioteka.com/pl/audiobook/gos-c-zuzia-antecka-jak-radzic-sobie-z-niejadkiem-odc-155-mama-lama/",
    "storytel": "https://www.storytel.com/pl/books/the-wealth-gap-is-a-knowledge-gap-14740522",
    "lubimyczytac": "https://lubimyczytac.pl/ksiazka/5187984/a",
    "empik": "https://www.empik.com/the-truth-about-pyecraft-a-humorous-science-fiction-story-wells-herbert-george,p1764931904,ebooki-i-mp3-p",
}


def fixture(site):
    name = "lc" if site == "lubimyczytac" else site
    return (FIXTURES / (name + ".html")).read_text(encoding="utf-8")


def schema(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()


class UrlTests(unittest.TestCase):
    def test_stable_id_and_escaped_unicode(self):
        self.assertEqual(identity("storytel", "https://www.storytel.com/pl/books/żółw-123?utm=x"), "123")
        self.assertEqual(canonical("storytel", "https://storytel.com/pl/books/żółw-123/"),
                         canonical("storytel", "https://www.storytel.com/pl/books/%C5%BC%C3%B3%C5%82w-123"))
        self.assertEqual(identity("lubimyczytac", URLS["lubimyczytac"]), "5187984")

    def test_foreign_subdomains_and_malformed_urls(self):
        self.assertIsNone(identity("storytel", "https://storytel.com.evil.test/pl/books/title-1"))
        self.assertIsNone(identity("storytel", "https://www.storytel.com/pl/books/title-1/reviews"))
        self.assertIsNone(identity("audioteka", "https://audioteka.com/pl/audiobook/[object Promise]/"))
        self.assertIsNone(identity("lubimyczytac", "https://lubimyczytac.pl/ksiazka/5187984/a/opinie"))


class DiscoveryTests(unittest.TestCase):
    def test_xml_namespace_whitespace_entity_and_gzip(self):
        content = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc> https://www.storytel.com/pl/books/a-1?x=1&amp;y=2 </loc><lastmod>2026-10-09</lastmod></url></urlset>'
        kind, entries = sitemap_entries(gzip.compress(content), "https://www.storytel.com/sitemap.xml")
        self.assertEqual(kind, "urlset")
        self.assertEqual(entries, [("https://www.storytel.com/pl/books/a-1?x=1&y=2", "2026-10-09")])

    def test_new_shard_and_nonstandard_extension(self):
        root = "https://www.storytel.com/sitemaps/sitemaps_full_poland.xml"
        shard = "https://www.storytel.com/pl/sitemaps/new-part?version=4"
        replies = {
            "https://www.storytel.com/robots.txt": b"Sitemap: " + root.encode(),
            root: f'<sitemapindex><sitemap><loc>{shard}</loc></sitemap></sitemapindex>'.encode(),
            shard: b'<urlset><url><loc>https://www.storytel.com/pl/books/title-321</loc></url></urlset>',
        }
        class FakeClient:
            def get(self, url):
                return Response(url, replies[url])
        result = Discovery()
        discover_sitemaps("storytel", FakeClient(), result)
        self.assertEqual(result.maps, 2)
        self.assertIn("321", result.urls)
        self.assertEqual(result.errors, [])

    def test_invalid_sitemap_is_error(self):
        class FakeClient:
            def get(self, url):
                return Response(url, b"<html>Cloudflare</html>")
        result = Discovery()
        discover_sitemaps("storytel", FakeClient(), result)
        self.assertTrue(result.errors)
        self.assertFalse(result.urls)

    def test_empik_products_and_pagination(self):
        html = (FIXTURES / "empik_list_browser.html").read_text(encoding="utf-8")
        products, pages = page_links("empik", html, "https://www.empik.com/audiobooki-i-ebooki/audiobooki/literatura-obyczajowa,350216,s")
        self.assertGreater(len(products), 40)
        self.assertEqual(len(pages), 1)
        self.assertTrue(all("ebooki-i-mp3-p" in url for url in products))

    def test_empik_incremental_scan_preserves_sort_and_stops(self):
        start = "https://www.empik.com/cat,s?sort=publishDesc"
        visited = []
        class FakeClient:
            def get(self, url, **kwargs):
                visited.append(url)
                page = len(visited)
                return Response(url, (f'<a href="/title,p1,ebooki-i-mp3-p">Book</a><a rel="next" href="/cat,s,{page+1}">Next</a>').encode())
        result = Discovery()
        discover_listings("empik", FakeClient(), result, [start], 20, {"1": 1})
        self.assertEqual(len(visited), 3)
        self.assertTrue(all("sort=publishDesc" in u for u in visited))


class ParserTests(unittest.TestCase):
    def test_current_four_services(self):
        for site, url in URLS.items():
            with self.subTest(site=site):
                data = parse(site, fixture(site), url)
                self.assertTrue(data["title"])
                self.assertTrue(data["authors"])
                self.assertTrue(data["publisher"])

    def test_storytel_graph_metadata(self):
        data = parse("storytel", fixture("storytel"), URLS["storytel"])
        self.assertEqual(data["authors"], ["Dorian Frinch"])
        self.assertEqual(data["narrators"], ["Hale Bennett"])
        self.assertEqual(data["duration_minutes"], 21)

    def test_book_tags_use_title_card_links(self):
        lc = """<h1 class="book__title">Test</h1>
            <div class="tags"><a class="tag" href="/ksiazki/t/Fantasy"> Fantasy </a>
            <a class="tag" href="/ksiazki/t/Fantasy">Fantasy</a></div>
            <a href="/ksiazki/t/Poza-sekcją">Poza sekcją</a>"""
        self.assertEqual(parse_tags("lubimyczytac", lc, URLS["lubimyczytac"]), ["Fantasy"])
        self.assertEqual(parse("lubimyczytac", fixture("lubimyczytac").replace("</body>", lc + "</body>"),
                               URLS["lubimyczytac"])["tags"], ["Fantasy"])
        storytel = fixture("storytel")
        self.assertEqual(parse("storytel", storytel, URLS["storytel"])["tags"], ["Rozwój kariery"])
        self.assertEqual(parse_tags("storytel", storytel, URLS["storytel"]), ["Rozwój kariery"])
        self.assertEqual(parse_tags("storytel", '<h1 class="a_styledTitle">Test</h1>', URLS["storytel"]), [])
        with self.assertRaises(BookValidationError):
            parse_tags("storytel", "<h1>Strona błędu</h1>", URLS["storytel"])

    def test_empik_browser_details(self):
        data = parse("empik", (FIXTURES / "empik_browser.html").read_text(encoding="utf-8"), URLS["empik"])
        self.assertEqual(data["duration_minutes"], 22)
        self.assertEqual(data["narrators"], ["Wilkie John"])
        self.assertEqual(data["release_date"], "2026-07-22")

    def test_empik_tags_include_hidden_spans_and_ignore_controls(self):
        html = """<h1 data-ta="title">Konfrontacja - audiobook</h1>
            <div class="ProductTags-module__container--new">
            <span class="ProductTags-module__productTag--new">kryminał akcji</span>
            <span class="ProductTags-module__productTag--new">kryminał policyjny</span>
            <span class="ProductTags-module__productTag--new ProductTags-module__hidden--new">kryminał polityczny</span>
            <span class="ProductTags-module__productTag--new"> kryminał akcji </span>
            <span class="ProductTags-module__ellipsis--new">...</span><button>Więcej</button></div>"""
        expected = ["kryminał akcji", "kryminał policyjny", "kryminał polityczny"]
        self.assertEqual(parse_tags("empik", html, URLS["empik"]), expected)
        self.assertEqual(parse("empik", html, URLS["empik"])["tags"], expected)
        self.assertEqual(parse_tags("empik", '<h1 data-ta="title">Test</h1>', URLS["empik"]), [])
        with self.assertRaises(BookValidationError):
            parse_tags("empik", '<h1>Strona błędu</h1>', URLS["empik"])
        with self.assertRaises(BookValidationError):
            parse_tags("empik", '<link rel="canonical" href="https://www.empik.com/inny,p123,ebooki-i-mp3-p">' + html, URLS["empik"])

    def test_product_keywords_and_categories_keep_only_own_signals(self):
        html = """<h1 data-ta="title">Test - audiobook</h1>
            <a data-ta="breadcrumb-category">Audiobooki i Ebooki</a>
            <a data-ta="breadcrumb-category">Audiobooki</a>
            <a data-ta="breadcrumb-category">Kryminały</a>
            <script type="application/ld+json">{"@type":"Product", "url":"https://www.empik.com/the-truth-about-pyecraft-a-humorous-science-fiction-story-wells-herbert-george,p1764931904,ebooki-i-mp3-p", "genre":["Thriller","Sensacja"],
            "keywords":"policja, śledztwo", "name":"Test"}</script>
            <script type="application/ld+json">{"@type":"Product", "name":"Polecany produkt",
            "keywords":"obcy tag"}</script>"""
        data = parse("empik", html, URLS["empik"])
        self.assertEqual(data["category"], "Kryminały")
        self.assertEqual(data["categories"], ["Kryminały", "Thriller", "Sensacja"])
        self.assertEqual(data["tags"], ["policja", "śledztwo"])

    def test_empik_rejects_ebooks(self):
        with self.assertRaises(NotAudiobook):
            parse("empik", fixture("empik").replace('"subtype":"MP3"', '"subtype":"EPUB"'), URLS["empik"])

    def test_canonical_must_match_requested_identity(self):
        html = '<link rel="canonical" href="https://www.storytel.com/pl/books/other-2"><h1>A</h1>'
        with self.assertRaises(ValueError):
            parse("storytel", html, "https://www.storytel.com/pl/books/title-1")

    def test_uuid_redirect_is_allowed_but_foreign_or_unrelated_redirects_are_rejected(self):
        source = "https://audioteka.com/pl/audiobook/7a8a297f-e998-4f6d-8521-e9deb9992b68/"
        target = "https://audioteka.com/pl/audiobook/roj-dronow-odcinek-1/"
        html = b'<h1 class="product-top_title">Test</h1>'
        client = SimpleNamespace(get=lambda *args, **kwargs: Response(target, html))
        self.assertEqual(fetch_book(client, "audioteka", source)["url"], target)
        for requested, final in ((source, "https://evil.test/pl/audiobook/other/"),
                                 (source, "https://audioteka.com/pl/"),
                                 (URLS["audioteka"], target)):
            with self.subTest(final=final):
                client = SimpleNamespace(get=lambda *args, **kwargs: Response(final, html))
                with self.assertRaises(ValueError):
                    fetch_book(client, "audioteka", requested)

    def test_durations(self):
        self.assertEqual(duration("PT2H3M30S"), 124)
        self.assertEqual(duration("12 godzin 54 minuty"), 774)
        self.assertEqual(duration("00:22:00"), 22)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        stop = patch("events.STOP_FILE", Path(self.temp.name) / "stop.request")
        stop.start()
        self.addCleanup(stop.stop)

    def database(self, site):
        path = Path(self.temp.name) / f"{site}.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript((FIXTURES / f"schema_{site}.sql").read_text(encoding="utf-8"))
        db = Database(site, path)
        self.addCleanup(db.close)
        return db

    def test_four_original_schemas_and_idempotent_upsert(self):
        for site, url in URLS.items():
            with self.subTest(site=site):
                db = self.database(site)
                before = schema(db.path)
                data = parse(site, fixture(site), url)
                self.assertEqual(db.save(data), "inserted")
                expected_tags = set(data.get("tags", [])) if "book_tags" in db.tables else set()
                data.update(description=None, authors=[], reviews=[], tags=[])
                self.assertEqual(db.save(data), "updated")
                self.assertEqual(db.stats()["books"], 1)
                self.assertEqual(before, schema(db.path))
                with closing(sqlite3.connect(db.path)) as conn:
                    self.assertTrue(conn.execute("SELECT description FROM books").fetchone()[0])
                    self.assertGreater(conn.execute("SELECT COUNT(*) FROM book_authors").fetchone()[0], 0)
                    if expected_tags:
                        self.assertEqual({name for (name,) in conn.execute(
                            "SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id"
                        )}, expected_tags)

    def backfill_settings(self, db):
        settings = load_settings(ROOT / "config.json")
        settings["databases"][db.site] = db.path
        settings["coverage_database"] = Path(self.temp.name) / "coverage.db"
        return settings

    def backfill_args(self, **overrides):
        values = dict(command="backfill", url=None, tags_only=True, limit=0,
                      backup=False, no_backup=True, no_covers=True)
        return SimpleNamespace(**{**values, **overrides})

    def test_tag_backfill_resumes_and_distinguishes_empty_and_failed_pages(self):
        site = "storytel"
        db = self.database(site)
        original_schema = schema(db.path)
        urls = [f"https://www.storytel.com/pl/books/test-{i}" for i in range(1, 5)]
        for url in urls:
            db.save({"url": url, "title": "Stary tytuł"})
        db.enqueue([urls[0]], refresh=True)
        original = db.path.read_bytes()
        settings = self.backfill_settings(db)
        title = b'<h1 class="a_styledTitle">Test</h1>'
        tagged = title + b'<ul class="x_tagList"><li><a href="/pl/tags/test-1">Tag</a></li></ul>'
        responses = {urls[0]: tagged, urls[1]: title, urls[3]: tagged}

        def get(url):
            if url not in responses:
                raise HTTPError(url, 429)
            return Response(url, responses[url])

        with patch("main.Client") as client:
            client.return_value.get.side_effect = get
            first = run_site(site, settings, self.backfill_args(limit=1), {})
            self.assertEqual(first["tagged"], 1)
            second = run_site(site, settings, self.backfill_args(), {})
            self.assertEqual(second["without_tags"], 1)
            self.assertTrue(second["stopped_on_rate_limit"])
            responses[urls[2]] = title
            third = run_site(site, settings, self.backfill_args(), {})
            self.assertEqual(third["tagged"], 1)
            self.assertEqual(third["without_tags"], 1)
            fourth = run_site(site, settings, self.backfill_args(), {})
            self.assertEqual(fourth["attempted"], 0)
        self.assertEqual(original_schema, schema(db.path))
        self.assertEqual(db.stats()["queue"], {"pending": 1})
        self.assertEqual(db.path.read_bytes(), original)
        with closing(sqlite3.connect(recommendations_path(settings))) as conn:
            self.assertEqual(conn.execute("SELECT product_key,label FROM labels WHERE kind='tag' ORDER BY product_key").fetchall(),
                             [("1", "Tag"), ("4", "Tag")])
            self.assertEqual(conn.execute("SELECT SUM(tags_checked) FROM titles").fetchone()[0], 4)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_empik_tag_backfill_imports_existing_and_rechecks_all_with_browser(self):
        db = self.database("empik")
        urls = [f"https://www.empik.com/test,p{i},ebooki-i-mp3-p" for i in (1, 2)]
        db.save({"url": urls[0], "title": "Test", "tags": ["Stary tag"]})
        db.save({"url": urls[1], "title": "Test"})
        original = db.path.read_bytes()
        settings = self.backfill_settings(db)
        empty = b'<h1 data-ta="title">Test</h1>'
        tagged = empty + '<span class="ProductTags-module__productTag--new ProductTags-module__hidden--new">Nowy tag</span>'.encode()
        with patch("main.Client") as client:
            get = client.return_value.get
            get.return_value = Response(urls[1], empty)
            report = run_site("empik", settings, self.backfill_args(), {})
            self.assertEqual(report["tagged"], 1)
            get.assert_called_once_with(urls[1], render=True)
            get.reset_mock()
            get.return_value = Response(urls[0], tagged)
            with patch("recommendations.ArchivedSignals.empty_tag_keys", return_value={"1", "2"}):
                first = run_site("empik", settings, self.backfill_args(recheck=True, limit=1), {})
            self.assertEqual(first["downloaded"], 1)
            get.assert_called_once_with(urls[0], render=True)
            get.reset_mock()
            get.side_effect = HTTPError(urls[1], 429)
            failed = run_site("empik", settings, self.backfill_args(recheck=True), {})
            self.assertTrue(failed["stopped_on_rate_limit"])
            get.side_effect = None
            get.return_value = Response(urls[1], empty)
            resumed = run_site("empik", settings, self.backfill_args(recheck=True), {})
            self.assertEqual(resumed["downloaded"], 1)
            self.assertEqual(resumed["without_tags"], 1)
        self.assertEqual(db.path.read_bytes(), original)
        with closing(sqlite3.connect(recommendations_path(settings))) as conn:
            self.assertEqual(set(conn.execute("SELECT product_key,label FROM labels WHERE kind='tag'")),
                             {("1", "Nowy tag")})
            self.assertEqual(conn.execute("SELECT SUM(tags_checked) FROM titles").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT last_id FROM checkpoints WHERE mode='tags-recheck'").fetchone()[0], 0)

    def test_missing_categories_are_audited_and_selected_for_metadata_backfill(self):
        for site in ("empik", "lubimyczytac"):
            with self.subTest(site=site):
                db = self.database(site)
                data = parse(site, fixture(site), URLS[site])
                data.pop("category", None)
                data.pop("categories", None)
                db.save(data)
                self.assertEqual(db.audit()["category"], 1)
                rows = db.backfill_candidates(0, 10, supplemental=False, metadata=True)
                self.assertEqual(len(rows), 1)
                db.save({**data, "category": "Kryminał", "categories": ["Kryminał"]}, fill_book_id=rows[0].id)
                self.assertEqual(db.audit()["category"], 0)
                self.assertIn("Kryminał", str(db.stored_signals([rows[0].id])))

    def test_metadata_backfill_only_fills_missing_fields_and_existing_records(self):
        site = "storytel"
        db = self.database(site)
        url = URLS[site]
        db.save({"url": url, "title": "Zapisany tytuł", "authors": ["Zapisany autor"],
                 "duration_minutes": 0, "cover_url": "https://example.org/cover.jpg"})
        settings = self.backfill_settings(db)
        parsed = parse(site, fixture(site), url)
        with patch("main.fetch_book", return_value=parsed):
            report = run_site(site, settings, self.backfill_args(tags_only=False, url=url), {})
        self.assertEqual(report["updated"], 1)
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT title, duration_minutes, cover_url FROM books"
            ).fetchall(), [("Zapisany tytuł", 21, "https://example.org/cover.jpg")])
            self.assertEqual(conn.execute("SELECT name FROM authors").fetchall(), [("Zapisany autor",)])
            self.assertTrue(conn.execute("SELECT description FROM books").fetchone()[0])
        with closing(sqlite3.connect(recommendations_path(settings))) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM labels WHERE kind='tag'").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "Brak rekordu"):
            run_site(site, settings, self.backfill_args(url="https://www.storytel.com/pl/books/other-42"), {})
        self.assertEqual(db.stats()["books"], 1)

    def test_recommendation_export_reads_catalogue_and_archive_without_network(self):
        from crawl_state import Coverage

        site = "lubimyczytac"
        db = self.database(site)
        data = parse(site, fixture(site), URLS[site])
        data.update(series={"name": "Cykl lokalny"}, volume_number="2", categories=["Fantastyka"])
        db.save(data)
        settings = self.backfill_settings(db)
        with closing(sqlite3.connect(db.path)) as conn:
            conn.executescript("""
                CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE book_tags (book_id INTEGER, tag_id INTEGER);
                INSERT INTO tags VALUES (1,'Stary tag');
                INSERT INTO book_tags VALUES (1,1);
            """)
        with closing(Coverage(settings["coverage_database"], site, db.path)) as archive:
            archive.save_metadata({"url": data["url"], "categories": ["Urban fantasy"], "tags": ["Tag z archiwum"]})
        original = db.path.read_bytes()
        cached = settings["coverage_database"].read_bytes()
        with patch("main.fetch_book") as fetch, patch("main.Client") as client:
            tags_report = run_site(site, settings, self.backfill_args(), {})
            args = self.backfill_args(tags_only=False, recommendations=True, local_only=True)
            report = run_site(site, settings, args, {})
            fetch.assert_not_called()
            client.return_value.get.assert_not_called()
        self.assertEqual(tags_report["tagged"], 1)
        self.assertEqual(report["signals"], 1)
        self.assertEqual(report["downloaded"], 0)
        self.assertEqual(db.path.read_bytes(), original)
        self.assertEqual(settings["coverage_database"].read_bytes(), cached)
        with closing(sqlite3.connect(recommendations_path(settings))) as conn:
            self.assertEqual(conn.execute(
                "SELECT series,volume_number,recommendations_checked FROM titles"
            ).fetchone(), ("Cykl lokalny", "2", 0))
            self.assertEqual(set(conn.execute("SELECT kind,label FROM labels")), {
                ("tag", "Stary tag"), ("tag", "Tag z archiwum"),
                ("category", "Fantastyka"), ("category", "Urban fantasy"),
            })

    def test_online_recommendations_save_only_storytel_similar_cards(self):
        site = "storytel"
        db = self.database(site)
        url = URLS[site]
        db.save({"url": url, "title": "Zapisany tytuł"})
        original = db.path.read_bytes()
        settings = self.backfill_settings(db)
        cards = """<section data-card-list-section="true"><h2>Książki podobne do Test</h2>
            <ol><li><a href="/pl/books/podobna-100">Pierwsza</a></li>
            <li><a href="/pl/books/alias-100">Duplikat</a></li>
            <li><a href="/pl/books/druga-200">Druga</a></li>
            <li><a href="https://inne.example/pl/books/zla-300">Obcy serwis</a></li></ol></section>
            <section data-card-list-section="true"><h2>Popularne</h2>
            <ol><li><a href="/pl/books/popularna-400">Inna lista</a></li></ol></section>"""
        html = fixture(site).replace("Książki podobne", "Inna sekcja").replace("</body>", cards + "</body>")
        data = parse(site, html, url)
        args = self.backfill_args(tags_only=False, recommendations=True)
        with patch("main.fetch_book", return_value=data) as fetch:
            first = run_site(site, settings, args, {})
            second = run_site(site, settings, args, {})
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.kwargs, {"include_reviews": False})
            args.recheck = True
            refreshed = run_site(site, settings, args, {})
            self.assertEqual(refreshed["downloaded"], 1)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(first["signals"], 1)
        self.assertEqual(second["attempted"], 0)
        self.assertEqual(db.path.read_bytes(), original)
        with closing(sqlite3.connect(recommendations_path(settings))) as conn:
            self.assertEqual(conn.execute(
                "SELECT similar_key,position FROM similar_titles ORDER BY position"
            ).fetchall(), [("100", 1), ("200", 2)])

    def test_recommendation_details_keep_online_values_and_bookbeat_rating_counts(self):
        sidecar = Path(self.temp.name) / "recommendations.db"
        url = "https://www.bookbeat.com/pl/book/test-42"
        with closing(Recommendations(sidecar)) as store, store.conn:
            store.save("bookbeat", {
                "url": url, "language": "polski", "duration_minutes": 450,
                "rating_count": 25, "rating_scale": 5,
                "source_metadata": {
                    "rating": {"ratingValue": 4.7, "numberOfRatings": 600},
                    "narratingRating": {"ratingValue": 4.8, "numberOfRatings": 520},
                    "swiftWords": [{"translationKey": "swift-book-fantastic"}],
                    "narratingSwiftWords": [{"translationKey": "swift-narration-credible"}],
                },
            })
            store.save("bookbeat", {
                "url": url, "avg_rating": 3, "rating_count": 400, "categories": ["Fantastyka"],
            }, only_missing=True)
            self.assertEqual(store.conn.execute("""
                SELECT language,duration_minutes,avg_rating,rating_count,rating_scale,
                       narration_rating,narration_rating_count FROM title_details
            """).fetchone(), ("polski", 450, 4.7, 600, 5, 4.8, 520))
            self.assertEqual(set(store.conn.execute("SELECT kind,label FROM labels")), {
                ("category", "Fantastyka"), ("impression", "swift-book-fantastic"),
                ("narration_impression", "swift-narration-credible"),
            })

    def test_wolnelektury_preserves_literary_kind_and_epoch_as_separate_labels(self):
        url = "https://wolnelektury.pl/katalog/lektura/lalka"
        data = parse("wolnelektury", json.dumps({
            "url": url, "title": "Lalka", "media": [{"type": "mp3"}],
            "genres": [{"name": "Powieść"}], "epochs": [{"name": "Pozytywizm"}],
            "kinds": [{"name": "Epika"}],
        }), url)
        with closing(Recommendations(Path(self.temp.name) / "recommendations.db")) as store, store.conn:
            store.save("wolnelektury", data)
            self.assertEqual(set(store.conn.execute("SELECT kind,label FROM labels")), {
                ("category", "Powieść"), ("epoch", "Pozytywizm"), ("literary_kind", "Epika"),
            })

    def test_lc_alias_does_not_duplicate_external_id(self):
        db = self.database("lubimyczytac")
        data = parse("lubimyczytac", fixture("lubimyczytac"), URLS["lubimyczytac"])
        db.save(data)
        data["url"] = URLS["lubimyczytac"]
        db.save(data)
        self.assertEqual(db.stats()["books"], 1)
        self.assertEqual(db.enqueue([data["url"]]), 0)

    def test_interruption_and_live_discovery_recover_queue(self):
        db = self.database("storytel")
        self.assertEqual(db.enqueue([URLS["storytel"]]), 1)
        item = db.pending()[0]
        db.mark(item["id"], "processing")
        db.recover()
        self.assertEqual(len(db.pending()), 1)
        db.mark(item["id"], "archived_error", failed=True)
        self.assertEqual(db.enqueue([URLS["storytel"]]), 1)
        self.assertEqual(db.pending()[0]["retry_count"], 0)

    def test_failed_transaction_rolls_back_book_and_relations(self):
        db = self.database("storytel")
        data = parse("storytel", fixture("storytel"), URLS["storytel"])
        data["reviews"] = [{"username": "A", "text": "B", "rating": {"bad": 1}}]
        with self.assertRaises(Exception):
            db.save(data)
        self.assertEqual(db.stats()["books"], 0)
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0], 0)

    def test_backup_includes_wal_and_schema(self):
        db = self.database("audioteka")
        db.enable_writes()
        db.save(parse("audioteka", fixture("audioteka"), URLS["audioteka"]))
        directory = Path(self.temp.name) / "backups"
        db.backup(directory)
        target = directory / "audioteka.db"
        self.assertEqual(schema(target), schema(db.path))
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM books").fetchone()[0], 1)

    def test_empty_file_is_rejected(self):
        path = Path(self.temp.name) / "empty.db"
        sqlite3.connect(path).close()
        with self.assertRaises(ValueError):
            Database("audioteka", path)

    def test_missing_lc_cover_is_replaced_without_duplicate(self):
        db = self.database("lubimyczytac")
        data = parse("lubimyczytac", fixture("lubimyczytac"), URLS["lubimyczytac"])
        cover = {"source_url": data["cover_url"], "local_path": str(Path(self.temp.name) / "missing.jpg"), "sha256": "old"}
        db.save(data, cover)
        self.assertTrue(db.needs_cover(data["url"]))
        new_path = Path(self.temp.name) / "new.jpg"
        new_path.write_bytes(b"cover")
        cover.update(local_path=str(new_path), sha256="new")
        db.save(data, cover)
        self.assertFalse(db.needs_cover(data["url"]))
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM covers").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT sha256 FROM covers").fetchone()[0], "new")

    def test_inherited_metadata_gaps_are_audited_and_repaired(self):
        db = self.database("storytel")
        data = parse("storytel", fixture("storytel"), URLS["storytel"])
        full_data = dict(data)
        data.update(authors=[], publisher=None, duration_minutes=None, description=None)
        db.save(data)
        gaps = db.audit()
        self.assertEqual(gaps["incomplete_books"], 1)
        self.assertEqual(gaps["authors"], 1)
        urls, cursor = db.repair_urls(0, 100)
        self.assertEqual(urls, [data["url"]])
        self.assertGreater(cursor, 0)
        db.save(full_data)
        self.assertEqual(db.audit()["incomplete_books"], 0)

    def test_failed_cover_download_is_not_reported_as_verified(self):
        db = self.database("storytel")
        settings = load_settings(ROOT / "config.json")
        settings["coverage_database"] = Path(self.temp.name) / "coverage.db"
        settings["databases"]["storytel"] = db.path
        settings["backup"] = False
        args = SimpleNamespace(command="update", url=URLS["storytel"], resume=False, full_catalog=False,
                               no_backup=True, refresh_limit=0, limit=1, no_covers=False)
        data = parse("storytel", fixture("storytel"), URLS["storytel"])
        with patch("main.fetch_book", return_value=data), patch("main.download_cover", return_value=None):
            report = run_site("storytel", settings, args, {})
        self.assertEqual(report["incomplete"], 1)
        self.assertFalse(report["queue_complete"])
        self.assertEqual(report["coverage"]["products"]["incomplete"], 1)

    def test_metadata_gaps_do_not_stop_queue_and_are_deferred_on_resume(self):
        from crawl_state import Coverage
        for site in ("audioteka", "storytel", "empik"):
            with self.subTest(site=site):
                db = self.database(site)
                original_schema = schema(db.path)
                settings = load_settings(ROOT / "config.json")
                settings["coverage_database"] = Path(self.temp.name) / f"{site}_coverage.db"
                settings["databases"][site] = db.path
                settings["backup"] = False
                if site == "audioteka":
                    urls = [f"https://audioteka.com/pl/audiobook/test-{n}/" for n in range(7)]
                elif site == "empik":
                    urls = [f"https://www.empik.com/test,p{n+100},ebooki-i-mp3-p" for n in range(7)]
                else:
                    urls = [f"https://www.storytel.com/pl/books/test-{n+100}" for n in range(7)]
                db.enable_writes()
                db.enqueue(urls)
                args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                                       no_backup=True, refresh_limit=0, limit=0, no_covers=True)
                def fetch(client, current_site, url):
                    html = (FIXTURES / "empik_browser.html").read_text(encoding="utf-8") if site == "empik" else fixture(site)
                    data = parse(site, html, URLS[site])
                    data["url"] = url
                    if url != urls[-1]:
                        data["publisher"] = None
                    return data
                with patch("main.fetch_book", side_effect=fetch) as getter:
                    report = run_site(site, settings, args, {})
                    self.assertEqual(getter.call_count, 7)
                self.assertEqual(report["inserted"], 7)
                self.assertEqual(report["incomplete"], 6)
                self.assertEqual(report["errors"], [])
                self.assertEqual(len(report["incomplete_samples"]), 6)
                self.assertEqual(report["after"]["queue"].get("incomplete"), 6)
                self.assertEqual(report["coverage"]["products"].get("verified"), 1)
                self.assertFalse(report["complete"])
                coverage = Coverage(settings["coverage_database"], site, db.path)
                try:
                    self.assertFalse(coverage.due(urls[0]))
                finally:
                    coverage.close()
                with patch("main.fetch_book") as getter:
                    resumed = run_site(site, settings, args, {})
                    getter.assert_not_called()
                self.assertEqual(resumed["errors"], [])
                self.assertEqual(original_schema, schema(db.path))

    def test_five_transport_errors_still_stop_service(self):
        db = self.database("storytel")
        urls = [f"https://www.storytel.com/pl/books/test-{n+100}" for n in range(7)]
        db.enable_writes()
        db.enqueue(urls)
        settings = load_settings(ROOT / "config.json")
        settings["coverage_database"] = Path(self.temp.name) / "coverage.db"
        settings["databases"]["storytel"] = db.path
        settings["backup"] = False
        args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                               no_backup=True, refresh_limit=0, limit=0, no_covers=True)
        with patch("main.fetch_book", side_effect=HTTPError(urls[0], 503)) as getter:
            report = run_site("storytel", settings, args, {})
        self.assertEqual(getter.call_count, 5)
        self.assertIn("5 kolejnych błędach", report["errors"][-1])
        self.assertTrue(report["stopped_on_errors"])
        self.assertEqual(report["inserted"], 0)
        self.assertFalse(report["complete"])

    def test_existing_review_rating_and_date_are_corrected(self):
        db = self.database("audioteka")
        data = parse("audioteka", fixture("audioteka"), URLS["audioteka"])
        data["reviews"] = [{"user": "A", "text": "Review", "stars": 5}]
        db.save(data)
        data["reviews"] = [{"user": "A", "text": "Review", "stars": 3, "date": "2026-10-10"}]
        db.save(data)
        with closing(sqlite3.connect(db.path)) as conn:
            self.assertEqual(conn.execute("SELECT rating,date FROM reviews").fetchall(), [(3, "2026-10-10")])

    def test_lc_ajax_retries_transient_network_failure(self):
        settings = load_settings(ROOT / "config.json")
        client = Client(settings)
        try:
            response = SimpleNamespace(status_code=200, text='{"data":{}}', content=b'{"data":{}}', url="https://lubimyczytac.pl/book/standardFilteredDataPageContent")
            with patch.object(client.session, "post", side_effect=[TimeoutError("network timeout"), response]) as post, patch.object(client, "throttle"), patch("http_client.time.sleep"):
                self.assertEqual(client.post_catalog(response.url, {}).status_code, 200)
                self.assertEqual(post.call_count, 2)
        finally:
            client.close()

    def test_cli_update_url_targets_requested_book_and_preserves_schema(self):
        db = self.database("storytel")
        original_schema = schema(db.path)
        db.enqueue(["https://www.storytel.com/pl/books/other-1"])
        settings = load_settings(ROOT / "config.json")
        settings["coverage_database"] = Path(self.temp.name) / "coverage.db"
        settings["databases"]["storytel"] = db.path
        settings["backup"] = False
        args = SimpleNamespace(command="update", url=URLS["storytel"], resume=False, full_catalog=False,
                               no_backup=True, refresh_limit=0, limit=1, no_covers=True)
        data = parse("storytel", fixture("storytel"), URLS["storytel"])
        with patch("main.fetch_book", return_value=data):
            report = run_site("storytel", settings, args, {})
        self.assertEqual(report["inserted"], 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(db.pending()[0]["url"], "https://www.storytel.com/pl/books/other-1")
        self.assertEqual(original_schema, schema(db.path))
        with closing(sqlite3.connect(recommendations_path(settings))) as conn:
            self.assertEqual(conn.execute("SELECT label FROM labels WHERE kind='tag'").fetchall(), [("Rozwój kariery",)])

    def test_diagnose_does_not_mutate_database(self):
        db = self.database("audioteka")
        before = db.path.read_bytes()
        settings = load_settings(ROOT / "config.json")
        settings["coverage_database"] = Path(self.temp.name) / "coverage.db"
        settings["databases"]["audioteka"] = db.path
        args = SimpleNamespace(command="diagnose", url=URLS["audioteka"], resume=False, samples=1)
        data = parse("audioteka", fixture("audioteka"), URLS["audioteka"])
        with patch("main.fetch_book", return_value=data):
            report = run_site("audioteka", settings, args, {})
        self.assertEqual(report["errors"], [])
        self.assertEqual(before, db.path.read_bytes())

    def test_service_failure_does_not_stop_remaining_services(self):
        directory = Path(self.temp.name)
        args = SimpleNamespace(command="diagnose", services=["audioteka", "storytel"])
        def fake_run(site, *arguments):
            if site == "audioteka":
                raise RuntimeError("test outage")
            return {"site": site, "errors": [], "warnings": [], "new_candidates": 1}
        with patch("main.ROOT", directory), patch("main.run_site", side_effect=fake_run) as runner, patch("main.log"):
            self.assertEqual(run_cycle({}, args), 1)
        self.assertEqual(runner.call_count, 2)

    def test_audioteka_uuid_alias_updates_existing_book_and_survives_restart(self):
        from crawl_state import Coverage
        alias = "https://audioteka.com/pl/audiobook/7a8a297f-e998-4f6d-8521-e9deb9992b68/"
        target = "https://audioteka.com/pl/audiobook/roj-dronow-odcinek-1/"
        for saved_url in (target, alias):
            with self.subTest(saved_url=saved_url):
                db = self.database("audioteka")
                data = parse("audioteka", fixture("audioteka"), URLS["audioteka"])
                data.update(url=saved_url, publisher="P")
                db.save(data)
                db.enable_writes()
                db.enqueue([alias], refresh=True)
                settings = load_settings(ROOT / "config.json")
                settings["coverage_database"] = Path(self.temp.name) / "aliases.db"
                settings["databases"]["audioteka"] = db.path
                settings["backup"] = False
                args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                                       no_backup=True, refresh_limit=0, limit=0, no_covers=True)
                data["url"] = target
                with patch("main.fetch_book", return_value=data):
                    report = run_site("audioteka", settings, args, {})
                self.assertEqual(report["inserted"], 0)
                self.assertEqual(report["updated"], 1)
                self.assertEqual(report["after"]["books"], 1)
                self.assertEqual(report["errors"], [])
                self.assertEqual(report["coverage"]["products"].get("redirected"), 1)
                restarted = Database("audioteka", db.path)
                ledger = Coverage(settings["coverage_database"], "audioteka", db.path)
                try:
                    ledger.apply_aliases(restarted.known)
                    self.assertEqual(restarted.known[identity("audioteka", alias)], restarted.known[identity("audioteka", target)])
                    self.assertEqual(restarted.enqueue([alias, target]), 0)
                finally:
                    ledger.close()
                    restarted.close()
                db.close()
                db.path.unlink()
                settings["coverage_database"].unlink()

    def test_incomplete_alias_retries_target_without_leaving_alias_incomplete(self):
        alias = "https://audioteka.com/pl/audiobook/7a8a297f-e998-4f6d-8521-e9deb9992b68/"
        target = "https://audioteka.com/pl/audiobook/roj-dronow-odcinek-1/"
        db = self.database("audioteka")
        db.enable_writes()
        db.enqueue([alias])
        settings = load_settings(ROOT / "config.json")
        settings["coverage_database"] = Path(self.temp.name) / "aliases.db"
        settings["databases"]["audioteka"] = db.path
        settings["backup"] = False
        args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                               no_backup=True, refresh_limit=0, limit=0, no_covers=True)
        data = parse("audioteka", fixture("audioteka"), URLS["audioteka"])
        data.update(url=target, publisher=None)
        with patch("main.fetch_book", return_value=data):
            report = run_site("audioteka", settings, args, {})
        self.assertEqual(report["incomplete"], 1)
        self.assertEqual(report["after"]["queue"].get("completed"), 1)
        self.assertEqual([item["url"] for item in db.pending()], [target])
        with patch("main.fetch_book") as getter:
            run_site("audioteka", settings, args, {})
            getter.assert_not_called()
        import time
        future = time.time()+7200
        data["publisher"] = "P"
        with patch("crawl_state.time.time", return_value=future), patch("main.fetch_book", return_value=data):
            done = run_site("audioteka", settings, args, {})
        self.assertEqual(done["after"]["books"], 1)
        self.assertEqual(done["after"]["queue"].get("completed"), 2)
        self.assertEqual(done["incomplete"], 0)
        self.assertEqual(done["errors"], [])
        self.assertEqual(done["coverage"]["unresolved"], 0)

    def test_resume_only_processes_existing_queue(self):
        db = self.database("storytel")
        data = parse("storytel", fixture("storytel"), URLS["storytel"])
        db.save(data)
        settings = load_settings(ROOT / "config.json")
        settings["coverage_database"] = Path(self.temp.name) / "coverage.db"
        settings["databases"]["storytel"] = db.path
        settings["backup"] = False
        args = SimpleNamespace(command="update", url=None, resume=True, full_catalog=False,
                               no_backup=True, refresh_limit=None, limit=0, no_covers=True)
        with patch("main.fetch_book") as fetch:
            report = run_site("storytel", settings, args, {})
        self.assertEqual(report["queued_refresh"], 0)
        fetch.assert_not_called()

    def test_lock_rejects_second_instance(self):
        path = Path(self.temp.name) / "scraper.lock"
        with run_lock(path):
            with self.assertRaises(RuntimeError):
                with run_lock(path):
                    self.fail("Second instance unexpectedly obtained the lock")


if __name__ == "__main__":
    unittest.main()
