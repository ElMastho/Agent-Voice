"""The microphone off for every app: a mute button for a mic that has none.

Not one app's mute (Zoom's, Teams'): the system's own, on every microphone plugged in, so no app hears you, whatever
its own button says. Windows: each capture endpoint's mute (Core Audio, IAudioEndpointVolume, through pycaw), the one
the Sound settings show. Linux: each source's mute (`pactl`). macOS: the input volume down to 0 (AppleScript's `set
volume input volume`), and back to where it was.

Only what we turned off is turned back on: a mic you had muted yourself stays muted. What we turned off is saved
(state.json, `mic_off`), so a crash or a restart can't lose it: the next start keeps the mic off and knows what to give
back. A mic plugged in while it's off (or turned back on behind our back) is turned off too (`enforce`).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading

log = logging.getLogger(__name__)


class _Windows:
    """Every active capture endpoint: id -> its IAudioEndpointVolume."""

    def _endpoints(self) -> dict:
        from ctypes import POINTER, cast

        import comtypes
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator
        from pycaw.pycaw import IAudioEndpointVolume

        comtypes.CoInitialize()
        enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
        found = enum.EnumAudioEndpoints(1, 1)  # eCapture, DEVICE_STATE_ACTIVE
        out = {}
        for i in range(found.GetCount()):
            dev = found.Item(i)
            try:
                iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
                out[str(dev.GetId())] = cast(iface, POINTER(IAudioEndpointVolume))
            except Exception:
                log.debug("capture endpoint %d has no volume control", i, exc_info=True)
        return out

    def mics(self) -> dict[str, bool]:
        return {key: bool(vol.GetMute()) for key, vol in self._endpoints().items()}

    def set(self, key: str, off: bool, restore=None) -> object:  # noqa: ARG002 - the macOS backend's signature
        vol = self._endpoints().get(key)
        if vol is not None:
            vol.SetMute(1 if off else 0, None)
        return None


class _Pulse:
    """Linux: every source but the speakers' monitors."""

    def mics(self) -> dict[str, bool]:
        from agent_voice import pulse

        return {str(s.get("name")): bool(s.get("mute")) for s in pulse.items("sources")
                if s.get("name") and not str(s.get("name")).endswith(".monitor")}

    def set(self, key: str, off: bool, restore=None) -> object:  # noqa: ARG002
        from agent_voice import pulse

        pulse._pactl("set-source-mute", key, "1" if off else "0")
        return None


class _Mac:
    """macOS has no input mute that every device honours: the input volume, down to 0 and back."""

    KEY = "input"

    @staticmethod
    def _osascript(script: str) -> str:
        try:
            return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def _level(self) -> int | None:
        try:
            return int(self._osascript("input volume of (get volume settings)"))
        except ValueError:
            return None  # "missing value": no input device

    def mics(self) -> dict[str, bool]:
        level = self._level()
        return {} if level is None else {self.KEY: level == 0}

    def set(self, key: str, off: bool, restore=None) -> object:  # noqa: ARG002
        before = self._level()
        level = 0 if off else int(restore or 75)
        self._osascript(f"set volume input volume {level}")
        return before  # what to put back


def backend():
    if sys.platform == "win32":
        try:
            import pycaw.pycaw  # noqa: F401
        except Exception:
            return None
        return _Windows()
    if sys.platform == "darwin":
        return _Mac()
    from agent_voice import pulse

    return _Pulse() if pulse.available() else None


class MicMute:
    """Off / on for every mic; `lift` / `lower` open it for a moment (your own dictation) and close it again.

    `saved` is what a previous run turned off ({mic id: what to give back}); `on_save` gets it each time it changes.
    """

    def __init__(self, saved: dict | None = None, on_save=None, impl=None):
        self.impl = impl if impl is not None else backend()
        self.ours: dict[str, object] = dict(saved or {})
        self.on_save = on_save
        self.lifted = False
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.impl is not None

    def _save(self) -> None:
        if self.on_save:
            self.on_save(dict(self.ours))

    def off(self) -> int:
        """Every mic off (and remember which were ours to turn off). How many mics there are."""
        with self._lock:
            self.lifted = False
            return self._close_all()

    def _close_all(self) -> int:
        if self.impl is None:
            return 0
        mics = self.impl.mics()
        changed = False
        for key, muted in mics.items():
            if muted:
                continue
            try:
                restore = self.impl.set(key, True)
            except Exception:
                log.exception("could not turn mic %s off", key)
                continue
            if key not in self.ours:
                self.ours[key] = restore
            changed = True
        if changed:
            self._save()
        return len(mics)

    def on(self) -> None:
        """Back on: only the mics we turned off."""
        with self._lock:
            self.lifted = False
            self._open_ours()
            self.ours = {}
            self._save()

    def _open_ours(self) -> None:
        if self.impl is None:
            return
        present = self.impl.mics()
        for key, restore in self.ours.items():
            if present.get(key):
                try:
                    self.impl.set(key, False, restore)
                except Exception:
                    log.exception("could not turn mic %s back on", key)

    def enforce(self) -> None:
        """While off: a mic plugged in since, or turned back on by someone else, goes off too."""
        with self._lock:
            if not self.lifted:
                self._close_all()

    def lift(self) -> None:
        """Open what we closed for a moment (you hold the push-to-talk key: agent-voice must hear you)."""
        with self._lock:
            self.lifted = True
            self._open_ours()

    def lower(self) -> None:
        with self._lock:
            if self.lifted:
                self.lifted = False
                self._close_all()
