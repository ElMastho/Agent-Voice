"""Rate limits and usage: readings, the estimate between them, warnings, out-of-quota threads resumed."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from agent_voice import install, launch, quota, textclean, usage
from agent_voice.config import HOME, save_state
from agent_voice.daemon.common import AGENT_TITLES, _clock
from agent_voice.i18n import tr

log = logging.getLogger("agent_voice.server")


class LimitsMixin:
    """Rate limits and usage: readings, the estimate between them, warnings, out-of-quota threads resumed."""

    def limit_hit(self, session: str, source: str, text: str = "", resets: float = 0.0) -> None:
        """A thread ran out of quota: offer to send "continue" once its limit resets (or do it, if you said so)."""
        if not session:
            return
        found = textclean.limit_reset(text) if text else None
        resets = resets or (found or 0.0)
        if not resets:
            full = [w["resets"] for w in self._limits_status().get(source, {}).values() if (w.get("pct") or 0) >= 100]
            resets = min(full) if full else 0.0
        before = self._limited.get(session) or {}
        resume = (bool(self.config.get("resume", {}).get("auto", True)) or bool(before.get("resume"))
                  or time.time() - float(before.get("sent", 0)) < 900)  # still out after our "continue": again
        guess = not resets
        self._limited[session] = {"source": source, "resets": int(resets or time.time() + 1800), "guess": guess,
                                  "resume": resume, "at": round(time.time())}
        self._save_limited()
        self.threads.setdefault(session, {"source": source, "last": time.time()})
        title = self._title(session, source)
        when = _clock(self._limited[session]["resets"], "five_hour")
        log.info("%s hit its limit (resets %s%s)", title, when, ", resuming then" if resume else "")
        self._event("needs", session, source, "limit reached")
        about = tr("about ") if guess else ""
        self._push("limit", title, tr("Out of quota until {when}", when=about + when)
                   + (tr(": it gets “continue” then.") if resume else "."))
        if source == "claude" and resets and not guess:
            window = "seven_day" if resets - time.time() > 6 * 3600 else "five_hour"
            self._set_limits("claude", {window: {"pct": 100, "resets": int(resets)}})
        if before.get("sent") and resume:
            return  # our "continue" came too early: quietly try again at the next reset
        if resume:
            self._show_banner(tr("{title} is out of quota until {when}: I'll send it “continue” then.", title=title,
                                 when=about + when), session, source, kind="note", tag="limit")
            return
        self._show_banner(tr("{title} is out of quota until {when}. Say “resume when it resets” or click Resume, and "
                             "I'll send it “continue” then.", title=title, when=about + when), session, source,
                          kind="needs", tag="limit")
        if self.chime:
            self.play_chime("attention")

    def set_resume(self, session: str = "", on: bool = True) -> bool:
        """Resume a thread (the most recent one out of quota) when its limit resets, or don't."""
        session = session or max(self._limited, key=lambda s: self._limited[s]["at"], default="")
        if session not in self._limited:
            return False
        self._limited[session]["resume"] = on
        self._save_limited()
        info = self._limited[session]
        title = self._title(session, info["source"])
        when = _clock(info["resets"], "five_hour")
        self._show_banner(tr("I'll send {title} “continue” at {when}." if on else "{title} won't be resumed.",
                             title=title, when=when),
                          session, info["source"], kind="note", tag="limit")
        return True

    def _save_limited(self) -> None:
        self.state["limited"] = self._limited
        save_state(self.state)

    def _check_resumes(self, now: float) -> None:
        """At reset time (plus a minute), send "continue" to the threads you asked to resume."""
        for session, info in list(self._limited.items()):
            if now - info.get("at", now) > 8 * 86400:
                self._limited.pop(session)  # a week old: forget it
                self._save_limited()
            elif info.get("resume") and not info.get("sent") and now >= info["resets"] + 60:
                info["sent"] = round(now)
                self._save_limited()
                message = str(self.config.get("resume", {}).get("message") or "Continue.")
                log.info("resuming %s after its limit reset", self._title(session, info["source"]))
                self._push("resumed", self._title(session, info["source"]),
                           tr("Sent “{message}” after its limit reset.", message=message))
                self.jobs.put(lambda s=session, m=message: self._send_to(s, m, "en"))

    def _not_limited(self, session: str) -> None:
        """The thread talks again: it isn't out of quota any more."""
        if session in self._limited:
            sent = self._limited.pop(session).get("sent")
            self._save_limited()
            if sent:
                log.info("%s resumed", self._title(session))

    def set_usage(self, data: dict) -> None:
        """Context used by a conversation, and its agent's rate limits (from Claude Code's status line)."""
        session, source = str(data.get("session") or ""), str(data.get("source") or "claude")
        context = data.get("context")
        if session and isinstance(context, (int, float)):
            self.usage[session] = {**self.usage.get(session, {}), "context": max(0, min(100, round(float(context)))),
                                   "at": time.time()}
            self.usage = dict(list(self.usage.items())[-50:])
        tokens = data.get("tokens")
        if session and isinstance(tokens, (int, float)) and tokens >= 0:  # this conversation's tokens today
            self.usage.setdefault(session, {"at": time.time()})["tokens"] = int(tokens)
            t = self._day()["threads"].setdefault(session, {"source": source, "turns": 0, "tokens": 0, "last": ""})
            t["tokens"] = int(tokens)
        limits = {}
        for window in quota.WINDOWS:
            w = data.get(window)
            if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)):
                limits[window] = {"pct": round(float(w["pct"])), "resets": int(w.get("resets") or 0)}
        if limits:
            self._set_limits(source, limits)

    def _set_limits(self, source: str, limits: dict, at: float = 0.0) -> None:
        """Keep an agent's latest windows; warn once when one gets close to full."""
        at = at or time.time()
        limits = {k: {"pct": max(0, min(100, int(v["pct"]))), "resets": int(v.get("resets") or 0)}
                  for k, v in limits.items() if k in quota.WINDOWS}
        if source == "claude":  # every real reading calibrates the estimate
            for window, w in limits.items():
                self._estimator.reading(window, w["pct"], w["resets"], at)
            self.state["claude_usage"] = self._estimator.state()
        if not limits or at < self._limits_at.get(source, 0.0):
            return  # older than what we have (Claude's cache vs its status line)
        self._limits_at[source] = at
        now = time.time()
        # One window reported (a limit hit, a drop file): the other one still runs as it was.
        limits = {**{k: v for k, v in (self.limits.get(source) or {}).items() if v.get("resets", 0) > now}, **limits}
        if limits == self.limits.get(source):
            return
        self.limits[source] = limits
        self.state["limits"] = self.limits
        save_state(self.state)
        if any(w["pct"] >= 100 for w in limits.values()):
            latest = max((s for s, i in self.threads.items() if i["source"] == source),
                         key=lambda s: self.threads[s]["last"], default="")
            if latest and latest not in self._limited:
                self.limit_hit(latest, source)
        self._warn_limits(source, limits)

    def _warn_limits(self, source: str, limits: dict) -> None:
        """A note, once per window, when one passes 90% (measured or estimated)."""
        for window, w in limits.items():
            if w.get("pct") is None:
                continue
            key = f"{source}:{window}:{w['resets'] // 600}"  # once per window (its reset time, give or take)
            if w["pct"] >= 90 and key not in self._limit_warned:
                self._limit_warned.add(key)
                name = AGENT_TITLES.get(source, source.capitalize())
                when = tr(", it resets at {time}", time=_clock(w['resets'], window)) if w["resets"] else ""
                log.info("%s %s limit at %d%%", source, window, w["pct"])
                about = tr("about ") if w.get("est") else ""
                self._show_banner(tr("{name} has used {pct}% of its 5-hour limit{when}." if window == "five_hour" else
                                     "{name} has used {pct}% of its weekly limit{when}.", name=name,
                                     pct=f"{about}{w['pct']}", when=when), "", "", kind="note", tag="limits")

    def _quota_watch(self) -> None:
        """Every minute: every agent's limits, re-read where they changed."""
        while True:
            self._read_limits()
            time.sleep(60)

    def refresh_limits(self) -> None:
        """The panel's ↻: ask Claude Code for its usage now, and re-read everything."""
        self._limits_refreshing = True
        try:
            self._read_limits(force=True)
        finally:
            self._limits_refreshing = False

    def _read_limits(self, force: bool = False) -> None:
        """Claude's cache (fresh numbers asked of Claude Code when due, or now with `force`) and its estimate, the
        usage folder, Codex's logs. One at a time: the minute's check and the refresh button share the ledger."""
        with self._limits_lock:
            for what, step in (("claude limits", lambda: self._read_claude(force)),
                               ("claude usage estimate", self._estimate_claude),
                               ("usage folder", self._read_usage_drops),
                               ("codex limits", lambda: self._read_codex(force))):
                try:
                    step()
                except Exception:
                    log.exception("%s check failed", what)

    def _read_claude(self, force: bool) -> None:
        claude_json = Path.home() / ".claude.json"
        self._ledger.refresh()  # first: a reading calibrates against the requests before it
        for asked in (False, True):  # what's cached now, then (when due) what Claude Code fetches for us
            if asked and not self._ask_claude_usage(force=force):
                return
            mtime = claude_json.stat().st_mtime if claude_json.exists() else 0.0
            if mtime != self._claude_seen or asked:
                self._claude_seen = mtime
                fetched, found = quota.claude_cached(claude_json)
                if found:
                    self._set_limits("claude", found, at=fetched)

    def _read_codex(self, force: bool) -> None:
        path = quota.latest_codex_log()
        if path is None:
            return
        seen = (path, path.stat().st_mtime)
        if seen != self._codex_seen or force:
            self._codex_seen = seen
            found = quota.codex_limits(path)
            if found:
                self._set_limits("codex", found)

    def _ask_claude_usage(self, now: float | None = None, force: bool = False) -> bool:
        """Have Claude Code fetch its own usage (`claude -p /usage`: a local command, no model request), which it
        saves to ~/.claude.json. Every few minutes while Claude is in use, and once at start."""
        now = time.time() if now is None else now
        minutes = float(self.config.get("limits", {}).get("claude_refresh_minutes", 10) or 0)
        if not force:
            if minutes <= 0 or now - self._claude_asked < minutes * 60:
                return False
            last = self._ledger.items[-1][0] if self._ledger.items else 0.0
            if self._claude_asked and now - last > 3600:
                return False  # Claude not used in the last hour: nothing moved
        exe = launch.find_cli("claude")
        if not exe:
            return False
        self._claude_asked = now
        import subprocess

        try:
            subprocess.run([exe, "-p", "/usage"], cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("claude /usage didn't run: %s", type(exc).__name__)
            return False
        return True

    def _estimate_claude(self, now: float | None = None) -> None:
        """Read the new requests in Claude's transcripts and estimate where its windows are."""
        now = time.time() if now is None else now
        self._ledger.refresh(now)
        est = {w: e for w in usage.SPANS if (e := self._estimator.estimate(w, now))}
        self._claude_est = est
        self._warn_limits("claude", {w: e for w, e in est.items() if e.get("pct") is not None and e["resets"]})

    def _read_usage_drops(self) -> None:
        """~/.agent-voice/usage/*.json: limits any agent or script reports (one file per agent, see the guide)."""
        folder = HOME / "usage"
        if not folder.is_dir():
            return
        now = time.time()
        for path in folder.glob("*.json"):
            mtime = path.stat().st_mtime
            if now - mtime > 12 * 3600 or mtime <= self._limits_at.get(path.stem, 0.0):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            windows = {w: v for w, v in data.items() if w in quota.WINDOWS and isinstance(v, dict)
                       and isinstance(v.get("pct"), (int, float))}
            if windows:
                self._set_limits(str(data.get("agent") or path.stem)[:40], windows,
                                 at=float(data.get("updated") or mtime))

    def _limits_status(self) -> dict:
        """Each agent's windows still running (a window past its reset starts again at zero: dropped).

        Claude: the last reading as it is until the next one is due, then the estimate from its transcripts ("est").
        """
        now = time.time()
        out = {source: {k: v for k, v in windows.items() if not v["resets"] or v["resets"] > now}
               for source, windows in self.limits.items()}
        refresh = float(self.config.get("limits", {}).get("claude_refresh_minutes", 10) or 10)
        fresh = now - self._limits_at.get("claude", 0.0) < refresh * 60 + 120  # until the next reading is due
        for window, e in self._claude_est.items():
            real = (out.get("claude") or {}).get(window)
            if (real and fresh) or (e.get("pct") is None and not e.get("tokens")):
                continue
            if not e["resets"] or e["resets"] > now:
                out.setdefault("claude", {})[window] = e
        for source, windows in out.items():
            for window, w in windows.items():
                full = self._forecast(source, window, w, now)
                if full:
                    windows[window] = {**w, "full_at": full}
        return {source: windows for source, windows in out.items() if windows}

    def _forecast(self, source: str, window: str, w: dict, now: float) -> int:
        """When this window is full at the pace of the last hour (of the last day, for the weekly one): epoch seconds,
        to the 5 minutes; 0 when it won't be before it resets, or nothing was used in the last 15 minutes.

        Claude: its requests' cost in the transcripts, over what one percent costs (`usage.Estimator`). Others:
        how their readings rose."""
        pct, resets = w.get("pct"), float(w.get("resets") or 0)
        key = (source, window)
        samples = [s for s in self._pace.get(key, []) if now - s[0] < 86400 and s[2] == int(resets // 600)]
        if pct is not None and (not samples or now - samples[-1][0] >= 60):
            samples.append((now, float(pct), int(resets // 600)))
        self._pace[key] = samples
        if pct is None or not resets or not 10 <= pct < 100:
            return 0
        span = 3600.0 if window == "five_hour" else 86400.0
        start = max(now - span, resets - usage.SPANS[window])
        rate = self._estimator.rate.get(window) if source == "claude" else None
        if rate:
            recent = [i for i in self._ledger.items if i[0] >= start]
            if not recent or now - recent[-1][0] > 900:
                return 0
            pace = sum(i[1] for i in recent) / rate / max(900.0, now - start)  # percent per second
        else:
            before = [s for s in samples if s[0] >= start]
            ago = [s for s in before if s[0] <= now - 900]
            if not ago or pct <= ago[-1][1]:  # nothing moved in the last 15 minutes
                return 0
            pace = (pct - before[0][1]) / max(900.0, now - before[0][0])
        if pace <= 0:
            return 0
        full = now + (100 - pct) / pace
        return 0 if full >= resets - 300 else int(round(full / 300) * 300)

    def usage_line(self, enable: bool) -> None:
        """Turn Claude Code's status line (its limits, for the notch) on or off from the widget."""
        try:
            result = install.usage_line(enable)
        except Exception as exc:
            log.exception("usage line failed")
            result = f"failed: {exc}"
        log.info("usage line: %s", result)
        self._agents_at = 0.0
        if result.startswith("skipped"):
            text = tr("Claude Code already has your own status line: agent-voice leaves it alone.")
        elif result.startswith("failed"):
            text = tr("Couldn't change Claude Code's status line ({why}).", why=result[8:])
        elif enable:
            text = tr("Claude Code's limits will show here after its next reply (from its status line).")
        else:
            self.limits.pop("claude", None)
            self.state["limits"] = self.limits
            save_state(self.state)
            text = tr("Claude Code's limits are off; its status line is back to normal.")
        self._show_banner(text, "", "", kind="note", tag="limits")
