"""The daemon's switches and modes: mute, chime, Do not disturb, speed, voice engine, languages, devices, focus; and the short spoken confirmations."""

from __future__ import annotations

import logging
import threading
import time

from agent_voice import devices
from agent_voice.config import save_state
from agent_voice.daemon.sounds import _beep, _play_apart, chime_audio
from agent_voice.i18n import set_language as set_ui_language
from agent_voice.i18n import tr

log = logging.getLogger("agent_voice.server")


class SettingsMixin:
    """The daemon's switches and modes: mute, chime, Do not disturb, speed, voice engine, languages, devices, focus; and the short spoken confirmations."""

    @property
    def muted(self) -> bool:
        return bool(self.state.get("muted"))

    @property
    def hands_free(self) -> bool:
        return bool(self.state.get("hands_free"))

    def set_hands_free(self, on: bool) -> None:
        self.state["hands_free"] = on
        save_state(self.state)
        log.info("hands_free=%s", on)
        self._confirm("Auto-send on. Your replies will be sent automatically." if on else "Auto-send off.")

    @property
    def summary(self) -> bool:
        if "summary" in self.state:
            return bool(self.state["summary"])
        return bool(self.config.get("summary", {}).get("enabled", True))

    def set_summary(self, on: bool) -> None:
        # Hooks read this from state.json on every event.
        self.state["summary"] = on
        save_state(self.state)
        log.info("summary=%s", on)
        self._confirm("Summaries on." if on else "Full answers.")

    @property
    def speed_x(self) -> float:
        """Playback rate as a multiplier (1.5 = 1.5x faster than natural)."""
        return round(1.0 / float(self.config["speed"]), 2)

    def set_speed(self, x: float) -> None:
        x = min(3.0, max(0.5, float(x)))
        self.config["speed"] = 1.0 / x
        self.state["speed_x"] = x
        save_state(self.state)
        log.info("speed=%sx", x)
        # Applies from the next sentence; give a sample when idle so the change can be heard.
        if not self.speaker.busy:
            self._confirm("Speed {x}.", x=f"{x:g}")

    @property
    def chime(self) -> bool:
        """Chimes instead of the voice: chosen, automatic while you're in a call or in focus, or on speakers."""
        return bool(self.state.get("chime")) or bool(self.focus) or self.on_speakers

    @property
    def engine(self) -> str:
        """'piper' (light) or 'kokoro' (natural)."""
        return str(self.state.get("engine") or self.config.get("engine") or "piper")

    def _apply_engine(self) -> None:
        """The voices the speaker uses: Kokoro's for the languages it has (when it's on and ready), Piper's else."""
        from agent_voice import kokoro
        from agent_voice.tts import KOKORO

        voices = dict(self._piper_voices)
        if self.engine == "kokoro" and kokoro.available() and kokoro.ready():
            chosen = {**kokoro.DEFAULT_VOICES, **(self.config.get("kokoro", {}).get("voices") or {})}
            voices.update({lang: KOKORO + name for lang, name in chosen.items() if lang in voices})
        self.config["voices"] = voices

    @property
    def language(self) -> str:
        return self.config.get("default_lang", "en")

    def _apply_language(self, code: str) -> None:
        from agent_voice import lang as langmod

        code = code if code in ("en", "fr") else "en"
        self.config["default_lang"] = code  # the voice's fallback (tts reads it for every reply)
        self.config.setdefault("stt", {})["prefer"] = code  # what an unsure transcription takes
        langmod.prefer(code)  # text detection's fallback

    def set_language(self, code: str) -> None:
        """English or French: the language you speak and hear most."""
        self._apply_language(code)
        self.state["language"] = self.language
        save_state(self.state)
        log.info("language=%s", self.language)
        self._confirm("English it is.")

    @property
    def ui_language(self) -> str:
        return self.state.get("ui_language") or self.config.get("ui_language") or "en"

    def set_ui_language(self, code: str) -> None:
        """English or French for what's shown: the notch, its notifications, the phone's (the widget relaunches)."""
        self.state["ui_language"] = "fr" if code == "fr" else "en"
        save_state(self.state)
        set_ui_language(self.ui_language)
        log.info("ui_language=%s", self.ui_language)

    def set_engine(self, name: str) -> None:
        """Light (Piper) or natural (Kokoro): the natural voice is downloaded the first time (~120 MB)."""
        from agent_voice import kokoro

        if name == "kokoro" and not kokoro.available():
            self._show_banner(tr("The natural voice needs the kokoro-onnx package: pip install kokoro-onnx."), "", "",
                              kind="note", tag="voice")
            return
        self.state["engine"] = name
        save_state(self.state)
        log.info("engine=%s", name)
        if name == "kokoro" and not kokoro.ready():
            if not self._engine_state:
                threading.Thread(target=self._fetch_kokoro, name="kokoro", daemon=True).start()
            return
        self._apply_engine()
        self.speaker.stop()
        self._confirm("This is the natural voice." if name == "kokoro" else "This is the light voice.")

    def _fetch_kokoro(self) -> None:
        from agent_voice import kokoro

        def progress(frac: float) -> None:
            self._engine_state = f"downloading {int(frac * 100)}%"

        self._engine_state = "downloading 0%"
        self._show_banner(tr("Downloading the natural voice (about 120 MB, once)..."), "", "", kind="note", tag="voice")
        try:
            kokoro.download(progress)
            kokoro.engine()  # loaded now, not on the first sentence
        except Exception as exc:
            log.exception("kokoro download failed")
            self.state["engine"] = "piper"
            save_state(self.state)
            self._show_banner(tr("Couldn't get the natural voice ({why}). Still the light one.", why=exc), "", "", kind="note",
                              tag="voice")
            return
        finally:
            self._engine_state = ""
        if self.engine == "kokoro":
            self._apply_engine()
            self._confirm("This is the natural voice.")

    @property
    def mic(self) -> str:
        """The microphone picked in Settings (its name), "" for the system's default."""
        return str(self.state.get("mic") or "")

    @property
    def speaker_device(self) -> str:
        """The speaker picked in Settings (its name), "" for the system's default."""
        return str(self.state.get("speaker") or "")

    def set_device(self, kind: str, name: str) -> None:
        """kind "mic" or "speaker"; name "" for the system's default."""
        self.state[kind] = name
        save_state(self.state)
        if kind == "mic":
            devices.mic = name
        else:
            devices.speaker = self._output.preferred = name
            self._check_output()  # headphones or speakers: the new device's
        log.info("%s=%s", kind, name or "system default")

    def _mic_silent(self) -> None:
        """No mic sent any sound: say so on the notch, at most every 10 minutes (the wake word retries quietly)."""
        if time.time() - self._mic_silent_at < 600:
            return
        self._mic_silent_at = time.time()
        self._show_banner(tr("The microphone sends no sound. Check it isn't muted or held by another app, or pick "
                             "another one in Settings."), "", "", kind="note", tag="mic")

    @property
    def headphones_only(self) -> bool:
        """The voice only in headphones: through speakers, chimes and banners instead (everyone around would hear)."""
        if "headphones_only" in self.state:
            return bool(self.state["headphones_only"])
        return bool(self.config.get("headphones", {}).get("only", False))

    def set_headphones_only(self, on: bool) -> None:
        self.state["headphones_only"] = on
        save_state(self.state)
        self._speaker_override = False
        log.info("headphones_only=%s", on)
        if self.on_speakers:
            self.stop()
            self.play_chime("done")
        else:
            self._confirm("Voice in headphones only." if on else "Voice on any speaker.")

    @property
    def on_speakers(self) -> bool:
        """The voice is kept off right now because the sound would come out of speakers."""
        return self.headphones_only and self._output.kind == "speakers" and not self._speaker_override

    @property
    def focus(self) -> str:
        """Why replies are held back right now: 'call (Teams)', 'presenting', 'full screen'; '' otherwise."""
        if self._call_override:
            return ""
        cfg = self.config.get("focus", {})
        if self.in_call:
            return f"call ({self.in_call})"
        if cfg.get("enabled", True) and cfg.get("full_screen", True) and self._fullscreen:
            return self._fullscreen
        if cfg.get("enabled", True) and self._focus_session():
            return "focus session"
        if cfg.get("enabled", True) and self.config.get("calendar", {}).get("quiet_in_meetings") and self._meeting():
            return "meeting"
        return ""

    def _focus_session(self) -> bool:
        """The Pomodoro's focus is running (and holds replies until the break)."""
        pomo = self.companion.data.get("pomodoro") or {}
        return bool(self.config.get("pomodoro", {}).get("hold", True) and pomo.get("phase") == "focus"
                    and pomo.get("end"))

    def _meeting(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        return next((e for e in self.agenda if not e["all_day"] and e["start"] <= now < e["end"]), None)

    def _holding(self) -> bool:
        """Focus: hold replies (approvals and questions still come through). Calls can keep chimes instead."""
        cfg = self.config.get("focus", {})
        if not self.focus or not cfg.get("enabled", True):
            return False
        return not (self.in_call and cfg.get("calls", "hold") == "chimes")

    @property
    def quiet(self) -> bool:
        """No voice (chimes or silent): replies show as a banner instead."""
        return self.muted or self.chime

    @property
    def output(self) -> str:
        return "silent" if self.muted else "chime" if self.chime else "voice"

    def _confirm(self, text: str, **values) -> None:
        """Say a setting change out loud, in your speech language, unless the voice is off (chimes, silent)."""
        if not self.quiet:
            self.speaker.stop()
            self.speaker.speak(tr(text, in_lang=self.language, **values), self.language)

    def _say(self, text: str, **values) -> None:
        """Something the app says itself (not an agent's reply): in your speech language."""
        self.speaker.speak(tr(text, in_lang=self.language, **values), self.language)

    def set_chime(self, on: bool) -> None:
        self.state["chime"] = on
        save_state(self.state)
        log.info("chime=%s", on)
        if self.focus and not on:
            self._call_override = True  # you asked for the voice during a call (or full screen): keep it
        if self.on_speakers and not on:
            self._speaker_override = True  # the voice on speakers anyway, until the device changes
        self.stop()  # no voice and no open mic in chime mode
        if on:
            self.play_chime("done")
        else:
            self._confirm("Voice mode.")

    def set_output(self, mode: str) -> None:
        """voice | chime | silent (Do not disturb): one choice instead of separate mute and chime switches.

        Do not disturb keeps the chime choice, so turning it off goes back to Voice or Chimes as you had it."""
        if mode == "silent":
            self.set_muted(True)
            return
        if self.muted:
            self.state["muted"] = False
        self.set_chime(mode == "chime")

    def play_chime(self, kind: str) -> None:
        if self.muted:
            return
        try:
            audio, rate = chime_audio(kind, float(self.config.get("chime", {}).get("volume", 0.35)))
            threading.Thread(target=_play_apart, args=(audio, rate), daemon=True).start()
        except Exception:
            log.exception("chime failed")

    def set_muted(self, muted: bool) -> None:
        """Do not disturb: no voice, no sound, no banner; what happens is kept for when you're back."""
        self.state["muted"] = muted
        save_state(self.state)
        if muted:
            self.stop()  # and no cue: you may be walking into a meeting
        else:
            _beep(880)
        log.info("do not disturb=%s", muted)

    @property
    def dnd_from(self) -> str:
        """What Do not disturb goes back to: 'chime' or 'voice'."""
        return "chime" if self.state.get("chime") else "voice"
