"""The companion: timers, a stopwatch, a to-do list and reminders, beside the notch's panel.

The daemon owns them (the widget is a view); they live in state.json, so a restart keeps them. Times are epoch
seconds: the widget counts the seconds down itself, nothing here changes every second.
Stdlib only: the voice phrases are parsed here too ("timer 10 minutes", "add milk to my list").
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable

from agent_voice import schedule

MAX_TIMERS, MAX_TODOS, MAX_REMINDERS, MAX_NOTES = 5, 60, 30, 200
# The two side panels' cards: Utils on the right (the tools), the Companion on the left (the friendly part).
WIDGETS = ("pomodoro", "timer", "stopwatch", "todo", "reminders", "notes", "clipboard")
COMPANION_WIDGETS = ("media", "calendar", "usage")
# "To-do for you: run the migration on staging": a line an agent ends its reply with (Claude Code is asked to).
TODO_LINE = re.compile(r"^[\s>*_`#-]*to-?do for you\s*:[\s*_`]*(.+?)[\s*_`]*$", re.I | re.M)
POMODORO = {"focus": 25, "short": 5, "long": 15, "every": 4}  # minutes; a long break after every 4th focus
PHASES = {"focus": "Focus", "short": "Short break", "long": "Long break"}

_UNITS = {
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "heure": 3600, "heures": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "mn": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "seconde": 1, "secondes": 1,
}
_WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "un": 1, "une": 1, "two": 2, "deux": 2, "three": 3, "trois": 3,
                 "four": 4, "quatre": 4, "five": 5, "cinq": 5, "ten": 10, "dix": 10, "fifteen": 15, "quinze": 15,
                 "twenty": 20, "vingt": 20, "thirty": 30, "trente": 30, "half an": 0.5, "a half": 0.5}
_AMOUNT = r"(\d+(?:[.,]\d+)?|half an|a half|an?|one|une?|two|deux|three|trois|four|quatre|five|cinq|ten|dix|" \
          r"fifteen|quinze|twenty|vingt|thirty|trente)"
_PART = re.compile(_AMOUNT + r"\s*-?\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b")
_HM = re.compile(r"^(\d{1,2})\s*h\s*(\d{1,2})$")  # "1h30"


def _amount(word: str) -> float:
    word = word.replace(",", ".")
    return float(word) if word[0].isdigit() else float(_WORD_NUMBERS[word])


def parse_duration(text: str) -> int | None:
    """'10 minutes', '1 hour 30', '1h30', '90 sec', 'half an hour', 'une demi-heure' -> seconds; None otherwise."""
    said = " ".join(text.lower().replace("-", " ").split()).strip(" .,!?")
    said = re.sub(r"\b(?:une\s+)?demi\s+heure\b", "30 minutes", said)
    half = bool(re.search(r"\band a half\b|\bet demie?\b", said))  # "2 minutes and a half", "1 heure et demie"
    said = re.sub(r"\s*\b(?:and a half|et demie?)\b", "", said).strip()
    if not said:
        return None
    m = _HM.match(said)
    if m:
        return int(m[1]) * 3600 + int(m[2]) * 60
    total, rest, unit = 0.0, said, 0
    while rest:
        m = _PART.match(rest)
        if not m:
            # "1 hour 30": a bare number after hours means minutes
            if unit == 3600 and re.fullmatch(r"\d{1,2}", rest.strip()):
                total += int(rest) * 60
                break
            return None
        unit = _UNITS[m[2]]
        total += _amount(m[1]) * unit
        rest = re.sub(r"^\s*(?:and|et)?\s*", "", rest[m.end():])
    if half:
        total += unit / 2
    return int(total) if 0 < total <= 24 * 3600 else None


def clock(seconds: float) -> str:
    """'4:05', '1:02:09': a timer's or the stopwatch's time."""
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def duration_words(seconds: int) -> str:
    """'25 min', '1 h 30', '45 s': a timer's length, as a label."""
    if seconds < 60:
        return f"{seconds} s"
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m:02d}" if h and m else f"{h} h" if h else f"{m} min"


# -- what you say -------------------------------------------------------------------------
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)  # phrases match however they were written


_TIMER_LEAD = _rx(
    r"^(?:(?:set|start|put)\s+(?:a\s+|the\s+)?|(?:mets|lance|démarre|demarre|programme)\s+(?:un\s+|le\s+)?)?"
    r"(?:timer|minuteur|compte à rebours)\s+(?:for\s+|of\s+|de\s+|d')?(?P<rest>.+)$")
_TIMER_TAIL = _rx(r"^(?:(?:set|start)\s+)?(?:a\s+|an\s+)?(?P<dur>.+?)\s+timer(?:\s+(?P<label>.+))?$")
_TIMER_CANCEL = _rx(r"^(?:cancel|stop|delete|clear)\s+(?:the\s+|my\s+|all\s+)?timers?$|"
                    r"^(?:annule|arrête|arrete|supprime|stoppe)\s+(?:le\s+|les\s+)?minuteurs?$")
_WATCH = r"(?:stopwatch|stop watch|chrono|chronomètre|chronometre)"
_WATCH_START = _rx(r"^(?:start|begin|launch|run)\s+(?:the\s+|a\s+)?" + _WATCH + r"$|"
                   r"^(?:lance|démarre|demarre)\s+(?:le\s+|un\s+)?" + _WATCH + r"$")
_WATCH_STOP = _rx(r"^(?:stop|pause)\s+(?:the\s+)?" + _WATCH + r"$|^(?:arrête|arrete|stoppe)\s+(?:le\s+)?"
                  + _WATCH + r"$")
_WATCH_RESET = _rx(r"^(?:reset|clear)\s+(?:the\s+)?" + _WATCH + r"$|^(?:remets|remet)\s+(?:le\s+)?" + _WATCH
                   + r"\s+à\s+zéro$")
_POMO = r"(?:pomodoro|pomodori|focus session|session de focus)"
_POMO_START = _rx(r"^(?:start|begin|launch|run)\s+(?:a\s+|the\s+)?" + _POMO + r"$|"
                  r"^(?:lance|démarre|demarre|commence)\s+(?:un\s+|le\s+)?" + _POMO + r"$")
_POMO_PAUSE = _rx(r"^pause\s+(?:the\s+)?" + _POMO + r"$|^(?:mets\s+)?(?:le\s+)?" + _POMO + r"\s+en\s+pause$")
_POMO_STOP = _rx(r"^(?:stop|end|cancel)\s+(?:the\s+)?" + _POMO + r"$|^(?:arrête|arrete|stoppe|annule)\s+(?:le\s+)?"
                 + _POMO + r"$")
_POMO_SKIP = _rx(r"^(?:skip|next)\s+(?:the\s+)?(?:break|pause|focus|" + _POMO[3:-1] + r")$|^(?:passe|saute)\s+(?:la\s+)?pause$")
_LIST = r"(?:to-?do|todo|task|tasks|shopping)?\s*list"
_TODO = (
    _rx(r"^(?:add|put)\s+(?P<item>.+?)\s+(?:to|on|in)\s+(?:my|the)\s+" + _LIST + r"$"),
    _rx(r"^(?:add|put)\s+(?:to|on)\s+(?:my|the)\s+" + _LIST + r"\s*[:,]?\s*(?P<item>.+)$"),
    _rx(r"^(?:ajoute|mets|rajoute)\s+(?P<item>.+?)\s+(?:à|a|dans|sur)\s+(?:ma|la)\s+(?:liste|to-?do)"
        r"(?:\s+de\s+(?:tâches|taches|courses|choses à faire))?$"),
    _rx(r"^(?:ajoute|mets|rajoute)\s+(?:à|a|dans|sur)\s+(?:ma|la)\s+(?:liste|to-?do)"
        r"(?:\s+de\s+(?:tâches|taches|courses|choses à faire))?\s*[:,]?\s*(?P<item>.+)$"),
)
_REMIND = _rx(r"^(?:remind\s+me|rappelle[- ]moi|fais[- ]moi penser)\s*(?P<rest>.*)$")
_NOTE = _rx(r"^(?:(?:take\s+a\s+)?note(?:\s+that)?|prends\s+(?:une\s+)?note|note\s+que)\s*[:,]?\s+(?P<text>.+)$")
_MEDIA = ((_rx(r"^(?:pause|stop)\s+(?:the\s+)?(?:music|song|video)$|^(?:mets|coupe)\s+(?:la\s+)?musique(?:\s+en\s+pause)?$"),
           "toggle"),
          (_rx(r"^(?:play|resume)\s+(?:the\s+)?(?:music|song|video)$|^(?:remets|relance)\s+(?:la\s+)?musique$"),
           "toggle"),
          (_rx(r"^(?:next|skip)\s+(?:the\s+)?(?:song|track|video)$|^(?:chanson|morceau)\s+suivante?$"), "next"),
          (_rx(r"^previous\s+(?:song|track|video)$|^(?:chanson|morceau)\s+précédente?$"), "previous"))
_REMIND_WHAT = _rx(r"^(?:(?:to|that|about|of|de|que)\b|d'|qu')\s*")


def _plain(text: str) -> str:
    return " ".join(text.replace("’", "'").split()).strip(" .!?")  # as written: a to-do keeps its capitals


def _label(text: str) -> str:
    text = re.sub(r"^(?:for|called|named|pour|le|la|les)\s+", "", (text or "").strip(" .,:"))
    return text[:1].upper() + text[1:60]


def _timer(rest: str) -> dict | None:
    """'10 minutes for the tea' -> 10 minutes, 'The tea': the longest start that is a duration, the rest a label."""
    words = rest.split()
    for n in range(len(words), 0, -1):
        seconds = parse_duration(" ".join(words[:n]))
        if seconds:
            return {"kind": "timer", "seconds": seconds, "label": _label(" ".join(words[n:]))}
    return None


def parse_reminder(text: str, now: float) -> tuple[float, str] | None:
    """'at 4 pm to call Paul', 'to call Paul in 20 minutes', 'dans 10 minutes de sortir' -> (when, 'Call Paul')."""
    words = text.strip(" .,").split()
    # The time first ("16:00 call Paul", "in 20 minutes stretch"; the longest phrase that is a time), else last.
    found = next(((at, words[n:]) for n in range(min(6, len(words) - 1), 0, -1)
                  if (at := schedule.parse_when(" ".join(words[:n]), now))), None)
    found = found or next(((at, words[:n]) for n in range(max(1, len(words) - 6), len(words))
                           if (at := schedule.parse_when(" ".join(words[n:]), now))), None)
    if not found:
        return None
    at, rest = found
    what = _REMIND_WHAT.sub("", " ".join(rest).strip(" ,:")).strip(" .")
    return (at, what[:1].upper() + what[1:200]) if what else None


def parse_command(text: str, now: float) -> dict | None:
    """What you said, as a companion action: a timer, the stopwatch, a to-do, a reminder; None if it's none of them."""
    said = _plain(text)
    if not said:
        return None
    m = _TIMER_LEAD.match(said)
    if m:
        return _timer(m["rest"])
    m = _TIMER_TAIL.match(said)
    if m and parse_duration(m["dur"]):
        return {"kind": "timer", "seconds": parse_duration(m["dur"]), "label": _label(m["label"] or "")}
    if _TIMER_CANCEL.match(said):
        return {"kind": "timer_cancel"}
    m = _NOTE.match(said)
    if m:
        return {"kind": "note", "text": m["text"][:1].upper() + m["text"][1:500]}
    for rx, action in _MEDIA:
        if rx.match(said):
            return {"kind": "media", "action": action}
    for rx, action in ((_POMO_START, "start"), (_POMO_PAUSE, "pause"), (_POMO_STOP, "stop"), (_POMO_SKIP, "skip")):
        if rx.match(said):
            return {"kind": "pomodoro", "action": action}
    for rx, action in ((_WATCH_START, "start"), (_WATCH_STOP, "pause"), (_WATCH_RESET, "reset")):
        if rx.match(said):
            return {"kind": "stopwatch", "action": action}
    for rx in _TODO:
        m = rx.match(said)
        if m:
            item = m["item"]
            return {"kind": "todo", "text": item[:1].upper() + item[1:300]}
    m = _REMIND.match(said)
    if m:
        found = parse_reminder(m["rest"], now)
        return {"kind": "reminder", "at": found[0], "text": found[1]} if found else None
    lead = schedule.split_when(said, now)  # "in 20 minutes remind me to stretch"
    if lead and _REMIND.match(lead[1]):
        what = _REMIND_WHAT.sub("", _REMIND.match(lead[1])["rest"]).strip()
        return {"kind": "reminder", "at": lead[0], "text": what[:1].upper() + what[1:200]} if what else None
    return None


# -- the state ----------------------------------------------------------------------------
def empty() -> dict:
    return {"timers": [], "stopwatch": {"start": 0.0, "elapsed": 0.0}, "todos": [], "reminders": [], "next_id": 1,
            "pomodoro": idle_pomodoro(), "notes": []}


def agent_todos(text: str) -> list[str]:
    """The "To-do for you: ..." lines of a reply: what only you can do (3 at most)."""
    return [" ".join(t.split())[:300] for t in TODO_LINE.findall(text or "")][:3]


def idle_pomodoro() -> dict:
    """phase: '' (not started), focus, short or long; `end` while it runs, `left` while paused; `round`: focus
    sessions done in this cycle; `today`: done today (`day`)."""
    return {"phase": "", "end": 0.0, "left": 0.0, "length": 0, "round": 0, "today": 0, "day": ""}


class Companion:
    """Timers, the stopwatch, the to-do list and reminders. `save` keeps them (state.json); `wake` is set on every
    change, so the daemon's loop re-reads when the next one is due."""

    def __init__(self, data: dict | None, save: Callable[[dict], None], pomodoro: dict | None = None):
        self.data = {**empty(), **(data if isinstance(data, dict) else {})}
        self.pomodoro_cfg = {**POMODORO, **(pomodoro or {})}
        self._save = save
        self.lock = threading.RLock()
        self.wake = threading.Event()

    def _id(self) -> int:
        self.data["next_id"] = int(self.data.get("next_id") or 1) + 1
        return self.data["next_id"] - 1

    def _changed(self) -> None:
        self._save(self.data)
        self.wake.set()

    def _find(self, kind: str, item_id: int) -> dict | None:
        return next((x for x in self.data[kind] if x["id"] == item_id), None)

    # timers: running ones have an `end`; paused ones keep what was `left`
    def timer(self, action: str, item_id: int = 0, seconds: float = 0, label: str = "", now: float | None = None):
        now = time.time() if now is None else now
        with self.lock:
            timers = self.data["timers"]
            if action == "start":
                seconds = int(seconds)
                if not 0 < seconds <= 24 * 3600:
                    raise ValueError("a length between 1 second and 24 hours")
                if len(timers) >= MAX_TIMERS:
                    raise ValueError(f"{MAX_TIMERS} timers at most")
                item = {"id": self._id(), "label": _label(label)[:60], "total": seconds, "end": now + seconds,
                        "left": 0.0}
                timers.append(item)
                self._changed()
                return item
            if action == "cancel" and not item_id:
                timers.clear()
                self._changed()
                return True
            item = self._find("timers", item_id) if item_id else (timers[-1] if timers else None)
            if item is None:
                return None
            if action == "pause" and item["end"]:
                item["left"], item["end"] = max(0.0, item["end"] - now), 0.0
            elif action == "resume" and not item["end"]:
                item["end"], item["left"] = now + item["left"], 0.0
            elif action == "add":
                if item["end"]:
                    item["end"] += seconds
                else:
                    item["left"] += seconds
                item["total"] += int(seconds)
            elif action == "cancel":
                timers.remove(item)
            else:
                return None
            self._changed()
            return item

    def stopwatch(self, action: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            sw = self.data["stopwatch"]
            if action == "start" and not sw["start"]:
                sw["start"] = now
            elif action == "pause" and sw["start"]:
                sw["elapsed"], sw["start"] = sw["elapsed"] + now - sw["start"], 0.0
            elif action == "toggle":
                return self.stopwatch("pause" if sw["start"] else "start", now)
            elif action == "reset":
                sw["start"], sw["elapsed"] = 0.0, 0.0
            else:
                return sw
            self._changed()
            return sw

    def todo(self, action: str, item_id: int = 0, text: str = "", done: bool | None = None, source: str = "you",
             session: str = ""):
        with self.lock:
            todos = self.data["todos"]
            if action == "add":
                text = " ".join(str(text).split())[:300]
                if not text:
                    raise ValueError("what to add")
                same = next((t for t in todos if not t["done"] and t["text"].lower() == text.lower()), None)
                if same is not None:  # said twice (an agent repeating its follow-up): once on the list
                    return same
                if len(todos) >= MAX_TODOS:
                    raise ValueError(f"{MAX_TODOS} items at most: clear the done ones")
                item = {"id": self._id(), "text": text, "done": False, "source": source[:30], "at": int(time.time()),
                        "session": session[:200]}
                todos.append(item)
                self._changed()
                return item
            if action == "clear_done":
                before = len(todos)
                todos[:] = [t for t in todos if not t["done"]]
                self._changed()
                return before - len(todos)
            item = self._find("todos", item_id)
            if item is None:
                return None
            if action == "done":
                item["done"] = True if done is None else bool(done)
            elif action == "toggle":
                item["done"] = not item["done"]
            elif action == "edit" and " ".join(str(text).split()):
                item["text"] = " ".join(str(text).split())[:300]
            elif action == "remove":
                todos.remove(item)
            else:
                return None
            self._changed()
            return item

    def note(self, action: str, item_id: int = 0, text: str = "") -> dict | None:
        """Quick notes: one line, kept (and searchable from History)."""
        with self.lock:
            notes = self.data.setdefault("notes", [])
            if action == "add":
                text = " ".join(str(text).split())[:500]
                if not text:
                    raise ValueError("what to note")
                item = {"id": self._id(), "text": text, "at": int(time.time())}
                notes.insert(0, item)
                del notes[MAX_NOTES:]
                self._changed()
                return item
            item = self._find("notes", item_id)
            if action != "remove" or item is None:
                return None
            notes.remove(item)
            self._changed()
            return item

    def reminder(self, action: str, item_id: int = 0, text: str = "", at: float = 0):
        with self.lock:
            reminders = self.data["reminders"]
            if action == "add":
                text = " ".join(str(text).split())[:200]
                if not text or not at:
                    raise ValueError("what to remind you of, and when")
                if len(reminders) >= MAX_REMINDERS:
                    raise ValueError(f"{MAX_REMINDERS} reminders at most")
                item = {"id": self._id(), "text": text, "at": int(at)}
                reminders.append(item)
                reminders.sort(key=lambda r: r["at"])
                self._changed()
                return item
            item = self._find("reminders", item_id)
            if action != "remove" or item is None:
                return None
            reminders.remove(item)
            self._changed()
            return item

    # the Pomodoro: focus, a break, focus... (a long break after every `every` focus sessions)
    def _phase_seconds(self, phase: str) -> int:
        return int(float(self.pomodoro_cfg[phase]) * 60)

    def _begin(self, phase: str, now: float) -> None:
        pomo = self.data["pomodoro"]
        length = self._phase_seconds(phase)
        pomo.update(phase=phase, end=now + length, left=0.0, length=length)

    def pomodoro(self, action: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            pomo = self.data["pomodoro"]
            if action == "toggle":
                action = "pause" if pomo["end"] else "start"
            if action == "start":
                if not pomo["phase"]:
                    self._begin("focus", now)
                elif not pomo["end"]:  # paused: on from where it was
                    pomo["end"], pomo["left"] = now + pomo["left"], 0.0
            elif action == "pause" and pomo["end"]:
                pomo["left"], pomo["end"] = max(0.0, pomo["end"] - now), 0.0
            elif action == "skip" and pomo["phase"]:
                self._advance(now, finished=False)
            elif action == "stop":
                self.data["pomodoro"] = {**idle_pomodoro(), "today": pomo["today"], "day": pomo["day"]}
            else:
                return pomo
            self._changed()
            return self.data["pomodoro"]

    def _advance(self, now: float, finished: bool = True) -> dict:
        """The phase is over (or skipped): what comes next starts now. Returns the phase that ended."""
        pomo = self.data["pomodoro"]
        ended = dict(pomo)
        if pomo["phase"] == "focus":
            pomo["round"] += 1
            if finished:
                today = time.strftime("%Y-%m-%d", time.localtime(now))
                pomo["today"] = (pomo["today"] if pomo["day"] == today else 0) + 1
                pomo["day"] = today
            every = max(1, int(self.pomodoro_cfg["every"]))
            self._begin("long" if pomo["round"] % every == 0 else "short", now)
        else:
            if pomo["phase"] == "long":
                pomo["round"] = 0
            self._begin("focus", now)
        return ended

    def next_due(self) -> float | None:
        with self.lock:
            times = [t["end"] for t in self.data["timers"] if t["end"]] + [r["at"] for r in self.data["reminders"]]
            if self.data["pomodoro"]["end"]:
                times.append(self.data["pomodoro"]["end"])
        return min(times) if times else None

    def due(self, now: float | None = None) -> list[tuple[str, dict]]:
        """Timers that ended and reminders whose time came: taken off the lists, oldest first."""
        now = time.time() if now is None else now
        with self.lock:
            ended = [("timer", t) for t in self.data["timers"] if t["end"] and t["end"] <= now]
            ended += [("reminder", r) for r in self.data["reminders"] if r["at"] <= now]
            pomo = self.data["pomodoro"]
            if pomo["end"] and pomo["end"] <= now:
                ended.append(("pomodoro", {**self._advance(now), "next": dict(pomo)}))
            if ended:
                self.data["timers"] = [t for t in self.data["timers"] if not (t["end"] and t["end"] <= now)]
                self.data["reminders"] = [r for r in self.data["reminders"] if r["at"] > now]
                self._changed()
        return sorted(ended, key=lambda e: e[1].get("end") or e[1].get("at") or 0)

    def status(self) -> dict:
        """For /status: only values that change when something is done (no seconds ticking)."""
        with self.lock:
            sw = self.data["stopwatch"]
            return {"timers": [dict(t) for t in self.data["timers"]],
                    "stopwatch": {"start": sw["start"], "elapsed": round(sw["elapsed"], 1)},
                    "todos": [dict(t) for t in self.data["todos"]],
                    "reminders": [dict(r) for r in self.data["reminders"]][:10],
                    "notes": [dict(n) for n in self.data.get("notes") or []][:20],
                    "pomodoro": {**self.data["pomodoro"], "every": int(self.pomodoro_cfg["every"]),
                                 "focus_length": self._phase_seconds("focus")}}
