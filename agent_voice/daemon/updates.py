"""Updates: the daily release check and the one-click install."""

from __future__ import annotations

import json
import logging
import os
import sys
import time

from agent_voice import __version__, updater
from agent_voice.i18n import tr

log = logging.getLogger("agent_voice.server")


RELEASES_API = f"https://api.github.com/repos/{updater.REPO}/releases/latest"


RELEASES_PAGE = f"https://github.com/{updater.REPO}/releases/latest"


UPDATE_EVERY = 3600  # re-check for a release every hour (daemons stay up for weeks; one small request)


class UpdatesMixin:
    """Updates: the daily release check and the one-click install."""

    def _check_update(self) -> None:
        """Tell the widget when GitHub has a newer release: a few seconds after start, then every hour."""
        if not self.config.get("update_check", True):
            return
        time.sleep(5)
        self._update_result()
        while True:
            self._fetch_release()
            time.sleep(UPDATE_EVERY)

    def _fetch_release(self) -> bool | None:
        """Ask GitHub for the latest release: True when it's newer than this one (`self.update` says how to get
        it), False when this is the latest, None when GitHub couldn't be reached."""
        import urllib.request

        try:
            req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                                "User-Agent": "agent-voice"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                latest = json.loads(resp.read().decode("utf-8"))
        except Exception:  # offline, rate-limited, private repo: no news is fine
            return None
        tag = str(latest.get("tag_name") or "")
        if not updater.is_newer(tag, __version__):
            return False
        if self.update and self.update.get("state"):  # already downloading or installing one
            return True
        asset = updater.pick_asset(latest)
        how = (("auto" if asset else "page") if updater.self_updatable() else "page" if getattr(sys, "frozen", False)
               else "git" if updater.checkout() else "page")
        self._release = {"asset": asset, "tag": tag}
        self.update = {"version": tag.lstrip("v"), "url": str(latest.get("html_url") or RELEASES_PAGE),
                       "how": how, "state": ""}
        log.info("update available: %s (%s)", tag, how)
        return True

    def check_updates(self, install: bool = True) -> None:
        """The tray's "Check for updates": ask GitHub now; a newer version is installed right away (`install`),
        and agent-voice restarts with it."""
        self._show_banner(tr("Checking for updates..."), "", "", kind="note", tag="update")
        found = self._fetch_release()
        if found is None:
            self._show_banner(tr("Couldn't reach GitHub to check for updates: try again in a moment."), "", "",
                              kind="note", tag="update")
        elif not found:
            self.update = None
            self._show_banner(tr("agent-voice is up to date (version {v}).", v=__version__), "", "", kind="note",
                              tag="update")
        elif install:
            self.install_update()
        else:
            self._show_banner(tr("Version {v} is available: click Update in the notch.", v=self.update['version']), "", "",
                              kind="note", tag="update")

    def _update_result(self) -> None:
        """Right after an update: say how it went, and clear what it left behind."""
        result = updater.read_result()
        if updater.self_updatable():
            updater.cleanup(updater.install_dir())
        if not result:
            return
        if result.get("ok"):
            text = tr("Updated to version {v}. Your settings and voices are unchanged.", v=result.get('version'))
        else:
            text = tr("The update to version {v} didn't go through: {why}. Nothing was changed; try again from the "
                      "tray.", v=result.get('version'), why=result.get('error'))
        self._show_banner(text, "", "", kind="note", tag="update")

    def install_update(self) -> None:
        """The widget's Update button: download, verify and hand off to the updater (release build only)."""
        up = self.update
        if not up:
            self._show_banner(tr("agent-voice is up to date."), "", "", kind="note", tag="update")
            return
        version = up["version"]
        if up.get("how") == "git":
            self._git_update(up)
            return
        if up.get("how") != "auto":
            self._show_banner(tr("Version {v} is out: download it from the release page.", v=version),
                              "", "", kind="note", tag="update")
            return
        if up.get("state"):
            return
        up["state"] = "downloading"
        self._show_banner(tr("Downloading version {v}...", v=version), "", "", kind="note", tag="update")
        try:
            def progress(frac: float) -> None:
                self._show_banner(tr("Downloading version {v}... {pct}%", v=version, pct=int(frac * 100)), "", "",
                                  kind="note", tag="update")

            zip_path = updater.download(self._release["asset"], progress)
            self._show_banner(tr("Checking version {v}...", v=version), "", "", kind="note", tag="update")
            root = updater.extract(zip_path, zip_path.parent / "build")
        except Exception as e:
            log.exception("update download failed")
            up["state"] = ""
            self._show_banner(tr("Couldn't download version {v} ({why}). Nothing was changed.", v=version, why=e),
                              "", "", kind="note", tag="update")
            return
        up["state"] = "installing"  # the widget quits when it sees this
        self._show_banner(tr("Installing version {v}: agent-voice restarts in a few seconds.", v=version),
                          "", "", kind="note", tag="update")
        try:
            updater.launch_apply(root, updater.install_dir(), os.getpid(), version)
        except OSError as e:
            log.exception("could not start the updater")
            up["state"] = ""
            self._show_banner(tr("Couldn't start the update ({why}). Nothing was changed.", why=e), "", "",
                              kind="note", tag="update")

    def _git_update(self, up: dict) -> None:
        """A source install: git pull (fast-forward only), reinstall if needed, restart. Never over local changes."""
        import subprocess

        from agent_voice.client import self_command

        if up.get("state"):
            return
        version, repo = up["version"], updater.checkout()
        up["state"] = "downloading"
        self._show_banner(tr("Updating to version {v}...", v=version), "", "", kind="note", tag="update")
        why = updater.git_update(repo) if repo else "this isn't a git checkout"
        if why:
            up["state"] = ""
            self._show_banner(tr("Couldn't update to version {v}: {why}. Nothing was changed. By hand: git pull, "
                                 "pip install -e . then agent-voice restart.", v=version, why=tr(why)), "", "",
                              kind="note", tag="update")
            return
        up["state"] = "installing"  # the widget quits when it sees this; the restart brings both back
        self._show_banner(tr("Updated to version {v}: agent-voice restarts.", v=version), "", "", kind="note", tag="update")
        flags = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                  | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True})
        subprocess.Popen(self_command("restart"), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, **flags)
