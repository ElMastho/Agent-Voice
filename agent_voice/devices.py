"""Which microphone and speaker to use (Settings › Devices), and opening the mic so one that sends nothing never hangs
the listening.

Each is a device's name, as the system shows it (Windows' sound settings; PortAudio's names elsewhere), or "" for the
system's default. A chosen device that isn't there (unplugged) falls back to the default.

Every mic stream is probed: until a block with some sound in it arrives (a real mic always has a little noise),
nothing reaches the caller; after `probe` seconds without one, the default is tried, then None. A stuck driver can make
closing a stream hang too, so streams are closed on a thread of their own.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

mic = ""  # set by the daemon from Settings: a device name, "" for the system's default
speaker = ""
on_silent = None  # the daemon's callback when no mic sends anything
safe_to_refresh = lambda: False  # the daemon's: True while nothing plays (restarting PortAudio would cut it off)

REFRESH_EVERY = 10.0  # a device plugged in after the start: PortAudio's list is read again, at most this often
_refreshed = 0.0
_lock = threading.Lock()


def _windows_names(flow: int) -> list[str]:
    import comtypes
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()
    enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    found = enum.EnumAudioEndpoints(flow, 1)  # active ones only
    return [AudioUtilities.CreateDevice(found.Item(i)).FriendlyName or "" for i in range(found.GetCount())]


def names(kind: str) -> list[str]:
    """The devices you can pick: kind "input" (mics) or "output" (speakers, headphones)."""
    try:
        if sys.platform == "win32":
            return sorted(n for n in _windows_names(1 if kind == "input" else 0) if n)
        import sounddevice as sd

        channels = "max_input_channels" if kind == "input" else "max_output_channels"
        return sorted({d["name"] for d in sd.query_devices() if d[channels] > 0 and d["hostapi"] == sd.default.hostapi})
    except Exception:
        log.debug("could not list the %s devices", kind, exc_info=True)
        return []


def match_index(name: str, devices: list[dict], hostapi: int, kind: str = "input") -> int | None:
    """PortAudio's index for a device name, in the default host API (MME cuts names at 31 characters)."""
    channels = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, dev in enumerate(devices):
        if dev.get(channels, 0) <= 0 or dev.get("hostapi") != hostapi:
            continue
        short = dev.get("name") or ""
        if short == name or (len(short) >= 20 and name.startswith(short)):
            return i
    return None


def index(name: str, kind: str) -> int | None:
    """The chosen device's PortAudio index, None for the default (or when it isn't plugged in)."""
    global _refreshed
    if not name:
        return None
    try:
        import sounddevice as sd

        with _lock:
            found = match_index(name, list(sd.query_devices()), sd.default.hostapi, kind)
            if found is None and time.time() - _refreshed > REFRESH_EVERY and safe_to_refresh():
                _refreshed = time.time()
                # PortAudio only reads the devices when it starts: one plugged in since is unknown to it.
                sd._terminate()
                sd._initialize()
                found = match_index(name, list(sd.query_devices()), sd.default.hostapi, kind)
        return found
    except Exception:
        log.debug("device %r not found", name, exc_info=True)
        return None


def output_device() -> int | None:
    return index(speaker, "output")


def candidates() -> list[int | None]:
    """The mics to try, in order: the chosen one, then the default."""
    found = index(mic, "input")
    if mic and found is None:
        log.info("microphone %r isn't plugged in: the default instead", mic)
    return [found, None] if found is not None else [None]


def close(stream) -> None:
    """Close without waiting: a stuck driver can hang it."""
    def run():
        try:
            stream.abort()
            stream.close()
        except Exception:
            pass

    threading.Thread(target=run, name="mic-close", daemon=True).start()


def open_input(callback, rate: int, blocksize: int = 0, probe: float = 1.0):
    """A started input stream on the first mic that sends sound, or None (none did within `probe` s each).

    `callback(indata, frames, time_info, status)` sees blocks from the first one with sound in it on.
    """
    import sounddevice as sd

    for device in candidates():
        alive = threading.Event()

        def cb(indata, frames, time_info, status, alive=alive):
            if not alive.is_set():
                if not np.any(indata):
                    return
                alive.set()
            callback(indata, frames, time_info, status)

        label = mic if device is not None else "default"
        try:
            stream = sd.InputStream(device=device, samplerate=rate, channels=1, dtype="float32", blocksize=blocksize,
                                    callback=cb)
            stream.start()
        except Exception as e:
            log.warning("microphone %s would not open: %s", label, e)
            continue
        if alive.wait(probe):
            return stream
        log.warning("microphone %s sends no sound", label)
        close(stream)
    if on_silent:
        on_silent()
    return None
