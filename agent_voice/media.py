"""What's playing (Spotify, YouTube in a browser...) and its controls, for the companion.

Windows: the media sessions apps report to Windows (the volume flyout's player: title, artist, playing or not),
through winrt; without them, the players' window titles (Spotify's is "Artist - Song" while it plays; a browser's is
its tab's, "Song - YouTube - Google Chrome") and whether the app is making sound (its audio session). The keyboard's
media keys control it. Linux: playerctl. Nothing is downloaded, nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import threading
import time

log = logging.getLogger("agent_voice.media")

BROWSERS = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "brave.exe": "Brave", "firefox.exe": "Firefox",
            "opera.exe": "Opera", "vivaldi.exe": "Vivaldi", "arc.exe": "Arc"}
# Sites whose tab title says what plays: (the title's suffix, the name shown).
SITES = ((" | YouTube Music", "YouTube Music"), (" - YouTube Music", "YouTube Music"), (" - YouTube", "YouTube"), (" | Deezer", "Deezer"),
         (" | SoundCloud", "SoundCloud"), (" | Spotify", "Spotify"), (" - Twitch", "Twitch"))
# A media session's app id -> its name (the YouTube Music web app's id is the same on every PC).
SESSION_APPS = (("spotify", "Spotify"), ("cinhimbnkkghhklpknlkffjgod", "YouTube Music"), ("msedge", "Edge"),
                ("chrome", "Chrome"), ("brave", "Brave"), ("308046b0af4a39cb", "Firefox"), ("firefox", "Firefox"),
                ("opera", "Opera"), ("vivaldi", "Vivaldi"))
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
                # An installed web app's window says its name first ("YouTube Music - Home | YouTube Music"); the
                # site's own name alone is a page, not a song.
                song = page[: -len(suffix)].strip().removeprefix(f"{site} - ").strip()
                return {"app": site, "title": "" if song in ("", site) else song, "artist": ""}
    return None


def app_name(app_id: str) -> str:
    """A media session's app (its AppUserModelID) -> the name shown: 'Spotify', 'YouTube Music', 'Chrome'..."""
    low = app_id.lower()
    for key, name in SESSION_APPS:
        if key in low:
            return name
    return app_id.split("!")[0].rsplit("\\", 1)[-1].removesuffix(".exe")


def _session_playing() -> dict | None:
    """What Windows' media sessions say (the volume flyout's player): the song's real title and artist, from any
    app that reports it (Spotify, browsers' tabs, installed web apps like YouTube Music, whose window never shows
    the song). The one playing wins, then the current one. None without winrt or without a session."""
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
        )
    except ImportError:
        return None

    async def read() -> dict | None:
        manager = await Manager.request_async()
        current = manager.get_current_session()
        current_id = current.source_app_user_model_id if current else ""
        ranked = []
        for session in manager.get_sessions():
            status = session.get_playback_info().playback_status
            if status in (Status.CLOSED, Status.STOPPED):
                continue  # nothing left to resume
            playing = status == Status.PLAYING
            ranked.append(((playing, session.source_app_user_model_id == current_id), session, playing))
        ranked.sort(key=lambda r: r[0], reverse=True)
        open_now: dict[str, set[str]] = {}
        for _rank, session, playing in ranked:
            # A paused session can outlive its app (a browser closed, still running in the background): what
            # plays is real; what's paused only while its app is still open.
            if not playing and not session_open(session.source_app_user_model_id, open_now):
                continue
            props = await session.try_get_media_properties_async()
            return {"app": app_name(session.source_app_user_model_id), "title": props.title or "",
                    "artist": props.artist or "", "playing": playing}
        return None

    return _within(lambda: asyncio.run(read()))


SESSION_WAIT = 2.0  # s: Windows' media service answers in a few ms, or (wedged) never
RETRY_STUCK = 60.0  # s before asking a service that didn't answer again
_stuck: list[tuple[float, threading.Thread]] = []  # looks it never answered (they can't be cancelled)


def _within(look):
    """`look()` on a thread of its own, for SESSION_WAIT at most: a wedged media service once froze the player on
    a closed video (the request never returns, and cancelling it doesn't stop it). None meanwhile: the window
    titles say what plays."""
    _stuck[:] = [(at, t) for at, t in _stuck if t.is_alive()]
    if _stuck and (time.time() - _stuck[-1][0] < RETRY_STUCK or len(_stuck) >= 3):
        return None
    out: list = []

    def run():
        try:
            out.append((True, look()))
        except Exception as e:
            out.append((False, e))

    thread = threading.Thread(target=run, name="media-session", daemon=True)
    thread.start()
    thread.join(SESSION_WAIT)
    if not out:
        if not _stuck:
            log.warning("Windows' media sessions don't answer: reading the players' windows instead")
        _stuck.append((time.time(), thread))
        return None
    ok, value = out[0]
    if not ok:
        raise value
    return value


def _windows_now() -> dict | None:
    """The media session when it has a song (named after the site for a browser tab: "YouTube", not "Chrome"),
    else the players' window titles."""
    item = _session_playing()
    if item and item["title"]:
        if item["app"] in BROWSERS.values():
            tab = _windows_playing()
            if tab and tab["app"] not in BROWSERS.values() and tab["app"] != "Spotify":
                item["app"] = tab["app"]
        return item
    return _windows_playing()


def session_process(app_id: str) -> str | None:
    """A media session's app -> the process that plays it ('chrome.exe' for a Chrome tab or the YouTube Music
    web app); None when it isn't one we know."""
    low = app_id.lower()
    if low.endswith(".exe"):
        return low.rsplit("\\", 1)[-1]
    if "_crx_" in low:  # an installed web app: its browser plays it
        low = low.split("_crx_")[0]
    name = app_name(low)
    if name == "Spotify":
        return "spotify.exe"
    return next((exe for exe, shown in BROWSERS.items() if shown == name), None)


def session_open(app_id: str, open_now: dict[str, set[str]]) -> bool:
    """The app behind a paused media session is still open: a browser with a window (closed, Edge and Chrome
    keep running in the background), any other player running (Spotify in the tray). Unknown apps are trusted.
    `open_now` caches what's open for one look ("windows", "running")."""
    exe = session_process(app_id)
    if exe is None:
        return True
    if exe in BROWSERS:
        if "windows" not in open_now:
            open_now["windows"] = {_process_name(pid).lower() for pid, _title in _visible_windows()}
        return exe in open_now["windows"]
    if "running" not in open_now:
        import psutil

        open_now["running"] = {(p.info["name"] or "").lower() for p in psutil.process_iter(["name"])}
    return exe in open_now["running"]


def _visible_windows() -> list[tuple[int, str]]:
    """(pid, title) of every visible window that has a title."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    found: list[tuple[int, str]] = []

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
    return found


def _process_name(pid: int) -> str:
    if pid not in _NAMES:
        if len(_NAMES) > 2000:
            _NAMES.clear()
        try:
            import psutil

            _NAMES[pid] = psutil.Process(pid).name()
        except Exception:
            _NAMES[pid] = ""
    return _NAMES[pid]


def _windows_playing() -> dict | None:
    found = _visible_windows()
    sounding: set[int] | None = None  # asked only once a player is found (the audio sessions cost the most)
    best = None
    seen: set[str] = set()
    for pid, title in found:
        item = parse_title(_process_name(pid), title)
        if item is None:
            continue
        seen.add(item["app"])
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
    for app in set(_LAST) - seen:  # its window closed: its song is gone with it
        del _LAST[app]
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
            return _windows_now()
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
