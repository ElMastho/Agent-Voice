"""Start a new conversation with an agent: in the Claude desktop app, or a CLI agent in a new terminal tab.

`agent-voice launch <agent> <prompt file>` is the trampoline the new tab runs: it reads the prompt from a
file (no quoting problems, whatever the prompt contains), deletes it, and starts the agent with it.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agent_voice.client import self_command
from agent_voice.config import HOME

# CLI agents: the program, and how to start it with a first prompt (staying interactive afterwards).
CLIS = {"claude": ("claude", []), "codex": ("codex", []), "gemini": ("gemini", ["-i"])}
LABELS = {"claude-app": "Claude app", "claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI"}
PROMPTS = HOME / "prompts"


def find_cli(agent: str) -> str | None:
    """The agent's program: on PATH, or where its installer puts it (Claude Code: ~/.local/bin)."""
    exe = CLIS[agent][0]
    found = shutil.which(exe)
    if found:
        return found
    for folder in (Path.home() / ".local" / "bin", Path.home() / ".claude" / "local", Path.home() / ".npm-global" / "bin"):
        for name in (f"{exe}.exe", exe, f"{exe}.cmd"):
            if (folder / name).is_file():
                return str(folder / name)
    return None


def targets(desktop_app: bool) -> list[str]:
    """Where a new conversation can start: the Claude desktop app (when it's open), then the CLIs installed."""
    return (["claude-app"] if desktop_app else []) + [a for a in CLIS if find_cli(a)]


def command(agent: str, prompt: str) -> list[str]:
    exe = find_cli(agent)
    if not exe:
        raise FileNotFoundError(f"{LABELS[agent]} isn't installed")
    return [exe, *CLIS[agent][1], prompt]


def open_terminal(agent: str, prompt: str, cwd: str) -> None:
    """A new terminal tab (Windows Terminal when there is one) in `cwd`, running the agent on `prompt`."""
    PROMPTS.mkdir(parents=True, exist_ok=True)
    path = PROMPTS / f"{agent}-{time.time_ns()}.txt"
    path.write_text(prompt, encoding="utf-8")
    tramp = self_command("launch", agent, str(path), windowed=False)
    if sys.platform == "win32":
        wt = shutil.which("wt")
        if wt:
            subprocess.Popen([wt, "-w", "0", "new-tab", "-d", cwd, "--title", LABELS[agent], *tramp])
        else:
            subprocess.Popen(tramp, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    elif sys.platform == "darwin":
        line = f"cd {shlex.quote(cwd)} && {shlex.join(tramp)}"
        script = f'tell application "Terminal" to do script "{line.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        subprocess.Popen(["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or "xterm"
        subprocess.Popen([term, "--", *tramp] if "gnome" in term else [term, "-e", shlex.join(tramp)], cwd=cwd)


def run(agent: str, prompt_file: str) -> int:
    """In the new tab: start the agent on the prompt, and keep the tab open if it can't."""
    path = Path(prompt_file)
    try:
        prompt = path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)
    try:
        return subprocess.call(command(agent, prompt))
    except (OSError, KeyError) as exc:
        print(f"agent-voice: could not start {agent}: {exc}")
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to close this tab...")
        return 1


def default_cwd(last: str = "") -> str:
    return last if last and os.path.isdir(last) else str(Path.home())
