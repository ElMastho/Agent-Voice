"""Other apps' sound while agent-voice talks or listens: lowered under the voice, a video paused while you reply.

Windows: the per-app volumes of the audio mixer (pycaw). Linux: PulseAudio / PipeWire streams (`pactl`), and
players paused through MPRIS (`playerctl`). Volumes are faded, never jumped, and put back as they were; one you change yourself in the meantime is left alone. What was lowered is saved, so a
crash can't leave a video stuck at 30%: the next start puts it back.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

log = logging.getLogger(__name__)


def available() -> bool:
    if sys.platform.startswith("linux"):
        from agent_voice import pulse

        return pulse.available()
    if sys.platform != "win32":
        return False
    try:
        import pycaw.pycaw  # noqa: F401
    except Exception:
        return False
    return True


class _Stream:
    """A Linux playback stream with the two calls the fades use (pycaw's names)."""

    def __init__(self, index: int, level: float):
        self.index, self.level = index, level

    def GetMasterVolume(self) -> float:  # noqa: N802 - pycaw's name
        return self.level

    def SetMasterVolume(self, level: float, _context=None) -> None:  # noqa: N802
        from agent_voice import pulse

        self.level = level
        pulse.set_volume(self.index, level)


class _Meter:
    def __init__(self, running: bool):
        self.running = running

    def GetPeakValue(self) -> float:  # noqa: N802
        return 1.0 if self.running else 0.0


def _sessions():
    """(key, process name, volume control, level meter) for every app playing through the speakers but us."""
    if sys.platform.startswith("linux"):
        from agent_voice import pulse

        return [(f"{s['name']}:{s['pid']}:{s['index']}", s["name"], _Stream(s["index"], s["volume"]),
                 _Meter(s["running"])) for s in pulse.playing()]
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

    comtypes.CoInitialize()
    out = []
    for s in AudioUtilities.GetAllSessions():
        if not s.Process or s.Process.pid == os.getpid():
            continue  # system sounds stay as they are, and so does our own voice
        try:
            meter = s._ctl.QueryInterface(IAudioMeterInformation)
        except Exception:
            meter = None
        out.append((f"{s.Process.name()}:{s.Process.pid}", s.Process.name(), s.SimpleAudioVolume, meter))
    return out


def _pause_media() -> bool:
    """Pause what's playing; True if something was (so it's played again after)."""
    if sys.platform.startswith("linux"):
        from agent_voice import pulse

        return pulse.pause_players()
    _media_key()
    return True


def _resume_media() -> None:
    if sys.platform.startswith("linux"):
        from agent_voice import pulse

        pulse.resume_players()
        return
    _media_key()


def _media_key() -> None:
    """Play/pause, as the keyboard's media key does: browsers (YouTube...), Spotify and players obey it."""
    import ctypes

    user32 = ctypes.WinDLL("user32")
    VK_MEDIA_PLAY_PAUSE, KEYEVENTF_KEYUP = 0xB3, 0x2
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)


def _sounding(sessions, samples: int = 6, gap: float = 0.03) -> list[str]:
    """The apps making sound: their meters read a few times over ~150 ms, so a quiet moment in a song (or between
    two) isn't taken for silence."""
    found: list[str] = []
    for i in range(samples):
        for _key, name, _volume, meter in sessions:
            try:
                if meter is not None and name not in found and meter.GetPeakValue() > 0.002:
                    found.append(name)
            except Exception:
                continue
        if found or i == samples - 1:
            break
        time.sleep(gap)
    return found


class Ducker:
    """duck(0.3) under the voice, duck(0.0, pause=True) while you speak, restore() after."""

    def __init__(self, saved: dict | None = None, on_save=None):
        self._lock = threading.Lock()
        self._original: dict[str, float] = {}  # key -> the app's own volume
        self._set: dict[str, float] = {}  # key -> what we set it to (changed since? then it's yours)
        self._paused = False
        self.level = 1.0  # where the others are now, relative to their own volume
        self._on_save = on_save or (lambda data: None)
        if saved:
            self._original = {k: float(v) for k, v in (saved.get("volumes") or {}).items()}
            self._set = {k: -1.0 for k in self._original}  # unknown: restore whatever it is
            self.level = 0.0

    def duck(self, level: float, pause: bool = False) -> None:
        with self._lock:
            try:
                sessions = _sessions()
            except Exception:
                log.debug("audio sessions unavailable", exc_info=True)
                return
            for key, _name, volume, _meter in sessions:
                if key not in self._original:
                    self._original[key] = volume.GetMasterVolume()
            self._save()
            if pause and not self._paused:
                playing = _sounding(sessions)
                if playing:
                    self._paused = _pause_media()  # a video playing while you reply: paused (and played again after)
                log.info("you speak: %s", f"media paused ({', '.join(playing)})" if self._paused
                         else "nothing playing to pause")
            self._fade(sessions, level)

    def restore(self) -> None:
        with self._lock:
            if self._paused:
                _resume_media()
                self._paused = False
                log.info("media resumed")
            if not self._original:
                self.level = 1.0
                return
            try:
                sessions = _sessions()
            except Exception:
                return
            self._fade(sessions, 1.0)
            self._original.clear()
            self._set.clear()
            self._save()

    def _fade(self, sessions, level: float, seconds: float = 0.25) -> None:
        """Glide every app from where it is to `level` of its own volume (no clicks, no jumps)."""
        start, steps = self.level, 8
        for i in range(1, steps + 1):
            k = start + (level - start) * i / steps
            for key, _name, volume, _meter in sessions:
                own = self._original.get(key)
                if own is None:
                    continue
                current = volume.GetMasterVolume()
                expected = self._set.get(key)
                if expected is not None and expected >= 0 and abs(current - expected) > 0.02:
                    self._original.pop(key)  # you moved it yourself: it's yours now
                    continue
                target = max(0.0, min(1.0, own * k))
                volume.SetMasterVolume(target, None)
                self._set[key] = target
            time.sleep(seconds / steps)
        self.level = level

    def _save(self) -> None:
        self._on_save({"volumes": dict(self._original)} if self._original else {})
