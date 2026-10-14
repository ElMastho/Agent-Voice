"""Is another app using the microphone (a Teams, Zoom or Meet call)? Read from Windows' privacy records.

Windows notes, per app, when it last started and stopped using the mic
(HKCU\\...\\CapabilityAccessManager\\ConsentStore\\microphone). An app with a start time and no stop
time has the mic open right now. A registry read every few seconds: no audio is touched, nothing leaves
the machine.
"""

from __future__ import annotations

import sys

KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
# Our own dictation and wake word open the mic too: never mistake ourselves for a call.
OURSELVES = ("python", "agent-voice", "agent_voice")


def _open_now(key) -> bool:
    import winreg

    try:
        start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
        stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
    except OSError:
        return False
    return bool(start) and not stop


def _apps(root) -> list[str]:
    """Apps (subkey names) that have the mic open, under one ConsentStore key."""
    import winreg

    busy = []
    i = 0
    while True:
        try:
            name = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        if name == "NonPackaged":
            continue
        try:
            with winreg.OpenKey(root, name) as sub:
                if _open_now(sub):
                    busy.append(name)
        except OSError:
            continue
    return busy


def apps_using_mic() -> list[str]:
    """Other apps holding the microphone right now (empty off Windows or when unknown)."""
    if sys.platform != "win32":
        return []
    import winreg

    busy: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as root:
            busy += _apps(root)  # Store apps (Teams, ...)
            try:
                with winreg.OpenKey(root, "NonPackaged") as desktop:
                    busy += _apps(desktop)  # desktop apps: Zoom, Chrome for Meet, ...
            except OSError:
                pass
    except OSError:
        return []
    return [name for name in busy if not any(us in name.lower() for us in OURSELVES)]


def app_label(name: str) -> str:
    """'C:#Program Files#Zoom#bin#Zoom.exe' -> 'Zoom'; 'MSTeams_8wekyb3d8bbwe' -> 'MSTeams'."""
    leaf = name.replace("#", "\\").rsplit("\\", 1)[-1]
    return leaf.split("_", 1)[0].removesuffix(".exe")
