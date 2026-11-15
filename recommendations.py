"""Additional catalogue signals keyed by service and stable product identifier."""

import json
import sqlite3
from pathlib import Path

from parse_books import names
from urls import canonical, identity
from recommendation_parsers import normalized_link


LABEL_FIELDS = {"tags": "tag", "categories": "category", "epochs": "epoch", "literary_kinds": "literary_kind", "content_types": "content_type"}
DETAIL_FIELDS = ("language", "duration_minutes", "avg_rating", "rating_count", "rating_scale",
                 "narration_rating", "narration_rating_count")
ARCHIVE_FIELDS = (*LABEL_FIELDS, *DETAIL_FIELDS, "category", "series", "volume_number",
                  "related_urls", "related_links", "related_readings_url", "recommendation_coverage", "collections", "series_position", "source_metadata")


def recommendations_path(settings):
    return Path(settings.get("recommendations_database") or Path(settings["coverage_database"]).with_name("recommendations.db"))


def merge_signals(local, archived, *, fresh=False):
    """Combine source labels; prefer explicit series information over inferred collections."""
    data = {**local, **{key: value for key, value in archived.items() if value not in (None, "", [], {})}}
    for field in LABEL_FIELDS:
        data[field] = (names(archived[field]) if fresh and field in archived else
                       list(dict.fromkeys(names(local.get(field)) + names(archived.get(field)))))
    if fresh and ("categories" in archived or "category" in archived):
        data["category"] = archived.get("category")
        data["categories"] = names(names(archived.get("categories")) + names(archived.get("category")))
    else:
        data["categories"] = names(data["categories"] + names(local.get("category")) + names(archived.get("category")))
    if fresh:
        for field in ("related_links", "related_urls"):
            if field in archived:
                data[field] = archived[field]
    if not data.get("series") and data.get("series_position"):
        data["series"] = (names(data.get("collections")) or [None])[0]
    return data


class Recommendations:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=60)
        try:
            tables = {name for (name,) in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if tables - {"titles", "labels", "similar_titles", "checkpoints", "title_details", "related_links", "recommendation_coverage"}:
                raise sqlite3.DatabaseError(f"Plik ma inny schemat niż baza rekomendacji: {path}")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS titles (
                    site TEXT NOT NULL, product_key TEXT NOT NULL, url TEXT NOT NULL,
                    series TEXT, volume_number TEXT,
                    tags_checked INTEGER NOT NULL DEFAULT 0,
                    recommendations_checked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(site, product_key)
                );
                CREATE TABLE IF NOT EXISTS labels (
                    site TEXT NOT NULL, product_key TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
                    PRIMARY KEY(site, product_key, kind, label),
                    FOREIGN KEY(site, product_key) REFERENCES titles(site, product_key) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_labels_kind_label ON labels(kind, label);
                CREATE TABLE IF NOT EXISTS similar_titles (
                    site TEXT NOT NULL, product_key TEXT NOT NULL,
                    similar_key TEXT NOT NULL, similar_url TEXT NOT NULL, position INTEGER NOT NULL,
                    PRIMARY KEY(site, product_key, similar_key),
                    FOREIGN KEY(site, product_key) REFERENCES titles(site, product_key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS related_links (
                    site TEXT NOT NULL, product_key TEXT NOT NULL, target_url TEXT NOT NULL,
                    target_key TEXT, relation_kind TEXT NOT NULL, position INTEGER NOT NULL,
                    title TEXT, source_heading TEXT, audio_available INTEGER,
                    PRIMARY KEY(site, product_key, target_url),
                    FOREIGN KEY(site, product_key) REFERENCES titles(site, product_key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS recommendation_coverage (
                    site TEXT NOT NULL, product_key TEXT NOT NULL, source_url TEXT,
                    scope TEXT NOT NULL, complete INTEGER, collected INTEGER NOT NULL, next_url TEXT,
                    PRIMARY KEY(site, product_key),
                    FOREIGN KEY(site, product_key) REFERENCES titles(site, product_key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS title_details (
                    site TEXT NOT NULL, product_key TEXT NOT NULL,
                    language TEXT, duration_minutes INTEGER, avg_rating REAL,
                    rating_count INTEGER, rating_scale REAL, narration_rating REAL, narration_rating_count INTEGER,
                    PRIMARY KEY(site, product_key),
                    FOREIGN KEY(site, product_key) REFERENCES titles(site, product_key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    site TEXT NOT NULL, database_path TEXT NOT NULL, mode TEXT NOT NULL, last_id INTEGER NOT NULL,
                    PRIMARY KEY(site, database_path, mode)
                );
            """)
        except BaseException:
            self.conn.close()
            raise

    def save(self, site, data, *, tags_checked=False, recommendations_checked=False, only_missing=False, replace_labels=False):
        url = canonical(site, data["url"])
        key = identity(site, url) if url else None
        if not key:
            raise ValueError("Brak identyfikatora książki dla danych rekomendacji")
        prior = self.conn.execute("SELECT tags_checked FROM titles WHERE site=? AND product_key=?", (site, key)).fetchone()
        series = (names(data.get("series")) or [None])[0]
        volume = data.get("volume_number")
        volume = (str(volume).strip() or None) if volume is not None else None
        labels = {
            kind: names(data.get(field))
            for field, kind in LABEL_FIELDS.items()
        }
        labels["category"] = list(dict.fromkeys(labels["category"] + names(data.get("category"))))
        details = {field: data.get(field) for field in DETAIL_FIELDS}
        details["language"] = (names(details["language"]) or [None])[0]
        if site == "bookbeat":
            source = data.get("source_metadata")
            source = source if isinstance(source, dict) else {}
            for kind, field in (("impression", "swiftWords"), ("narration_impression", "narratingSwiftWords")):
                labels[kind] = [item["translationKey"] for item in source.get(field) or []
                                if isinstance(item, dict) and isinstance(item.get("translationKey"), str)]
            for rating_field, count_field, source_field in (
                ("avg_rating", "rating_count", "rating"),
                ("narration_rating", "narration_rating_count", "narratingRating"),
            ):
                rating = source.get(source_field)
                if not isinstance(rating, dict):
                    continue
                if rating.get("ratingValue") is not None:
                    details[rating_field] = rating["ratingValue"]
                if rating.get("numberOfRatings") is not None:
                    details[count_field] = rating["numberOfRatings"]
        details = {field: value for field, value in details.items()
                   if isinstance(value, (str, int, float)) and value != ""
                   and (field != "duration_minutes" or value != 0)}
        related = {}
        for item in data.get("related_urls") or []:
            target = identity(site, item)
            if target and target != key:
                related.setdefault(target, canonical(site, item))

        links = {}
        for raw in data.get("related_links") or []:
            item = normalized_link(site, raw)
            if item and item["target_key"] != key:
                links.setdefault(item["target_key"] or item["url"], item)
        if "related_links" in data:
            related = {item["target_key"]: item["url"] for item in links.values() if item["target_key"]}
        has_signals = bool(series or volume or details or related or links or any(labels.values()))
        if not (has_signals or tags_checked or recommendations_checked):
            return False

        def coalesce(table, field):
            incoming, existing = f"excluded.{field}", f"{table}.{field}"
            first, second = (existing, incoming) if only_missing else (incoming, existing)
            return f"COALESCE({first},{second})"

        self.conn.execute(f"""INSERT INTO titles VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(site, product_key) DO UPDATE SET
                url=excluded.url,
                series={coalesce("titles", "series")},
                volume_number={coalesce("titles", "volume_number")},
                tags_checked=MAX(titles.tags_checked,excluded.tags_checked),
                recommendations_checked=MAX(titles.recommendations_checked,excluded.recommendations_checked)""",
            (site, key, url, series, volume,
             int(tags_checked), int(recommendations_checked)))
        for kind, values in labels.items():
            if only_missing and ((kind == "tag" and prior and prior[0]) or self.conn.execute(
                "SELECT 1 FROM labels WHERE site=? AND product_key=? AND kind=? LIMIT 1", (site, key, kind)
            ).fetchone()):
                continue
            explicit_tags = kind == "tag" and "tags" in data
            replace_kind = replace_labels is True or (replace_labels and kind in replace_labels)
            if replace_kind and not only_missing and (values or explicit_tags):
                self.conn.execute("DELETE FROM labels WHERE site=? AND product_key=? AND kind=?", (site, key, kind))
            self.conn.executemany("INSERT OR IGNORE INTO labels VALUES (?,?,?,?)",
                                  ((site, key, kind, label) for label in values))
        if details:
            columns = ",".join(details)
            slots = ",".join("?" for _ in details)
            assignments = ",".join(f"{field}={coalesce('title_details', field)}" for field in details)
            self.conn.execute(f"""INSERT INTO title_details(site,product_key,{columns}) VALUES (?,?,{slots})
                ON CONFLICT(site,product_key) DO UPDATE SET {assignments}""", (site, key, *details.values()))
        if (related or "related_links" in data) and not (only_missing and self.conn.execute(
            "SELECT 1 FROM similar_titles WHERE site=? AND product_key=? LIMIT 1", (site, key)
        ).fetchone()):
            self.conn.execute("DELETE FROM similar_titles WHERE site=? AND product_key=?", (site, key))
            self.conn.executemany("INSERT INTO similar_titles VALUES (?,?,?,?,?)",
                                  ((site, key, target, target_url, position)
                                   for position, (target, target_url) in enumerate(related.items(), 1)))
        coverage = data.get("recommendation_coverage")
        if isinstance(coverage, dict) and not coverage.get("error") and "scope" in coverage:
            insert = "INSERT OR IGNORE" if only_missing else "INSERT OR REPLACE"
            complete = coverage.get("complete")
            self.conn.execute(f"{insert} INTO recommendation_coverage VALUES (?,?,?,?,?,?,?)",
                              (site, key, coverage.get("source_url"), coverage["scope"],
                               int(complete) if isinstance(complete, bool) else None,
                               int(coverage.get("collected", 0)), coverage.get("next_url")))
        if "related_links" in data and not (only_missing and self.conn.execute(
            "SELECT 1 FROM related_links WHERE site=? AND product_key=? LIMIT 1", (site, key)
        ).fetchone()):
            self.conn.execute("DELETE FROM related_links WHERE site=? AND product_key=?", (site, key))
            self.conn.executemany("""INSERT INTO related_links
                (site,product_key,target_url,target_key,relation_kind,position,title,source_heading,audio_available)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                ((site, key, item["url"], item["target_key"], item["relation_kind"], position,
                  item["title"], item["source_heading"], item["audio_available"])
                 for position, item in enumerate(links.values(), 1)))
        return has_signals

    def checked_keys(self, site, *, tags_only):
        column = "tags_checked" if tags_only else "recommendations_checked"
        return {key for (key,) in self.conn.execute(f"SELECT product_key FROM titles WHERE site=? AND {column}=1", (site,))}

    def checkpoint(self, db, mode):
        row = self.conn.execute("SELECT last_id FROM checkpoints WHERE site=? AND database_path=? AND mode=?",
                                (db.site, str(db.path), mode)).fetchone()
        return row[0] if row else 0

    def set_checkpoint(self, db, mode, last_id):
        self.conn.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?)", (db.site, str(db.path), mode, last_id))

    def close(self):
        self.conn.close()


class ArchivedSignals:
    """Read the existing coverage cache without creating or altering it."""

    def __init__(self, path, site):
        path = Path(path)
        isolated = path.parent / site / path.name
        if path.parent.name == site:
            path = path if path.is_file() else path.parent.parent / path.name
        elif isolated.is_file():
            path = isolated
        self.site = site
        self.conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30) if path.is_file() else None
        try:
            self.tables = {name for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} if self.conn else set()
        except BaseException:
            self.close()
            raise

    def get(self, keys):
        if not keys or "metadata" not in self.tables:
            return {}
        paths = ",".join(f"'$.{field}'" for field in ARCHIVE_FIELDS)
        slots = ",".join("?" for _ in keys)
        rows = self.conn.execute(f"""SELECT product_key,json_extract(data,{paths}) FROM metadata
            WHERE site=? AND product_key IN ({slots}) AND json_valid(data)""", (self.site, *keys))
        return {key: dict(zip(ARCHIVE_FIELDS, json.loads(raw))) for key, raw in rows}

    def empty_tag_keys(self):
        if "empty_tag_checks" not in self.tables:
            return set()
        return {key for (key,) in self.conn.execute("SELECT product_key FROM empty_tag_checks WHERE site=?", (self.site,))}

    def close(self):
        if self.conn:
            self.conn.close()
