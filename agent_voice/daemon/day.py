"""Your day: what's playing, today's meetings, the day's turns, the morning brief and the daily recap."""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from agent_voice import agenda, media, textclean
from agent_voice.config import save_state
from agent_voice.daemon.common import AGENT_TITLES, _join, _meeting_key
from agent_voice.i18n import plural, tr

log = logging.getLogger("agent_voice.server")


class DayMixin:
    """Your day: what's playing, today's meetings, the day's turns, the morning brief and the daily recap."""

    def _media_loop(self) -> None:
        """Every 4 s: what's playing (a window title read: about a millisecond when nothing plays)."""
        while True:
            time.sleep(4)
            self.media = media.now_playing()

    def media_control(self, action: str) -> bool:
        done = media.control(action)
        if done:
            threading.Timer(0.6, lambda: setattr(self, "media", media.now_playing())).start()  # show it at once
        return done

    def calendars(self) -> list[str]:
        return [str(c) for c in self.state.get("calendars") or []]

    def set_calendar(self, action: str, source: str = "") -> list[str]:
        """Add or remove a calendar link (or .ics file); "clear" removes them all. Fetched right away."""
        links = self.calendars()
        source = source.strip()
        if action == "add":
            if not re.match(r"^(?:https?|webcal)://\S+$", source, re.I) and not Path(source).expanduser().is_file():
                raise ValueError("an iCal link (https://... .ics) or a .ics file")
            links = [*[c for c in links if c != source], source]
        elif action == "remove":
            links = [c for c in links if c != source]
        elif action == "clear":
            links = []
        self.state["calendars"] = links[-5:]
        save_state(self.state)
        self._agenda_wake.set()
        return self.calendars()

    def _agenda_loop(self) -> None:
        """Today's meetings: the links every 15 minutes (or when one is added), Outlook's every 5."""
        from datetime import date

        events: list[dict] = []
        fetched = 0.0
        while True:
            now = time.time()
            today = date.today()
            if now - fetched > 900 or self._agenda_wake.is_set():
                self._agenda_wake.clear()
                events, errors = [], []
                for link in self.calendars():
                    try:
                        events += agenda.parse(agenda.fetch(link))
                    except Exception as exc:
                        log.info("calendar link failed: %s", type(exc).__name__)
                        errors.append(type(exc).__name__)
                self._agenda_error = "couldn't read a calendar link" if errors else ""
                fetched = now
            try:
                found = agenda.on_day(events, today) + agenda.outlook_day(today)
                seen: dict[tuple, dict] = {}
                for e in sorted(found, key=lambda x: (not x["all_day"], x["start"])):
                    key = (e["title"], int(e["start"]))
                    if key not in seen:  # in both Outlook and a link
                        seen[key] = e
                    elif e.get("join") and not seen[key].get("join"):
                        seen[key] = {**seen[key], "join": e["join"]}
                self.agenda = list(seen.values())
            except Exception:
                log.exception("today's meetings unavailable")
            self._agenda_wake.wait(300)

    def _check_meetings(self, now: float) -> None:
        """A meeting about to start (`calendar.alert_minutes` before): its banner, with Join when it has a video
        link and +5 min. Once per meeting, and again when snoozed; not while you're already in a call."""
        lead = float(self.config.get("calendar", {}).get("alert_minutes", 2))
        if lead < 0 or self.in_call:
            return
        for e in self.agenda:
            if e["all_day"] or now >= e["end"]:
                continue
            key = _meeting_key(e)
            again = self._meeting_snooze.get(key)
            due = now >= again if again else (key not in self._meeting_alerted
                                              and e["start"] - lead * 60 <= now < e["start"] + 300)
            if due:
                self._meeting_snooze.pop(key, None)
                self._meeting_alerted.add(key)
                self._meeting_due(e, now)

    def _meeting_due(self, e: dict, now: float) -> None:
        """'Design review starts in 2 min.' Shown even in Do not disturb (silently), like your reminders."""
        import math

        soon, sl = e["start"] - now, self.language
        if soon > 30:
            n = max(1, math.ceil(soon / 60))
            text = tr("{title} starts in {n} min.", title=e["title"], n=n)
            said = tr("{title} starts in {n} minutes.", in_lang=sl, title=e["title"], n=n)
        elif soon > -60:
            text = tr("{title} starts now.", title=e["title"])
            said = tr("{title} starts now.", in_lang=sl, title=e["title"])
        else:
            n = math.floor(-soon / 60)
            text = tr("{title} started {n} min ago.", title=e["title"], n=n)
            said = tr("{title} started {n} minutes ago.", in_lang=sl, title=e["title"], n=n)
        if e.get("location") and not agenda.join_link(e["location"]):
            text += f" {e['location']}"
        join = bool(agenda.join_link(e.get("join") or ""))
        log.info("meeting alert: %r (join link: %s)", e["title"][:60], join)
        self._show_banner(text, "", "", kind="meeting", tag="meeting",
                          extra={"meeting": {"start": int(e["start"]), "join": join}, "snooze": {"seconds": 300}})
        self._push("scheduled", tr("Meeting"), text)
        if self.muted:
            return
        self.play_chime("attention")
        if not self.chime:
            self._wake_cancel.set()
            self.speaker.speak(said, sl)

    def snooze_meeting(self, start: float, seconds: float = 300) -> bool:
        """+5 min on a meeting's banner: told again then (even once it has started, until it ends)."""
        meeting = next((e for e in self.agenda if int(e["start"]) == int(start) and not e["all_day"]), None)
        if meeting is None:
            return False
        self._meeting_snooze[_meeting_key(meeting)] = time.time() + max(60.0, min(3600.0, float(seconds)))
        return True

    def _agenda_status(self) -> dict:
        return {"events": self.agenda[:12], "sources": len(self.calendars()), "error": self._agenda_error}

    def _joinable(self, now: float | None = None, start: float = 0) -> dict | None:
        """The meeting to join: the one starting at `start`, else the one on now, else the next in 15 minutes (with
        a video link: Teams, Zoom, Meet...)."""
        now = time.time() if now is None else now
        meetings = [e for e in self.agenda if e.get("join") and not e["all_day"] and e["end"] > now]
        if start:
            return next((e for e in meetings if int(e["start"]) == int(start)), None)
        return next((e for e in meetings if e["start"] - now < 15 * 60), None)

    def join_meeting(self, start: float = 0) -> str:
        """Open the meeting's link (Teams, Zoom, Meet... open their app from it); its title, "" when there's none."""
        import webbrowser

        meeting = self._joinable(start=start)
        link = agenda.join_link(meeting["join"]) if meeting else ""
        if not link:
            return ""
        key = _meeting_key(meeting)
        self._meeting_alerted.add(key)  # joined: no alert for it any more, snoozed or not
        self._meeting_snooze.pop(key, None)
        log.info("joining the meeting %r", meeting["title"][:60])
        webbrowser.open(link)
        return meeting["title"]

    def _day(self) -> dict:
        """Today's log (turns, tokens, what each thread did), started afresh each morning."""
        today = time.strftime("%Y-%m-%d")
        day = self.state.get("day")
        if not isinstance(day, dict) or day.get("date") != today:
            if isinstance(day, dict) and day.get("threads"):  # for the morning brief: the last day you worked
                done = list(day["threads"].values())
                top = max(done, key=lambda t: t.get("turns", 0))
                self.state["yesterday"] = {"date": day.get("date"), "turns": sum(t.get("turns", 0) for t in done),
                                           "threads": len(done), "top": top.get("title") or ""}
            day = {"date": today, "threads": {}, "hours": [0] * 24}
            self.state["day"] = day
        return day

    def _log_turn(self, session: str, source: str, summary: str) -> None:
        day = self._day()
        t = day["threads"].setdefault(session or source or "?", {"source": source, "turns": 0, "tokens": 0, "last": ""})
        t["turns"] += 1
        t["title"] = self._title(session, source)
        if summary:
            t["last"] = " ".join(summary.split())[:200]
        day["hours"][time.localtime().tm_hour] += 1
        save_state(self.state)

    def _day_status(self) -> dict:
        day = self._day()
        threads = day["threads"].values()
        return {"turns": sum(t["turns"] for t in threads), "threads": len(day["threads"]),
                "tokens": sum(int(t.get("tokens") or 0) for t in threads), "hours": day["hours"]}

    def recap_text(self) -> tuple[str, str]:
        """(shown, spoken): what each thread did today, what's waiting, the limits and the tokens. Shown in the
        interface's language, spoken in your speech language."""
        day, stats = self._day(), self._day_status()
        if not stats["turns"]:
            return tr("Nothing yet today."), tr("Nothing yet today.", in_lang=self.language)
        threads = sorted(day["threads"].items(), key=lambda kv: -kv[1]["turns"])
        lines = [plural(stats["turns"], "{n} turn", "{n} turns") + " "
                 + plural(stats["threads"], "in {n} thread", "in {n} threads")
                 + (tr(" · {tokens} tokens", tokens=textclean.tokens_label(stats['tokens'])) if stats["tokens"] else "")
                 + "."]
        for session, t in threads[:5]:
            title = self._title(session, t.get("source", "")) if session in self.threads else t.get("title") or "?"
            lines.append(f"{title} ({t['turns']}): {textclean.truncate(t['last'], 110) or tr('done')}")
        waiting = sorted({n["title"] for n in self._needs_status()})
        if waiting:
            lines.append(tr("Still waiting on you: {who}.", who=_join(waiting)))
        lines += self._limits_lines()
        top, sl = threads[0][1], self.language
        said = tr("Today: {turns} turns in {threads} threads, most of it in {top}.", in_lang=sl, turns=stats['turns'],
                  threads=stats['threads'], top=top.get('title') or tr('one thread', in_lang=sl))
        if waiting:
            said += " " + plural(len(waiting), "{who} still needs you.", "{who} still need you.", in_lang=sl,
                                 who=_join(waiting, sl))
        said += self._week_said()
        return " ".join(lines), said

    def _limits_lines(self) -> list[str]:
        """'Limits: Claude 5h 40%, week ~62%.' (none when no agent reports any)."""
        limits = []
        for source, windows in self._limits_status().items():
            parts = [(tr("5h") if k == "five_hour" else tr("week")) + " "
                     + (f"{'~' if w.get('est') else ''}{w['pct']}%" if w.get("pct") is not None
                        else tr("{tokens} tokens", tokens=textclean.tokens_label(w.get('tokens', 0))))
                     for k, w in sorted(windows.items())]
            limits.append(f"{AGENT_TITLES.get(source, source.capitalize())} {', '.join(parts)}")
        return [tr("Limits: ") + "; ".join(limits) + "."] if limits else []

    def _week_said(self) -> str:
        week = [w["pct"] for windows in self._limits_status().values() for k, w in windows.items()
                if k == "seven_day" and w.get("pct") is not None]
        return " " + tr("You've used {pct}% of your weekly limit.", in_lang=self.language, pct=max(week)) if week else ""

    def brief_text(self, away: list[dict] | None = None) -> tuple[str, str]:
        """(shown, spoken): today's meetings, what's due later (reminders, scheduled prompts), your open to-dos,
        what's waiting on you, what finished while you were away, the last day you worked, the limits.
        ("", "") when there's nothing to tell."""
        from datetime import date, datetime, timedelta

        now, sl = time.time(), self.language
        self._day()  # a new day: the last one is put aside first
        tonight = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).timestamp()

        def hm(at: float) -> str:
            return datetime.fromtimestamp(at).strftime("%H:%M")

        shown, said = [], []
        meetings = [e for e in self.agenda if not e["all_day"] and e["end"] > now]
        if meetings:
            first = meetings[0]
            shown.append(plural(len(meetings), "{n} meeting: {list}.", "{n} meetings: {list}.",
                                list=", ".join(f"{hm(e['start'])} {e['title']}" for e in meetings[:3])))
            said.append(plural(len(meetings), "You have {n} meeting today: {title} at {time}.",
                               "You have {n} meetings today, the first is {title} at {time}.", in_lang=sl,
                               title=first["title"], time=hm(first["start"])))
        elif self.calendars():
            shown.append(tr("No meetings today."))
            said.append(tr("No meetings today.", in_lang=sl))
        later = [(r["at"], r["text"]) for r in self.companion.data.get("reminders") or [] if now <= r["at"] < tonight]
        later += [(s["at"], f"{s['title']}: {s['prompt']}") for s in self._schedules if now <= s["at"] < tonight]
        if later:
            later.sort(key=lambda x: x[0])
            shown.append(tr("Later today: {list}.",
                            list="; ".join(f"{hm(at)} {textclean.truncate(text, 60)}" for at, text in later[:3])))
            said.append(plural(len(later), "One thing later today, at {time}.", "{n} things later today, the first at {time}.",
                               in_lang=sl, time=hm(later[0][0])))
        todos = [t for t in self.companion.data.get("todos") or [] if not t["done"]]
        if todos:
            mine = [t for t in todos if (t.get("source") or "you") == "you"] or todos
            shown.append(plural(len(todos), "{n} to-do: {list}.", "{n} to-dos: {list}.",
                                list=", ".join(textclean.truncate(t["text"], 40) for t in mine[:3])))
            said.append(plural(len(todos), "{n} to-do is open.", "{n} to-dos are open.", in_lang=sl))
        done = list(dict.fromkeys(self._title(e["session"], e["source"]) for e in away or [] if e["kind"] == "done"))
        if done:
            shown.append(tr("While you were away: {news}.", news=plural(len(done), "{who} finished", "{who} finished",
                                                                         who=_join(done))))
        waiting = sorted({n["title"] for n in self._needs_status()})
        if waiting:
            shown.append(tr("Still waiting on you: {who}.", who=_join(waiting)))
            said.append(plural(len(waiting), "{who} still needs you.", "{who} still need you.", in_lang=sl,
                               who=_join(waiting, sl)))
        last = self.state.get("yesterday") or {}
        if last.get("turns") and shown:  # only beside something else: it's no news on its own
            when = tr("Yesterday") if last.get("date") == (date.today() - timedelta(days=1)).isoformat() else tr("Last time")
            shown.append(f"{when}: " + plural(int(last["turns"]), "{n} turn", "{n} turns") + " "
                         + plural(int(last.get("threads") or 1), "in {n} thread", "in {n} threads")
                         + (tr(", most of it in {top}", top=last["top"]) if last.get("top") else "") + ".")
        if not shown:
            return "", ""
        shown += self._limits_lines()
        hello = [tr("Good morning.", in_lang=sl)] if time.localtime(now).tm_hour < 12 else []
        return " ".join(shown), " ".join([*hello, *said]) + self._week_said()

    def brief(self, spoken: bool = True, away: list[dict] | None = None, asked: bool = False) -> None:
        """The morning brief: once a day by itself (`_check_brief`), or when you ask for it (at night, that doesn't
        stand in for the morning's)."""
        if not asked or self._brief_due(time.time()):
            self.state["brief_day"] = time.strftime("%Y-%m-%d")
            save_state(self.state)
        shown, said = self.brief_text(away)
        if not shown:
            if not asked:
                if away:
                    self._catch_up(away)
                return
            shown, said = tr("Nothing planned today."), tr("Nothing planned today.", in_lang=self.language)
        log.info("morning brief: %s", said)
        self._show_banner(shown, "", "", kind="brief")
        if spoken and not self.quiet:
            self._wake_cancel.set()
            self.speaker.speak(said, self.language)

    def _brief_due(self, now: float) -> bool:
        """Not given yet today, and it's morning (`brief.from` to `brief.until`)."""
        cfg = self.config.get("brief", {})
        if not cfg.get("enabled", True) or self.focus:
            return False
        local = time.localtime(now)
        if self.state.get("brief_day") == time.strftime("%Y-%m-%d", local):
            return False
        try:
            start, end = (tuple(int(x) for x in str(cfg.get(k, d)).split(":", 1))
                          for k, d in (("from", "05:00"), ("until", "12:00")))
        except ValueError:
            return False
        return start <= (local.tm_hour, local.tm_min) < end

    def _check_brief(self, now: float, idle: float) -> None:
        """The first time you're at the PC in the morning (back from the night's away: `_check_away`)."""
        if idle < 60 and not self._away_since and self._brief_due(now):
            self.brief()

    def recap(self, spoken: bool = True) -> None:
        shown, said = self.recap_text()
        log.info("recap: %s", said)
        self._show_banner(shown, "", "", kind="recap")
        self._push("recap", tr("Today"), shown, full=shown)
        if spoken and not self.quiet:
            self._wake_cancel.set()
            self.speaker.speak(said, "en")

    def _check_recap(self, now: float) -> None:
        """The evening recap, once a day, when you're there to see it."""
        cfg = self.config.get("recap", {})
        if not cfg.get("enabled", True) or self._away_since or self.focus:
            return
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.state.get("recap_day") == today:
            return
        try:
            hour, minute = (int(x) for x in str(cfg.get("at", "18:00")).split(":", 1))
        except ValueError:
            return
        local = time.localtime(now)
        if (local.tm_hour, local.tm_min) < (hour, minute) or not self._day_status()["turns"]:
            return
        self.state["recap_day"] = today
        save_state(self.state)
        self.recap()
