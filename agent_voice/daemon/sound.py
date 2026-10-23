"""Other apps' sound: lowered under the voice, silenced while you reply, restored after."""

from __future__ import annotations

import logging

from agent_voice.config import save_state

log = logging.getLogger("agent_voice.server")


class SoundMixin:
    """Other apps' sound: lowered under the voice, silenced while you reply, restored after."""

    def _duck_loop(self) -> None:
        """Runs the fades (one at a time, the latest wish wins); first puts back what a crash left lowered."""
        from agent_voice import ducking

        if not ducking.available():
            return

        def save(data: dict) -> None:
            self.state["ducked"] = data
            save_state(self.state)

        self._ducker = ducking.Ducker(self.state.get("ducked"), on_save=save)
        if self.state.get("ducked"):
            self._ducker.restore()
        while True:
            job = self._duck_jobs.get()
            while not self._duck_jobs.empty():
                job = self._duck_jobs.get()  # only the latest matters
            try:
                if job[0] == "duck":
                    self._ducker.duck(job[1], pause=job[2])
                else:
                    self._ducker.restore()
            except Exception:
                log.exception("ducking failed")

    def release_audio(self) -> None:
        """Shutting down: other apps get their volume back now (and a paused video plays), not at the next start."""
        if self._ducker is not None:
            try:
                self._ducker.restore()
            except Exception:
                log.exception("ducking restore failed")

    def _duck_tick(self, now: float) -> None:
        """Every 100 ms: speaking -> others lowered; you speak -> silenced (a video paused); done -> as they were.

        The mic open for your reply isn't you speaking: until you do, the others come back (in headphones all the
        way; through speakers to the voice's level, or the mic would take the video for you)."""
        cfg = self.config.get("ducking", {})
        if not cfg.get("enabled", True) or self._ducker is None:
            return
        if self._ptt_active or self._mic_phase == "recording":
            want = "listen"
        elif self.speaker.busy or self._mic_phase == "waiting" and self._output.kind != "headphones":
            want = "voice"
        else:
            want = ""
        if want:
            self._duck_idle = 0.0
            if want != self._duck_state:
                self._duck_state = want
                level = float(cfg.get("listen", 0.0) if want == "listen" else cfg.get("voice", 0.3))
                self._duck_jobs.put(("duck", level, want == "listen" and bool(cfg.get("pause_media", True))))
        elif self._duck_state:
            # A pause between two sentences, or between the voice and the mic, isn't the end: wait a moment.
            self._duck_idle = self._duck_idle or now
            # While the thread that spoke is still at work, its next progress note is seconds away: the others
            # stay lowered (up to 15 s), instead of coming back up and going down again with each note.
            hold = 15.0 if self._voice_session and self._voice_session in self.activity else 0.4
            if now - self._duck_idle >= hold:
                self._duck_state, self._duck_idle = "", 0.0
                self._duck_jobs.put(("restore",))
