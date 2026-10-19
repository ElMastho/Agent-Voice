"""What's playing (Spotify, YouTube in a browser...) and its controls, for the companion.

Windows: the players' window titles (Spotify's is "Artist - Song" while it plays; a browser's is its tab's,
"Song - YouTube - Google Chrome"), whether the app is making sound (its audio session), and the keyboard's media
keys to control it. Linux: playerctl. Nothing is downloaded, nothing leaves the machine.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys

log = logging.getLogger("agent_voice.media")

BROWSERS = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "brave.exe": "Brave", "firefox.exe": "Firefox",
            "opera.exe": "Opera", "vivaldi.exe": "Vivaldi", "arc.exe": "Arc"}
# Sites whose tab title says what plays: (the title's suffix, the name shown).
SITES = ((" - YouTube Music", "YouTube Music"), (" - YouTube", "YouTube"), (" | Deezer", "Deezer"),
         (" | SoundCloud", "SoundCloud"), (" | Spotify", "Spotify"), (" - Twitch", "Twitch"))
KEYS = {"toggle": 0xB3, "next": 0xB0, "previous": 0xB1}  # VK_MEDIA_PLAY_PAUSE, _NEXT_TRACK, _PREV_TRACK
_IDLE_SPOTIFY = re.compile(r"^Spotify(?: Free| Premium)?$")
_NAMES: dict[int, str] = {}  # pid -> process name (polled every few seconds: not asked again each time)
_LAST: dict[str, dict] = {}  # app -> the last song seen playing: paused, Spotify's window shows only its name


def parse_title(process: str, title: str) -> dict | None:
    """A player's window title -> {app, title, artist}; None when it isn't something playing."""
    process = process.lower()
    title = " ".join(title.split())
    if process == "spotify.exe":
        if not title or _IDLE_SPOTIFY.match(title):
            return {"app": "Spotify", "title": "", "artist": ""}  # paused: Spotify shows only its name
        artist, _, song = title.partition(" - ")
        return {"app": "Spotify", "title": song or artist, "artist": artist if song else ""}
    if process in BROWSERS:
        page = re.sub(r" [-—] (?:Google Chrome|Microsoft Edge|Brave|Mozilla Firefox|Opera|Vivaldi|Arc)$", "", title)
        page = re.sub(r"^\(\d+\)\s*", "", page)  # "(3) ..." unread notifications
        page = re.sub(r"\s+and \d+ more pages?$", "", page)  # Edge: "... and 4 more pages"
        for suffix, site in SITES:
            if page.endswith(suffix):
                return {"app": site, "title": page[: -len(suffix)].strip(), "artist": ""}
    return None


def _windows_playing() -> dict | None:
    import ctypes
    from ctypes import wintypes

    import psutil

    user32 = ctypes.WinDLL("user32")
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    found: list[tuple[int, str]] = []
    if len(_NAMES) > 2000:
        _NAMES.clear()

    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if buf.value:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((int(pid.value), buf.value))
        return True

    user32.EnumWindows(proc(collect), 0)
    sounding: set[int] | None = None  # asked only once a player is found (the audio sessions cost the most)
    best = None
    for pid, title in found:
        if pid not in _NAMES:
            try:
                _NAMES[pid] = psutil.Process(pid).name()
            except Exception:
                _NAMES[pid] = ""
        item = parse_title(_NAMES[pid], title)
        if item is None:
            continue
        if sounding is None and item["title"]:
            sounding = _sounding_pids()
        item["playing"] = bool(item["title"]) and _pid_family(pid, sounding or set())
        if item["title"]:
            _LAST[item["app"]] = {"title": item["title"], "artist": item["artist"]}
        else:
            item.update(_LAST.get(item["app"], {}))  # paused: still the song it was
        # What's making sound wins; then Spotify (its window is the player), then a browser tab.
        rank = (item["playing"], item["app"] == "Spotify", bool(item["title"]))
        if best is None or rank > best[0]:
            best = (rank, item)
    return best[1] if best else None


def _sounding_pids() -> set[int]:
    """Processes whose audio session is active right now (a browser plays from a child process)."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        comtypes.CoInitialize()
        return {s.Process.pid for s in AudioUtilities.GetAllSessions() if s.Process and s.State == 1}
    except Exception:
        return set()


def _pid_family(pid: int, sounding: set[int]) -> bool:
    """The window's process, or one of its children (Chrome's audio service), is making sound."""
    if pid in sounding:
        return True
    try:
        import psutil

        return any(child.pid in sounding for child in psutil.Process(pid).children(recursive=True))
    except Exception:
        return False


def _linux_playing() -> dict | None:
    try:
        out = subprocess.run(["playerctl", "metadata", "--format", "{{status}}\t{{playerName}}\t{{artist}}\t{{title}}"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    status, player, artist, title = (out.split("\t") + ["", "", "", ""])[:4]
    return {"app": player.capitalize(), "title": title, "artist": artist, "playing": status == "Playing"}


def now_playing() -> dict | None:
    """{app, title, artist, playing}, or None when no player is open."""
    try:
        if sys.platform == "win32":
            return _windows_playing()
        if sys.platform.startswith("linux"):
            return _linux_playing()
    except Exception:
        log.exception("now playing unavailable")
    return None


def control(action: str) -> bool:
    """toggle, next or previous: the media keys (Windows), playerctl (Linux)."""
    if action not in KEYS:
        return False
    if sys.platform == "win32":
        import ctypes

        user32 = ctypes.WinDLL("user32")
        user32.keybd_event(KEYS[action], 0, 0, 0)
        user32.keybd_event(KEYS[action], 0, 0x2, 0)  # KEYEVENTF_KEYUP
        return True
    if sys.platform.startswith("linux"):
        verb = {"toggle": "play-pause", "next": "next", "previous": "previous"}[action]
        try:
            return subprocess.run(["playerctl", verb], capture_output=True, timeout=2).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return False
