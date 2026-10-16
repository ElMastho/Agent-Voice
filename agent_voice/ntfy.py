"""Phone notifications through ntfy (https://ntfy.sh): opt-in, the one feature that sends anything off the machine.

The notch shows a QR code for a private topic (a long random name); the ntfy app subscribes to it. What is sent
is up to `ntfy.content`: the thread and what happened ("titles"), plus its one-line summary ("summary"), or the
reply itself ("full"). A burst of events becomes one notification. Self-hosted servers and access tokens work too.
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"
BATCH_SECONDS = 8.0  # events this close together go out as one notification
# Per kind: (ntfy priority 1-5, tags shown as emoji, what it says when only titles are sent).
KINDS = {
    "permission": (5, ["warning"], "wants your approval"),
    "needs": (4, ["raising_hand"], "needs you"),
    "limit": (3, ["hourglass_flowing_sand"], "is out of quota"),
    "resumed": (3, ["arrow_forward"], "resumed after its limit"),
    "done": (3, ["white_check_mark"], "finished"),
    "scheduled": (3, ["alarm_clock"], "got its scheduled prompt"),
    "recap": (2, ["city_sunset"], "today's recap"),
    "test": (3, ["iphone"], "is connected"),
}


def new_topic() -> str:
    """A topic nobody can guess: whoever knows it can read the notifications (keep the QR code to yourself)."""
    # Lowercase letters and digits only: ntfy topics are case-sensitive, and phones love to capitalise.
    return "agent-voice-" + secrets.token_hex(12)


def subscribe_url(server: str, topic: str) -> str:
    return f"{(server or DEFAULT_SERVER).rstrip('/')}/{topic}"


def message(kind: str, title: str, text: str, content: str = "summary") -> dict:
    """One event as ntfy fields, its text cut down to what `content` allows."""
    priority, tags, said = KINDS.get(kind, (3, [], ""))
    text = " ".join((text or "").split())
    if content == "titles" or not text:
        body = f"{title} {said}." if said else title
    elif content == "full":
        body = text[:2000]
    else:
        body = text if len(text) <= 240 else text[:237].rsplit(" ", 1)[0] + "…"
    return {"title": title or "agent-voice", "message": body, "priority": priority, "tags": list(tags)}


def batch(events: list[dict]) -> dict:
    """Several events at once: one notification listing them, at the highest priority among them."""
    if len(events) == 1:
        return dict(events[0])
    lines = [f"• {e['title']}: {e['message']}" if not e["message"].startswith(e["title"]) else f"• {e['message']}"
             for e in events[-8:]]
    more = len(events) - 8
    return {"title": f"agent-voice: {len(events)} updates", "message": "\n".join(lines + ([f"+{more} more"] if more > 0 else [])),
            "priority": max(e["priority"] for e in events), "tags": events[-1]["tags"]}


class Notifier:
    """Queues events and sends them from a thread of its own, so nothing waits on the network."""

    def __init__(self, settings):
        self._settings = settings  # () -> {"server", "token", "topic", "content"} or None when off
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.last_error = ""

    def push(self, kind: str, title: str, text: str = "") -> None:
        cfg = self._settings()
        if not cfg:
            return
        self._queue.put(message(kind, title, text, cfg.get("content", "summary")))
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="ntfy", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=60)
            except queue.Empty:
                return  # idle: the thread ends, the next push starts another
            events, end = [first], time.time() + BATCH_SECONDS
            while time.time() < end:
                try:
                    events.append(self._queue.get(timeout=max(0.05, end - time.time())))
                except queue.Empty:
                    break
            cfg = self._settings()
            if cfg:
                self.send(batch(events), cfg)

    def send(self, payload: dict, cfg: dict) -> bool:
        """POST one notification (ntfy's JSON publishing: the topic goes in the body)."""
        body = json.dumps({**payload, "topic": cfg["topic"]}).encode("utf-8")
        request = urllib.request.Request((cfg.get("server") or DEFAULT_SERVER).rstrip("/"), data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        if cfg.get("token"):
            request.add_header("Authorization", f"Bearer {cfg['token']}")
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                ok = 200 <= reply.status < 300
        except Exception as exc:  # offline, a wrong server: logged, never raised (the token is never logged)
            self.last_error = type(exc).__name__
            log.warning("ntfy: not sent (%s)", self.last_error)
            return False
        self.sent += ok
        self.last_error = "" if ok else "refused"
        return ok
