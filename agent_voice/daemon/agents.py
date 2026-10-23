"""Agents: which are connected, connecting one, and starting a new thread."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from agent_voice import claudeapp, install, launch, winfocus
from agent_voice.config import save_state
from agent_voice.daemon.phrases import NEW_THREAD
from agent_voice.daemon.sounds import _beep
from agent_voice.i18n import tr

log = logging.getLogger("agent_voice.server")


class AgentsMixin:
    """Agents: which are connected, connecting one, and starting a new thread."""

    def agent_states(self) -> dict[str, str]:
        """Which agents are connected; re-read every 30 s at most (status is polled 8 times a second).

        The browser is only "on" once its extension has talked to us: until then the bridge is ready but the
        extension isn't loaded ("setup").
        """
        if time.time() - self._agents_at > 30:
            self._agents, self._agents_at = install.agent_states(), time.time()
            self._usage_line = install.usage_line_state()
            if self._agents.get("browser") == "on" and not self.state.get("browser_seen"):
                self._agents["browser"] = "setup"
        return self._agents

    def bridge_seen(self) -> None:
        """The browser extension started its bridge: it's loaded and working."""
        if not self.state.get("browser_seen"):
            log.info("browser extension connected")
            self.state["browser_seen"] = round(time.time())
            save_state(self.state)
            self._show_banner(tr("The extension is connected: replies on ChatGPT, Claude and Gemini will be read out."),
                              "", "", kind="note", tag="browser")
        self._agents_at = 0.0

    # What to do after connecting each kind of agent, said in the banner.
    _NEXT_STEP = {"claude": "Open a new session to load it.", "codex": "Restart Codex to load it.",
                  "gemini": "Restart Gemini CLI to load it.", "cursor": "Restart Cursor to load it.",
                  "cline": "In Cline, turn on Settings > Features > Enable Hooks.",
                  "browser": "Now in Chrome or Edge: open chrome://extensions, turn on Developer mode, click "
                             "Load unpacked and pick the folder that just opened."}

    def install_agent(self, agent: str) -> None:
        """Connect an agent from the widget (same as `agent-voice install <agent>`), and say how it went."""
        name = install.AGENT_TITLES.get(agent, agent)
        try:
            result = install.INSTALLERS[agent]()
            log.info("install %s: %s", agent, result)
            if result.startswith("skipped"):
                text = f"{name}: {result}"
            else:
                text = tr("{name} connected.", name=name) + " " + (
                    tr(self._NEXT_STEP[agent]) if agent in self._NEXT_STEP
                    else tr("Restart {name} to load agent-voice.", name=name))
        except Exception as exc:  # a malformed settings file must not take the daemon down
            log.exception("install %s failed", agent)
            text = tr("Couldn't connect {name}: {why}", name=name, why=exc)
        self._agents_at = 0.0
        if agent == "browser":
            self.state.pop("browser_seen", None)  # "on" again once the extension says hello
            save_state(self.state)
            if sys.platform == "win32":
                os.startfile(install.extension_dir())  # the folder to pick in "Load unpacked"
        self._show_banner(text, "", "", kind="note", tag="agents")

    def _desktop_window(self) -> int:
        """The Claude desktop app's window: one a conversation was prompted from, else any open one."""
        for hwnd in [*self._session_hwnd.values(), self._target_hwnd]:
            if hwnd and winfocus.is_window(hwnd) and claudeapp.is_desktop_app(hwnd):
                return hwnd
        found = winfocus.find_window("Claude")
        return found if found and claudeapp.is_desktop_app(found) else 0

    def new_targets(self) -> list[str]:
        """Where a new thread can start: the desktop app (if open), then the agent CLIs found. Cached 30 s."""
        if time.time() - self._targets_at > 30:
            self._targets, self._targets_at = launch.targets(bool(self._desktop_window())), time.time()
        return self._targets

    def _default_target(self) -> str:
        """Where your last conversation was: the desktop app or a terminal."""
        targets = self.new_targets()
        last = self._session_hwnd.get(self._last_session) or self._target_hwnd
        if "claude-app" in targets and last and claudeapp.is_desktop_app(last):
            return "claude-app"
        return next((t for t in ("claude", "claude-app", "codex", "gemini") if t in targets), "")

    def new_thread(self, prompt: str, target: str = "", cwd: str = "", lang: str = "") -> bool:
        """Start a new conversation with `prompt`: in the Claude desktop app, or a CLI agent in a new terminal tab."""
        from agent_voice import lang as langmod

        prompt = prompt.strip()
        if not prompt:
            return False
        target = target or self._default_target()
        cwd = launch.default_cwd(cwd or self.state.get("last_cwd", ""))
        label = launch.LABELS.get(target, target or "an agent")
        log.info("new thread in %s (%s)", label, cwd)
        ok = False
        try:
            if target == "claude-app":
                hwnd = self._desktop_window()
                ok = bool(hwnd) and claudeapp.new_session(hwnd, prompt, Path(cwd).name)
                if ok:
                    self._new_window = {"hwnd": hwnd, "at": time.time()}
            elif target in launch.CLIS:
                launch.open_terminal(target, prompt, cwd)
                ok = True
        except Exception:
            log.exception("new thread failed")
        if ok:
            self._show_banner(prompt, "", "", kind="you", tag=tr("new · {where}", where=label))
            self.history = [{"text": prompt, "lang": lang or langmod.detect(prompt), "at": round(time.time())},
                            *self.history][:5]
        else:
            self._show_banner(tr("Couldn't start a new thread in {where}. Your prompt is on the clipboard.", where=label), "", "",
                              kind="note", tag="new thread")
            import pyperclip

            pyperclip.copy(prompt)
            _beep(300)
        return ok

    def listen_new_thread(self, target: str = "", cwd: str = "") -> None:
        """Dictate a new thread's first prompt: what you say next starts it."""
        self._new_pending = {"target": target, "cwd": cwd}
        self.reply_now(NEW_THREAD)

    def _ensure_start_menu(self) -> None:
        """Once (Windows): agent-voice in the Start menu, so it can be found and started like any app."""
        if not (os.name == "nt" or sys.platform.startswith("linux")) or self.state.get("start_menu"):
            return
        try:
            log.info("start menu: %s", install.start_menu(True))
        except Exception:
            log.exception("start menu entry failed")
        self.state["start_menu"] = True  # once: if you remove it, it stays removed
        save_state(self.state)
