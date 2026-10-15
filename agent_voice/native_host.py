"""Bridge between the browser extension and the daemon (Chrome / Edge native messaging).

The browser starts this process for the extension and talks to it on stdin/stdout: each message is a 4-byte
length then JSON. Web pages can't reach the daemon's API (it refuses any request with an Origin), so the
extension goes through here instead: the browser only lets our extension's id start it.
Stdlib only; stdout carries nothing but replies to the browser.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys

from agent_voice import client, winfocus
from agent_voice.hooks import strip_summary, summarize, summary_enabled
from agent_voice.config import load_config

SITES = {"chatgpt", "claude-web", "gemini-web", "aistudio", "perplexity", "copilot", "mistral", "deepseek"}


def read_message(stream) -> dict | None:
    head = stream.read(4)
    if len(head) < 4:
        return None
    size = struct.unpack("<I", head)[0]
    if size > 4_000_000:
        return None
    try:
        return json.loads(stream.read(size).decode("utf-8"))
    except ValueError:
        return {}


def write_message(stream, msg: dict) -> None:
    body = json.dumps(msg).encode("utf-8")
    stream.write(struct.pack("<I", len(body)) + body)
    stream.flush()


def session_of(msg: dict) -> str:
    """One conversation per site and page (chatgpt.com/c/<id>): its own colour and thread row."""
    page = hashlib.sha1(str(msg.get("conversation", "")).encode()).hexdigest()[:10]
    return f"web-{msg.get('site')}-{page}"


def handle(msg: dict) -> None:
    site = str(msg.get("site", ""))
    if site not in SITES:
        return
    session = session_of(msg)
    title = " ".join(str(msg.get("title") or "").split())[:80]
    kind = msg.get("type")
    if kind == "start":
        # You just sent a prompt from the browser: it's in front, so this is where replies go back.
        client.request("/prompt", {"hwnd": winfocus.foreground(), "session": session, "source": site,
                                   "title": title}, timeout=1.0)
    elif kind == "status":
        client.request("/activity", {"session": session, "source": site,
                                     "label": str(msg.get("label") or "Writing")[:80]}, timeout=1.0)
    elif kind == "reply":
        text = str(msg.get("text") or "")
        if summary_enabled():
            text = summarize(text, int(load_config().get("summary", {}).get("max_chars", 280)))
        else:
            text = strip_summary(text)
        client.speak(text, source=site, final=True, session=session, autostart=False)


def run() -> int:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        msg = read_message(stdin)
        if msg is None:  # the browser closed the port
            return 0
        try:
            handle(msg)
        except Exception:  # a voice problem must never break the page
            pass
        write_message(stdout, {"ok": True})
