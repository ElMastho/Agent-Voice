"""User configuration and persistent state, stored in ~/.agent-voice/."""

from __future__ import annotations

import json
import os
from pathlib import Path

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
    # Piper length_scale: 1.0 = normal, 0.67 = about 1.5x faster.
    "speed": 0.67,
    "volume": 1.0,
    # Replies longer than this are cut (at a sentence boundary) so you are not read a novel.
    "max_chars": 1500,
    # Also speak the text an agent writes between tool calls (Claude Code), not only the final answer.
    "speak_progress": True,
    "hotkeys": {
        "stop": "ctrl+alt+s",
        # Skip the rest of the speech and start listening for your answer right away.
        "reply": "ctrl+alt+a",
        # Hands-free mode: everything you say is sent automatically (Enter pressed for you).
        "hands_free": "ctrl+alt+h",
        "mute": "ctrl+alt+m",
        "read_selection": "ctrl+alt+r",
        # Show / hide the floating widget (also from the tray icon).
        "widget": "ctrl+alt+w",
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
    # Floating status widget (glass panel, draggable, always on top).
    "widget": {"enabled": True},
    # After an agent finishes speaking its final answer, open the mic so you can just reply.
    "auto_listen": {
        "enabled": True,
        # How long to wait for you to start talking.
        "window_seconds": 15,
        # A pause this long ends your reply.
        "silence_seconds": 1.5,
        "max_seconds": 90,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
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
    return dict(DEFAULTS)


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


def save_state(state: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)
