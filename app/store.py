"""Remembered fingerprints (sha1) and capture times, so re-inserting a card doesn't mean
re-reading it at ~40 MB/s.

A row is only reused when the card's hardware serial, path, size and mtime (ns) all match.
Writes happen only when something new is learned, never on a timer. Size is capped.
"""
import sqlite3
import threading
import time
from datetime import datetime

MAX_ROWS = 300_000
MAX_AGE_DAYS = 365


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS files (
                 card TEXT, rel TEXT, size INTEGER, mtime_ns INTEGER,
                 sha1 TEXT, taken TEXT, seen REAL,
                 PRIMARY KEY (card, rel, size, mtime_ns))"""
        )

    def lookup(self, card: str) -> dict[tuple, tuple]:
        """{(rel, size, mtime_ns): (sha1|None, taken|None)} for one card."""
        with self.lock:
            rows = self.db.execute(
                "SELECT rel, size, mtime_ns, sha1, taken FROM files WHERE card = ?", (card,)
            ).fetchall()
        return {(r, s, m): (h, datetime.fromisoformat(t) if t else None) for r, s, m, h, t in rows}

    def save(self, card: str, rows: list[tuple]) -> None:
        """rows: (rel, size, mtime_ns, sha1|None, taken|None). Never overwrites a value with NULL."""
        if not rows:
            return
        now = time.time()
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany(
                """INSERT INTO files (card, rel, size, mtime_ns, sha1, taken, seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (card, rel, size, mtime_ns) DO UPDATE SET
                     sha1 = COALESCE(excluded.sha1, sha1),
                     taken = COALESCE(excluded.taken, taken),
                     seen = excluded.seen""",
                [(card, r, s, m, h, t.isoformat() if t else None, now) for r, s, m, h, t in rows],
            )
            self.db.execute("COMMIT")

    def prune(self) -> None:
        with self.lock:
            self.db.execute("DELETE FROM files WHERE seen < ?", (time.time() - MAX_AGE_DAYS * 86400,))
            self.db.execute(
                """DELETE FROM files WHERE rowid IN (
                     SELECT rowid FROM files ORDER BY seen DESC LIMIT -1 OFFSET ?)""",
                (MAX_ROWS,),
            )
