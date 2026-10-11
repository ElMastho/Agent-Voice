"""Register (or remove) agent-voice hooks in each agent's configuration."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

MARKER = "agent_voice hook"
AGENTS = ("claude", "codex", "gemini", "cursor", "antigravity")


def _python() -> str:
    return Path(sys.executable).as_posix()


def hook_command(agent: str) -> str:
    return f'"{_python()}" -m agent_voice hook {agent}'


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-agent-voice"))


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _strip_ours(groups: list) -> list:
    """Remove our entries from a Claude/Gemini-style [{matcher, hooks: [...]}] list."""
    out = []
    for group in groups:
        hooks = [h for h in group.get("hooks", []) if MARKER not in str(h.get("command", ""))]
        if hooks:
            out.append({**group, "hooks": hooks})
    return out


# -- Claude Code: ~/.claude/settings.json -------------------------------------------------
def claude(remove: bool = False) -> str:
    path = Path.home() / ".claude" / "settings.json"
    settings = _read_json(path)
    _backup(path)
    hooks = settings.setdefault("hooks", {})
    events = ("Stop", "PreToolUse", "UserPromptSubmit")
    for event in events:
        hooks[event] = _strip_ours(hooks.get(event, []))
    if not remove:
        entry = {"type": "command", "command": hook_command("claude"), "timeout": 20}
        hooks["Stop"].append({"hooks": [entry]})
        # async so tool calls never wait on the voice (Stop stays sync: async processes may be killed there).
        hooks["PreToolUse"].append({"matcher": "*", "hooks": [{**entry, "async": True}]})
        # Records the prompt's window and silences the voice as you send a new message.
        hooks["UserPromptSubmit"].append({"hooks": [{**entry, "timeout": 5}]})
    for event in events:
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    _write_json(path, settings)
    return f"{'removed from' if remove else 'installed in'} {path}"


# -- Gemini CLI: ~/.gemini/settings.json --------------------------------------------------
def gemini(remove: bool = False) -> str:
    path = Path.home() / ".gemini" / "settings.json"
    settings = _read_json(path)
    _backup(path)
    hooks = settings.setdefault("hooks", {})
    hooks["AfterAgent"] = _strip_ours(hooks.get("AfterAgent", []))
    if not remove:
        hooks["AfterAgent"].append(
            {"matcher": "*", "hooks": [{"type": "command", "command": hook_command("gemini"), "timeout": 20000}]}
        )
    if not hooks["AfterAgent"]:
        del hooks["AfterAgent"]
    if not hooks:
        del settings["hooks"]
    _write_json(path, settings)
    return f"{'removed from' if remove else 'installed in'} {path}"


# -- Cursor: ~/.cursor/hooks.json ---------------------------------------------------------
def cursor(remove: bool = False) -> str:
    path = Path.home() / ".cursor" / "hooks.json"
    data = _read_json(path)
    _backup(path)
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    entries = [h for h in hooks.get("afterAgentResponse", []) if MARKER not in str(h.get("command", ""))]
    if not remove:
        entries.append({"command": hook_command("cursor")})
    hooks["afterAgentResponse"] = entries
    if not entries:
        del hooks["afterAgentResponse"]
    _write_json(path, data)
    return f"{'removed from' if remove else 'installed in'} {path}"


# -- Codex CLI: ~/.codex/config.toml (top-level `notify`) ----------------------------------
_NOTIFY = re.compile(r"^notify\s*=.*$", re.M)


def codex(remove: bool = False) -> str:
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = _NOTIFY.search(text)
    if existing and MARKER.replace(" ", '", "') not in existing.group(0) and not remove:
        return f"skipped: {path} already has its own `notify` ({existing.group(0)}); add agent-voice there by hand"
    _backup(path)
    text = _NOTIFY.sub("", text).lstrip("\n")
    if not remove:
        line = "notify = " + json.dumps([_python(), "-m", "agent_voice", "hook", "codex"])
        # Must be top-level, i.e. before the first [table].
        text = f"{line}\n{text}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"{'removed from' if remove else 'installed in'} {path}"


def antigravity(remove: bool = False) -> str:  # noqa: ARG001
    return "no hook API: select the agent's reply and press the read-selection hotkey (default ctrl+alt+r)"


INSTALLERS = {"claude": claude, "codex": codex, "gemini": gemini, "cursor": cursor, "antigravity": antigravity}


# -- Windows autostart ---------------------------------------------------------------------
def _startup_file() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "agent-voice.vbs"


def autostart(enable: bool = True) -> str:
    if os.name != "nt":
        return "autostart is only automated on Windows; run `agent-voice start` from your login scripts"
    from agent_voice.client import pythonw

    path = _startup_file()
    if not enable:
        path.unlink(missing_ok=True)
        return f"removed {path}"
    cmd = f'""{pythonw()}"" -m agent_voice serve --background'
    path.write_text(f'CreateObject("WScript.Shell").Run "{cmd}", 0, False\n', encoding="utf-8")
    return f"created {path}"
