"""The wake word: "Hey Claude, ..." while idle."""

from __future__ import annotations

import logging
import threading
import time

from agent_voice.config import save_state
from agent_voice.daemon.phrases import (
    _new_target,
    match_brief,
    match_join,
    match_recap,
    match_resume,
    parse_handoff,
    parse_look,
    parse_new_thread,
    strip_wake,
)
from agent_voice.daemon.sounds import _beep

log = logging.getLogger("agent_voice.server")


class WakeMixin:
    """The wake word: "Hey Claude, ..." while idle."""

    @property
    def wake(self) -> bool:
        return bool(self.state.get("wake"))

    def set_wake(self, on: bool) -> None:
        self.state["wake"] = on
        save_state(self.state)
        log.info("wake=%s", on)
        self._wake_cancel.set()
        self._confirm("Wake word on. Say: Hey Claude." if on else "Wake word off.")

    def _wake_loop(self) -> None:
        """While idle, listen for "Hey Claude ..." and send what follows as a new prompt.

        A tiny keyword spotter does the listening (only while someone speaks); Whisper only runs once
        it fires, to confirm the phrase and read the command.
        """
        from agent_voice.stt import is_hallucination

        phrases = list(self.config.get("wake_word", {}).get("phrases") or ["hey claude"])
        listener = None
        while True:
            time.sleep(0.3)
            if not self.wake or self.muted or self.chime or self.transcriber is None:
                continue
            # Approvals waiting don't stop it: "Hey Claude, approve Codex" answers them once the reply mic closed.
            if (self.speaker.busy or self._listening or self._listen_armed or self._ptt_active
                    or self.question or self._queue):
                continue
            try:
                if listener is None:
                    from agent_voice.wakeword import WakeListener

                    listener = WakeListener()
                    # Load (first time: download) the fallback model now, not on the first "Hey Claude".
                    threading.Thread(target=self._wake_model().warm_up, daemon=True).start()
                self._wake_cancel.clear()
                heard = listener.listen(self._wake_cancel)
            except Exception:
                log.exception("wake word unavailable")
                time.sleep(30)
                continue
            if heard is None or self._wake_cancel.is_set():
                continue
            audio, spotted = heard
            if not spotted:
                # A short phrase the spotter missed (an accent it doesn't know): the smallest Whisper
                # decides, ~0.2 s of CPU. Only a confirmed wake word goes on to the accurate model.
                # The wake phrase is English: say so, a 1-second clip is easily taken for another language.
                quick, _ = self._wake_model().transcribe(audio, language="en")
                woke = strip_wake(quick, phrases) is not None
                log.info("wake fallback heard %r: %s", quick[:60], "wake" if woke else "no")
                if not woke:
                    continue
            text, lang = self.transcriber.transcribe(audio)
            command = strip_wake(text, phrases)
            if command is None or is_hallucination(text):
                log.info("wake word: not confirmed (heard %r)", text[:60])
                continue
            log.info("wake word heard (%d chars follow)", len(command))
            session = self._voice_session or self._reply_session
            if self._perms and self._voice_approval(command):  # "Hey Claude, approve Codex"
                self._heard(command, lang, "", tag="answer")
                continue
            if self.companion_said(command, lang):  # "Hey Claude, timer 10 minutes", "... add milk to my list"
                continue
            if self.schedule_said(command, lang, session):  # "Hey Claude, at 2 pm tell Codex to run the tests"
                self._heard(command, lang, "", tag="scheduled")
                continue
            if match_recap(command):  # "Hey Claude, daily recap"
                self._heard(command, lang, "", tag="command")
                self.recap()
                continue
            if match_brief(command):  # "Hey Claude, what's my day?"
                self._heard(command, lang, "", tag="command")
                self.brief(asked=True)
                continue
            if match_join(command):  # "Hey Claude, join my meeting"
                self._heard(command, lang, "", tag="command")
                if not self.join_meeting():
                    self._confirm("No meeting to join right now.")
                continue
            if self._limited and match_resume(command):  # "Hey Claude, resume Codex when it resets"
                named = self._mentioned(command, set(self._limited))
                if self.set_resume(named or ""):
                    self._heard(command, lang, named or "", tag="command")
                continue
            look = parse_look(command)
            if look is not None:  # "Hey Claude, look at this and tell me why it's red"
                self.look_at(session or self._last_session, look, lang)
                continue
            handoff = parse_handoff(command)
            if handoff:  # "Hey Claude, send this to Codex and ask it to explain it"
                self.handoff(handoff[0], handoff[1], lang)
                continue
            new = parse_new_thread(command)
            if new:  # "Hey Claude, new thread: add a health check"
                self.new_thread(new[1], _new_target(new[0]), lang=lang)
                continue
            routed = self._routed(command)
            if routed:  # "Hey Claude, tell the notch redesign to run the tests"
                self._send_to(routed[0], routed[1], lang, spoken=True)
                continue
            full, expanded = self._expand(command)
            if expanded:  # "Hey Claude, commit this": a saved prompt, to the current thread
                self._heard(full, lang, session, tag="shortcut")
                _beep(880)
                self._send_spoken(full, self._hwnd_for(session), session, lang)
            elif len(command.split()) >= 3:
                self._heard(command, lang, session, tag="sent")
                _beep(880)
                self._send_spoken(command, self._hwnd_for(session), session, lang)
            else:
                # Just "Hey Claude": open the mic for the prompt, and send it.
                _beep(880)
                self._reply_session = session
                self._force_submit = True
                self._auto_listen()

    def _wake_model(self):
        """Whisper tiny, made once: it confirms the short phrases the keyword spotter missed."""
        if self._wake_transcriber is None:
            from agent_voice.stt import Transcriber

            self._wake_transcriber = Transcriber({"stt": {**self.config["stt"], "model": "tiny"}})
        return self._wake_transcriber
