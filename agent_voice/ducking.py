"""Other apps' sound while agent-voice talks or listens: lowered under the voice, a video paused while you reply.

Windows only (the per-app volumes of the audio mixer, through pycaw). Volumes are faded, never jumped, and put
back as they were; one you change yourself in the meantime is left alone. What was lowered is saved, so a
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
    if sys.platform != "win32":
        return False
    try:
        import pycaw.pycaw  # noqa: F401
    except Exception:
        return False
    return True


def _sessions():
    """(key, process name, volume control, level meter) for every app playing through the speakers but us."""
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


def _media_key() -> None:
    """Play/pause, as the keyboard's media key does: browsers (YouTube...), Spotify and players obey it."""
    import ctypes

    user32 = ctypes.WinDLL("user32")
    VK_MEDIA_PLAY_PAUSE, KEYEVENTF_KEYUP = 0xB3, 0x2
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)


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
            playing = False
            for key, _name, volume, meter in sessions:
                if key not in self._original:
                    self._original[key] = volume.GetMasterVolume()
                if meter is not None and meter.GetPeakValue() > 0.005:
                    playing = True
            self._save()
            if pause and playing and not self._paused:
                _media_key()  # a video playing while you reply: paused (and played again after)
                self._paused = True
                log.info("media paused while you speak")
            self._fade(sessions, level)

    def restore(self) -> None:
        with self._lock:
            if self._paused:
                _media_key()
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
