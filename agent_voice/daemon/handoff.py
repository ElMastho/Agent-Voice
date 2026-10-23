"""Handing things over: your selection, a screenshot or the window you look at, sent to a thread."""

from __future__ import annotations

import logging
import time

from agent_voice import keys, winfocus
from agent_voice.config import HOME
from agent_voice.daemon.sounds import _beep
from agent_voice.i18n import tr

log = logging.getLogger("agent_voice.server")


class HandoffMixin:
    """Handing things over: your selection, a screenshot or the window you look at, sent to a thread."""

    def _copy_selection(self, settle: float = 0.25) -> tuple[str, str]:
        """(the text selected in the app in front, what the clipboard held): copied, then the clipboard put back."""
        import pyperclip

        self._wait_modifiers_released()
        previous = pyperclip.paste()
        pyperclip.copy("")
        keys.copy()
        time.sleep(settle)
        selected = pyperclip.paste()
        pyperclip.copy(previous)
        return selected, previous

    def handoff(self, name: str, message: str, lang: str) -> None:
        """'Send this to Codex': the selection (or the screenshot on the clipboard) goes to that conversation.

        With a message ("... and ask it to fix it") it's sent; without, it waits in the box for your question.
        """
        session = self._find_thread(name)
        if session is None:
            self._show_banner(tr("I don't know a thread called {name}.", name=name), "", "", kind="note", tag="not sent")
            _beep(300)
            return
        window = winfocus.foreground()  # what you're looking at, before anything moves
        content = self._copy_selection()[0].strip()
        if not content:
            # A screenshot you copied, else a picture of the window you're looking at.
            content = self._save_shot(winfocus.clipboard_png() or winfocus.window_png(window))
        title = self._title(session, self.threads.get(session, {}).get("source", ""))
        if not content:
            self._show_banner(tr("Nothing to send to {title}: select some text or copy a screenshot first.", title=title), "", "",
                              kind="note", tag="not sent")
            _beep(300)
            return
        self._deliver(session, message, content, lang, tr("(to {title}) {text}", title=title, text=content[:120]))

    def look_at(self, session: str, message: str, lang: str) -> None:
        """'Look at this': a picture of the window in front goes to the conversation, with what you asked."""
        window = winfocus.foreground()
        path = self._save_shot(winfocus.window_png(window))
        if not path:
            self._show_banner(tr("Couldn't take a picture of that window."), "", "", kind="note", tag="not sent")
            _beep(300)
            return
        log.info("look at %r", winfocus.title(window)[:60])
        self._deliver(session, message, path, lang,
                      tr("(a picture of {window})", window=winfocus.title(window)[:60] or tr("the window")))

    def _deliver(self, session: str, message: str, content: str, lang: str, shown: str) -> None:
        """Content for a conversation: with a message, sent (after the take-back); alone, typed in for your question."""
        message, _ = self._expand(message) if message else ("", False)
        log.info("to %r: %d chars, %s", self._title(session), len(content), "sent" if message else "not sent")
        self._heard(message or shown, lang, session, tag="sent" if message else "handoff")
        hwnd = self._hwnd_for(session)
        if message:
            self._send_spoken(f"{message}\n\n{content}", hwnd, session, lang)
        else:
            self._paste(content + " ", hwnd, submit=False, session=session)

    @staticmethod
    def _save_shot(png: bytes) -> str:
        """Keep a picture for an agent to open (the last 20 stay); its path, or '' with no picture."""
        if not png:
            return ""
        shots = HOME / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        for old in sorted(shots.glob("*.png"))[:-20]:
            old.unlink(missing_ok=True)
        path = shots / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        path.write_bytes(png)
        return str(path).replace("\\", "/")
