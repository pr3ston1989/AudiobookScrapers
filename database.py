"""Schema-preserving SQLite operations with named SQLAlchemy parameters."""

import hashlib
import io
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import bindparam, create_engine, event, text
from sqlalchemy.engine import URL

from urls import canonical, identity
from events import check_stop
from sources import TAG_SITES
from recommendations import DETAIL_FIELDS

log = logging.getLogger(__name__)


class Database:
    def __init__(self, site, path, review_index=None, *, read_only=False):
        self.site = site
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"Brak istniejącej bazy: {self.path}. Popraw config.json.")
        if read_only:
            self.engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, timeout=60
            ))
        else:
            self.engine = create_engine(URL.create("sqlite", database=str(self.path)), connect_args={"timeout": 60})
        @event.listens_for(self.engine, "connect")
        def configure_connection(dbapi_connection, connection_record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
            dbapi_connection.execute("PRAGMA busy_timeout=60000")
        with self.engine.connect() as conn:
            self.tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
            self.columns = {
                table: {r[1] for r in conn.execute(text(f'PRAGMA table_info("{table}")'))}
                for table in self.tables
            }
        if not {"books", "scrape_queue"}.issubset(self.tables):
            self.engine.dispose()
            raise ValueError(f"Baza {self.path} jest pusta lub ma inny schemat.")
        self.review_index = None
        if review_index and "reviews" in self.tables:
            from review_index import ReviewIndex
            self.review_index = ReviewIndex(review_index, self.path, site)
        self.known = {}
        with self.engine.connect() as conn:
            fields = "id,external_id" if site == "lubimyczytac" else "id,url"
            for row in conn.execute(text(f"SELECT {fields} FROM books")):
                key = str(row.external_id) if site == "lubimyczytac" else identity(site, row.url)
                if key:
                    self.known[key] = row.id

    def backup(self, folder):
        """Use SQLite backup to include committed WAL contents without copying sidecars."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{self.site}.db"
        metadata = folder / f"{self.site}.source"
        today = datetime.now().date()
        same_source = metadata.exists() and metadata.read_text(encoding="utf-8") == str(self.path)
        if same_source and target.exists() and datetime.fromtimestamp(target.stat().st_mtime).date() == today:
            return
        temp = target.with_suffix(".db.tmp")
        log.info("[%s] Kopia bezpieczeństwa: %s", self.site, target)
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(temp)) as dest:
                    source.backup(dest, pages=2048, sleep=0.05, progress=lambda *args: check_stop())
            temp.replace(target)
            metadata.write_text(str(self.path), encoding="utf-8")
        finally:
            if temp.exists():
                temp.unlink()

    def enable_writes(self):
        with self.engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))

    def stats(self):
        with self.engine.connect() as conn:
            return {
                "database": str(self.path),
                "books": conn.execute(text("SELECT COUNT(*) FROM books")).scalar_one(),
                "queue": dict(conn.execute(text("SELECT status, COUNT(*) FROM scrape_queue GROUP BY status")).all()),
            }

    def recover(self):
        """Recover interrupted tasks and eligible legacy transient errors once per run."""
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE scrape_queue SET status=:pending WHERE status=:processing OR (status=:error AND retry_count<3)"),
                         {"pending": "pending", "processing": "processing", "error": "error"})

    def missing_conditions(self):
        """Return schema-aware checks for metadata required by current parsers."""
        checks = {"title": "COALESCE(TRIM(b.title),'')=''", "description": "COALESCE(TRIM(b.description),'')=''",
                  "authors": "NOT EXISTS (SELECT 1 FROM book_authors a WHERE a.book_id=b.id)"}
        if self.site == 'wolnelektury':
            # API wielu utworów nie publikuje nawet fragmentu opisu.
            checks.pop('description')
        if "cover_url" in self.columns["books"]:
            checks["cover_url"] = "COALESCE(TRIM(b.cover_url),'')=''"
        else:
            checks["cover_url"] = "NOT EXISTS (SELECT 1 FROM covers c WHERE c.book_id=b.id AND COALESCE(c.source_url,'')<>'')"
        if self.site != "lubimyczytac":
            audio = "COALESCE(LOWER(b.type),'') NOT IN ('ebook','e-book')"
            checks["duration_minutes"] = f"({audio} AND COALESCE(b.duration_minutes,0)<=0)"
            if self.site != "wolnelektury":
                checks["publisher"] = f"({audio} AND b.publisher_id IS NULL)"
        return checks

    def audit(self):
        """Count inherited metadata gaps instead of assuming existing rows are complete."""
        checks = self.missing_conditions()
        columns = ",".join(f'SUM(CASE WHEN {condition} THEN 1 ELSE 0 END) AS "{field}"' for field, condition in checks.items())
        overall = " OR ".join(f"({condition})" for condition in checks.values())
        with self.engine.connect() as conn:
            row = conn.execute(text(f"SELECT {columns},SUM(CASE WHEN {overall} THEN 1 ELSE 0 END) AS incomplete_books FROM books b")).mappings().one()
        return {key: value or 0 for key, value in row.items()}

    def repair_urls(self, after_id, limit, wrap=True):
        """Walk inherited incomplete rows in bounded batches without changing schemas."""
        if limit <= 0:
            return [], after_id
        conditions = " OR ".join(f"({value})" for value in self.missing_conditions().values())
        fields = "b.id,b.url,b.external_id" if self.site == "lubimyczytac" else "b.id,b.url"
        query = text(f"SELECT {fields} FROM books b WHERE b.id>:after AND ({conditions}) ORDER BY b.id LIMIT :limit")
        with self.engine.connect() as conn:
            rows = conn.execute(query, {"after": after_id, "limit": limit}).all()
            if not rows and after_id and wrap:
                rows = conn.execute(query, {"after": 0, "limit": limit}).all()
        return [self.book_url(row) for row in rows], rows[-1].id if rows else (0 if wrap else after_id)

    def backfill_candidates(self, after_id, limit, *, supplemental, metadata, book_id=None):
        checks = self.missing_conditions() if metadata else {}
        missing = " OR ".join(f"({condition})" for condition in checks.values()) or "0"
        external = ",b.external_id" if self.site == "lubimyczytac" else ""
        missing_cover_row = "0"
        if metadata and "covers" in self.tables:
            missing_cover_row = "NOT EXISTS (SELECT 1 FROM covers c WHERE c.book_id=b.id)"
        scope = "b.id=:book_id" if book_id is not None else "b.id>:after"
        query = text(f"""SELECT * FROM (
            SELECT b.id,b.url{external}, ({missing}) AS missing_metadata,
                   ({missing_cover_row}) AS missing_cover_row
            FROM books b WHERE {scope}
        ) WHERE missing_metadata OR :supplemental ORDER BY id LIMIT :limit""")
        with self.engine.connect() as conn:
            return conn.execute(query, {"after": after_id, "limit": limit, "book_id": book_id,
                                        "supplemental": supplemental}).all()

    def stored_signals(self, ids):
        """Read only the recommendation fields present in the source schema."""
        data = {book_id: {} for book_id in ids}
        if not ids:
            return data
        columns = self.columns["books"]
        fields = ["b.id"]
        joins = []
        fields.extend(f"b.{field}" for field in ("volume_number", *DETAIL_FIELDS) if field in columns)
        for field, table in (("category", "categories"), ("series", "series")):
            if field + "_id" in columns and table in self.tables:
                joins.append(f"LEFT JOIN {table} AS {field} ON {field}.id=b.{field}_id")
                fields.append(f"{field}.name AS {field}")
        with self.engine.connect() as conn:
            stmt = text(f"SELECT {','.join(fields)} FROM books b {' '.join(joins)} WHERE b.id IN :ids")
            for row in conn.execute(stmt.bindparams(bindparam("ids", expanding=True)), {"ids": ids}).mappings():
                data[row["id"]].update({field: value for field, value in row.items()
                                        if field != "id" and value not in (None, "")})
            relations = [("tags", "tag_id"), ("categories", "category_id")]
            if self.site in ("bookbeat", "legimi"):
                relations.append(("collections", "collection_id"))
            for field, foreign in relations:
                if not {field, f"book_{field}"}.issubset(self.tables):
                    continue
                stmt = text(f"""SELECT link.book_id, item.name FROM book_{field} link
                    JOIN {field} item ON item.id=link.{foreign} WHERE link.book_id IN :ids""")
                for book_id, name in conn.execute(stmt.bindparams(bindparam("ids", expanding=True)), {"ids": ids}):
                    data[book_id].setdefault(field, []).append(name)
        for signals in data.values():
            collections = signals.pop("collections", [])
            if len(collections) == 1 and not signals.get("series"):
                signals["series"] = collections[0]
        return data

    def last_book_id(self):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT COALESCE(MAX(id),0) FROM books")).scalar_one()

    def enqueue(self, urls, refresh=False, priority=50):
        """Queue missing identities and revive discovered live URLs lacking saved books."""
        added = 0
        with self.engine.begin() as conn:
            seen = set()
            for url in urls:
                key = identity(self.site, url)
                if key is None or key in seen or (key in self.known and not refresh):
                    continue
                seen.add(key)
                url = canonical(self.site, url)
                item = conn.execute(text("SELECT id,status FROM scrape_queue WHERE url=:url"), {"url": url}).first()
                if item:
                    if item.status != "pending":
                        conn.execute(text("UPDATE scrape_queue SET status=:status,retry_count=0,priority=:priority WHERE id=:id"),
                                     {"status": "pending", "priority": priority, "id": item.id})
                        added += 1
                else:
                    fields = {"url": url, "type": "book", "status": "pending", "priority": priority, "retry_count": 0}
                    if "depth" in self.columns["scrape_queue"]:
                        fields["depth"] = 0
                    self.insert(conn, "scrape_queue", fields)
                    added += 1
        return added

    def refresh_urls(self, after_id, limit):
        """Walk existing books in bounded batches and wrap around on the next run."""
        if limit <= 0:
            return [], after_id
        with self.engine.connect() as conn:
            fields = "id,url,external_id" if self.site == "lubimyczytac" else "id,url"
            rows = conn.execute(text(f"SELECT {fields} FROM books WHERE id>:after ORDER BY id LIMIT :limit"),
                                {"after": after_id, "limit": limit}).all()
            if not rows:
                rows = conn.execute(text(f"SELECT {fields} FROM books ORDER BY id LIMIT :limit"), {"limit": limit}).all()
        return [self.book_url(row) for row in rows], rows[-1].id if rows else after_id

    def book_url(self, row):
        """Use LC's primary external ID when inherited URL paths disagree."""
        if self.site == "lubimyczytac" and identity(self.site, row.url or "") != str(row.external_id):
            return f"https://lubimyczytac.pl/ksiazka/{row.external_id}/a"
        return row.url

    def pending(self, limit=0):
        """Snapshot IDs so failures are retried on the next invocation, not in a hot loop."""
        sql = "SELECT id,url,type,retry_count FROM scrape_queue WHERE status=:status ORDER BY priority DESC,id"
        params = {"status": "pending"}
        if limit:
            sql += " LIMIT :limit"
            params["limit"] = limit
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    def mark(self, item_id, status, failed=False):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE scrape_queue SET status=:status,retry_count=retry_count+:increment WHERE id=:id"),
                         {"status": status, "increment": int(failed), "id": item_id})

    def id_gap_urls(self, start, target, budget):
        """Probe bounded LC ID ranges, skipping known books and legacy confirmed 404s."""
        end = min(target, start + budget)
        urls = [f"https://lubimyczytac.pl/ksiazka/{number}/a" for number in range(start + 1, end + 1)
                if str(number) not in self.known]
        if urls:
            params = {f"url{i}": url for i, url in enumerate(urls)}
            binds = ",".join(":" + key for key in params)
            with self.engine.connect() as conn:
                archived = {row.url for row in conn.execute(text(f"SELECT url FROM scrape_queue WHERE status='archived_error' AND url IN ({binds})"), params)}
            urls = [url for url in urls if url not in archived]
        return urls, end

    @staticmethod
    def insert(conn, table, values):
        """Insert trusted internal column names with bound values."""
        columns = ",".join(f'"{key}"' for key in values)
        binds = ",".join(f":{key}" for key in values)
        return conn.execute(text(f'INSERT INTO "{table}" ({columns}) VALUES ({binds})'), values).lastrowid

    def name_id(self, conn, table, name):
        row = conn.execute(text(f'SELECT id FROM "{table}" WHERE name=:name'), {"name": name}).first()
        return row.id if row else self.insert(conn, table, {"name": name})

    def save_links(self, conn, book_id, data, only_missing):
        changed = False
        for field, table, junction, foreign in (
            ("authors", "authors", "book_authors", "author_id"),
            ("narrators", "narrators", "book_narrators", "narrator_id"),
            ("collections", "collections", "book_collections", "collection_id"),
            ("categories", "categories", "book_categories", "category_id"),
            ("tags", "tags", "book_tags", "tag_id"),
        ):
            if field == "tags" and self.site in TAG_SITES:
                continue
            if junction not in self.tables or table not in self.tables:
                continue
            names = {n.get("name") if isinstance(n, dict) else n for n in (data.get(field) or [])}
            names.discard(None)
            names.discard("")
            if not names:
                continue
            params = {"book": book_id}
            if only_missing and field != "tags" and conn.execute(
                text(f'SELECT 1 FROM "{junction}" WHERE book_id=:book LIMIT 1'), params
            ).first():
                continue
            for name in sorted(names):
                entity_id = self.name_id(conn, table, name)
                row = conn.execute(text(f'SELECT 1 FROM "{junction}" WHERE book_id=:book AND "{foreign}"=:entity'),
                                   {**params, "entity": entity_id}).first()
                if not row:
                    link = {"book_id": book_id, foreign: entity_id}
                    if "role" in self.columns.get(junction, ()):
                        link["role"] = "author"
                    self.insert(conn, junction, link)
                    changed = True
        return changed

    def save(self, data, cover=None, *, fill_book_id=None):
        """Upsert a product, or fill gaps in a specific existing book."""
        key = identity(self.site, data["url"])
        only_missing = fill_book_id is not None
        if key is None or (not only_missing and not data.get("title")):
            raise ValueError("Brak identyfikatora lub tytułu książki")
        existing_id = self.known.get(key)
        with self.engine.begin() as conn:
            if only_missing:
                # Check which fields are empty while holding the write lock.
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                prior = conn.execute(text("SELECT * FROM books WHERE id=:id"), {"id": fill_book_id}).mappings().first()
                if prior is None:
                    return "skipped"
                existing_id = fill_book_id
            elif self.site == "lubimyczytac":
                row = conn.execute(text("SELECT id,url FROM books WHERE external_id=:external_id"), {"external_id": int(key)}).first()
                existing_id = row.id if row else None
            # Dane puste nie nadpisują poprawnych metadanych i powiązań.
            values = {k: v for k, v in data.items() if k in self.columns["books"] and k not in ("id", "url") and v is not None and v != "" and not isinstance(v, (dict, list))}
            if not only_missing and self.site == "lubimyczytac" and row and identity(self.site, row.url) != key:
                values["url"] = canonical(self.site, data["url"])
            if values.get("duration_minutes", 1) <= 0:
                values.pop("duration_minutes", None)
            if only_missing:
                values = {k: v for k, v in values.items()
                          if k not in ("external_id", "slug", "created_at") and
                          (prior[k] is None or isinstance(prior[k], str) and not prior[k].strip()
                           or k == "duration_minutes" and prior[k] <= 0)}
            for field, table in (("publisher", "publishers"), ("category", "categories"), ("series", "series")):
                raw = data.get(field)
                names = raw if isinstance(raw, list) else [raw]
                name = names[0] if names else None
                name = name.get("name") if isinstance(name, dict) else name
                column = field + "_id"
                if name and table in self.tables and column in self.columns["books"] and (not only_missing or prior[column] is None):
                    values[column] = self.name_id(conn, table, name)
            changed = bool(values)
            if existing_id is not None:
                if values:
                    binds = ",".join(f'"{k}"=:{k}' for k in values)
                    conn.execute(text(f"UPDATE books SET {binds} WHERE id=:book_id"), {**values, "book_id": existing_id})
                book_id = existing_id
            else:
                values["url"] = canonical(self.site, data["url"])
                if "created_at" in self.columns["books"]:
                    values["created_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
                book_id = self.insert(conn, "books", values)
            changed |= self.save_links(conn, book_id, data, only_missing)
            if not only_missing:
                self.save_reviews(conn, book_id, data.get("reviews", []))
            elif "covers" in self.tables and data.get("cover_url"):
                result = conn.execute(text("""UPDATE covers SET source_url=:url WHERE book_id=:book
                    AND COALESCE(TRIM(source_url),'')=''"""), {"url": data["cover_url"], "book": book_id})
                changed |= bool(result.rowcount)
            if cover:
                if "covers" in self.tables:
                    row = conn.execute(text("SELECT id FROM covers WHERE book_id=:book"), {"book": book_id}).first()
                    if not row:
                        self.insert(conn, "covers", {"book_id": book_id, **cover})
                        changed = True
                    elif not only_missing:
                        values = {key: value for key, value in cover.items() if key in self.columns["covers"]}
                        assignments = ",".join(f'"{key}"=:{key}' for key in values)
                        conn.execute(text(f"UPDATE covers SET {assignments} WHERE id=:cover_id"), {**values, "cover_id": row.id})
                elif "cover_local_path" in self.columns["books"] and (not only_missing or not prior["cover_local_path"]):
                    conn.execute(text("UPDATE books SET cover_local_path=:path WHERE id=:book"), {"path": cover["local_path"], "book": book_id})
                    changed = True
        if only_missing:
            return "updated" if changed else "skipped"
        if self.review_index:
            self.review_index.flush()
        self.known[key] = book_id
        return "updated" if existing_id else "inserted"

    def save_reviews(self, conn, book_id, reviews):
        if "reviews" not in self.tables:
            return
        text_column = "full_text" if "full_text" in self.columns["reviews"] else "text"
        if self.review_index and reviews:
            self.review_index.sync(conn, text_column)
        for review in reviews:
            body = review.get(text_column) or review.get("text") or review.get("full_text")
            if not body:
                continue
            username = review.get("username") or review.get("user") or "Anonim"
            params = {"book": book_id, "user": username, "body": body}
            if self.review_index:
                cached_id = self.review_index.find(book_id, username, body)
                exists = conn.execute(text(f"SELECT id FROM reviews WHERE id=:id AND book_id=:book AND username=:user AND {text_column}=:body"),
                                      {**params, "id": cached_id}).first() if cached_id is not None else None
                if cached_id is not None and exists is None:
                    # Nieaktualny wpis po zewnętrznej edycji bazy sprawdzamy w źródle.
                    exists = conn.execute(text(f"SELECT id FROM reviews WHERE book_id=:book AND username=:user AND {text_column}=:body"), params).first()
            else:
                exists = conn.execute(text(f"SELECT id FROM reviews WHERE book_id=:book AND username=:user AND {text_column}=:body"), params).first()
            if not exists:
                values = {"book_id": book_id, "username": username, text_column: body, "rating": review.get("rating", review.get("stars"))}
                if "date" in self.columns["reviews"]:
                    values["date"] = review.get("date")
                if "is_featured" in self.columns["reviews"]:
                    values["is_featured"] = int(review.get("is_featured", False))
                review_id = self.insert(conn, "reviews", values)
                if self.review_index:
                    self.review_index.remember(book_id, username, body, review_id)
            else:
                values = {"rating": review.get("rating", review.get("stars")), "date": review.get("date")}
                values = {key: value for key, value in values.items() if key in self.columns["reviews"] and value is not None and value != ""}
                if values:
                    assignments = ",".join(f'"{key}"=:{key}' for key in values)
                    conn.execute(text(f"UPDATE reviews SET {assignments} WHERE id=:review_id"), {**values, "review_id": exists.id})

    def needs_cover(self, url):
        key = identity(self.site, url)
        book_id = self.known.get(key)
        if not book_id:
            return True
        with self.engine.connect() as conn:
            if "covers" in self.tables:
                paths = conn.execute(text("SELECT local_path FROM covers WHERE book_id=:book"), {"book": book_id}).scalars()
                return not any(path and Path(path).is_file() for path in paths)
            row = conn.execute(text("SELECT cover_local_path,cover_url FROM books WHERE id=:book"), {"book": book_id}).first()
            if not row or not row.cover_local_path or not Path(row.cover_local_path).is_file():
                return True
            if self.site == "empik" and "/img/defaults/" in (row.cover_url or ""):
                return True
            if self.site == "audioteka":
                from parsers.audioteka import clean_at_cover
                # Adres wariantu CDN oznacza plik pobrany przed naprawą rozdzielczości.
                if row.cover_url and row.cover_url != clean_at_cover(row.cover_url):
                    return True
                try:
                    from PIL import Image
                    with Image.open(row.cover_local_path) as image:
                        return min(image.size) <= 300
                except ImportError:
                    return False
                except (OSError, ValueError):
                    return True
            return False

    def close(self):
        if self.review_index:
            self.review_index.close()
        self.engine.dispose()


def download_cover(client, url, directory, key):
    """Return cover metadata or degrade gracefully when Pillow or the image is unavailable."""
    try:
        from PIL import Image
        if urlsplit(url).hostname == "atkcdn.audioteka.com":
            from parsers.audioteka import clean_at_cover
            url = clean_at_cover(url)
        response = client.get(url)
        content = response.content
        digest = hashlib.sha256(content).hexdigest()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{hashlib.sha256(str(key).encode()).hexdigest()[:16]}_{digest[:12]}.jpg"
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            image.convert("RGB").save(path, "JPEG", quality=95)
        stored_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"source_url": url, "local_path": str(path.resolve()), "width": width, "height": height,
                "size_bytes": path.stat().st_size, "sha256": stored_digest}
    except Exception as exc:
        log.warning("Nie udało się pobrać okładki %s: %s", url, exc)
        return None
