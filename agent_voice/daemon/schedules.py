"""Scheduled prompts: sent to a thread, or as a new thread, at a given time."""

from __future__ import annotations

import logging
import time

from agent_voice import launch, schedule
from agent_voice.config import save_state
from agent_voice.daemon.common import _clock
from agent_voice.daemon.phrases import _new_target, parse_new_thread
from agent_voice.i18n import tr

log = logging.getLogger("agent_voice.server")


class SchedulesMixin:
    """Scheduled prompts: sent to a thread, or as a new thread, at a given time."""

    def add_schedule(self, prompt: str, at: float, session: str = "", new: dict | None = None,
                     repeat: str = "") -> dict:
        """Send `prompt` at `at`: to a conversation (`session`), or as a new thread (`new`: target, cwd)."""
        prompt = prompt.strip()
        if not prompt or not at:
            raise ValueError("a prompt and a time")
        title = (tr("New thread ({where})", where=launch.LABELS.get((new or {}).get('target', ''),
                                                                     tr('where you last worked')))
                 if new is not None else self._title(session, self.threads.get(session, {}).get("source", "")))
        item = {"id": max((s["id"] for s in self._schedules), default=0) + 1, "at": int(at), "prompt": prompt[:4000],
                "session": session, "new": new, "repeat": repeat if repeat in schedule.REPEATS else "",
                "title": title}
        self._schedules.append(item)
        self._save_schedules()
        log.info("scheduled #%d for %s: %s", item["id"], _clock(at, "seven_day"), title)
        self._show_banner(tr("At {when}", when=_clock(at, 'seven_day'))
                          + (f" ({tr(item['repeat'])})" if item["repeat"] else "")
                          + tr(", {title} gets: {prompt}", title=title, prompt=prompt[:160]), "", "", kind="note",
                          tag="scheduled")
        return item

    def cancel_schedule(self, item_id: int) -> bool:
        before = len(self._schedules)
        self._schedules = [s for s in self._schedules if s["id"] != item_id]
        self._save_schedules()
        return len(self._schedules) < before

    def _save_schedules(self) -> None:
        self.state["schedules"] = self._schedules
        save_state(self.state)

    def schedule_said(self, text: str, lang: str, session: str) -> bool:
        """'At 2 pm tell Codex to run the tests', 'in 20 minutes, new thread: ...', 'à 14h lance les tests'."""
        timed = schedule.split_when(text, time.time())
        if not timed:
            return False
        at, rest = timed
        new = parse_new_thread(rest)
        if new:
            self.add_schedule(new[1], at, new={"target": _new_target(new[0]), "cwd": ""})
            return True
        routed = self._routed(rest)
        if routed:
            self.add_schedule(routed[1], at, session=routed[0])
            return True
        self.add_schedule(rest, at, session=session or self._last_session)
        return True

    def _check_schedules(self, now: float) -> None:
        """Send what's due; a prompt missed by more than 6 hours (the PC was off) is dropped, and said so."""
        changed = False
        for item in list(self._schedules):
            if item["at"] > now:
                continue
            late = now - item["at"] > 6 * 3600
            following = schedule.next_run(item["at"], item.get("repeat", ""), now)
            if following:
                item["at"] = int(following)
            else:
                self._schedules.remove(item)
            changed = True
            if late:
                self._show_banner(tr("Missed while the PC was off: {title}: {prompt}", title=item['title'],
                                     prompt=item['prompt'][:120]), "", "",
                                  kind="note", tag="scheduled")
                continue
            self.jobs.put(lambda i=dict(item): self._run_schedule(i))
        if changed:
            self._save_schedules()

    def _run_schedule(self, item: dict) -> None:
        from agent_voice import lang

        log.info("running scheduled #%d (%s)", item["id"], item["title"])
        self._push("scheduled", item["title"], item["prompt"])
        if item.get("new") is not None:
            self.new_thread(item["prompt"], item["new"].get("target", ""), item["new"].get("cwd", ""))
        else:
            self._send_to(item["session"], item["prompt"], lang.detect(item["prompt"]))

    def _schedules_status(self) -> list[dict]:
        return [{k: s[k] for k in ("id", "at", "title", "prompt", "repeat")}
                for s in sorted(self._schedules, key=lambda s: s["at"])][:6]
