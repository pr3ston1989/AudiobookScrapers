"""Durable discovery, retry and catalog checkpoints outside the source schemas."""

import json
import sqlite3
import time
from pathlib import Path

from urls import canonical, identity


class Coverage:
    def __init__(self, path, site, database):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.site = site
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources(site TEXT PRIMARY KEY, database_path TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products(
                site TEXT NOT NULL, product_key TEXT NOT NULL, url TEXT NOT NULL,
                first_seen REAL NOT NULL, last_seen REAL NOT NULL, lastmod TEXT,
                status TEXT NOT NULL DEFAULT 'discovered', attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, details TEXT,
                PRIMARY KEY(site,product_key));
            CREATE TABLE IF NOT EXISTS checkpoints(
                site TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(site,name));
            CREATE TABLE IF NOT EXISTS catalog_pages(
                site TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(site,url));
            CREATE TABLE IF NOT EXISTS metadata(
                site TEXT NOT NULL, product_key TEXT NOT NULL, fetched_at REAL NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(site,product_key));
        """)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(catalog_pages)")}
        for name, declaration in (("retry_at", "REAL NOT NULL DEFAULT 0"), ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("last_error", "TEXT")):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE catalog_pages ADD COLUMN {name} {declaration}")
        row = self.conn.execute("SELECT database_path FROM sources WHERE site=?", (site,)).fetchone()
        if row and row[0] != str(Path(database).resolve()):
            for table in ("products", "checkpoints", "catalog_pages", "metadata"):
                self.conn.execute(f"DELETE FROM {table} WHERE site=?", (site,))
        self.conn.execute("INSERT OR REPLACE INTO sources VALUES (?,?)", (site, str(Path(database).resolve())))
        self.conn.commit()
        self.buffer = 0

    def discover(self, url, lastmod=None):
        """Retain URLs immediately so an interrupted sitemap scan can be resumed."""
        key = identity(self.site, url)
        if key is None:
            return
        self.conn.execute("""INSERT INTO products(site,product_key,url,first_seen,last_seen,lastmod)
            VALUES (?,?,?,?,?,?) ON CONFLICT(site,product_key) DO UPDATE SET
            url=excluded.url,last_seen=excluded.last_seen,
            status=CASE WHEN excluded.lastmod IS NOT NULL AND excluded.lastmod<>COALESCE(products.lastmod,'')
                        AND products.status IN ('verified','existing') THEN 'changed' ELSE products.status END,
            lastmod=COALESCE(excluded.lastmod,products.lastmod)""",
            (self.site, key, canonical(self.site, url), time.time(), time.time(), lastmod))
        self.buffer += 1
        if self.buffer >= 500:
            self.flush()

    def flush(self):
        self.conn.commit()
        self.buffer = 0

    def reconcile(self, known):
        """Label inherited books as existing, without claiming their metadata were verified."""
        rows = self.conn.execute("SELECT product_key FROM products WHERE site=? AND status='discovered'", (self.site,)).fetchall()
        self.conn.executemany("UPDATE products SET status='existing' WHERE site=? AND product_key=? AND status='discovered'",
                              ((self.site, key) for (key,) in rows if key in known))
        self.flush()

    def apply_aliases(self, known):
        """Restore verified redirect aliases without changing primary database schemas."""
        rows = self.conn.execute("SELECT product_key,details FROM products WHERE site=? AND status='redirected'", (self.site,)).fetchall()
        for alias, raw in rows:
            target = identity(self.site, json.loads(raw or "{}").get("redirect_url", ""))
            book_id = known.get(target) or known.get(alias)
            if target and book_id:
                known[alias] = known[target] = book_id

    def work(self, known, deadline=None):
        """Retry incomplete and failed records when due, including already saved books."""
        self.flush()
        rows = self.conn.execute("""SELECT product_key,url,status FROM products WHERE site=? AND retry_at<=?
            AND status IN ('discovered','changed','incomplete','error','not_found')""", (self.site, time.time() if deadline is None else deadline)).fetchall()
        return [url for key, url, status in rows if key not in known or status != "discovered"]

    def due(self, url, deadline=None):
        row = self.conn.execute("SELECT retry_at FROM products WHERE site=? AND product_key=?", (self.site, identity(self.site, url))).fetchone()
        return not row or row[0] <= (time.time() if deadline is None else deadline)

    def mark(self, url, status, details=None):
        self.discover(url)
        row = self.conn.execute("SELECT attempts FROM products WHERE site=? AND product_key=?", (self.site, identity(self.site, url))).fetchone()
        attempts = (row[0] if row else 0) + 1 if status in ("error", "incomplete", "not_found") else 0
        delay = min(7 * 86400, 3600 * 2 ** min(attempts - 1, 8)) if status in ("error", "incomplete") else 0
        if status in ("not_found", "skipped"):
            delay = 7 * 86400
        self.conn.execute("UPDATE products SET status=?,attempts=?,retry_at=?,details=? WHERE site=? AND product_key=?",
                          (status, attempts, time.time() + delay if delay else 0, json.dumps(details, ensure_ascii=False),
                           self.site, identity(self.site, url)))
        self.flush()

    def save_metadata(self, data, *, only_missing=False):
        """Keep every parsed field, including fields unsupported by legacy schemas."""
        key = identity(self.site, data["url"])
        row = self.conn.execute("SELECT data FROM metadata WHERE site=? AND product_key=?", (self.site, key)).fetchone()
        merged = json.loads(row[0]) if row else {}
        merged.update({field: value for field, value in data.items() if value is not None and value != "" and value != []
                       and (not only_missing or merged.get(field) in (None, "", [], {}))})
        self.conn.execute("INSERT OR REPLACE INTO metadata VALUES (?,?,?,?)",
                          (self.site, key, time.time(), json.dumps(merged, ensure_ascii=False)))
        self.flush()

    def checkpoint(self, name, default=None):
        row = self.conn.execute("SELECT value FROM checkpoints WHERE site=? AND name=?", (self.site, name)).fetchone()
        return json.loads(row[0]) if row else default

    def set_checkpoint(self, name, value):
        self.conn.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)", (self.site, name, json.dumps(value)))
        self.flush()

    def seed_pages(self, urls):
        self.conn.executemany("INSERT OR IGNORE INTO catalog_pages(site,url) VALUES (?,?)", ((self.site, url) for url in urls))
        self.flush()

    def restart_catalog(self):
        self.conn.execute("DELETE FROM catalog_pages WHERE site=?", (self.site,))
        self.flush()

    def pending_page(self, deadline=None):
        row = self.conn.execute("SELECT url FROM catalog_pages WHERE site=? AND status='pending' AND retry_at<=? ORDER BY rowid LIMIT 1", (self.site, time.time() if deadline is None else deadline)).fetchone()
        return row[0] if row else None

    def has_pending_pages(self):
        return self.conn.execute("SELECT 1 FROM catalog_pages WHERE site=? AND status='pending' LIMIT 1", (self.site,)).fetchone() is not None

    def fail_page(self, url, error):
        """Defer one broken page while allowing independent branches to proceed."""
        self.conn.execute("UPDATE catalog_pages SET retry_at=?,attempts=attempts+1,last_error=? WHERE site=? AND url=?",
                          (time.time() + 3600, str(error), self.site, url))
        self.flush()

    def finish_page(self, url):
        self.conn.execute("UPDATE catalog_pages SET status='completed' WHERE site=? AND url=?", (self.site, url))
        self.flush()

    def summary(self):
        self.flush()
        counts = dict(self.conn.execute("SELECT status,COUNT(*) FROM products WHERE site=? GROUP BY status", (self.site,)).fetchall())
        pending_pages = self.conn.execute("SELECT COUNT(*) FROM catalog_pages WHERE site=? AND status='pending'", (self.site,)).fetchone()[0]
        return {"products": counts, "catalog_pages_pending": pending_pages,
                "unresolved": sum(counts.get(s, 0) for s in ("discovered", "changed", "incomplete", "error")),
                "metadata_verified": counts.get("verified", 0), "inherited_not_verified": counts.get("existing", 0)}

    def close(self):
        self.flush()
        self.conn.close()
