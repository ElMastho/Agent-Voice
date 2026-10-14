"""Register (or remove) agent-voice hooks in each agent's configuration."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from agent_voice.client import self_command

# How our hook commands look, from source (python -m agent_voice hook) or from the release exe.
MARKERS = ("agent_voice hook", 'agent_voice", "hook', 'agent-voice.exe" hook', 'agent-voice.exe", "hook',
           "agent_voice mcp", 'agent_voice", "mcp', 'agent-voice.exe" mcp', 'agent-voice.exe", "mcp')
AGENTS = ("claude", "codex", "gemini", "cursor", "cline", "antigravity")


def _ours(command: str) -> bool:
    return any(m in command for m in MARKERS)


def _hook_argv(agent: str) -> list[str]:
    exe, *rest = self_command("hook", agent, windowed=False)
    return [Path(exe).as_posix(), *rest]


def hook_command(agent: str) -> str:
    exe, *rest = _hook_argv(agent)
    return " ".join([f'"{exe}"', *rest])


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
        hooks = [h for h in group.get("hooks", []) if not _ours(str(h.get("command", "")))]
        if hooks:
            out.append({**group, "hooks": hooks})
    return out


# -- Claude Code: ~/.claude/settings.json -------------------------------------------------
def claude(remove: bool = False) -> str:
    path = Path.home() / ".claude" / "settings.json"
    settings = _read_json(path)
    _backup(path)
    hooks = settings.setdefault("hooks", {})
    events = ("Stop", "PreToolUse", "UserPromptSubmit", "Notification", "PermissionRequest")
    for event in events:
        hooks[event] = _strip_ours(hooks.get(event, []))
    if not remove:
        entry = {"type": "command", "command": hook_command("claude"), "timeout": 20}
        hooks["Stop"].append({"hooks": [entry]})
        # async so tool calls never wait on the voice (Stop stays sync: async processes may be killed there).
        hooks["PreToolUse"].append({"matcher": "*", "hooks": [{**entry, "async": True}]})
        # Records the prompt's window and silences the voice as you send a new message.
        hooks["UserPromptSubmit"].append({"hooks": [{**entry, "timeout": 5}]})
        # Permission prompts: a chime in chime mode.
        hooks["Notification"].append({"hooks": [{**entry, "timeout": 5, "async": True}]})
        # Permission prompts answered by voice or from the widget (waits up to permissions.wait_seconds).
        hooks["PermissionRequest"].append({"matcher": "*", "hooks": [{**entry, "timeout": 300}]})
    for event in events:
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    if remove and _ours(str((settings.get("statusLine") or {}).get("command", ""))):
        settings.pop("statusLine")
    _write_json(path, settings)
    return f"{'removed from' if remove else 'installed in'} {path}"


def usage_line(enable: bool = True) -> str:
    """Claude Code's status line, fed to the notch: rate limits (5-hour, weekly) next to your threads.

    Opt-in: a custom status line replaces some of Claude Code's footer hints. Never replaces your own.
    """
    path = Path.home() / ".claude" / "settings.json"
    settings = _read_json(path)
    line = settings.get("statusLine") or {}
    if line and not _ours(str(line.get("command", ""))):
        return f"skipped: {path} already has your own status line"
    _backup(path)
    if enable:
        settings["statusLine"] = {"type": "command", "command": hook_command("statusline")}
    else:
        settings.pop("statusLine", None)
    _write_json(path, settings)
    return f"usage {'on' if enable else 'off'} ({path})"


# -- Gemini CLI: ~/.gemini/settings.json --------------------------------------------------
def gemini(remove: bool = False) -> str:
    path = Path.home() / ".gemini" / "settings.json"
    settings = _read_json(path)
    _backup(path)
    hooks = settings.setdefault("hooks", {})
    # AfterAgent: the reply. Notification: a tool waits for your confirmation ("needs you").
    for event in ("AfterAgent", "Notification"):
        hooks[event] = _strip_ours(hooks.get(event, []))
        if not remove:
            hooks[event].append(
                {"matcher": "*", "hooks": [{"type": "command", "command": hook_command("gemini"), "timeout": 20000}]}
            )
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    servers = settings.setdefault("mcpServers", {})
    for name in [n for n, s in servers.items() if _our_server(s)]:
        del servers[name]
    if not remove:
        servers["agent-voice"] = mcp_entry("gemini", hooked=True)  # live status and questions
    if not servers:
        del settings["mcpServers"]
    _write_json(path, settings)
    return f"{'removed from' if remove else 'installed in'} {path}"


# -- Cursor: ~/.cursor/hooks.json ---------------------------------------------------------
def cursor(remove: bool = False) -> str:
    path = Path.home() / ".cursor" / "hooks.json"
    data = _read_json(path)
    _backup(path)
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    entries = [h for h in hooks.get("afterAgentResponse", []) if not _ours(str(h.get("command", "")))]
    if not remove:
        entries.append({"command": hook_command("cursor")})
    hooks["afterAgentResponse"] = entries
    if not entries:
        del hooks["afterAgentResponse"]
    _write_json(path, data)
    # The MCP server too: live status and questions (the replies come from the hook).
    mcp = _mcp_json(Path.home() / ".cursor" / "mcp.json", "cursor", remove, hooked=True)
    return f"{'removed from' if remove else 'installed in'} {path}; MCP {mcp}"


# -- Codex CLI: ~/.codex/config.toml (top-level `notify`) ----------------------------------
_NOTIFY = re.compile(r"^notify\s*=.*$", re.M)


def codex(remove: bool = False) -> str:
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = _NOTIFY.search(text)
    if existing and not _ours(existing.group(0)) and not remove:
        return f"skipped: {path} already has its own `notify` ({existing.group(0)}); add agent-voice there by hand"
    _backup(path)
    text = _NOTIFY.sub("", text).lstrip("\n")
    if not remove:
        line = "notify = " + json.dumps(_hook_argv("codex"))
        # Must be top-level, i.e. before the first [table].
        text = f"{line}\n{text}"
    text = _codex_mcp(text, remove)  # live status and questions through MCP
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"{'removed from' if remove else 'installed in'} {path}"


# -- Cline: global hook files in ~/Documents/Cline/Hooks ------------------------------------
CLINE_HOOKS = ("UserPromptSubmit", "PreToolUse", "Notification")
CLINE_MARKER = "agent-voice hook"


def _cline_script(windows: bool) -> str:
    exe, *rest = _hook_argv("cline")
    if not windows:
        return f'#!/bin/sh\n# {CLINE_MARKER} (`agent-voice uninstall cline` removes it)\nexec "{exe}" {" ".join(rest)}\n'
    args = " ".join(f"'{a}'" for a in rest)
    return (
        f"# {CLINE_MARKER} (`agent-voice uninstall cline` removes it)\n"
        "$utf8 = New-Object Text.UTF8Encoding $false  # UTF-8 without BOM: a BOM would break the JSON\n"
        "[Console]::InputEncoding = $utf8\n"
        "$OutputEncoding = $utf8\n"
        "$payload = [Console]::In.ReadToEnd()\n"
        "# Cline waits for PreToolUse hooks: only the tools agent-voice speaks reach Python.\n"
        "if ($payload -match '\"hookName\"\\s*:\\s*\"PreToolUse\"' -and "
        "$payload -notmatch '\"toolName\"\\s*:\\s*\"(attempt_completion|ask_followup_question|plan_mode_respond)\"') "
        "{ '{}'; exit 0 }\n"
        f"$payload | & '{exe}' {args}\n"
    )


def cline(remove: bool = False) -> str:
    """Cline runs every file named after a hook in its global hooks folder (.ps1 on Windows)."""
    folder = Path.home() / "Documents" / "Cline" / "Hooks"
    windows = os.name == "nt"
    done, skipped = [], []
    for hook in CLINE_HOOKS:
        path = folder / (f"{hook}.ps1" if windows else hook)
        ours = path.exists() and CLINE_MARKER in path.read_text(encoding="utf-8", errors="replace")
        if path.exists() and not ours:
            skipped.append(path.name)  # the user's own hook: leave it alone
            continue
        if remove:
            if ours:
                path.unlink()
                done.append(path.name)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(_cline_script(windows), encoding="utf-8")
        if not windows:
            path.chmod(0o755)
        done.append(path.name)
    msg = f"{'removed' if remove else 'installed'} {', '.join(done) or 'nothing'} in {folder}"
    if skipped:
        msg += f"; skipped your own {', '.join(skipped)} (add agent-voice there by hand)"
    if not remove:
        msg += ". In Cline, turn on Settings > Features > Enable Hooks"
    return msg


# -- MCP: apps without hooks (Antigravity, Claude Desktop, Windsurf, VS Code) --------------------
def _app_data() -> Path:
    """Where desktop apps keep their settings: %APPDATA% on Windows, Application Support on macOS."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def mcp_entry(client: str, hooked: bool = False) -> dict:
    exe, *rest = self_command("mcp", "--client", client, *(["--hooks"] if hooked else []), windowed=False)
    return {"command": Path(exe).as_posix(), "args": rest}


def _our_server(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    command = " ".join([str(entry.get("command", "")), *map(str, entry.get("args") or [])])
    return _ours(command.replace(".exe ", '.exe" '))


def _mcp_servers(path: Path, key: str = "mcpServers") -> dict:
    try:
        servers = _read_json(path).get(key)
    except (OSError, ValueError):
        return {}
    return servers if isinstance(servers, dict) else {}


def _mcp_json(path: Path, client: str, remove: bool, key: str = "mcpServers", hooked: bool = False,
              extra: dict | None = None) -> str:
    """Add (or remove) our server in an app's MCP config; other servers stay as they are."""
    settings = _read_json(path)
    _backup(path)
    servers = settings.setdefault(key, {})
    for name in [n for n, s in servers.items() if _our_server(s)]:
        del servers[name]
    if not remove:
        servers["agent-voice"] = {**(extra or {}), **mcp_entry(client, hooked)}
    if not servers:
        del settings[key]
    _write_json(path, settings)
    return f"{'removed from' if remove else 'installed in'} {path}"


def _antigravity_configs() -> list[Path]:
    """Antigravity's MCP config: the older place, the newer one, or both when both exist."""
    places = [Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
              Path.home() / ".gemini" / "config" / "mcp_config.json"]
    found = [p for p in places if p.exists()]
    return found or places[:1]


def antigravity(remove: bool = False) -> str:
    return "; ".join(_mcp_json(p, "antigravity", remove) for p in _antigravity_configs())


def claude_desktop(remove: bool = False) -> str:
    return _mcp_json(_app_data() / "Claude" / "claude_desktop_config.json", "claude-desktop", remove)


def windsurf(remove: bool = False) -> str:
    return _mcp_json(Path.home() / ".codeium" / "windsurf" / "mcp_config.json", "windsurf", remove)


def vscode(remove: bool = False) -> str:
    return _mcp_json(_app_data() / "Code" / "User" / "mcp.json", "vscode", remove, key="servers",
                     extra={"type": "stdio"})


_CODEX_MCP = re.compile(r"\n*^\[mcp_servers\.agent-voice\]\n(?:^(?!\[).*\n?)*", re.M)


def _codex_mcp(text: str, remove: bool) -> str:
    """Codex reads MCP servers from [mcp_servers.<name>] tables (at the end: after every top-level key)."""
    text = _CODEX_MCP.sub("", text).rstrip("\n")
    if remove:
        return text + "\n" if text else ""
    entry = mcp_entry("codex", hooked=True)
    block = f"[mcp_servers.agent-voice]\ncommand = {json.dumps(entry['command'])}\nargs = {json.dumps(entry['args'])}\n"
    return f"{text}\n\n{block}" if text else block


INSTALLERS = {"claude": claude, "codex": codex, "gemini": gemini, "cursor": cursor, "cline": cline,
              "antigravity": antigravity, "claude-desktop": claude_desktop, "windsurf": windsurf, "vscode": vscode}

# Where each agent keeps its settings (relative to home): the folder existing means the agent is installed.
AGENT_DIRS = {"claude": ".claude", "codex": ".codex", "gemini": ".gemini", "cursor": ".cursor",
              "cline": "Documents/Cline", "antigravity": ".gemini/antigravity", "windsurf": ".codeium/windsurf"}


def _agent_dir(agent: str) -> Path:
    if agent == "claude-desktop":
        return _app_data() / "Claude"
    if agent == "vscode":
        return _app_data() / "Code" / "User"
    return Path.home() / AGENT_DIRS[agent]


AGENT_KEYS = (*AGENT_DIRS, "claude-desktop", "vscode")
MCP_ONLY = {"antigravity", "claude-desktop", "windsurf", "vscode"}


def _config_file(agent: str) -> Path:
    home = Path.home()
    if agent == "codex":
        return Path(os.environ.get("CODEX_HOME", home / ".codex")) / "config.toml"
    if agent == "antigravity":
        return _antigravity_configs()[0]
    return {"claude": home / ".claude" / "settings.json", "gemini": home / ".gemini" / "settings.json",
            "cursor": home / ".cursor" / "hooks.json", "windsurf": home / ".codeium" / "windsurf" / "mcp_config.json",
            "claude-desktop": _app_data() / "Claude" / "claude_desktop_config.json",
            "vscode": _app_data() / "Code" / "User" / "mcp.json"}[agent]


def is_installed(agent: str) -> bool:
    """Our hook (or MCP server) is in the agent's settings."""
    try:
        if agent == "cline":
            folder = Path.home() / "Documents" / "Cline" / "Hooks"
            return any(CLINE_MARKER in p.read_text(encoding="utf-8", errors="replace")
                       for p in folder.glob("*") if p.is_file())
        path = _config_file(agent)
        if agent in MCP_ONLY:
            return any(_our_server(s) for s in _mcp_servers(path, "servers" if agent == "vscode" else "mcpServers")
                       .values())
        # JSON escapes the quotes around the exe path; the markers are written unescaped.
        return path.exists() and _ours(path.read_text(encoding="utf-8", errors="replace").replace('\\"', '"'))
    except OSError:
        return False


def agent_states() -> dict[str, str]:
    """For each agent: 'on' (connected), 'off' (installed, not connected) or 'missing'."""
    return {agent: "on" if is_installed(agent) else "off" if _agent_dir(agent).is_dir() else "missing"
            for agent in AGENT_KEYS}


# -- Windows autostart ---------------------------------------------------------------------
def _startup_file() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "agent-voice.vbs"


def autostart(enable: bool = True) -> str:
    if os.name != "nt":
        return "autostart is only automated on Windows; run `agent-voice start` from your login scripts"
    path = _startup_file()
    if not enable:
        path.unlink(missing_ok=True)
        return f"removed {path}"
    exe, *rest = self_command("serve", "--background")
    cmd = " ".join([f'""{exe}""', *rest])
    path.write_text(f'CreateObject("WScript.Shell").Run "{cmd}", 0, False\n', encoding="utf-8")
    return f"created {path}"
