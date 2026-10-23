"""Replies: queued, spoken (or shown as a banner), remembered per turn, replayed."""

from __future__ import annotations

import logging
import re
import time

from agent_voice import companion, textclean
from agent_voice.config import save_state
from agent_voice.daemon.common import THREAD_PITCHES, thread_color
from agent_voice.daemon.phrases import _voice_line
from agent_voice.hooks import strip_summary
from agent_voice.textclean import result_label

log = logging.getLogger("agent_voice.server")


class SpeechMixin:
    """Replies: queued, spoken (or shown as a banner), remembered per turn, replayed."""

    def notify(self, kind: str, data: dict | None = None) -> None:
        """The agent needs you: a question with options, a permission, a plan to approve."""
        data = data or {}
        log.info("notify kind=%s chime=%s", kind, self.chime)
        options = [str(o) for o in data.get("options") or [] if str(o).strip()]
        if kind == "message":  # a glance, not a voice: the banner only (and a chime when chimes are on)
            session, source = str(data.get("session") or ""), str(data.get("agent") or "")
            if session:
                self.threads[session] = {"source": source, "last": time.time()}
            self._show_banner(str(data.get("text") or ""), session, source, tag="note")
            if self.chime:
                self.play_chime("done")
            return
        if kind == "input":
            self.need_input(str(data.get("session") or ""), str(data.get("agent") or data.get("source") or ""),
                            str(data.get("text") or ""), announce=bool(data.get("announce", True)))
            return
        # A question with options, or (asked through MCP, with an id to collect the answer) a free one.
        if kind == "question" and (options or data.get("id")):
            self.question = {
                "session": str(data.get("session") or ""),
                "agent": str(data.get("agent") or ""),
                "question": str(data.get("question") or ""),
                "header": str(data.get("header") or ""),
                "options": options,
                "more": int(data.get("more") or 0),
                "id": int(data.get("id") or 0),
                "at": time.time(),
            }
            self._event("needs", self.question["session"], self.question["agent"], self.question["question"])
            self._push("needs", self._title(self.question["session"], self.question["agent"]),
                       self.question["question"] + (f" ({', '.join(options)})" if options else ""))
            if self.muted:
                return
            if self.chime:
                self.play_chime("attention")
                return
            preface = str(data.get("preface") or "")
            if preface.strip():
                self.speaker.speak(preface)
            self._wake_cancel.set()
            listed = " ".join(f"{i}: {o}." for i, o in enumerate(options, 1))
            self.speaker.speak(f"{self.question['question']} {listed}".strip())
            if self.config["auto_listen"].get("enabled") and self.transcriber is not None:
                self._reply_session = self.question["session"]
                self._listen_armed = True  # your spoken answer picks an option
            return
        if self.chime:
            self.play_chime("attention")

    def speak(
        self,
        text: str,
        lang: str | None = None,
        source: str = "",
        interrupt: bool = False,
        final: bool = False,
        voice: str | None = None,
        session: str = "",
        hwnd: int = 0,
        result: dict | None = None,
        full: str = "",
    ) -> int:
        """Queue text; `final` marks the end of an agent's turn and arms auto-listen.

        `hwnd`: the agent's window when its hook knows it (a terminal), so replies and messages can reach it.
        `result`: what the turn changed ({"files": 3, "tests": "passed"}), shown in the notch.
        `full`: the whole reply when `text` is only its summary (for the preview and the history).
        """
        if session and self.question and self.question["session"] == session and not self.question.get("id"):
            self.question = None  # the agent moved on: the question was answered
        if session:
            self.threads[session] = {"source": source, "last": time.time()}
            if text.strip() or final:
                self._settled(session)
            if hwnd and not self._session_hwnd.get(session):
                self._remember_hwnd(session, hwnd)
        with self._lock:
            if session and self._voice_session and session != self._voice_session and not interrupt and (
                    self.speaker.busy or self._listen_armed or self._listening):
                # Another thread is talking (or waiting for your reply): this one waits its turn.
                self._queue.append((text, lang, source, interrupt, final, voice, session, 0, result, full))
                log.info("queued speech from another thread (%d waiting)", len(self._queue))
                return 0
        if final and session and textclean.limit_reset(text) is not None:
            self.limit_hit(session, source, text)  # "5-hour limit reached ∙ resets 3pm": not a reply
            self.activity.pop(session, None)
            return 0
        if text.strip() and self._said_lately(text, session):
            # The same words from another conversation a moment ago: one reply reaching us twice (an app's MCP
            # `speak` beside its hooks). Said once, and the mic isn't opened a second time.
            log.info("already said a moment ago: source=%s final=%s", source, final)
            return 0
        if session and text.strip():
            self._not_limited(session)
        self._record_turn(session, source, text, lang, voice, final, full)
        if final:
            self.activity.pop(session, None)  # the agent is done working
            self._finished(session, source, text, result)
        if self._holding():
            # Focus (a call, presenting, full screen): nothing now; the catch-up tells you afterwards.
            log.info("held (%s): source=%s final=%s", self.focus, source, final)
            return 0
        if self.quiet:
            # Chimes or silent: no voice and no open mic; the end of a turn shows as a banner.
            log.info("speak source=%s output=%s final=%s", source, self.output, final)
            if final:
                turn = self._turn_text(session)
                shown = text.strip() if self.summary and text.strip() else turn or text.strip()
                if shown:
                    self._show_banner(shown, session, source, tag=result_label(result))
                self.play_chime("done")  # silent: no sound either
            return 0
        if interrupt:
            self.speaker.stop()
        if text.strip() or final:
            self._voice_session, self._voice_source = session, source  # whose voice this is, for the widget
            self._wake_cancel.set()  # never let the wake word hear our own voice
        if text.strip():
            # Read along: a new speech replaces the text, one queued behind the current one adds to it.
            busy = self.speaker.busy and session == self._voice_session
            self._reading = f"{self._reading}\n\n{text.strip()}" if busy and self._reading else text.strip()
        n = self.speaker.speak(text, lang, voice, pitch=self._pitch(session)) if text.strip() else 0
        log.info("speak source=%s sentences=%d final=%s", source, n, final)
        if final and self.config["auto_listen"].get("enabled") and self.transcriber is not None:
            self._reply_session = session
            self._listen_armed = True
        return n

    def _said_lately(self, text: str, session: str, window: float = 120.0) -> bool:
        """Was this said in the last `window` seconds by another conversation? (Remembered either way.) Within one
        conversation the hooks' own dedupe already decides."""
        key = " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())[:300]
        if not key:
            return False
        now = time.time()
        self._recent_said = {k: v for k, v in self._recent_said.items() if now - v[0] < window}
        before = self._recent_said.get(key)
        if before is not None and before[1] != session:
            return True  # still the first one's: it said them
        self._recent_said[key] = (now, session)
        return False

    def _record_turn(self, session: str, source: str, text: str, lang, voice, final: bool, full: str = "") -> None:
        """Collect what a conversation says in one turn (progress, then the end), for replay and copy; and all of
        it (`full`: the reply behind a summary) for the preview and the history."""
        turn = self._turns.get(session)
        if turn is None or (turn["done"] and (text.strip() or full.strip())):
            turn = {"texts": [], "full": [], "done": False, "lang": lang, "voice": voice, "source": source}
            self._turns[session] = turn
            self._turns = dict(list(self._turns.items())[-20:])
        if full.strip() or text.strip():
            turn["full"].append((full or text).strip())
        if text.strip():
            turn["texts"].append(text.strip())
            turn["lang"], turn["voice"] = lang, voice
            self._last_session = session
        if final:
            turn["done"] = True
            self._agent_todo(companion.agent_todos("\n".join(turn["full"])), source, session)

    AGENT_TODOS_PER_THREAD, AGENT_TODOS_OPEN = 2, 8

    def _agent_todo(self, tasks: list[str], source: str, session: str) -> None:
        """A reply's "To-do for you" line -> your list. Only the first of a turn; and not once that thread already has
        2 waiting on you, nor once your agents have 8 open: the list stays yours, not a log of their suggestions."""
        if not tasks:
            return
        mine = [t for t in self.companion.data["todos"] if not t["done"] and (t.get("source") or "you") != "you"]
        if len(mine) >= self.AGENT_TODOS_OPEN:
            return
        if session and sum(1 for t in mine if t.get("session") == session) >= self.AGENT_TODOS_PER_THREAD:
            return
        try:
            self.companion.todo("add", text=tasks[0], source=source or "agent", session=session)
        except ValueError:
            pass

    def _finished(self, session: str, source: str, text: str, result: dict | None) -> None:
        """A turn ended: keep its outcome for the notch, and note it for when you come back."""
        turn = self._turns.get(session)
        if turn is not None and result:
            turn["result"] = result
        summary = text.strip() or self._turn_text(session)
        self._event("done", session, source, textclean.clean(summary) if summary else "")
        said = _voice_line(summary)  # its own one-line summary, if it wrote one
        full = textclean.clean(strip_summary(self._turn_text(session) or summary or ""))
        self._push("done", self._title(session, source), said or full, full=full)
        self._log_turn(session, source, said or (textclean.clean(summary) if summary else ""))
        whole = self._turn_full(session)
        if self.history_db is not None and whole:
            try:
                self.history_db.add("reply", whole, session, source, self._title(session, source))
            except Exception:
                log.exception("history: reply not kept")
        if result and (result.get("files") or result.get("tests")):
            title = self._title(session, source)
            self.last_result = {"session": session, "title": title, "color": thread_color(session or title),
                                "files": int(result.get("files") or 0), "tests": result.get("tests") or "",
                                "at": round(time.time())}

    def _remember_hwnd(self, session: str, hwnd: int) -> None:
        self._session_hwnd[session] = hwnd
        self._session_hwnd = dict(list(self._session_hwnd.items())[-50:])
        self.state["session_hwnds"] = self._session_hwnd
        save_state(self.state)

    def _turn_text(self, session: str) -> str:
        return "\n\n".join((self._turns.get(session) or {}).get("texts") or [])

    def _turn_full(self, session: str) -> str:
        """The turn as the agent wrote it (not only what was read out), without its summary line."""
        turn = self._turns.get(session) or {}
        return strip_summary("\n\n".join(turn.get("full") or turn.get("texts") or [])).strip()

    def _pitch(self, session: str) -> float:
        """This conversation's voice: the first one natural, the next ones a little lower or higher."""
        if not session or not self.config.get("thread_voices", True):
            return 1.0
        if session not in self._pitches:
            self._pitches[session] = THREAD_PITCHES[len(self._pitches) % len(THREAD_PITCHES)]
        return self._pitches[session]

    def copy_reply(self, session: str = "") -> bool:
        import pyperclip

        text = self._turn_text(session or self._last_session)
        if text:
            pyperclip.copy(text)
        return bool(text)

    def _reading_status(self) -> dict | None:
        """Voice mode: the text being read out, for the widget to show while (and only while) it plays."""
        if not (self.speaker.busy and self._reading and self.config.get("read_along", True)) or self.quiet:
            return None
        session = self._voice_session
        title = self._title(session, self._voice_source)
        return {"text": textclean.clean(self._reading)[:1500], "title": title,
                "color": thread_color(session or title), "session": session}

    def _show_banner(self, text: str, session: str, source: str, kind: str = "reply", tag: str = "",
                     extra: dict | None = None) -> None:
        """A notification under the notch: a reply in chime mode, or ("you") what you just said.

        Do not disturb: only what you do yourself shows (and your own timers, silently); the rest waits for the
        catch-up when you're back."""
        if self.muted and kind not in ("you", "pending", "timer", "reminder", "pomodoro", "meeting"):
            return
        title = self._title(session, source) if session or source else ""
        self.banner = {"id": (self.banner or {}).get("id", 0) + 1, "text": textclean.clean(text)[:800],
                       "title": title, "color": thread_color(session or title), "session": session,
                       "kind": kind, "tag": tag, "at": time.time(), **(extra or {})}

    def replay(self, session: str = "") -> None:
        """Say a conversation's last turn again (the last one heard by default); the mic stays closed."""
        session = session or self._last_session
        turn = self._turns.get(session)
        if not turn or not turn["texts"]:
            return
        if self.quiet:  # no voice now: show it instead
            self._show_banner(self._turn_text(session), session, turn["source"])
            return
        self.speaker.stop()
        self._wake_cancel.set()
        self._voice_session, self._voice_source = session, turn["source"]
        self._reading = self._turn_text(session)
        self.speaker.speak(self._turn_text(session), turn["lang"], turn["voice"], pitch=self._pitch(session))

    def stop(self) -> None:
        self.speaker.stop()
        self.cancel_listen()

    def _play_next_thread(self) -> None:
        """Speak the next waiting thread, announced by its name."""
        with self._lock:
            if not self._queue:
                return
            session = self._queue[0][6]
            jobs = [job for job in self._queue if job[6] == session]
            self._queue = [job for job in self._queue if job[6] != session]
        title = self._title(session, jobs[0][2])
        log.info("playing queued thread %r (%d left)", title, len(self._queue))
        self._voice_session, self._voice_source = session, jobs[0][2]
        if not self.quiet:
            self.speaker.speak(f"{title}.", pitch=self._pitch(session))
        for job in jobs:
            self.speak(*job)
