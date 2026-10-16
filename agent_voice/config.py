"""User configuration and persistent state, stored in ~/.agent-voice/."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

LOOPBACK = ("127.0.0.1", "localhost")  # the only addresses the local API listens on
HOME = Path(os.environ.get("AGENT_VOICE_HOME", Path.home() / ".agent-voice"))
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"
VOICES_DIR = HOME / "voices"
LOG_DIR = HOME / "logs"

DEFAULTS: dict = {
    "host": "127.0.0.1",
    "port": 59125,
    # Piper voice per language. Any voice from https://huggingface.co/rhasspy/piper-voices works.
    "voices": {
        "en": "en_US-hfc_male-medium",
        "fr": "fr_FR-siwis-medium",
    },
    "default_lang": "en",
    # "piper" (light, the default) or "kokoro" (natural: more expressive, ~3x the CPU, a 120 MB download once).
    # The panel's Voice row switches it; kokoro.voices picks its voices (af_heart, bf_emma, am_adam, ff_siwis...).
    "engine": "piper",
    "kokoro": {"voices": {"en": "af_heart", "fr": "ff_siwis"}},
    # Piper length_scale: 1.0 = normal, 0.67 = about 1.5x faster.
    "speed": 0.67,
    "volume": 1.0,
    # Replies longer than this are cut (at a sentence boundary) so you are not read a novel.
    "max_chars": 1500,
    # Also speak the text an agent writes between tool calls (Claude Code), not only the final answer.
    "speak_progress": True,
    # Final answers: speak a one-sentence summary instead of the whole text.
    # Claude Code is asked to end its reply with "Voice summary: ..."; otherwise the opening sentences are used.
    # Toggle at runtime with `agent-voice summary` or the tray menu.
    "summary": {"enabled": True, "max_chars": 280},
    "hotkeys": {
        "stop": "ctrl+alt+s",
        # Skip the rest of the speech and start listening for your answer right away.
        "reply": "ctrl+alt+a",
        # Hands-free mode: everything you say is sent automatically (Enter pressed for you).
        "hands_free": "ctrl+alt+h",
        "mute": "ctrl+alt+m",
        # Chime mode: no voice, no mic, just a soft sound when the agent is done or needs you.
        "chime": "ctrl+alt+c",
        "read_selection": "ctrl+alt+r",
        # Show / hide the floating widget (also from the tray icon).
        "widget": "ctrl+alt+w",
        # Start a new thread: type (or dictate) its first prompt.
        "new_thread": "ctrl+alt+n",
        "push_to_talk": "ctrl+alt+space",
    },
    "stt": {
        "enabled": True,
        # tiny / base / small / medium. "small" is a good FR+EN compromise on CPU.
        "model": "small",
        "compute_type": "int8",
        # Restrict auto-detection to these languages.
        "languages": ["fr", "en"],
        # Press Enter after pasting the transcription.
        "auto_submit": False,
    },
    # The notch (or floating island) and the tray icon, started with the service.
    "widget": {"enabled": True},
    # After an agent finishes speaking its final answer, open the mic so you can just reply.
    "auto_listen": {
        "enabled": True,
        # How long to wait for you to start talking.
        "window_seconds": 15,
        # A pause this long ends your reply.
        "silence_seconds": 1.5,
        # Safety cap only: a long reply must never be cut mid-sentence.
        "max_seconds": 600,
    },
    # Claude desktop app: with auto-send on, type and send spoken replies without bringing the
    # window forward (falls back to focusing it when that fails).
    "background_send": True,
    # Claude Code permission prompts: asked out loud and in the widget, answered by voice or a click.
    # The hook waits this long for you before Claude shows its own dialog.
    "permissions": {"enabled": True, "wait_seconds": 60},
    # "Hey Claude, ..." while idle sends a new prompt (turn on from the widget or `agent-voice wake on`).
    # A small keyword spotter listens (only while someone speaks); these phrases confirm it in the transcript.
    "wake_word": {"phrases": ["hey claude", "ok claude", "okay claude", "hé claude", "hey cloud", "hey clod",
                              "hey clode", "eh claude", "et claude", "hi claude", "hay claude", "hey clawed"]},
    # Tell you when a newer version is on GitHub.
    "update_check": True,
    # While another app has the mic (Teams, Zoom, Meet...), chimes instead of the voice.
    "auto_chime_in_calls": True,
    # While a reply is read out, show its text under the notch (gone 5 s after the voice stops).
    "read_along": True,
    # Back after a while (no input for `away_minutes`, or a call ending): what finished and who needs you.
    "catch_up": {"enabled": True, "away_minutes": 5},
    # Desktop AI apps without hooks: their notifications ("ChatGPT: here's your answer...") read out.
    # Off until you turn it on (`agent-voice apps on`); only these apps' notifications are ever read.
    "desktop_apps": {"enabled": False, "apps": {"chatgpt": "chatgpt", "copilot": "copilot", "perplexity": "perplexity"}},
    # Each conversation speaks a little lower or higher than the others, so you can tell them apart.
    "thread_voices": True,
    # Chime mode sounds (0.0-1.0). Kept low so they don't disturb people around you.
    "chime": {"volume": 0.35},
    # The day's recap at this time (what each thread did, what's waiting, limits, tokens); also on demand:
    # "Hey Claude, daily recap", the tray, or `agent-voice recap`.
    "recap": {"enabled": True, "at": "18:00"},
    # Focus: in a call, presenting or with an app full screen, replies are held (approvals still come
    # through) and a catch-up follows. calls: "hold", or "chimes" to keep hearing chimes in calls.
    "focus": {"enabled": True, "full_screen": True, "calls": "hold"},
    # Claude's limits: every this many minutes while you use Claude, agent-voice runs Claude Code's own `claude -p
    # /usage` (a local command: no model request; your credentials stay in Claude Code) and reads what it reports.
    # 0 turns it off; between readings the notch estimates from your transcripts.
    "limits": {"claude_refresh_minutes": 10},
    # Other apps' sound (Windows): lowered to `voice` of their volume while agent-voice speaks, to `listen` while
    # you reply, and a video or song playing is paused then (`pause_media`) and played again after.
    "ducking": {"enabled": True, "voice": 0.3, "listen": 0.0, "pause_media": True},
    # What you dictate and send waits this many seconds under the notch first: say "wait" (it's typed in, not sent),
    # "cancel that", or "send it"; keep talking and it's added. 0 sends at once.
    "take_back": {"seconds": 5},
    # only: the voice only in headphones; through speakers (an open office), chimes and the banner instead. The
    # panel's Headphones button switches it.
    "headphones": {"only": False},
    # Out of quota: once the limit resets, send this to the thread so it picks up where it stopped (auto: without
    # asking; off: only after "resume when it resets" or a click on Resume).
    "resume": {"auto": True, "message": "Continue."},
    # Saved prompts: say a name ("Hey Claude, run the tests", "tell Codex to commit this") and its prompt is sent.
    "shortcuts": {
        "run the tests": "Run the test suite and fix anything that fails.",
        "commit this": "Commit the current changes with a clear, short commit message.",
        "review this": "Review the changes you just made: bugs, edge cases, anything you would do differently.",
        "explain that": "Explain what you just did in a few sentences.",
        "lance les tests": "Lance les tests et corrige ce qui échoue.",
        "fais un commit": "Fais un commit des changements avec un message clair et court.",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)  # nested defaults are never shared: a caller may change its copy
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return _merge(DEFAULTS, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return copy.deepcopy(DEFAULTS)


def write_default_config() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
    return CONFIG_PATH


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


_SAVING = threading.Lock()


def save_state(state: dict) -> None:
    """Write state.json atomically. The daemon saves from several threads, and hooks read the file all the time:
    one writer at a time, and a retry when Windows refuses the swap while a hook has the file open."""
    with _SAVING:
        HOME.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            try:
                text = json.dumps(state, indent=2)
                break
            except RuntimeError:  # another thread changed it mid-way: take it again
                time.sleep(0.01)
        else:
            return
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, STATE_PATH)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02)
