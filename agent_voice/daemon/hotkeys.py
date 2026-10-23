"""Global hotkeys and push-to-talk."""

from __future__ import annotations

import logging
import threading
import time

from agent_voice import keys, winfocus
from agent_voice.daemon.sounds import _beep

log = logging.getLogger("agent_voice.server")


class HotkeysMixin:
    """Global hotkeys and push-to-talk."""

    def start_hotkeys(self) -> None:
        hk = self.config["hotkeys"]
        if hk.get("push_to_talk") and self.config["stt"].get("enabled"):
            from agent_voice.stt import Transcriber

            self.transcriber = Transcriber(self.config)
            threading.Thread(target=self.transcriber.warm_up, daemon=True).start()
        actions = {
            "stop": self.stop,
            "reply": self.reply_now,
            "widget": lambda: setattr(self, "widget_toggles", self.widget_toggles + 1),
            "new_thread": lambda: setattr(self, "new_thread_opens", self.new_thread_opens + 1),
            "hands_free": lambda: self.set_hands_free(not self.hands_free),
            "mute": lambda: self.set_muted(not self.muted),
            "chime": lambda: self.set_chime(not self.chime),
            "read_selection": lambda: threading.Thread(target=self._read_selection, daemon=True).start(),
            "push_to_talk": self._ptt_pressed if self.transcriber is not None else None,
        }
        try:
            for name, action in actions.items():
                if hk.get(name) and action is not None:
                    keys.add_hotkey(hk[name], action)
        except Exception:  # no `keyboard` / pynput, or macOS without the Accessibility permission
            log.exception("global hotkeys unavailable")
            return
        log.info("hotkeys registered: %s", hk)

    @staticmethod
    def _wait_modifiers_released(timeout: float = 3.0) -> None:
        end = time.time() + timeout
        while time.time() < end and keys.modifiers_down():
            time.sleep(0.02)

    def _read_selection(self) -> None:
        """Speak the selected text (any app, e.g. Antigravity), or the clipboard if nothing is selected."""
        selected, previous = self._copy_selection(0.2)
        text = selected or previous
        if text.strip():
            self.speaker.stop()
            # Explicit request: speak even when muted.
            self.speaker.speak(text)

    def _ptt_pressed(self) -> None:
        """Hotkey: record while the key is held."""
        if not self.ptt_start():  # key auto-repeat
            return
        trigger = self.config["hotkeys"]["push_to_talk"].split("+")[-1].strip()

        def watch_release():
            while keys.is_pressed(trigger):
                time.sleep(0.02)
            self.ptt_stop()

        threading.Thread(target=watch_release, daemon=True).start()

    def ptt_start(self) -> bool:
        """Start a push-to-talk recording (hotkey or widget). False if one is already running."""
        if self._ptt_active or self.transcriber is None:
            return False
        self._ptt_active = True
        self._wake_cancel.set()
        self._ptt_release.clear()
        threading.Thread(target=self._ptt_session, daemon=True).start()
        return True

    def ptt_stop(self) -> None:
        self._ptt_release.set()

    def _ptt_session(self) -> None:
        from agent_voice.stt import Recorder

        try:
            self.speaker.stop()  # you talk, the agent shuts up
            self.speaker.hold()  # ...and stays quiet until you're done
            self.cancel_listen()
            rec = Recorder(on_level=self._set_level)
            rec.start()
            self._mic_phase = "recording"
            _beep(660)
            # Safety net: a lost "release" (widget closed mid-press) must not record forever.
            self._ptt_release.wait(timeout=600)
            audio = rec.stop()
            _beep(520)
            self._mic_phase = "transcribing"
            text, lang = self.transcriber.transcribe(audio)
            log.info("dictation lang=%s chars=%d", lang, len(text))
            if not text:
                return
            submit = self.hands_free or bool(self.config["stt"].get("auto_submit"))
            self._heard(text, lang, "", tag="sent" if submit else "typed")  # push-to-talk goes to the focused window
            self._dictated(text, winfocus.foreground() if submit else 0, "", lang, submit)
        except Exception:
            log.exception("push-to-talk failed")
        finally:
            self._ptt_active = False
            self.speaker.release()
            self._mic_phase = ""
            self.level = 0.0
