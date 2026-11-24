"""Mic off: every microphone muted at the system level, so no app hears you (a call included, whatever its own mute
button says). The notch's mic button, the tray, a hotkey and `agent-voice mic off` switch it."""

from __future__ import annotations

import logging
import time

from agent_voice import micmute
from agent_voice.config import save_state
from agent_voice.daemon.sounds import _beep
from agent_voice.i18n import tr

log = logging.getLogger("agent_voice.server")

ENFORCE_EVERY = 2.0  # seconds: a mic plugged in (or turned back on elsewhere) while it's off goes off too


class MicMixin:
    """Mic off: every microphone muted at the system level."""

    @property
    def mic_off(self) -> bool:
        return bool(self.state.get("mic_off"))

    def _mic_mute(self) -> micmute.MicMute:
        if self._micmute is None:
            def save(ours: dict) -> None:
                self.state["mic_off_ours"] = ours
                save_state(self.state)

            self._micmute = micmute.MicMute(self.state.get("mic_off_ours"), on_save=save)
        return self._micmute

    def set_mic_off(self, off: bool) -> None:
        mm = self._mic_mute()
        if not mm.available():
            self._show_banner(tr("Turning the microphone off isn't available on this system."), "", "", kind="note",
                              tag="mic")
            return
        self.state["mic_off"] = off
        save_state(self.state)
        if off:
            # Nothing of ours keeps listening to a mic that sends silence (it would say the mic is broken).
            self.cancel_listen()
            self._wake_cancel.set()
            self._barge_cancel.set()
            log.info("mic off (%d mics)", mm.off())
            _beep(330)
        else:
            mm.on()
            _beep(880)
            log.info("mic on")

    def _mic_watch(self) -> None:
        """Keeps the mic off across restarts, and every mic off while it is (new ones too)."""
        mm = self._mic_mute()
        if not mm.available():
            return
        if self.mic_off:
            mm.off()
        elif mm.ours:  # turned back on while the service was down: give back what we had taken
            mm.on()
        while True:
            time.sleep(ENFORCE_EVERY)
            if self.mic_off and not mm.lifted:
                try:
                    mm.enforce()
                except Exception:
                    log.exception("mic off check failed")
                    time.sleep(30)

    def _lift_mic(self) -> bool:
        """You asked agent-voice to listen (push-to-talk, Talk now) while the mic is off: open it for that moment.
        True if it was lifted (then `_lower_mic` closes it again)."""
        if not self.mic_off:
            return False
        try:
            self._mic_mute().lift()
        except Exception:
            log.exception("could not open the mic")
            return False
        return True

    def _lower_mic(self) -> None:
        """Closed again after a lift (a no-op if the mic was turned back on meanwhile: `on` ends the lift)."""
        try:
            self._mic_mute().lower()
        except Exception:
            log.exception("could not turn the mic off again")
