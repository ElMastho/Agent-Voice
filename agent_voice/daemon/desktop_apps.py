"""Desktop AI apps without hooks (ChatGPT, Copilot...): their notifications are their replies."""

from __future__ import annotations

import logging
import time

from agent_voice import winfocus
from agent_voice.config import save_state
from agent_voice.daemon.common import AGENT_TITLES

log = logging.getLogger("agent_voice.server")


class DesktopAppsMixin:
    """Desktop AI apps without hooks (ChatGPT, Copilot...): their notifications are their replies."""

    @property
    def desktop_apps(self) -> bool:
        if "desktop_apps" in self.state:
            return bool(self.state["desktop_apps"])
        return bool(self.config.get("desktop_apps", {}).get("enabled", False))

    def set_desktop_apps(self, on: bool) -> None:
        self.state["desktop_apps"] = on
        save_state(self.state)
        log.info("desktop_apps=%s", on)
        self._confirm("Desktop apps on." if on else "Desktop apps off.")

    def _apps_watch(self) -> None:
        """ChatGPT, Copilot... have no hooks: their notifications are their replies (opt-in)."""
        from agent_voice import apps

        watcher = None
        while True:
            time.sleep(3)
            if not self.desktop_apps or not apps.available():
                watcher = None
                continue
            try:
                if watcher is None:
                    watcher = apps.Watcher(self.config.get("desktop_apps", {}).get("apps"))
                for toast in watcher.poll():
                    self._app_reply(toast)
            except Exception:
                log.exception("desktop apps check failed")
                time.sleep(60)

    def _app_reply(self, toast: dict) -> None:
        app = toast["app"]
        label = AGENT_TITLES.get(app, app.capitalize())
        log.info("%s notification (%d chars)", label, len(toast["text"]))
        hwnd = winfocus.find_window(label)
        self.jobs.put(lambda: self.speak(toast["text"], source=app, final=True, session=f"app-{app}", hwnd=hwnd))
