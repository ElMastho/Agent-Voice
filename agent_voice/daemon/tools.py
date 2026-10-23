"""Utils: the Pomodoro, timers, stopwatch, to-do, reminders and notes (the state is companion.Companion)."""

from __future__ import annotations

import logging
import time

from agent_voice import companion, media
from agent_voice.config import save_state
from agent_voice.daemon.phrases import COMPANION
from agent_voice.daemon.sounds import _beep
from agent_voice.i18n import tr

log = logging.getLogger("agent_voice.server")


class ToolsMixin:
    """Utils: the Pomodoro, timers, stopwatch, to-do, reminders and notes (the state is companion.Companion)."""

    def _save_companion(self, data: dict) -> None:
        self.state["companion"] = data
        save_state(self.state)

    def _companion_loop(self) -> None:
        """Sleep until the next timer or reminder is due (woken by any change): no polling, nothing per second."""
        while True:
            due = self.companion.next_due()
            self.companion.wake.wait(None if due is None else max(0.05, min(3600.0, due - time.time())))
            self.companion.wake.clear()
            try:
                for kind, item in self.companion.due():
                    self._companion_due(kind, item)
            except Exception:
                log.exception("companion check failed")

    def _companion_due(self, kind: str, item: dict, now: float | None = None) -> None:
        """A timer ended or a reminder's time came: the banner (even in Do not disturb, silently), a chime, the voice."""
        now = time.time() if now is None else now
        late = now - float(item.get("end") or item.get("at") or now) > 120  # the PC was asleep or off
        if kind == "pomodoro":
            self._pomodoro_due(item, late)
            return
        if kind == "timer":
            what = item.get("label") or companion.duration_words(int(item.get("total") or 0))
            title, text = "Timer done", what
            said = tr("Time's up: {what}.", in_lang=self.language, what=what) if item.get("label") else tr(
                "Time's up.", in_lang=self.language)
            snooze = {"kind": "timer", "seconds": 300, "label": item.get("label") or ""}
        else:
            title, text = "Reminder", item["text"]
            said = tr("Reminder: {text}", in_lang=self.language, text=item["text"])
            snooze = {"kind": "reminder", "seconds": 600, "text": item["text"]}
        if late:
            text += tr(" (due at {time})", time=time.strftime('%H:%M', time.localtime(item.get('end') or item.get('at'))))
        log.info("companion: %s due", kind)
        self._show_banner(text, "", "", kind=kind, tag=title, extra={"snooze": snooze})
        self._push("scheduled", tr(title), text)
        if self.muted or late:
            return
        self.play_chime("attention")
        if not self.chime:
            self.speaker.speak(said, self.language)

    def _pomodoro_due(self, ended: dict, late: bool) -> None:
        """A focus session or a break ended: the next one has started. Say so, softly."""
        nxt = ended["next"]
        minutes = companion.duration_words(int(nxt["length"]))
        long = nxt["phase"] == "long"
        if ended["phase"] == "focus":
            text = tr("Focus done ({n} today): {minutes} long break." if long else
                      "Focus done ({n} today): {minutes} break.", n=nxt['today'], minutes=minutes)
            said = tr("Time for a long break: {minutes}." if long else "Time for a break: {minutes}.",
                      in_lang=self.language, minutes=minutes)
        else:
            text = tr("Break over: {minutes} of focus.", minutes=minutes)
            said = tr("Back to focus.", in_lang=self.language)
        self._show_banner(text, "", "", kind="pomodoro", tag="Pomodoro")
        if self.muted or late:
            return
        self.play_chime("done" if ended["phase"] == "focus" else "attention")
        if not self.chime:
            self.speaker.speak(said, self.language)

    def companion_said(self, text: str, lang: str = "") -> bool:
        """'Timer 10 minutes', 'add milk to my list', 'remind me at 4 to call Paul': done here; False if it isn't one."""
        action = companion.parse_command(text, time.time())
        if action is None:
            return False
        self._heard(text, lang, "", tag="companion")
        kind = action["kind"]
        try:
            if kind == "timer":
                self.companion.timer("start", seconds=action["seconds"], label=action["label"])
            elif kind == "timer_cancel":
                self.companion.timer("cancel")
            elif kind == "stopwatch":
                self.companion.stopwatch(action["action"])
            elif kind == "pomodoro":
                self.companion.pomodoro(action["action"])
            elif kind == "note":
                self.add_note(action["text"])
            elif kind == "media":
                media.control(action["action"])
            elif kind == "todo":
                self.companion.todo("add", text=action["text"], source="you")
            elif kind == "reminder":
                self.companion.reminder("add", text=action["text"], at=action["at"])
        except ValueError as exc:
            self._show_banner(tr("Not done: {why}.", why=tr(str(exc))), "", "", kind="note", tag="companion")
            _beep(300)
            return True
        _beep(880)
        return True

    def add_note(self, text: str) -> dict:
        item = self.companion.note("add", text=text)
        if self.history_db is not None:  # searchable from History, with everything else
            try:
                self.history_db.add("note", item["text"], title=tr("Note"))
            except Exception:
                log.exception("note not kept in the history")
        return item

    def listen_companion(self) -> None:
        """The companion's mic: what you say next is a timer, a to-do, a reminder (else it joins the to-do list)."""
        self.reply_now(COMPANION)
