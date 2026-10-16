"""Scheduled prompts: when to send them ("14:00", "in 20 minutes", "tomorrow at 9", "demain à 9h") and repeats."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

REPEATS = ("", "daily", "weekdays")

_CLOCK = r"(?P<h>\d{1,2})(?:(?:[:h.](?P<m>\d{2}))|h)?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?"
_AT = re.compile(r"^(?:(?P<day>tomorrow|demain|today|aujourd'hui)\s+)?(?:at|à|a|vers|for)?\s*" + _CLOCK + r"$", re.I)
_DAY_FIRST = re.compile(r"^(?:at|à|a)?\s*" + _CLOCK + r"\s+(?P<day>tomorrow|demain|today|aujourd'hui)$", re.I)
_IN = re.compile(r"^(?:in|dans)\s+(?P<n>\d+|an?|une?|half an)\s*(?P<unit>h|hours?|heures?|min|mins|minutes?)$", re.I)
_NOON = {"noon": 12, "midi": 12, "midnight": 0, "minuit": 0}
# The time phrase at the start of something said: "at 2 pm tell Codex to ...", "dans 20 minutes, ..."
_LEAD = re.compile(
    r"^(?P<when>(?:(?:tomorrow|demain)\s+)?(?:at|à|vers)\s+(?:\d{1,2}(?:[:h.]\d{2}|h)?\s*(?:am|pm|a\.m\.|p\.m\.)?|noon|midi|"
    r"midnight|minuit)(?:\s+(?:tomorrow|demain))?|(?:in|dans)\s+(?:\d+|an?|une?|half an)\s*(?:h|hours?|heures?|min|mins|"
    r"minutes?))[\s,:]+(?P<rest>.+)$", re.I | re.S)


def parse_when(text: str, now: float) -> float | None:
    """The moment `text` names, as epoch seconds (always in the future); None when it isn't a time."""
    said = " ".join(text.lower().replace("’", "'").split()).strip(" .,")
    base = datetime.fromtimestamp(now)
    m = _IN.match(said)
    if m:
        n = m["n"]
        amount = 0.5 if n == "half an" else 1 if not n.isdigit() else int(n)
        return now + amount * (3600 if m["unit"].startswith("h") else 60)
    word = re.sub(r"^(?:at|à|vers)\s+", "", said)
    day = ""
    for d in ("tomorrow", "demain"):
        if d in word.split():
            day, word = "tomorrow", " ".join(w for w in word.split() if w != d)
    if word in _NOON:
        hour, minute = _NOON[word], 0
    else:
        m = _AT.match(word) or _DAY_FIRST.match(word)
        if not m:
            return None
        day = day or ("tomorrow" if (m["day"] or "").lower() in ("tomorrow", "demain") else "")
        hour, minute = int(m["h"]), int(m["m"] or 0)
        ap = (m["ap"] or "").lower().replace(".", "")
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
    if hour > 23 or minute > 59:
        return None
    when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day == "tomorrow":
        when += timedelta(days=1)
    elif when.timestamp() <= now:
        when += timedelta(days=1)  # "at 9" said at 10: tomorrow
    return when.timestamp()


def split_when(text: str, now: float) -> tuple[float, str] | None:
    """'At 2 pm tell Codex to run the tests' -> (2 pm, 'tell Codex to run the tests'); None without a time first."""
    m = _LEAD.match(text.strip())
    if not m:
        return None
    at = parse_when(m["when"], now)
    rest = m["rest"].strip()
    return (at, rest[:1].upper() + rest[1:]) if at and len(rest.split()) >= 2 else None


def next_run(at: float, repeat: str, now: float) -> float | None:
    """When a repeating prompt runs next (the same time of day); None for a one-off."""
    if repeat not in ("daily", "weekdays"):
        return None
    when = datetime.fromtimestamp(at)
    while when.timestamp() <= now or (repeat == "weekdays" and when.weekday() >= 5):
        when += timedelta(days=1)
    return when.timestamp()
