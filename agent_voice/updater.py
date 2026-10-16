"""Self-update for the Windows release build: download the new zip, verify it, swap the install folder's files.

The daemon downloads and verifies the release, extracts it to a temp folder, then starts the *new* build from
there (`agent-voice-w.exe apply-update ...`): that process is outside the install folder, so it can replace the
install's files once the daemon and widget are gone, then restart the daemon. Nothing in ~/.agent-voice is
touched, except a small result file the next daemon start reads to say how it went.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable

from agent_voice.config import HOME, LOG_DIR

log = logging.getLogger("agent-voice.update")

REPO = "ElMastho/agent-voice"
ASSET = "agent-voice-windows-x64.zip"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
EXES = ("agent-voice.exe", "agent-voice-w.exe")
BACKUP = ".update-old"  # inside the install folder: same volume, so the old files can be renamed away
STAGING_PREFIX = "agent-voice-update-"
RESULT_PATH = LOG_DIR / "update-result.json"
MAX_SIZE = 1_000_000_000


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def is_newer(tag: str, current: str) -> bool:
    return bool(tag) and version_tuple(tag) > version_tuple(current)


def pick_asset(release: dict) -> dict | None:
    """The release's Windows zip, only if it is exactly the asset our workflow publishes."""
    tag = str(release.get("tag_name") or "")
    for a in release.get("assets") or []:
        url = str(a.get("browser_download_url") or "")
        size = a.get("size")
        if (a.get("name") == ASSET and a.get("state", "uploaded") == "uploaded"
                and url == f"{DOWNLOAD_PREFIX}{tag}/{ASSET}" and isinstance(size, int) and 0 < size < MAX_SIZE):
            return a
    return None


def self_updatable() -> bool:
    """Only the packaged Windows build replaces itself; a source checkout updates with git."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    return Path(sys.executable).resolve().parent


# -- download (in the daemon) ----------------------------------------------------------------------------
def download(asset: dict, progress: Callable[[float], None] | None = None) -> Path:
    """Download the asset to a new temp folder and check its size, digest (when GitHub gives one) and CRCs."""
    import urllib.request

    url, size = asset["browser_download_url"], int(asset["size"])
    folder = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX))
    path = folder / ASSET
    sha = hashlib.sha256()
    got, last = 0, 0.0
    req = urllib.request.Request(url, headers={"User-Agent": "agent-voice", "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(path, "wb") as out:
        while chunk := resp.read(1 << 20):
            got += len(chunk)
            if got > size:
                raise ValueError("the download is bigger than the release says")
            sha.update(chunk)
            out.write(chunk)
            if progress and time.time() - last > 0.5:
                progress(got / size)
                last = time.time()
    verify(path, size, str(asset.get("digest") or ""), sha.hexdigest())
    return path


def verify(path: Path, size: int, digest: str = "", sha256: str = "") -> None:
    if path.stat().st_size != size:
        raise ValueError(f"incomplete download ({path.stat().st_size} of {size} bytes)")
    if digest.startswith("sha256:") and sha256 and digest.split(":", 1)[1].lower() != sha256:
        raise ValueError("the download doesn't match the release's checksum")
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
    except zipfile.BadZipFile as e:
        raise ValueError("the download isn't a valid zip") from e
    if bad:
        raise ValueError(f"corrupt file in the zip: {bad}")


def extract(zip_path: Path, dest: Path) -> Path:
    """Extract (refusing paths that escape `dest`) and return the folder holding the exes."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            target = (dest / name).resolve()
            if name.startswith("/") or (target != dest and dest not in target.parents):
                raise ValueError(f"unsafe path in the zip: {info.filename}")
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return build_root(dest)


def build_root(folder: Path) -> Path:
    for candidate in (folder, *sorted(p for p in folder.iterdir() if p.is_dir())):
        if all((candidate / exe).is_file() for exe in EXES) and (candidate / "_internal").is_dir():
            return candidate
    raise ValueError("the zip doesn't contain the agent-voice build")


def launch_apply(root: Path, target: Path, daemon_pid: int, version: str) -> None:
    """Start the new build's updater from the staging folder; it takes over once we are gone."""
    cmd = [str(root / "agent-voice-w.exe"), "apply-update", "--source", str(root), "--target", str(target),
           "--pid", str(daemon_pid), "--version", version]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(cmd, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)


# -- file swap ---------------------------------------------------------------------------------------------
def _retry(fn: Callable[[], object], until: float) -> None:
    """Windows keeps a file locked a moment after its process exits (and antivirus scans): retry briefly."""
    while True:
        try:
            fn()
            return
        except PermissionError:
            if time.time() > until:
                raise
            time.sleep(0.5)


def swap(source: Path, target: Path, wait: float = 30.0) -> None:
    """Replace `target`'s build with `source`'s, file by file; on any failure put every old file back.

    Each old file is renamed into target/.update-old (renaming works even for an exe or DLL still mapped by a
    process, e.g. an MCP server an app keeps open; deleting doesn't). Files in target that aren't part of a
    build (anything outside the top-level names the new build ships) are left alone. Stale files of the old
    build under those names (e.g. a library dropped from _internal) are moved away too.
    """
    until = time.time() + wait
    backup = target / BACKUP
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    if backup.exists():  # still in use from the last update: keep it, use a fresh one
        backup = target / f"{BACKUP}-{int(time.time())}"
    tops = {p.name for p in source.iterdir()} - {BACKUP}
    new = [p.relative_to(source) for p in sorted(source.rglob("*")) if p.is_file()]
    old = [p.relative_to(target) for top in sorted(tops) if (target / top).exists()
           for p in ([target / top] if (target / top).is_file() else sorted((target / top).rglob("*")))
           if p.is_file()]
    moved: list[Path] = []  # old files now in the backup
    placed: list[Path] = []  # new files now in target
    try:
        for rel in old:
            dst = backup / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _retry(lambda rel=rel, dst=dst: os.replace(target / rel, dst), until)
            moved.append(rel)
        for rel in new:
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, dst)
            placed.append(rel)
    except Exception:
        for rel in reversed(placed):
            try:
                (target / rel).unlink()
            except OSError:
                pass
        for rel in reversed(moved):
            try:
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup / rel, target / rel)
            except OSError:
                log.exception("could not restore %s", rel)
        raise
    shutil.rmtree(backup, ignore_errors=True)  # whatever is still in use goes at the next update or start


def cleanup(target: Path | None = None) -> None:
    """Remove what the last update left behind: old files still in use then, the downloaded zip."""
    if target is not None:
        for p in target.glob(f"{BACKUP}*"):
            shutil.rmtree(p, ignore_errors=True)
    for p in Path(tempfile.gettempdir()).glob(f"{STAGING_PREFIX}*"):
        try:
            if time.time() - p.stat().st_mtime > 3600:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


# -- the updater process (runs from the staging folder) ------------------------------------------------------
def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_exit(pid: int, timeout: float) -> bool:
    end = time.time() + timeout
    while _alive(pid):
        if time.time() > end:
            return False
        time.sleep(0.25)
    return True


def _ours(pid: int) -> bool:
    """Is `pid` one of our exes? A pid from a stale lock file may belong to another program by now."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        return Path(buf.value).name.lower() in EXES
    finally:
        k32.CloseHandle(handle)


def _stop(pid: int) -> None:
    if not _ours(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError:
        pass


def _widget_pid() -> int:
    """The widget's pid from its QLockFile (first line), 0 if none."""
    try:
        return int((HOME / "widget.lock").read_text(encoding="utf-8", errors="replace").split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def _write_result(ok: bool, version: str, error: str = "") -> None:
    try:
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps({"ok": ok, "version": version, "error": error, "at": time.time()}),
                               encoding="utf-8")
    except OSError:
        pass


def read_result() -> dict | None:
    """What the last update did (once: the file is removed)."""
    try:
        data = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        RESULT_PATH.unlink()
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and time.time() - float(data.get("at") or 0) < 600 else None


def apply(source: Path, target: Path, daemon_pid: int, version: str) -> int:
    """`agent-voice apply-update`: wait for the daemon and widget to exit, swap the files, restart the daemon."""
    from agent_voice import client

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_DIR / "update.log", encoding="utf-8")])
    log.info("updating %s to v%s from %s", target, version, source)
    ok, error = False, ""
    try:
        if Path(sys.executable).resolve().parent == target.resolve():
            raise RuntimeError("the updater must not run from the install folder")
        if not all((target / exe).is_file() for exe in EXES):
            raise RuntimeError(f"{target} isn't an agent-voice install")
        time.sleep(1.5)  # let the widget see "installing" and quit on its own
        client.request("/shutdown", {}, timeout=3)
        if not _wait_exit(daemon_pid, 20):
            log.warning("daemon %s didn't stop: ending it", daemon_pid)
            _stop(daemon_pid)
            _wait_exit(daemon_pid, 5)
        widget = _widget_pid()
        if widget and _ours(widget) and not _wait_exit(widget, 15):
            log.warning("widget %s didn't quit: ending it", widget)
            _stop(widget)
            _wait_exit(widget, 5)
        swap(source, target)
        ok = True
        log.info("updated to v%s", version)
    except Exception as e:
        error = str(e) or type(e).__name__
        log.exception("update failed (the install is unchanged)")
    _write_result(ok, version, error)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW \
        if sys.platform == "win32" else 0
    try:
        subprocess.Popen([str(target / "agent-voice-w.exe"), "serve", "--background"], cwd=str(target),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags)
    except OSError:
        log.exception("could not restart agent-voice")
        return 1
    return 0 if ok else 1
