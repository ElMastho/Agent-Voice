"""The self-updater: version comparison, which asset it downloads, verification and the file swap."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from agent_voice import updater


def test_version_comparison():
    assert updater.is_newer("v0.6.0", "0.5.0")
    assert updater.is_newer("v0.10.0", "0.9.3")  # numeric, not alphabetical
    assert updater.is_newer("v1.0.0", "0.99.99")
    assert not updater.is_newer("v0.5.0", "0.5.0")
    assert not updater.is_newer("v0.4.9", "0.5.0")
    assert not updater.is_newer("", "0.5.0")


def _release(tag="v0.6.0", **asset):
    a = {"name": updater.ASSET, "state": "uploaded", "size": 1234,
         "browser_download_url": f"https://github.com/ElMastho/agent-voice/releases/download/{tag}/{updater.ASSET}"}
    a.update(asset)
    return {"tag_name": tag, "assets": [{"name": "source.tar.gz", "size": 10, "browser_download_url": "x"}, a]}


def test_asset_choice():
    assert updater.pick_asset(_release())["size"] == 1234
    assert updater.pick_asset(_release(name="agent-voice-macos.zip")) is None
    assert updater.pick_asset(_release(state="starter")) is None  # still uploading
    assert updater.pick_asset(_release(size=0)) is None
    assert updater.pick_asset(_release(browser_download_url="https://evil.example/agent-voice-windows-x64.zip")) is None
    # The url must be this release's, not another tag's.
    other = f"https://github.com/ElMastho/agent-voice/releases/download/v0.1.0/{updater.ASSET}"
    assert updater.pick_asset(_release(browser_download_url=other)) is None
    assert updater.pick_asset({"tag_name": "v0.6.0"}) is None


def _build(folder: Path, version: str, extra: dict[str, str] | None = None) -> Path:
    files = {"agent-voice.exe": f"console {version}", "agent-voice-w.exe": f"windowed {version}",
             "_internal/base_library.zip": f"lib {version}", "LICENSE": "GPL", **(extra or {})}
    for rel, text in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")
    return folder


def _zip(path: Path, build: Path, prefix: str = "agent-voice/") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for f in build.rglob("*"):
            if f.is_file():
                z.write(f, prefix + f.relative_to(build).as_posix())
    return path


def test_verify_and_extract(tmp_path):
    zip_path = _zip(tmp_path / updater.ASSET, _build(tmp_path / "b", "0.6.0"))
    size = zip_path.stat().st_size
    updater.verify(zip_path, size)
    with pytest.raises(ValueError, match="incomplete"):
        updater.verify(zip_path, size + 1)
    with pytest.raises(ValueError, match="checksum"):
        updater.verify(zip_path, size, "sha256:" + "0" * 64, "f" * 64)
    root = updater.extract(zip_path, tmp_path / "out")
    assert root == (tmp_path / "out" / "agent-voice").resolve()
    assert (root / "agent-voice-w.exe").read_text() == "windowed 0.6.0"


def test_rejects_a_bad_zip(tmp_path):
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip at all")
    with pytest.raises(ValueError, match="valid zip"):
        updater.verify(junk, junk.stat().st_size)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../outside.txt", "x")
    with pytest.raises(ValueError, match="unsafe"):
        updater.extract(evil, tmp_path / "out")
    assert not (tmp_path / "outside.txt").exists()
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(ValueError, match="build"):
        updater.extract(empty, tmp_path / "out2")


def test_swap_replaces_the_build_and_keeps_the_rest(tmp_path):
    target = _build(tmp_path / "install", "0.5.0", {"_internal/dropped.pyd": "old only", "notes.txt": "mine"})
    source = _build(tmp_path / "new", "0.6.0", {"_internal/added.pyd": "new only"})
    updater.swap(source, target)
    assert (target / "agent-voice-w.exe").read_text() == "windowed 0.6.0"
    assert (target / "_internal/base_library.zip").read_text() == "lib 0.6.0"
    assert (target / "_internal/added.pyd").exists()
    assert not (target / "_internal/dropped.pyd").exists()  # stale files of the old build go too
    assert (target / "notes.txt").read_text() == "mine"  # not part of the build: untouched
    assert not (target / updater.BACKUP).exists()
    assert (source / "agent-voice-w.exe").exists()  # copied, not moved: the updater runs from there


def test_swap_failure_restores_the_old_build(tmp_path, monkeypatch):
    target = _build(tmp_path / "install", "0.5.0", {"_internal/dropped.pyd": "old only"})
    source = _build(tmp_path / "new", "0.6.0")
    real_copy, calls = updater.shutil.copy2, []

    def flaky(src, dst, *a, **k):
        calls.append(dst)
        if len(calls) == 3:  # fail halfway through: disk full, a file in use...
            raise OSError("disk full")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(updater.shutil, "copy2", flaky)
    with pytest.raises(OSError):
        updater.swap(source, target)
    assert (target / "agent-voice.exe").read_text() == "console 0.5.0"
    assert (target / "agent-voice-w.exe").read_text() == "windowed 0.5.0"
    assert (target / "_internal/base_library.zip").read_text() == "lib 0.5.0"
    assert (target / "_internal/dropped.pyd").read_text() == "old only"


def test_swap_retries_a_locked_file(tmp_path, monkeypatch):
    target = _build(tmp_path / "install", "0.5.0")
    source = _build(tmp_path / "new", "0.6.0")
    real_replace, fails = updater.os.replace, [2]

    def locked(src, dst):
        if fails[0] and Path(src).name == "agent-voice-w.exe":
            fails[0] -= 1
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(updater.os, "replace", locked)
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    updater.swap(source, target)
    assert (target / "agent-voice-w.exe").read_text() == "windowed 0.6.0"


def test_result_file_is_read_once(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "RESULT_PATH", tmp_path / "update-result.json")
    updater._write_result(True, "0.6.0")
    assert updater.read_result()["version"] == "0.6.0"
    assert updater.read_result() is None


def test_daemon_says_how_to_update(monkeypatch):
    from agent_voice import server

    d = server.Daemon.__new__(server.Daemon)
    d.banner, d._release, d.state = None, {}, {}
    d._title = lambda session, source: ""
    d.update = {"version": "0.6.0", "url": server.RELEASES_PAGE, "how": "git", "state": ""}
    d.install_update()
    assert "git pull" in d.banner["text"] and d.banner["kind"] == "note"
    d.update = None
    d.install_update()
    assert "up to date" in d.banner["text"]


def test_failed_download_changes_nothing(monkeypatch):
    from agent_voice import server

    d = server.Daemon.__new__(server.Daemon)
    d.state = {}
    d.banner, d._release = None, {"asset": {"browser_download_url": "x", "size": 1}}
    d._title = lambda session, source: ""
    d.update = {"version": "0.6.0", "url": server.RELEASES_PAGE, "how": "auto", "state": ""}

    def offline(asset, progress=None):
        raise OSError("offline")

    launched = []
    monkeypatch.setattr(updater, "download", offline)
    monkeypatch.setattr(updater, "launch_apply", lambda *a: launched.append(a))
    d.install_update()
    assert not launched and d.update["state"] == "" and "Nothing was changed" in d.banner["text"]
