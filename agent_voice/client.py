"""Lightweight client for the daemon. Imported by hooks, so it must stay stdlib-only and fast."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from agent_voice.config import HOME, load_config


def _url(path: str) -> str:
    cfg = load_config()
    return f"http://{cfg['host']}:{cfg['port']}{path}"


def request(path: str, payload: dict | None = None, timeout: float = 2.0) -> dict | None:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(_url(path), data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def is_running() -> bool:
    return request("/status", timeout=0.5) is not None


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
    subprocess.Popen([pythonw(), "-m", "agent_voice", "serve", "--background"], **kwargs)


def speak(
    text: str,
    source: str = "",
    lang: str | None = None,
    interrupt: bool = False,
    final: bool = False,
    voice: str | None = None,
    autostart: bool = True,
) -> dict | None:
    payload = {"text": text, "source": source, "lang": lang, "interrupt": interrupt, "final": final, "voice": voice}
    result = request("/speak", payload)
    if result is None and autostart and start_detached():
        result = request("/speak", payload)
    return result
