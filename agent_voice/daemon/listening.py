"""The reply mic: auto-listen after a reply, talking over the voice, what was heard, the take-back."""

from __future__ import annotations

import logging
import threading
import time

from agent_voice import winfocus
from agent_voice.config import save_state
from agent_voice.daemon.phrases import (
    COMPANION,
    NEW_THREAD,
    match_command,
    match_interrupt,
    match_resume,
    match_take_back,
    parse_look,
    strip_wake,
)
from agent_voice.daemon.sounds import _beep

log = logging.getLogger("agent_voice.server")


class ListeningMixin:
    """The reply mic: auto-listen after a reply, talking over the voice, what was heard, the take-back."""

    def _send_spoken(self, text: str, hwnd: int, session: str, lang: str) -> None:
        """A dictated message that sends itself: shown under the notch for a few seconds first, while the mic
        listens for "wait" (it's typed in, not sent, to fix it), "cancel that" or "send it"; more words are added."""
        seconds = float(self.config.get("take_back", {}).get("seconds", 5) or 0)
        if seconds <= 0:
            self._paste(text, hwnd, submit=True, session=session)
            return
        self._pending_seq += 1
        p = {"id": self._pending_seq, "text": text, "session": session, "until": time.time() + seconds, "action": ""}
        self._pending = p
        self._pending_event.clear()
        self._show_pending(p)
        try:
            self._await_take_back(p, seconds)
        finally:
            self._pending = None
        action = p["action"] or ("keep" if self._pending_event.is_set() else "send")
        source = self.threads.get(session, {}).get("source", "") if session else ""
        log.info("take-back: %s", action)
        if action == "send":
            self._show_banner(p["text"], session, source, kind="you", tag="sent")
            self._paste(p["text"], hwnd, submit=True, session=session)
        elif action == "hold":  # typed into its input, not sent: fix it there and press Enter
            self._show_banner(p["text"], session, source, kind="you", tag="not sent: fix it, then Enter")
            self._paste(p["text"], hwnd, submit=False, session=session)
        elif action == "cancel":
            self._show_banner(p["text"], session, source, kind="you", tag="cancelled")
            _beep(400)
        else:  # interrupted (Stop, a prompt you typed yourself): nothing sent, nothing lost
            import pyperclip

            pyperclip.copy(p["text"])
            self._show_banner(p["text"], session, source, kind="you", tag="not sent · on the clipboard")

    def _show_pending(self, p: dict) -> None:
        source = self.threads.get(p["session"], {}).get("source", "") if p["session"] else ""
        self._show_banner(p["text"], p["session"], source, kind="pending", tag="sending")
        if self.banner:
            self.banner.update(pending=p["id"], until=p["until"])

    def _await_take_back(self, p: dict, seconds: float) -> None:
        """Until the message goes: listen for what you say about it (or a click on Send, Edit, Cancel)."""
        from agent_voice.stt import is_hallucination, listen_for_speech

        if self.transcriber is None:  # no mic: the buttons only
            self._pending_event.wait(max(0.0, p["until"] - time.time()))
            return
        cfg = self.config["auto_listen"]
        listening, self._listening = self._listening, True
        self.speaker.hold()
        try:
            while not p["action"] and not self._pending_event.is_set():
                left = p["until"] - time.time()
                if left <= 0:
                    return
                self._mic_phase = "waiting"
                audio = listen_for_speech(left, float(cfg["silence_seconds"]), float(cfg["max_seconds"]),
                                          self._pending_event, on_level=self._set_level,
                                          on_start=lambda: setattr(self, "_mic_phase", "recording"))
                if audio is None or p["action"] or self._pending_event.is_set():
                    return
                self._mic_phase = "transcribing"
                said, _ = self.transcriber.transcribe(audio)
                if not said or is_hallucination(said):
                    continue
                what = match_take_back(said)
                if what:
                    p["action"] = what
                    return
                p["text"] = f"{p['text']} {said}".strip()  # you went on: it's part of the message
                p["until"] = time.time() + seconds
                self._show_pending(p)
        finally:
            self.speaker.release()
            self._listening = listening
            self._mic_phase = ""
            self.level = 0.0

    def pending_action(self, pending_id: int, action: str) -> bool:
        """The banner's Send, Edit and Cancel."""
        p = self._pending
        if not p or p["id"] != pending_id or action not in ("send", "hold", "cancel"):
            return False
        p["action"] = action
        self._pending_event.set()
        return True

    def cancel_listen(self) -> None:
        self._listen_armed = False
        self._listen_cancel.set()
        self._pending_event.set()  # a message about to go stays unsent (on the clipboard)
        # Closing the mic can take ~1 s on some drivers; show "idle" right away.
        if self._listening and not self._ptt_active:
            self._mic_phase = ""
            self.level = 0.0

    def reply_now(self, session: str | None = None) -> None:
        """Skip the rest of the speech and listen for your answer right away.

        `session`: talk to that conversation (its thread row, its banner): what you say is sent to it.
        """
        self.speaker.stop()
        self._listen_armed = False
        if self._listening or self._ptt_active or self.transcriber is None:
            return
        if session:
            self._reply_session = session
            self._force_submit = True  # you picked the thread: send it, there's no prompt in front of you
            if session != NEW_THREAD:
                self._new_pending = {}
        threading.Thread(target=self._auto_listen, daemon=True).start()

    def _set_level(self, value: float) -> None:
        self.level = value

    def _heard(self, text: str, lang: str, session: str | None = None, tag: str = "typed") -> None:
        """You said something: keep it, and show it under the notch, marked as yours, before it goes out."""
        self.last_heard, self.last_heard_lang = text, lang
        session = self._reply_session if session is None else session
        self._show_banner(text, session, self.threads.get(session, {}).get("source", "") if session else "",
                          kind="you", tag=tag)
        self.history = [{"text": text, "lang": lang, "at": round(time.time())}, *self.history][:5]
        self.state["history"] = self.history
        save_state(self.state)

    def _listen_monitor(self) -> None:
        idle_since = None
        queue_idle = None
        while True:
            time.sleep(0.1)
            try:
                self._duck_tick(time.time())
            except Exception:
                log.exception("ducking check failed")
            if self._barging and not self._barged and not self.speaker.busy:
                self._barge_cancel.set()  # the voice finished without you cutting in
            free = not (self.speaker.busy or self._listen_armed or self._listening or self._ptt_active
                        or self.question or self.permission)
            if free and self._queue and not self.muted:
                queue_idle = queue_idle or time.time()
                if time.time() - queue_idle >= 0.6:
                    queue_idle = None
                    self._play_next_thread()
                continue
            queue_idle = None
            if not self._listen_armed or self._ptt_active or self.muted:
                idle_since = None
                continue
            if self.speaker.busy:
                idle_since = None
                continue
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= 0.4:  # truly done speaking, not a gap between sentences
                self._listen_armed = False
                idle_since = None
                self._auto_listen()

    def _auto_listen(self) -> None:
        from agent_voice.stt import is_hallucination, listen_for_speech

        cfg = self.config["auto_listen"]
        session = self._reply_session
        target = self._hwnd_for(session) or winfocus.foreground()
        question = self.question
        permission = self.permission
        force_submit, self._force_submit = self._force_submit, False
        self._wake_cancel.set()
        self._listen_cancel.clear()
        self._listening = True
        self._mic_phase = "waiting"
        self.speaker.hold()
        try:
            _beep(700)
            time.sleep(0.15)  # don't record our own beep
            audio = listen_for_speech(
                float(cfg["window_seconds"]),
                float(cfg["silence_seconds"]),
                float(cfg["max_seconds"]),
                self._listen_cancel,
                on_level=self._set_level,
                on_start=lambda: setattr(self, "_mic_phase", "recording"),
            )
            if audio is None or self._listen_cancel.is_set():
                log.info("auto-listen: nothing heard")
                _beep(400)
                return
            _beep(520)
            self._mic_phase = "transcribing"
            text, lang = self.transcriber.transcribe(audio)
            log.info("auto-listen lang=%s chars=%d", lang, len(text))
            if self._listen_cancel.is_set() or is_hallucination(text):
                return
            self._handle_heard(text, lang, session, target, question, permission, force_submit)
        except Exception:
            log.exception("auto-listen failed")
        finally:
            self._listening = False
            self.speaker.release()
            self._mic_phase = ""
            self.level = 0.0

    def _handle_heard(self, text: str, lang: str, session: str, target: int, question, permission,
                      force_submit: bool) -> None:
        """What you said back to a conversation: an answer, a command, a message for another thread, or the reply."""
        if session == NEW_THREAD:  # dictating a new thread's first prompt
            pending, self._new_pending = self._new_pending, {}
            self.new_thread(text, pending.get("target", ""), pending.get("cwd", ""), lang)
            return
        if session == COMPANION:  # the companion's mic: a timer, a reminder... or a to-do
            if not self.companion_said(text, lang):
                self._heard(text, lang, "", tag="to-do")
                self.companion.todo("add", text=text.strip(" .")[:300], source="you")
            return
        approving = permission is not None and permission is self.permission
        answering = approving or (question is not None and question is self.question)
        command = None if answering else match_command(text)
        look = None if answering or command else parse_look(text)
        if look is not None:  # "Look at this and tell me what's wrong"
            self.look_at(session or self._last_session, look, lang)
            return
        routed = None if answering or command else self._routed(text)
        if routed and routed[0] != session:
            self._send_to(routed[0], routed[1], lang, spoken=True)  # "Tell Codex to ...": goes to Codex, not here
            return
        tag = ("answer" if answering else "command" if command
               else "sent" if self.hands_free or force_submit else "typed")
        self._heard(text, lang, session, tag=tag)
        full, expanded = self._expand(text)
        rest = strip_wake(text, list(self.config.get("wake_word", {}).get("phrases") or [])) if self.wake else None
        if approving:
            if not self._voice_approval(text):
                log.info("permission answer unclear: %r", text)
                _beep(300)
        elif answering:
            self.answer(text=text)
        elif command:
            self._run_command(command, session)
        elif session in self._limited and match_resume(text):
            self.set_resume(session)
        elif expanded:  # a saved prompt's name: its full text, sent
            self._send_spoken(full, target, session, lang)
        elif rest is not None:
            # "Hey Claude, ..." while the reply mic was open: the rest is the reply; the phrase alone is nothing.
            if rest.strip(" .!?,"):
                self._dictated(rest, target, session, lang, self.hands_free or force_submit)
            else:
                log.info("reply was only the wake phrase: nothing sent")
        else:
            self._dictated(text, target, session, lang, self.hands_free or force_submit)

    def _barge_ok(self) -> bool:
        """Listen while it speaks? Voice on, a mic model, and headphones (speakers would let it hear itself)."""
        cfg = self.config.get("barge_in", {})
        if not cfg.get("enabled", True) or self.transcriber is None or self.quiet:
            return False
        if self._listening or self._ptt_active:
            return False
        return self._output.kind == "headphones" or bool(cfg.get("speakers", False))

    def _barge_loop(self) -> None:
        """While the voice speaks, listen: talk over it and it stops. "Stop" or "no, wait" opens the mic for what
        you want to say instead; anything longer is your reply, as if the mic had opened after it."""
        while True:
            time.sleep(0.2)
            if self.speaker.busy and self._barge_ok():
                try:
                    self._barge_once()
                except Exception:
                    log.exception("barge-in failed")
                    time.sleep(30)

    def _barge_once(self) -> None:
        from agent_voice import stt

        cfg = self.config["auto_listen"]
        session = self._voice_session
        self._barge_cancel.clear()
        self._barging, self._barged = True, False
        try:
            audio = stt.listen_for_speech(3600, float(cfg["silence_seconds"]), float(cfg["max_seconds"]),
                                          self._barge_cancel, on_level=self._set_level, on_start=self._barge_in,
                                          start_blocks=10)  # 300 ms: a cough or a click doesn't cut it off
        finally:
            self._barging = False
        if not self._barged:
            return
        handed_over = False  # the mic opened for your correction: it owns the listening state from here
        try:
            if audio is None:
                return
            self._mic_phase = "transcribing"
            text, lang = self.transcriber.transcribe(audio)
            log.info("barge-in: %d chars", len(text))
            if not text or stt.is_hallucination(text):
                return
            if match_interrupt(text):  # "No, wait": say what you want instead
                self._listening, self._mic_phase, self.level = False, "", 0.0
                handed_over = True
                self.reply_now(session or None)
                return
            target = self._hwnd_for(session) or winfocus.foreground()
            self._handle_heard(text, lang, session, target, self.question, self.permission, False)
        finally:
            self._barged = False
            if not handed_over and not self._ptt_active:
                self._listening, self._mic_phase, self.level = False, "", 0.0

    def _barge_in(self) -> None:
        """You started talking over the voice: it stops at once (and what was queued with it)."""
        self._barged = True
        self.speaker.stop()
        self._listen_armed = False
        self._listening, self._mic_phase = True, "recording"
        log.info("barge-in: you talked over the voice")

    def _dictated(self, text: str, hwnd: int, session: str, lang: str, submit: bool) -> None:
        """What you said: sent (after the take-back), or typed into the input for you to send."""
        if submit:
            self._send_spoken(text, hwnd, session, lang)
        else:
            self._paste(text, hwnd, submit=False, session=session)

    def _run_command(self, command: str, session: str) -> None:
        """A spoken command in the reply window: handled here, nothing is sent to the agent."""
        log.info("voice command: %s", command)
        if command == "repeat":
            self.replay(session)
        elif command in ("slower", "faster"):
            self.set_speed(self.speed_x + (0.25 if command == "faster" else -0.25))
        else:
            _beep(400)
