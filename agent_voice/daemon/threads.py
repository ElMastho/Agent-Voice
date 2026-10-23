"""Conversations: which window each lives in, what each is doing, and typing a reply into it."""

from __future__ import annotations

import logging
import threading
import time

from agent_voice import claudeapp, keys, textclean, winfocus
from agent_voice.config import save_state
from agent_voice.daemon.common import AGENT_TITLES, thread_color
from agent_voice.daemon.phrases import _names, names_match, parse_route
from agent_voice.daemon.sounds import _beep
from agent_voice.i18n import tr

log = logging.getLogger("agent_voice.server")


class ThreadsMixin:
    """Conversations: which window each lives in, what each is doing, and typing a reply into it."""

    def _title_for(self, hwnd: int, session: str) -> str:
        """Desktop app conversation title for an agent session ("" when it isn't one)."""
        if not (hwnd and session and claudeapp.is_desktop_app(hwnd)):
            return ""
        return claudeapp.session_title(session) or self._titles.get(session, "")

    def _find_thread(self, name: str, among: set[str] | None = None) -> str | None:
        """The most recent conversation called `name` ("Codex", "the notch redesign")."""
        for session, _info in sorted(self.threads.items(), key=lambda kv: -kv[1]["last"]):
            if among is not None and session not in among:
                continue
            if any(label and names_match(name, label) for label in self._labels(session)):
                return session
        return None

    def _mentioned(self, text: str, among: set[str]) -> str | None:
        """A conversation named somewhere in `text` ("approve Codex", "deny the notch one")."""
        words = _names(text)
        for session in among:
            for label in self._labels(session):
                if label and _names(label) and _names(label) <= words:
                    return session
        return None

    def _routed(self, text: str) -> tuple[str, str] | None:
        """'Tell Codex to run the tests' -> (Codex's session, 'Run the tests'), when we know that conversation."""
        parsed = parse_route(text)
        if not parsed:
            return None
        session = self._find_thread(parsed[0])
        return (session, parsed[1]) if session is not None else None

    def _expand(self, text: str) -> tuple[str, bool]:
        """A saved prompt's full text when `text` is its name ("run the tests"); (text, expanded?)."""
        full = textclean.expand_shortcut(text, self.config.get("shortcuts") or {})
        return (full, True) if full else (text, False)

    def _send_to(self, session: str, message: str, lang: str, spoken: bool = False) -> None:
        """Type a message into a conversation's own window (and send it), then come back.

        `spoken`: you said it, so it waits a moment under the notch first (scheduled prompts and resumes don't)."""
        message, expanded = self._expand(message)
        hwnd = self._session_hwnd.get(session, 0)
        title = self._title(session, self.threads.get(session, {}).get("source", ""))
        if not (hwnd and winfocus.is_window(hwnd)):
            log.info("no window known for %r: message not sent", title)
            self._show_banner(tr("I don't know where {title} is yet: send it one message from its window first.",
                                 title=title),
                              session, "", kind="you", tag="not sent")
            _beep(300)
            return
        self._heard(message, lang, session, tag="shortcut" if expanded else "sent")
        if spoken:
            self._send_spoken(message, hwnd, session, lang)
        else:
            self._paste(message, hwnd, submit=True, session=session)

    def send_text(self, session: str, text: str) -> None:
        """A reply typed in the notch: into that conversation's window, sent, and back to where you were."""
        from agent_voice import lang

        text = text.strip()
        if not text:
            return
        session = session or self._last_session
        text, expanded = self._expand(text)
        self._heard(text, lang.detect(text), session, tag="shortcut" if expanded else "sent")
        self._paste(text, self._hwnd_for(session), submit=True, session=session)

    def prompt_submitted(self, hwnd: int, session: str = "", source: str = "", title: str = "", cwd: str = "",
                         prompt: str = "") -> None:
        """The user sent a new prompt: remember its window (and conversation), and stop talking/listening.

        `title`: the conversation's name when the caller knows it (a browser tab's title). `prompt`: what you wrote,
        for the history.
        """
        if session and title:
            self._file_titles[session] = title
        if cwd and cwd != self.state.get("last_cwd"):
            self.state["last_cwd"] = cwd  # where a new thread starts by default
            recent = [c for c in self.state.get("recent_cwds") or [] if c != cwd]
            self.state["recent_cwds"] = [cwd, *recent][:8]  # offered in the new-thread box
            save_state(self.state)
        pending = self._new_window
        if session and pending and session not in self._session_hwnd and time.time() - pending["at"] < 60:
            hwnd = pending["hwnd"]  # a session we just started in the desktop app: it lives in that window
            self._new_window = {}
        if hwnd and hwnd != self._target_hwnd:
            self._target_hwnd = hwnd
            self.state["target_hwnd"] = hwnd
            save_state(self.state)
        if hwnd and session and claudeapp.is_desktop_app(hwnd):
            threading.Thread(target=self._learn_title, args=(hwnd, session), daemon=True).start()
        if session:
            with self._lock:
                self._queue = [job for job in self._queue if job[6] != session]  # you moved on in that thread
            source = source or self.threads.get(session, {}).get("source", "")
            self.threads[session] = {"source": source, "last": time.time()}
            if hwnd and self._session_hwnd.get(session) != hwnd:
                self._remember_hwnd(session, hwnd)
            self._settled(session)
            if session in self._turns:
                self._turns[session]["done"] = True  # what comes next is a new turn
            now = time.time()
            self.activity[session] = {"source": source, "started": now, "last": now, "steps": 0, "label": "Thinking"}
            if prompt.strip() and self.history_db is not None:
                try:
                    self.history_db.add("you", prompt, session, source, self._title(session, source))
                except Exception:
                    log.exception("history: prompt not kept")
        self.stop()

    def _hwnd_for(self, session: str) -> int:
        """The window a conversation was prompted from; the last prompted window otherwise."""
        hwnd = self._session_hwnd.get(session, 0)
        return hwnd if hwnd and winfocus.is_window(hwnd) else self._target_hwnd

    def tool_used(self, session: str, source: str, label: str) -> None:
        """An agent is working: one more step, and what it's doing now."""
        now = time.time()
        act = self.activity.get(session)
        if act is None or now - act["last"] > 900:
            act = {"source": source, "started": now, "steps": 0}
            self.activity[session] = act
        act.update(last=now, label=label[:80], steps=act["steps"] + 1)
        self._settled(session)
        if session:
            self.threads[session] = {"source": source or self.threads.get(session, {}).get("source", ""),
                                     "last": now}

    def activity_done(self, session: str) -> None:
        """An agent says it finished working (MCP set_status done): no reply to speak, just stop the timer."""
        self.activity.pop(session, None)

    def _activity_status(self) -> dict | None:
        """The busiest-looking conversation's live activity (the most recent), and how many others work."""
        now = time.time()
        live = {s: a for s, a in self.activity.items() if now - a["last"] < 900}
        self.activity = live
        if not live:
            return None
        session, act = max(live.items(), key=lambda kv: kv[1]["last"])
        title = self._title(session, act["source"])
        return {"session": session, "title": title, "color": thread_color(session or title),
                "label": act.get("label", ""), "steps": act["steps"], "elapsed": int(now - act["started"]),
                "others": len(live) - 1, "context": (self.usage.get(session) or {}).get("context")}

    def _learn_title(self, hwnd: int, session: str) -> None:
        """You type in the conversation on screen, so its title belongs to this session.

        Not when another session already owns that title: a prompt sent by Claude to a side
        session it started also fires this hook, while you are still looking at your own.
        """
        title = claudeapp.current_title(hwnd)
        if not title or any(t == title for s, t in self._titles.items() if s != session):
            return
        if self._titles.get(session) != title:
            self._titles[session] = title
            self._titles = dict(list(self._titles.items())[-50:])
            self.state["desktop_titles"] = self._titles
            save_state(self.state)
            log.info("conversation %r is session %s", title, session[:8])

    def _paste(self, text: str, hwnd: int = 0, submit: bool = False, session: str = "") -> None:
        import pyperclip

        self._wait_modifiers_released()
        title = self._title_for(hwnd, session)
        if title and submit and self.config.get("background_send", True):
            # Desktop app: type and send it in place; the app stays wherever it is.
            if claudeapp.send_message(hwnd, title, text):
                pyperclip.copy(text)
                log.info("sent to %r in the background", title)
                return
            log.warning("background send to %r failed; typing it instead", title)
        # Where you were: once the reply is sent we hand the screen back to it.
        came_from = winfocus.foreground()
        was_minimized = winfocus.is_minimized(hwnd)
        switched = bool(hwnd) and came_from != hwnd
        if hwnd and not winfocus.focus(hwnd):
            # Don't type into whatever app is in front: leave the text on the clipboard instead.
            log.warning("could not focus target window %s (%s); text left on clipboard", hwnd, winfocus.title(hwnd))
            pyperclip.copy(text)
            _beep(300)
            return
        # Desktop app: several conversations share one window; open the one this reply answers.
        if title and not claudeapp.select_session(hwnd, title):
            log.warning("could not open conversation %r; text left on clipboard", title)
            pyperclip.copy(text)
            _beep(300)
            return
        time.sleep(0.1)
        # The text stays on the clipboard afterwards, so a reply that landed in the wrong place
        # can always be pasted again by hand.
        pyperclip.copy(text)
        keys.paste()
        time.sleep(0.3)
        if submit:
            keys.enter()
        if submit and switched and came_from:
            time.sleep(0.2)  # let the agent window register the Enter
            if was_minimized:
                winfocus.minimize(hwnd)
            winfocus.focus(came_from)

    def _title(self, session: str, source: str = "") -> str:
        """A conversation's name: its desktop-app title when known, else the one Claude Code gave the session (its
        tab's), else the agent's name."""
        title = self._titles.get(session, "")
        if session and not title:
            if session not in self._file_titles:  # app session files: read once per session, not per poll
                self._file_titles[session] = claudeapp.session_title(session)
            title = self._file_titles[session]
        if session and not title and source in ("claude", ""):
            title = self._transcript_titles.title(session)
        return title or AGENT_TITLES.get(source, source.capitalize())

    def _thread_status(self) -> dict | None:
        """The conversation the voice (or the open mic) belongs to: its title and a colour of its own."""
        q, perm = self.question, self.permission
        if perm:
            session, source = perm["session"], perm["source"]
        elif q:
            session, source = q["session"], q["agent"]
        elif self._mic_phase:
            session, source = self._reply_session, self._voice_source
        elif self.speaker.busy:
            session, source = self._voice_session, self._voice_source
        else:
            return None
        title = self._title(session, source)
        if not title:
            return None
        return {"title": title, "color": thread_color(session or title)}

    def _threads_status(self) -> list[dict]:
        """The last few conversations heard from, newest first, with what each is doing."""
        queued = {job[6] for job in self._queue}
        asking = {p["session"] for p in self._perms.values()}
        out = []
        for session, info in sorted(self.threads.items(), key=lambda kv: -kv[1]["last"])[:4]:
            if session in asking:
                state = "permission"
            elif session == (self.question or {}).get("session"):
                state = "question"
            elif session in self.attention:
                state = "input"
            elif session == self._voice_session and self.speaker.busy:
                state = "speaking"
            elif session == self._reply_session and self._mic_phase:
                state = "listening"
            elif session in queued:
                state = "queued"
            else:
                state = ""
            title = self._title(session, info["source"])
            if not state and session in self.activity:
                state = "working"
            reply = self._turn_text(session)
            turn = self._turns.get(session) or {}
            out.append({"session": session, "title": title, "color": thread_color(session or title),
                        "state": state, "ago": int(time.time() - info["last"]),
                        "reply": " ".join(reply.split())[-160:],
                        "context": (self.usage.get(session) or {}).get("context"),
                        "tokens": (self.usage.get(session) or {}).get("tokens"),
                        "result": turn.get("result") if turn.get("done") else None})
        return out

    def thread_detail(self, session: str) -> dict:
        """A conversation's last turn in full (markdown), what it changed, and its numbers today."""
        info = self.threads.get(session)
        if info is None:
            return {}
        turn = self._turns.get(session) or {}
        title = self._title(session, info["source"])
        day = self._day()["threads"].get(session) or {}
        return {"session": session, "title": title, "color": thread_color(session or title),
                "text": self._turn_full(session)[-12000:], "result": turn.get("result"),
                "ago": int(time.time() - info["last"]), "turns": day.get("turns", 0),
                "tokens": (self.usage.get(session) or {}).get("tokens") or day.get("tokens", 0),
                "context": (self.usage.get(session) or {}).get("context")}

    def attach(self, paths: list[str], text: str = "", session: str = "") -> None:
        """Files (or text) dropped on the notch: typed into the conversation's input, not sent, so you can add to it."""
        session = session or self._last_session
        quoted = " ".join(f'"{p}"' if " " in p else p for p in paths)
        message = " ".join(x for x in (quoted, text.strip()) if x)
        if not message:
            return
        hwnd = self._hwnd_for(session)
        log.info("dropped %d file(s) into %r", len(paths), self._title(session))
        self._paste(message + " ", hwnd, submit=False, session=session)

    def open_thread(self, session: str) -> None:
        """Bring a conversation to the front (widget thread list)."""
        hwnd = self._hwnd_for(session)
        title = self._title_for(hwnd, session)
        if hwnd:
            winfocus.focus(hwnd)
            if title:
                claudeapp.select_session(hwnd, title)
