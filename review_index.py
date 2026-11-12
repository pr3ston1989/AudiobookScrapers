"""Persistent review lookup outside the original database schema."""

import hashlib
import sqlite3
from pathlib import Path

from sqlalchemy import text
from events import check_stop, emit


def digest(body):
    """Keep full review text out of the auxiliary lookup database."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ReviewIndex:
    def __init__(self, path, source, site):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS source(path TEXT PRIMARY KEY, cursor INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS lookup(
                book_id INTEGER NOT NULL, username TEXT NOT NULL, hash TEXT NOT NULL,
                review_id INTEGER NOT NULL, PRIMARY KEY(book_id,username,hash)) WITHOUT ROWID;
        """)
        self.source = str(Path(source).resolve())
        self.site = site
        row = self.conn.execute("SELECT path FROM source").fetchone()
        if row and row[0] != self.source:
            self.conn.execute("DELETE FROM lookup")
            self.conn.execute("DELETE FROM source")
        self.conn.execute("INSERT OR IGNORE INTO source VALUES (?,0)", (self.source,))
        self.conn.commit()
        self.synced = False

    def sync(self, primary, column):
        """Read each primary review once, then incrementally import newly appended IDs."""
        if self.synced:
            return
        cursor = self.conn.execute("SELECT cursor FROM source WHERE path=?", (self.source,)).fetchone()[0]
        maximum = primary.execute(text("SELECT COALESCE(MAX(id),0) FROM reviews")).scalar_one()
        if maximum < cursor:
            self.conn.execute("DELETE FROM lookup")
            cursor = 0
        if maximum > cursor:
            emit("phase", site=self.site, phase="Budowanie pomocniczego indeksu opinii (pierwszy raz)")
        while cursor < maximum:
            check_stop()
            rows = primary.execute(text(f"SELECT id,book_id,username,{column} AS body FROM reviews WHERE id>:cursor AND id<=:maximum ORDER BY id LIMIT :batch"),
                                   {"cursor": cursor, "maximum": maximum, "batch": 2000}).mappings().all()
            if not rows:
                cursor = maximum
                break
            self.conn.executemany("INSERT OR IGNORE INTO lookup VALUES (?,?,?,?)",
                                  ((r["book_id"], r["username"] or "", digest(r["body"]), r["id"]) for r in rows if r["body"]))
            cursor = rows[-1]["id"]
            self.conn.execute("UPDATE source SET cursor=? WHERE path=?", (cursor, self.source))
            self.conn.commit()
        self.conn.execute("UPDATE source SET cursor=? WHERE path=?", (cursor, self.source))
        self.conn.commit()
        self.synced = True

    def find(self, book_id, username, body):
        row = self.conn.execute("SELECT review_id FROM lookup WHERE book_id=? AND username=? AND hash=?",
                                (book_id, username, digest(body))).fetchone()
        return row[0] if row else None

    def remember(self, book_id, username, body, review_id):
        self.conn.execute("INSERT OR REPLACE INTO lookup VALUES (?,?,?,?)", (book_id, username, digest(body), review_id))

    def flush(self):
        self.conn.commit()

    def close(self):
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None
