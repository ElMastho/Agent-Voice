"""Small helpers shared by the daemon's parts: agents' names, threads' colours and pitches, times."""

from __future__ import annotations

import logging
import time
import zlib

from agent_voice.i18n import day_time, tr

log = logging.getLogger("agent_voice.server")


# How each agent is called when a conversation has no title of its own.
AGENT_TITLES = {"claude": "Claude Code", "cline": "Cline", "claude-desktop": "Claude Desktop", "vscode": "VS Code",
                "chatgpt": "ChatGPT", "claude-web": "Claude", "gemini-web": "Gemini", "aistudio": "AI Studio",
                "perplexity": "Perplexity", "copilot": "Copilot", "gemini": "Gemini", "codex": "Codex",
                "cursor": "Cursor", "windsurf": "Windsurf", "antigravity": "Antigravity", "aider": "Aider",
                "opencode": "OpenCode", "goose": "Goose"}


# One colour per conversation, picked from its id so it never changes: you learn which is which.
THREAD_COLORS = ("#F472B6", "#60A5FA", "#34D399", "#FBBF24", "#A78BFA", "#F87171", "#22D3EE", "#FB923C")


def thread_color(key: str) -> str:
    return THREAD_COLORS[zlib.crc32(key.encode("utf-8")) % len(THREAD_COLORS)]


# A slightly different voice per conversation, so you can tell threads apart by ear. The first one
# keeps the natural voice; playback-rate shifts cost nothing (no second voice to download).
THREAD_PITCHES = (1.0, 0.92, 1.08, 0.86, 1.14)


def _join(items: list[str], in_lang: str | None = None) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + tr(" and ", in_lang=in_lang) + items[-1]


def _clock(at: float, window: str) -> str:
    """A reset time: '15:20' today, 'Mon 09:00' further away."""
    from datetime import datetime

    when = datetime.fromtimestamp(at)
    return when.strftime("%H:%M") if window == "five_hour" and at - time.time() < 20 * 3600 else day_time(when)


def _meeting_key(e: dict) -> str:
    return f"{e['title']}|{int(e['start'])}"
