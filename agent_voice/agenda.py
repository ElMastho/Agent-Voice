"""Today's meetings, for the companion: from calendar links you add (iCal: Outlook's or Google's "secret address",
or a .ics file) and, on Windows, Outlook (classic) while it's open.

A link is fetched only once you add it, then every 15 minutes (`Daemon._agenda_loop`); nothing else is sent.
Stdlib only: a small iCal reader with the repeats meetings use (daily, weekly on given days, monthly, yearly).
"""

from __future__ import annotations

import logging
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("agent_voice.agenda")

DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_BYTES = 5_000_000


def fetch(source: str, timeout: float = 15.0) -> str:
    """A calendar's text: an https (or webcal) link, or a local .ics file."""
    if re.match(r"^(?:https?|webcal)://", source, re.I):
        url = re.sub(r"^webcal://", "https://", source, flags=re.I)
        req = urllib.request.Request(url, headers={"User-Agent": "agent-voice"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (a link the user added)
            return resp.read(MAX_BYTES).decode("utf-8", "replace")
    return Path(source).expanduser().read_text(encoding="utf-8", errors="replace")[:MAX_BYTES]


def _lines(text: str) -> list[str]:
    """Unfolded content lines (a line starting with a space or tab continues the one before)."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif raw:
            out.append(raw)
    return out


def _unescape(value: str) -> str:
    return value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _when(value: str, params: str) -> tuple[datetime, bool]:
    """(local naive datetime, all day). UTC times ('Z') are moved to local time; a TZID's are taken as local
    (the calendar is in your time zone, or close enough for a day's list)."""
    value = value.strip()
    if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d"), True
    when = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        when = when.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return when, False


def _duration(value: str) -> timedelta:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return timedelta(hours=1)
    sign = -1 if m[1] == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse(text: str) -> list[dict]:
    """The calendar's events: {uid, title, location, start, end, all_day, rrule, exdates, recurrence_id}."""
    events, current = [], None
    for line in _lines(text):
        name, _, value = line.partition(":")
        key, _, params = name.partition(";")
        key = key.upper()
        if key == "BEGIN" and value.upper() == "VEVENT":
            current = {"uid": "", "title": "", "location": "", "start": None, "end": None, "all_day": False,
                       "rrule": {}, "exdates": set(), "recurrence_id": None, "cancelled": False, "duration": None}
        elif key == "END" and value.upper() == "VEVENT" and current is not None:
            if current["start"] is not None and not current["cancelled"]:
                if current["end"] is None:
                    current["end"] = current["start"] + (current["duration"] or (
                        timedelta(days=1) if current["all_day"] else timedelta(hours=1)))
                events.append(current)
            current = None
        elif current is None:
            continue
        elif key == "UID":
            current["uid"] = value
        elif key == "SUMMARY":
            current["title"] = _unescape(value)
        elif key == "LOCATION":
            current["location"] = _unescape(value)
        elif key == "DTSTART":
            current["start"], current["all_day"] = _when(value, params)
        elif key == "DTEND":
            current["end"] = _when(value, params)[0]
        elif key == "DURATION":
            current["duration"] = _duration(value)
        elif key == "RRULE":
            current["rrule"] = dict(part.split("=", 1) for part in value.split(";") if "=" in part)
        elif key == "EXDATE":
            current["exdates"] |= {_when(v, params)[0] for v in value.split(",") if v.strip()}
        elif key == "RECURRENCE-ID":
            current["recurrence_id"] = _when(value, params)[0]
        elif key == "STATUS" and value.upper() == "CANCELLED":
            current["cancelled"] = True
    return events


def _occurs(event: dict, day: date) -> datetime | None:
    """When a repeating event starts on `day`, if it does (daily, weekly by day, monthly, yearly)."""
    start: datetime = event["start"]
    rule = event["rrule"]
    if day < start.date():
        return None
    freq = rule.get("FREQ", "").upper()
    interval = max(1, int(rule.get("INTERVAL", 1) or 1))
    until = rule.get("UNTIL")
    if until:
        try:
            if day > _when(until, "")[0].date():
                return None
        except ValueError:
            pass
    by_day = [DAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in DAYS]
    delta = (day - start.date()).days
    if freq == "DAILY":
        ok = delta % interval == 0 and (not by_day or day.weekday() in by_day)
    elif freq == "WEEKLY":
        weeks = (day - (start.date() - timedelta(days=start.weekday()))).days // 7
        ok = weeks % interval == 0 and day.weekday() in (by_day or [start.weekday()])
    elif freq == "MONTHLY":
        months = (day.year - start.year) * 12 + day.month - start.month
        if rule.get("BYDAY") and not rule.get("BYMONTHDAY"):  # "2TU": the second Tuesday
            m = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", rule["BYDAY"].split(",")[0])
            nth = int(m[1] or 1) if m else 1
            weekday = DAYS[m[2]] if m else start.weekday()
            if nth > 0:
                which = (day.day - 1) // 7 + 1
            else:
                which = -(((date(day.year + day.month // 12, day.month % 12 + 1, 1) - timedelta(days=1)).day
                           - day.day) // 7 + 1)
            ok = months % interval == 0 and day.weekday() == weekday and which == nth
        else:
            ok = months % interval == 0 and day.day == int(rule.get("BYMONTHDAY", start.day) or start.day)
    elif freq == "YEARLY":
        ok = (day.year - start.year) % interval == 0 and (day.month, day.day) == (start.month, start.day)
    else:
        return None
    if not ok:
        return None
    when = datetime.combine(day, start.time())
    if "COUNT" in rule:  # the nth occurrence: count them from the start (a rule with COUNT is usually short)
        count, probe = 0, start.date()
        while probe <= day and count <= int(rule["COUNT"]):
            if probe == day or _occurs({**event, "rrule": {k: v for k, v in rule.items() if k != "COUNT"}}, probe):
                count += 1
            probe += timedelta(days=1)
        if count > int(rule["COUNT"]):
            return None
    return None if when in event["exdates"] else when


def on_day(events: list[dict], day: date) -> list[dict]:
    """The events of `day`, in order: {title, location, start, end (epoch seconds), all_day}."""
    moved = {(e["uid"], e["recurrence_id"]) for e in events if e["recurrence_id"] is not None}
    out = []
    for e in events:
        length = e["end"] - e["start"]
        starts = []
        if e["rrule"] and e["recurrence_id"] is None:
            when = _occurs(e, day)
            if when and (e["uid"], when) not in moved:
                starts.append(when)
        elif e["start"].date() <= day < (e["end"] if e["all_day"] else e["end"] + timedelta(seconds=1)).date() \
                or e["start"].date() == day:
            starts.append(e["start"])
        for start in starts:
            out.append({"title": e["title"] or "(no title)", "location": e["location"],
                        "start": start.timestamp(), "end": (start + length).timestamp(), "all_day": e["all_day"]})
    return sorted(out, key=lambda x: (not x["all_day"], x["start"]))


def _ole_time(value) -> datetime:
    """An Outlook date as a local naive datetime: a datetime, or (late-bound comtypes) an OLE date's day count."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, second=0, microsecond=0)
    return (datetime(1899, 12, 30) + timedelta(days=float(value))).replace(second=0, microsecond=0)


def _outlook_filter(day: date) -> str:
    """Restrict's query for the items overlapping `day`. DASL with UTC times, because Jet's '[Start] < ...' reads its
    dates in the system locale: '10/05' was the 10th of May on a French Windows."""
    start, end = (datetime.combine(d, datetime.min.time()).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
                  for d in (day, day + timedelta(days=1)))  # each midnight on its own: a DST day isn't 24 h
    return f"@SQL=\"urn:schemas:calendar:dtstart\" < '{end}' AND \"urn:schemas:calendar:dtend\" > '{start}'"


def _outlook_items(found, limit: int = 30) -> list[dict]:
    """GetFirst/GetNext, never indexing: Outlook counts from 1 (comtypes' iteration asks for item 0, 'Array index
    out of bounds'), and with IncludeRecurrences the collection's Count is meaningless."""
    out = []
    item = found.GetFirst()
    while item is not None and len(out) < limit:
        out.append({"title": str(item.Subject or "(no title)"), "location": str(item.Location or ""),
                    "start": _ole_time(item.Start).timestamp(), "end": _ole_time(item.End).timestamp(),
                    "all_day": bool(item.AllDayEvent)})
        item = found.GetNext()
    return out


def outlook_day(day: date) -> list[dict]:
    """Outlook (classic)'s calendar for `day`, only while Outlook is open (it's never started for this)."""
    if sys.platform != "win32":
        return []
    try:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        app = comtypes.client.GetActiveObject("Outlook.Application")
    except Exception:
        return []
    try:
        items = app.GetNamespace("MAPI").GetDefaultFolder(9).Items  # olFolderCalendar
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        return _outlook_items(items.Restrict(_outlook_filter(day)))
    except Exception:
        log.exception("Outlook's calendar unavailable")
        return []
