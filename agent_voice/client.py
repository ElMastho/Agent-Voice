"""Lightweight client for the daemon. Imported by hooks, so it must stay stdlib-only and fast."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import http.client
import time
from pathlib import Path

from agent_voice.config import HOME, load_config


def request(path: str, payload: dict | None = None, timeout: float = 2.0) -> dict | None:
    """GET (no payload) or POST JSON to the daemon; None if it isn't there or refused.

    http.client rather than urllib: hooks run once per tool call, and urllib's import alone costs ~40 ms.
    """
    cfg = load_config()
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    conn = http.client.HTTPConnection(cfg["host"], int(cfg["port"]), timeout=timeout)
    try:
        conn.request("GET" if body is None else "POST", path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status >= 400:
            return None
        return json.loads(resp.read().decode("utf-8") or "{}")
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def is_running() -> bool:
    return request("/status", timeout=0.5) is not None


FROZEN = bool(getattr(sys, "frozen", False))  # running from the packaged .exe


def self_command(*args: str, windowed: bool = True) -> list[str]:
    """How to run agent-voice itself: the bundled exe in a release build, else this Python.

    `windowed` picks the console-less variant (daemon, widget, autostart) over the console one (hooks).
    """
    if FROZEN:
        exe = Path(sys.executable).with_name("agent-voice-w.exe" if windowed else "agent-voice.exe")
        return [str(exe), *args]
    return [pythonw() if windowed else sys.executable, "-m", "agent_voice", *args]


def pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


STARTING_PATH = HOME / "starting"


def _claim_start(fresh: float = 20.0) -> bool:
    """True for the one caller allowed to spawn; parallel hooks seeing the marker just wait for the daemon."""
    STARTING_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - STARTING_PATH.stat().st_mtime > fresh:
            STARTING_PATH.unlink()
    except OSError:
        pass
    try:
        os.close(os.open(STARTING_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except OSError:
        return False


def start_detached(wait: float = 15.0) -> bool:
    """Start the daemon in the background (no console window) and wait until it answers."""
    if is_running():
        return True
    if not _claim_start():
        return _wait_running(wait)
    try:
        _spawn()
        return _wait_running(wait)
    finally:
        try:
            STARTING_PATH.unlink()
        except OSError:
            pass


def restart(wait: float = 30.0) -> bool:
    """Stop the service and start it again; the notch notices and comes back with the new code too."""
    if is_running():
        request("/shutdown", {})
        end = time.time() + 10
        while time.time() < end and is_running():
            time.sleep(0.25)
    return start_detached(wait)


def _wait_running(wait: float) -> bool:
    end = time.time() + wait
    while time.time() < end:
        if is_running():
            return True
        time.sleep(0.25)
    return False


def _spawn() -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(self_command("serve", "--background"), **kwargs)


def speak(
    text: str,
    source: str = "",
    lang: str | None = None,
    interrupt: bool = False,
    final: bool = False,
    voice: str | None = None,
    autostart: bool = True,
    session: str = "",
    hwnd: int = 0,
    result: dict | None = None,
) -> dict | None:
    payload = {"text": text, "source": source, "lang": lang, "interrupt": interrupt, "final": final, "voice": voice,
               "session": session, "hwnd": hwnd, "result": result}
    result = request("/speak", payload)
    if result is None and autostart and start_detached():
        result = request("/speak", payload)
    return result
