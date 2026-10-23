"""Are you there? Away, in a call, full screen, headphones or speakers; the catch-up when you're back."""

from __future__ import annotations

import logging
import time

from agent_voice import calls, devices, media, winfocus
from agent_voice.daemon.common import _join
from agent_voice.i18n import plural, tr

log = logging.getLogger("agent_voice.server")


class PresenceMixin:
    """Are you there? Away, in a call, full screen, headphones or speakers; the catch-up when you're back."""

    def _event(self, kind: str, session: str, source: str, text: str = "") -> None:
        self._events = [*self._events[-49:], {"kind": kind, "session": session, "source": source,
                                              "text": " ".join(text.split())[:200], "at": time.time()}]

    def _away_watch(self) -> None:
        """Every few seconds: have you been away (no input, or in a call)? Tell you what happened when you're back."""
        while True:
            time.sleep(5)
            try:
                now = time.time()
                idle = winfocus.idle_seconds()
                self._check_away(idle, now)
                self._check_brief(now, idle)
                self._check_resumes(now)
                self._check_schedules(now)
                self._check_recap(now)
                self._check_meetings(now)
            except Exception:
                log.exception("away check failed")

    def _check_away(self, idle: float, now: float) -> None:
        cfg = self.config.get("catch_up", {})
        if not cfg.get("enabled", True):
            return
        # Do not disturb counts as away: what happened meanwhile is told when you turn it off.
        held = bool(self.in_call) or self._holding() or self.muted
        away = idle >= 60 * float(cfg.get("away_minutes", 5)) or held
        if away and not self._away_since:
            self._away_since = now - (0 if held and idle < 60 else idle)
        elif not away and self._away_since:
            since, self._away_since = self._away_since, 0.0
            events = [e for e in self._events if e["at"] >= since]
            if self._brief_due(now):  # the first time you're back in the morning: the brief tells it all
                self.brief(away=events)
            else:
                self._catch_up(events)

    def _catch_up(self, events: list[dict]) -> None:
        """'While you were away: Notch redesign finished; Codex needs you.' Once, as you come back."""
        if not events:
            return
        waiting = {item["session"] for item in self._needs_status()}
        done, needs, summaries = [], [], {}
        for e in events:
            title = self._title(e["session"], e["source"])
            if e["kind"] == "done":
                if title not in done:
                    done.append(title)
                if e["text"]:
                    summaries[title] = e["text"]  # its latest turn
            elif e["kind"] == "needs" and e["session"] in waiting and title not in needs:
                needs.append(title)
        def news(in_lang: str | None = None) -> list[str]:
            return ([plural(len(done), "{who} finished", "{who} finished", in_lang=in_lang, who=_join(done, in_lang))]
                    if done else []) + ([plural(len(needs), "{who} needs you", "{who} need you", in_lang=in_lang,
                                                who=_join(needs, in_lang))] if needs else [])

        parts = news()
        if not parts:
            return
        said = tr("While you were away: {news}.", in_lang=self.language, news="; ".join(news(self.language)))
        log.info("catch-up: %s", said)
        # The banner's header already says "While you were away": the body goes straight to the news. One
        # conversation finished: its summary right after its name, not the name twice.
        if len(done) == 1 and done[0] in summaries:
            body = [tr("{who} finished: {summary}", who=done[0], summary=summaries[done[0]])] + (
                [plural(len(needs), "{who} needs you.", "{who} need you.", who=_join(needs))] if needs else [])
        else:
            body = [f"{'; '.join(parts)}."] + [f"{t}: {x}" for t, x in list(summaries.items())[-3:]]
        self._show_banner(" ".join(body), "", "", kind="catchup")
        if not self.quiet:
            self._wake_cancel.set()
            self.speaker.speak(said, self.language)

    def _call_watch(self) -> None:
        """Every few seconds: is another app on the mic? Then chimes until the call ends. Headphones or speakers?"""
        while True:
            time.sleep(4)
            self._check_output()
            try:
                busy = winfocus.busy_state()
            except Exception:
                busy = ""
            if busy != self._fullscreen:
                log.info("focus: %s", busy or "off")
                if not busy and not self.in_call:
                    self._call_override = False
                self._fullscreen = busy
            if not self.config.get("auto_chime_in_calls", True):
                continue
            try:
                apps = calls.apps_using_mic()
            except Exception:
                log.exception("call check failed")
                time.sleep(60)
                continue
            label = calls.app_label(apps[0]) if apps else ""
            if label and not self.in_call:
                log.info("call started (%s): chimes until it ends", label)
                self.in_call, self._call_override = label, False
                if not self.state.get("chime"):
                    self.stop()  # stop talking over your call
                self._pause_for_call()
            elif not label and self.in_call:
                log.info("call ended")
                self.in_call, self._call_override = "", False
                self._resume_after_call()

    def _pause_for_call(self) -> None:
        """A song playing when a call starts: paused (the media key is a toggle, so only when it plays)."""
        if not self.config.get("pause_media_in_calls", True):
            return
        now = media.now_playing()
        if now and now.get("playing") and self.media_control("toggle"):
            self._call_paused = True
            log.info("music paused for the call (%s)", now.get("app", ""))

    def _resume_after_call(self) -> None:
        if not self._call_paused:
            return
        self._call_paused = False
        now = media.now_playing()
        if now and not now.get("playing"):  # not if you've played it again yourself meanwhile
            self.media_control("toggle")
            log.info("music resumed after the call")

    def _check_output(self) -> None:
        if self.speaker_device:
            devices.output_device()  # Settings' speaker plugged in since the start: PortAudio learns of it now
        before = self._output.kind
        try:
            now = self._output.poll()
        except Exception:
            log.debug("sound output unknown", exc_info=True)
            return
        if now != before:
            self._speaker_override = False  # a new device: your choice for the old one doesn't carry over
            if self.on_speakers and self.speaker.busy:
                self.stop()  # headphones unplugged mid-sentence: not out loud in the room
