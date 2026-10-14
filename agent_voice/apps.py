"""Desktop AI apps without hooks (ChatGPT, Copilot, Perplexity...): their "reply ready" notifications, in the notch.

Windows keeps the notifications it shows in a local database (wpndatabase.db). When it changes, we read the new
toasts of the apps listed in `desktop_apps.apps` and nothing else, and pass their text on like an agent's
reply. Opt-in (`desktop_apps.enabled`), read-only, nothing leaves the machine.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DB = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"

# App id fragment (lower case) -> our name for it. Claude's desktop app is left out by default: its Claude Code
# sessions already speak through the hooks.
DEFAULT_APPS = {"chatgpt": "chatgpt", "copilot": "copilot", "perplexity": "perplexity"}

QUERY = ("SELECT n.Id, h.PrimaryId, n.Payload FROM Notification n "
         "JOIN NotificationHandler h ON h.RecordId = n.HandlerId "
         "WHERE n.Type = 'toast' AND n.Id > ? AND ({apps}) ORDER BY n.Id")


def toast_text(payload) -> tuple[str, str]:
    """(title, body) from a toast's XML: the first <text> is the title, the others the message."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return "", ""
    texts = [" ".join((t.text or "").split()) for t in root.iter("text")]
    texts = [t for t in texts if t]
    return (texts[0] if texts else ""), " ".join(texts[1:])


class Watcher:
    """New toasts from the listed apps, each time the database changes."""

    def __init__(self, apps: dict[str, str] | None = None, db: Path = DB):
        self.apps = {k.lower(): v for k, v in (apps or DEFAULT_APPS).items()}
        self.db = db
        self.last_id = -1
        self._stamp: tuple | None = None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True, timeout=1.0)

    def _changed(self) -> bool:
        """Cheap check first (file times and sizes): no query while nothing new was shown."""
        stamp = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                      for p in (self.db, self.db.with_name(self.db.name + "-wal")))
        changed, self._stamp = stamp != self._stamp, stamp
        return changed

    def poll(self) -> list[dict]:
        """[{app, title, text}] shown since the last poll (none on the first: old toasts aren't news)."""
        if not self.db.exists() or not self._changed():
            return []
        con = self._connect()
        try:
            if self.last_id < 0:
                self.last_id = con.execute("SELECT COALESCE(MAX(Id), 0) FROM Notification").fetchone()[0]
                return []
            # Only the listed apps' toasts are ever read: the filter is in the query itself.
            where = " OR ".join("lower(h.PrimaryId) LIKE ?" for _ in self.apps)
            rows = con.execute(QUERY.format(apps=where),
                               (self.last_id, *(f"%{frag}%" for frag in self.apps))).fetchall()
        finally:
            con.close()
        out = []
        for row_id, app_id, payload in rows:
            self.last_id = max(self.last_id, row_id)
            name = next((ours for frag, ours in self.apps.items() if frag in str(app_id).lower()), "app")
            title, body = toast_text(payload)
            if body or title:
                out.append({"app": name, "title": title, "text": body or title})
        return out


def available() -> bool:
    return sys.platform == "win32" and DB.exists()
