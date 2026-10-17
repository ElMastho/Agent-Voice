"""Every conversation, searchable: what you asked and what each agent answered, across all of them.

A local SQLite file (~/.agent-voice/history.db) with a full-text index (FTS5): "which agent fixed the login bug last
week?" is one search. Kept `history.days` days (90 by default); nothing leaves the machine.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import threading
import time
from pathlib import Path

from agent_voice.config import HOME

PATH = HOME / "history.db"
_SCHEMA = ("CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(text, title, agent UNINDEXED, session UNINDEXED, "
           "kind UNINDEXED, at UNINDEXED, tokenize='unicode61 remove_diacritics 2')")


class History:
    """Writes come from the daemon's threads one at a time; every call opens its own connection (thread-safe)."""

    def __init__(self, path: Path | None = None, days: float = 90):
        self.path = Path(path or PATH)
        self.days = days
        self._lock = threading.Lock()
        self._pruned = 0.0

    @contextlib.contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute(_SCHEMA)
            yield db
            db.commit()
        finally:
            db.close()

    def add(self, kind: str, text: str, session: str = "", agent: str = "", title: str = "",
            at: float | None = None) -> None:
        """kind: 'you' (a prompt), 'reply' (an agent's turn) or 'note'."""
        text = (text or "").strip()
        if not text:
            return
        at = time.time() if at is None else at
        with self._lock, self._db() as db:
            db.execute("INSERT INTO entries (text, title, agent, session, kind, at) VALUES (?, ?, ?, ?, ?, ?)",
                       (text[:50000], title, agent, session, kind, at))
            if at - self._pruned > 86400:  # once a day: what's older than `days` goes
                self._pruned = at
                db.execute("DELETE FROM entries WHERE CAST(at AS REAL) < ?", (at - self.days * 86400,))

    def search(self, query: str = "", limit: int = 30) -> list[dict]:
        """The best matches for `query` (every word, as a prefix), newest first among equals; the latest without one."""
        words = [w for w in re.findall(r"\w+", query.lower()) if w]
        if not self.path.exists():
            return []
        with self._db() as db:
            if words:
                match = " ".join(f'"{w}"*' for w in words)
                rows = db.execute(
                    "SELECT rowid, at, session, agent, title, kind, snippet(entries, 0, '[', ']', '…', 18) "
                    "FROM entries WHERE entries MATCH ? ORDER BY rank, CAST(at AS REAL) DESC LIMIT ?",
                    (match, limit)).fetchall()
            else:
                rows = db.execute("SELECT rowid, at, session, agent, title, kind, substr(text, 1, 160) FROM entries "
                                  "ORDER BY CAST(at AS REAL) DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "at": float(r[1]), "session": r[2], "agent": r[3], "title": r[4], "kind": r[5],
                 "snippet": " ".join(str(r[6]).split())} for r in rows]

    def get(self, entry_id: int) -> dict:
        if not self.path.exists():
            return {}
        with self._db() as db:
            row = db.execute("SELECT rowid, at, session, agent, title, kind, text FROM entries WHERE rowid = ?",
                             (entry_id,)).fetchone()
        if not row:
            return {}
        return {"id": row[0], "at": float(row[1]), "session": row[2], "agent": row[3], "title": row[4],
                "kind": row[5], "text": row[6]}

    def clear(self) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM entries")
