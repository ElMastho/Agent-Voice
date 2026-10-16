"""The background daemon: a tiny local HTTP API + global hotkeys.

Keeping models loaded here is what makes speech start instantly; agent hooks only POST text to it.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import zlib
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from agent_voice import (__version__, calls, claudeapp, install, keys, ntfy, quota, schedule, textclean, updater,
                         usage, winfocus)
from agent_voice.config import HOME, LOG_DIR, load_config, load_state, save_state
from agent_voice.hooks import strip_summary
from agent_voice.textclean import result_label
from agent_voice.tts import Speaker

log = logging.getLogger("agent_voice.server")

LOCK_PATH = HOME / "daemon.lock"


class _Server(ThreadingHTTPServer):
    # HTTPServer sets SO_REUSEADDR, which on Windows lets a second daemon bind the same port and stay resident.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class InstanceLock:
    """Exclusive lock on a file, released by the OS when the process dies, so a crash never leaves it stale."""

    def __init__(self, path=LOCK_PATH):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None


def _acquire_instance(lock: InstanceLock, wait: float = 10.0) -> bool:
    """Wait briefly for a daemon that is shutting down; give up at once if one is already serving."""
    from agent_voice.client import is_running

    end = time.time() + wait
    while not lock.acquire():
        if time.time() > end or is_running():
            return False
        time.sleep(0.25)
    return True


def _beep(freq: int, ms: int = 90) -> None:
    if sys.platform == "win32":
        import winsound

        threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()


SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0)
RELEASES_API = "https://api.github.com/repos/ElMastho/agent-voice/releases/latest"
RELEASES_PAGE = "https://github.com/ElMastho/agent-voice/releases/latest"
UPDATE_EVERY = 24 * 3600  # re-check for a release once a day (daemons stay up for weeks)

# Chime mode: (frequency Hz, start s) notes. "done" rises, "attention" asks twice.
CHIMES = {
    "done": ((659.25, 0.0), (987.77, 0.13)),
    "attention": ((880.0, 0.0), (1174.66, 0.16), (880.0, 0.42), (1174.66, 0.58)),
}


def chime_audio(kind: str, volume: float = 0.35, rate: int = 44100):
    """A soft bell-like jingle: sine + a quiet octave, fast attack, exponential decay."""
    import numpy as np

    notes = CHIMES.get(kind, CHIMES["done"])
    length = notes[-1][1] + 0.9
    t = np.arange(int(rate * length)) / rate
    out = np.zeros_like(t, dtype=np.float32)
    for freq, start in notes:
        u = t - start
        on = u >= 0
        env = np.where(on, np.minimum(u / 0.008, 1.0) * np.exp(-np.clip(u, 0, None) * 5.5), 0.0)
        out += (env * (np.sin(2 * np.pi * freq * u) + 0.25 * np.sin(4 * np.pi * freq * u))).astype(np.float32)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * max(0.0, min(1.0, volume))).astype(np.float32), rate


# Spoken answers to a question: "the second one", "option 2", "la deuxième"...
_ORDINALS = {
    1: ("1", "one", "first", "un", "une", "premier", "première", "premiere"),
    2: ("2", "two", "second", "deux", "deuxième", "deuxieme", "seconde"),
    3: ("3", "three", "third", "trois", "troisième", "troisieme"),
    4: ("4", "four", "fourth", "quatre", "quatrième", "quatrieme"),
    5: ("5", "five", "fifth", "cinq", "cinquième", "cinquieme"),
}


# Where a new thread can start (launch.targets), as said in the notch.
TARGET_NAMES = {"claude-app": "Claude app", "claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI"}
# How each agent is called when a conversation has no title of its own.
AGENT_TITLES = {"claude": "Claude Code", "cline": "Cline", "claude-desktop": "Claude Desktop", "vscode": "VS Code",
                "chatgpt": "ChatGPT", "claude-web": "Claude", "gemini-web": "Gemini", "aistudio": "AI Studio",
                "perplexity": "Perplexity", "copilot": "Copilot", "gemini": "Gemini", "codex": "Codex",
                "cursor": "Cursor", "windsurf": "Windsurf", "antigravity": "Antigravity", "aider": "Aider",
                "opencode": "OpenCode", "goose": "Goose"}

# One colour per conversation, picked from its id so it never changes: you learn which is which.
THREAD_COLORS = ("#F472B6", "#60A5FA", "#34D399", "#FBBF24", "#A78BFA", "#F87171", "#22D3EE", "#FB923C")


def thread_color(key: str) -> str:
    return THREAD_COLORS[zlib.crc32(key.encode("utf-8")) % len(THREAD_COLORS)]


_YES = {"yes", "yeah", "yep", "allow", "approve", "ok", "okay", "sure", "go", "oui", "ouais", "vas", "autorise",
        "autoriser", "accepte", "daccord", "valide"}
_NO = {"no", "nope", "deny", "refuse", "stop", "dont", "non", "refuser", "annule", "pas"}


def match_yes_no(text: str) -> bool | None:
    """A spoken permission answer: True (allow), False (deny), or None when it isn't clear."""
    words = set(_plain(text.replace("'", "")).split())
    yes, no = bool(words & _YES), bool(words & _NO)
    return None if yes == no else yes


# "Claude" as Whisper writes it across accents: Claude, Clod, Klöte, Clot, Cloud, Clawed...
_WAKE_FUZZY = re.compile(
    r"^\W*(?:\w+\W+){0,2}?(?:hey|hé|he|hi|ok|okay|eh|et|ey|hay|ay)\W+[ck]l(?:au|eau|aw|ou|oa|o|ö)"
    r"(?:de|d|te|tte|t|th|ed)?\b\W*",
    re.I,
)


def strip_wake(text: str, phrases: list[str]) -> str | None:
    """The command after a wake phrase near the start ("Hey Claude, run the tests" -> "run the tests").

    Up to two words may come first ("Uh, hey Claude..."), not more: "I told Claude" is no wake-up call.
    """
    for phrase in phrases:
        words = [re.escape(w) for w in phrase.split()]
        m = re.match(r"^\W*(?:\w+\W+){0,2}?" + r"\W+".join(words) + r"\b\W*", text, re.I)
        if m:
            return text[m.end():].strip()
    m = _WAKE_FUZZY.match(text)
    return text[m.end():].strip() if m else None


# Said on their own in the reply window, these drive agent-voice instead of being sent to the agent.
_COMMANDS = {
    "repeat": re.compile(r"^(?:repeat|repeat that|say (?:that|it) again|again|what did you say|"
                         r"r[ée]p[èe]te|r[ée]p[èe]te[- ]?le|tu peux r[ée]p[ée]ter)$"),
    "slower": re.compile(r"^(?:slower|slow down|speak slower|plus lentement|moins vite|ralentis)$"),
    "faster": re.compile(r"^(?:faster|speed up|speak faster|plus vite|acc[ée]l[èe]re)$"),
    "cancel": re.compile(r"^(?:cancel|never ?mind|forget it|nothing|annule|laisse tomber|rien)$"),
}
# A slightly different voice per conversation, so you can tell threads apart by ear. The first one
# keeps the natural voice; playback-rate shifts cost nothing (no second voice to download).
THREAD_PITCHES = (1.0, 0.92, 1.08, 0.86, 1.14)


def match_command(text: str) -> str | None:
    """'Repeat that.' -> 'repeat'; anything longer than a few words is a reply, never a command."""
    plain = re.sub(r"[^\w\s'-]", "", text.lower()).strip()
    plain = re.sub(r"^(?:please|s'il te pla[iî]t|ok|okay)\s+|\s+(?:please|s'il te pla[iî]t)$", "", plain)
    if not plain or len(plain.split()) > 5:
        return None
    return next((name for name, rx in _COMMANDS.items() if rx.match(plain)), None)


# "Tell Codex to run the tests": a message for another conversation, said from anywhere.
_ROUTES = (
    re.compile(r"^(?:please\s+)?(?:tell|ask)\s+(?P<name>.+?)\s+(?:to|that)\s+(?P<msg>.+)$", re.I | re.S),
    re.compile(r"^(?:send|message|write)\s+(?:to\s+)?(?P<name>[^,:]+?)\s*[,:]\s*(?P<msg>.+)$", re.I | re.S),
    re.compile(r"^(?:dis|demande)\s+(?:à|a)\s+(?P<name>.+?)\s+(?:de\s+|d'|d’|que\s+|qu'|qu’)(?P<msg>.+)$", re.I | re.S),
)
# Words that name nothing in particular: "approve the agent" must not pick a conversation.
_FILLER = {"the", "a", "an", "le", "la", "les", "l", "agent", "thread", "conversation", "one", "code", "please"}


# "New thread: fix the login bug", "start a new conversation with Codex to ...", "nouvelle session : ..."
_NEW_THREAD = re.compile(
    r"^(?:please\s+)?(?:start\s+)?(?:a\s+)?(?:new|nouvelle|nouveau)\s+(?:thread|conversation|session|chat|discussion|fil)"
    r"(?:\s+(?:with|in|avec|dans)\s+(?P<agent>claude(?:\s+code)?|codex|gemini))?\s*[:,.]?\s*(?:to\s+|pour\s+)?"
    r"(?P<prompt>.+)$", re.I | re.S)
NEW_THREAD = "__new__"  # the "session" a dictated new-thread prompt goes to


def parse_new_thread(text: str) -> tuple[str, str] | None:
    """'New thread with Codex: add a health check' -> ('codex', 'Add a health check'); None otherwise."""
    m = _NEW_THREAD.match(text.strip())
    if not m or len(m["prompt"].split()) < 2:
        return None
    agent = (m["agent"] or "").lower().split()[0] if m["agent"] else ""
    prompt = m["prompt"].strip()
    return agent, prompt[:1].upper() + prompt[1:]


def parse_route(text: str) -> tuple[str, str] | None:
    """'Tell Codex to run the tests.' -> ('Codex', 'run the tests.'); None for anything else."""
    text = text.strip()
    for rx in _ROUTES:
        m = rx.match(text)
        if m and 0 < len(m["name"].split()) <= 4 and m["msg"].strip(" .!?,"):
            msg = m["msg"].strip()
            return m["name"].strip(" ,"), msg[:1].upper() + msg[1:]
    return None


# "Send this to Codex", "give the screenshot to the notch one and ask it to fix it", "envoie ça à Codex : ..."
_HANDOFF = re.compile(
    r"^(?:please\s+)?(?:send|give|pass|share|hand|envoie|donne|passe|transmets)\s+"
    r"(?:this|that|it|the selection|my selection|the screenshot|this screenshot|my screenshot|ça|ca|ceci|cela|"
    r"la sélection|la selection|la capture)\s+(?:over\s+)?(?:to|à|a)\s+(?P<rest>.+)$", re.I | re.S)
_HANDOFF_ASK = re.compile(r"^(?:ask|tell)\s+(?:it|him|her|them)\s+to\s+|^(?:demande-lui|dis-lui)\s+(?:de\s+|d')?",
                          re.I)


def parse_handoff(text: str) -> tuple[str, str] | None:
    """'Send this to Codex and ask it to fix it' -> ('Codex', 'Fix it'); 'Send this to Codex' -> ('Codex', '')."""
    m = _HANDOFF.match(text.strip())
    if not m:
        return None
    parts = re.split(r"\s*[,:]\s*|\s+(?:and|et)\s+", m["rest"].strip(), maxsplit=1)
    name = parts[0].strip(" .!?")
    msg = _HANDOFF_ASK.sub("", parts[1].strip()) if len(parts) > 1 else ""
    if not name or len(name.split()) > 4:
        return None
    return name, msg[:1].upper() + msg[1:]


# "Resume when it resets", "continue Codex after the limit", "reprends quand la limite est levée"
_RESUME = re.compile(r"\b(?:resume|continue|carry on|keep going|reprends|reprendre|continuer?)\b.*"
                     r"\b(?:resets?|limit|limits|quota|limite|réinitialis\w*|reinitialis\w*)\b", re.I | re.S)
_RECAP = re.compile(r"^(?:give me\s+)?(?:the\s+|my\s+|today'?s\s+)?(?:daily\s+recap|recap(?: of (?:the|my) day)?|"
                    r"what did i do today|what happened today|day summary|summary of (?:the|my) day|"
                    r"résumé de (?:la|ma) journée|resume de (?:la|ma) journee|bilan de (?:la|ma) journée)\W*$", re.I)


def match_resume(text: str) -> bool:
    return bool(_RESUME.search(text)) and len(text.split()) <= 12


def match_recap(text: str) -> bool:
    return bool(_RECAP.match(_plain(text).replace(" s ", "'s ")) or _RECAP.match(text.strip().lower()))


def spoken_number(words: set[str], most: int) -> int | None:
    """'two', '2', 'the second one' -> 2 (up to `most`); 'one' alone only when nothing else is said."""
    found = [n for n, keys in _ORDINALS.items() if n <= most and any(k in words for k in keys)]
    if len(found) > 1:
        found = [n for n in found if n != 1 or not words & {"one", "un", "une"}]
    return found[0] if found else None


_ALL = {"all", "everything", "everyone", "both", "tout", "tous", "toutes", "les deux"}


def _tokens_label(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{round(n / 1e3)}k" if n >= 1e3 else str(n)


def _names(label: str) -> set[str]:
    return {w for w in _plain(label).split() if w not in _FILLER and len(w) > 1}


def names_match(said: str, label: str) -> bool:
    """Does `said` ("codex", "the notch one") name the conversation called `label`?"""
    want, have = _names(said), _names(label)
    return bool(want) and want <= have


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def match_option(text: str, options: list[str]) -> int | None:
    """Which option a spoken answer picks: its label said out loud, or its number. None otherwise."""
    said = _plain(text)
    labels = [_plain(re.sub(r"\(.*?\)", "", o)) for o in options]
    for i in sorted(range(len(labels)), key=lambda i: -len(labels[i])):
        if labels[i] and labels[i] in said:
            return i
    words = said.split()
    if len(words) <= 5:  # a short answer that names a position, not a sentence that happens to contain "one"
        found = [n for n, keys in _ORDINALS.items() if n <= len(options) and any(k in words for k in keys)]
        if len(found) > 1:  # "the second one": "one" is just a filler word there
            found = [n for n in found if not any(w in words for w in ("one", "un", "une")) or n != 1]
        if found:
            return found[0] - 1
    return None


class Daemon:
    def __init__(self, config: dict, start_threads: bool = True):
        self.config = config
        self.state = load_state()
        if self.state.get("speed_x"):
            # The speaker reads config["speed"] (Piper length_scale) for every sentence.
            config["speed"] = 1.0 / float(self.state["speed_x"])
        self.speaker = Speaker(config)
        # HTTP handlers, the job worker, the mic and wake threads all touch the thread queue.
        self._lock = threading.RLock()
        self.transcriber = None
        self._ptt_active = False
        # Requests are processed in order by one worker so replies are never spoken out of order.
        self.jobs: queue.Queue = queue.Queue()
        # Auto-listen: armed by an agent's final answer, starts once the speaker goes quiet.
        self._listen_armed = False
        self._listen_cancel = threading.Event()
        self._listening = False
        # Window the user last sent a prompt from (where replies are typed back).
        # Persisted so a daemon restart doesn't send the next reply to whatever app is in front.
        saved = int(self.state.get("target_hwnd") or 0)
        self._target_hwnd = saved if winfocus.is_window(saved) else 0
        # Agent session whose turn just ended: the conversation a spoken reply belongs to.
        self._reply_session = ""
        # A question the agent is waiting on (options to pick from the widget or by voice).
        self.question: dict | None = None
        # Session and agent behind the current speech, so the widget can say which thread talks.
        self._voice_session, self._voice_source = "", ""
        self._file_titles: dict[str, str] = {}
        # Speech from other threads that arrived while one was talking (or waiting for your reply).
        self._queue: list[tuple] = []
        # Recent conversations, for the widget's thread list: session -> {source, last}.
        self.threads: dict[str, dict] = {}
        # A tool call waiting for your permission (Claude Code's PermissionRequest hook polls it).
        self.permission: dict | None = None
        self._perm_seq = 0
        self._perm_decisions: dict[int, str] = {}
        # Newer release on GitHub, if any (checked at start, then daily): version, url, how
        # ("auto": one-click install; "git": a source checkout; "page": download by hand) and state.
        self.update: dict | None = None
        self._release: dict = {}
        # Wake word: always listening for "Hey Claude" while idle (off unless you turn it on).
        self._wake_cancel = threading.Event()
        self._wake_transcriber = None  # Whisper tiny: checks short phrases the keyword spotter missed
        self._force_submit = False
        # Desktop app: CLI session id -> conversation title, learned when you send a prompt.
        self._titles: dict[str, str] = dict(self.state.get("desktop_titles") or {})
        # What the mic is doing, for the widget: "" | waiting | recording | transcribing
        self._mic_phase = ""
        self.level = 0.0
        self.last_heard = ""
        self.last_heard_lang = ""
        # Last few transcriptions, newest first, so a reply can be copied again from the widget.
        self.history: list[dict] = list(self.state.get("history") or [])[:5]
        # Each conversation's latest turn (everything said since your prompt): replay and copy use it.
        self._turns: dict[str, dict] = {}
        self._last_session = ""
        # The window each conversation was prompted from, so a reply goes back to the right terminal.
        self._session_hwnd: dict[str, int] = {k: int(v) for k, v in (self.state.get("session_hwnds") or {}).items()}
        # What each agent is doing right now (from its tool calls), for the notch's live activity.
        self.activity: dict[str, dict] = {}
        self._pitches: dict[str, float] = {}
        self._agents: dict[str, str] = {}
        self._agents_at = 0.0
        self.banner: dict | None = None  # chime/silent: the reply shown as a notification under the notch
        self._reading = ""  # voice mode: what is being read out, shown under the notch to read along
        # Another app holds the mic (a call): chimes instead of the voice until it ends.
        self.in_call = ""
        self._call_override = False
        self._ptt_release = threading.Event()
        # Every permission request still waiting (several agents can ask at once); `permission` is the one shown.
        self._perms: dict[int, dict] = {}
        # Conversations waiting on you that we can't answer for you ("waiting for your input"): open them.
        self.attention: dict[str, dict] = {}
        # Answers to questions asked through the MCP server (ask_user), collected by its polling.
        self._answers: dict[int, str] = {}
        self._ask_seq = 0
        # Context used per conversation, and each agent's rate limits (Claude Code's status line, Codex's logs).
        # Limits are kept across restarts: Claude only reports them while its status line runs.
        self.usage: dict[str, dict] = {}
        self.limits: dict[str, dict] = {k: v for k, v in (self.state.get("limits") or {}).items() if isinstance(v, dict)}
        self._limit_warned: set[str] = set()
        # Threads out of quota: when they reset, and whether to send "continue" then. Kept across restarts.
        self._limited: dict[str, dict] = {k: v for k, v in (self.state.get("limited") or {}).items()
                                          if isinstance(v, dict)}
        self._fullscreen = ""  # "presenting" / "full screen" (focus)
        # Phone notifications (ntfy, opt-in) and scheduled prompts (kept across restarts).
        self.notifier = ntfy.Notifier(self._ntfy_settings)
        self._schedules: list[dict] = [s for s in self.state.get("schedules") or [] if isinstance(s, dict)]
        self._limits_at: dict[str, float] = {}  # when each agent's numbers were measured (newest wins)
        # Claude's usage from its transcripts: an estimate between real readings (the desktop app reports none).
        self._ledger = usage.Ledger()
        self._estimator = usage.Estimator(self._ledger, self.state.get("claude_usage"))
        self._claude_est: dict[str, dict] = {}
        self._claude_asked = 0.0  # the last time we ran `claude -p /usage`
        self._usage_line = ""  # Claude Code's status line: "on" (ours), "off" or "custom" (yours); cached with agents
        # The last finished turn's outcome (files changed, tests), shown for a while in the notch.
        self.last_result: dict | None = None
        # What happened recently (turns done, agents asking for you), told when you come back.
        self._events: list[dict] = []
        self._away_since = 0.0
        # New threads: where they can start (cached), the folder you last worked in, and the one being dictated.
        self._targets: list[str] = []
        self._targets_at = 0.0
        self._new_pending: dict = {}
        self._new_window: dict = {}  # a session started in the desktop app: its window, for the first prompt
        self.new_thread_opens = 0  # bumped by the hotkey: the widget opens its new-thread box
        # Bumped by the show/hide hotkey; the widget toggles when it sees the number change.
        self.widget_toggles = 0
        if start_threads:
            threading.Thread(target=self._job_loop, name="jobs", daemon=True).start()
            threading.Thread(target=self._listen_monitor, name="listen", daemon=True).start()
            threading.Thread(target=self._wake_loop, name="wake", daemon=True).start()
            threading.Thread(target=self._check_update, name="update", daemon=True).start()
            threading.Thread(target=self._call_watch, name="calls", daemon=True).start()
            threading.Thread(target=self._away_watch, name="away", daemon=True).start()
            threading.Thread(target=self._apps_watch, name="apps", daemon=True).start()
            threading.Thread(target=self._quota_watch, name="quota", daemon=True).start()

    def _job_loop(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception:
                log.exception("job failed")

    # -- state ------------------------------------------------------------------
    @property
    def muted(self) -> bool:
        return bool(self.state.get("muted"))

    @property
    def hands_free(self) -> bool:
        return bool(self.state.get("hands_free"))

    def set_hands_free(self, on: bool) -> None:
        self.state["hands_free"] = on
        save_state(self.state)
        log.info("hands_free=%s", on)
        self._confirm("Auto-send on. Your replies will be sent automatically." if on else "Auto-send off.")

    @property
    def summary(self) -> bool:
        if "summary" in self.state:
            return bool(self.state["summary"])
        return bool(self.config.get("summary", {}).get("enabled", True))

    def set_summary(self, on: bool) -> None:
        # Hooks read this from state.json on every event.
        self.state["summary"] = on
        save_state(self.state)
        log.info("summary=%s", on)
        self._confirm("Summaries on." if on else "Full answers.")

    @property
    def speed_x(self) -> float:
        """Playback rate as a multiplier (1.5 = 1.5x faster than natural)."""
        return round(1.0 / float(self.config["speed"]), 2)

    def set_speed(self, x: float) -> None:
        x = min(3.0, max(0.5, float(x)))
        self.config["speed"] = 1.0 / x
        self.state["speed_x"] = x
        save_state(self.state)
        log.info("speed=%sx", x)
        # Applies from the next sentence; give a sample when idle so the change can be heard.
        if not self.speaker.busy:
            self._confirm(f"Speed {x:g}.")

    @property
    def chime(self) -> bool:
        """Chimes instead of the voice: chosen, or automatic while you're in a call or in focus."""
        return bool(self.state.get("chime")) or bool(self.focus)

    @property
    def focus(self) -> str:
        """Why replies are held back right now: 'call (Teams)', 'presenting', 'full screen'; '' otherwise."""
        if self._call_override:
            return ""
        cfg = self.config.get("focus", {})
        if self.in_call:
            return f"call ({self.in_call})"
        if cfg.get("enabled", True) and cfg.get("full_screen", True) and self._fullscreen:
            return self._fullscreen
        return ""

    def _holding(self) -> bool:
        """Focus: hold replies (approvals and questions still come through). Calls can keep chimes instead."""
        cfg = self.config.get("focus", {})
        if not self.focus or not cfg.get("enabled", True):
            return False
        return not (self.in_call and cfg.get("calls", "hold") == "chimes")

    @property
    def quiet(self) -> bool:
        """No voice (chimes or silent): replies show as a banner instead."""
        return self.muted or self.chime

    @property
    def output(self) -> str:
        return "silent" if self.muted else "chime" if self.chime else "voice"

    def _confirm(self, text: str) -> None:
        """Say a setting change out loud, unless the voice is off (chimes, silent)."""
        if not self.quiet:
            self.speaker.stop()
            self.speaker.speak(text, "en")

    def set_chime(self, on: bool) -> None:
        self.state["chime"] = on
        save_state(self.state)
        log.info("chime=%s", on)
        if self.focus and not on:
            self._call_override = True  # you asked for the voice during a call (or full screen): keep it
        self.stop()  # no voice and no open mic in chime mode
        if on:
            self.play_chime("done")
        else:
            self._confirm("Voice mode.")

    def set_output(self, mode: str) -> None:
        """voice | chime | silent: one choice instead of separate mute and chime switches."""
        if mode == "silent":
            self.state["chime"] = False
            self.set_muted(True)
            return
        if self.muted:
            self.state["muted"] = False
        self.set_chime(mode == "chime")

    def play_chime(self, kind: str) -> None:
        if self.muted:
            return
        import sounddevice as sd

        try:
            audio, rate = chime_audio(kind, float(self.config.get("chime", {}).get("volume", 0.35)))
            sd.play(audio, rate)
        except Exception:
            log.exception("chime failed")

    def notify(self, kind: str, data: dict | None = None) -> None:
        """The agent needs you: a question with options, a permission, a plan to approve."""
        data = data or {}
        log.info("notify kind=%s chime=%s", kind, self.chime)
        options = [str(o) for o in data.get("options") or [] if str(o).strip()]
        if kind == "message":  # a glance, not a voice: the banner only (and a chime when chimes are on)
            session, source = str(data.get("session") or ""), str(data.get("agent") or "")
            if session:
                self.threads[session] = {"source": source, "last": time.time()}
            self._show_banner(str(data.get("text") or ""), session, source, tag="note")
            if self.chime:
                self.play_chime("done")
            return
        if kind == "input":
            self.need_input(str(data.get("session") or ""), str(data.get("agent") or data.get("source") or ""),
                            str(data.get("text") or ""), announce=bool(data.get("announce", True)))
            return
        # A question with options, or (asked through MCP, with an id to collect the answer) a free one.
        if kind == "question" and (options or data.get("id")):
            self.question = {
                "session": str(data.get("session") or ""),
                "agent": str(data.get("agent") or ""),
                "question": str(data.get("question") or ""),
                "header": str(data.get("header") or ""),
                "options": options,
                "more": int(data.get("more") or 0),
                "id": int(data.get("id") or 0),
                "at": time.time(),
            }
            self._event("needs", self.question["session"], self.question["agent"], self.question["question"])
            self._push("needs", self._title(self.question["session"], self.question["agent"]),
                       self.question["question"] + (f" ({', '.join(options)})" if options else ""))
            if self.muted:
                return
            if self.chime:
                self.play_chime("attention")
                return
            preface = str(data.get("preface") or "")
            if preface.strip():
                self.speaker.speak(preface)
            self._wake_cancel.set()
            listed = " ".join(f"{i}: {o}." for i, o in enumerate(options, 1))
            self.speaker.speak(f"{self.question['question']} {listed}".strip())
            if self.config["auto_listen"].get("enabled") and self.transcriber is not None:
                self._reply_session = self.question["session"]
                self._listen_armed = True  # your spoken answer picks an option
            return
        if self.chime:
            self.play_chime("attention")

    def _title_for(self, hwnd: int, session: str) -> str:
        """Desktop app conversation title for an agent session ("" when it isn't one)."""
        if not (hwnd and session and claudeapp.is_desktop_app(hwnd)):
            return ""
        return claudeapp.session_title(session) or self._titles.get(session, "")

    def answer(self, index: int | None = None, text: str = "") -> bool:
        """Answer the pending question: option `index`, or a spoken/typed `text`."""
        import pyperclip

        q = self.question
        if not q:
            return False
        options = q["options"]
        if index is None:
            index = match_option(text, options)
        label = options[index] if index is not None and 0 <= index < len(options) else ""
        if q.get("id"):  # asked through MCP: the asking agent collects the answer, nothing to type
            self._answers[q["id"]] = label or text
            self._answers = dict(list(self._answers.items())[-20:])
            self.question = None
            log.info("answer to ask #%d collected", q["id"])
            return True
        session = q["session"]
        hwnd = self._hwnd_for(session)
        title = self._title_for(hwnd, session)
        log.info("answer option=%r free_text=%s", label, not label)
        if title:
            ok = claudeapp.answer_question(hwnd, title, label=label, other="" if label else text)
        else:
            # Agents whose question UI we can't drive (Cline): the answer is simply the reply.
            self._paste(label or text, hwnd, submit=True, session=session)
            ok = True
        if ok:
            self.question = None
            if q["more"] and title:
                claudeapp.select_session(hwnd, title)  # more questions: show them
                winfocus.focus(hwnd)
        else:
            pyperclip.copy(label or text)
            _beep(300, 150)
            if title:  # let you click it yourself
                winfocus.focus(hwnd)
                claudeapp.select_session(hwnd, title)
        return ok

    # -- agents asking for you ----------------------------------------------------
    def ask(self, session: str, source: str, question: str, options: list[str]) -> int:
        """A question from an MCP client (ask_user): read out and shown; its answer is collected by ask_state()."""
        self._ask_seq += 1
        self.notify("question", {"session": session, "agent": source, "question": question, "options": options,
                                 "id": self._ask_seq})
        if session:
            self.threads[session] = {"source": source, "last": time.time()}
        return self._ask_seq

    def ask_state(self, ask_id: int, cancel: bool = False) -> dict:
        if ask_id in self._answers:
            return {"answer": self._answers.pop(ask_id)}
        pending = bool(self.question and self.question.get("id") == ask_id)
        if cancel and pending:
            self.question = None
            self.cancel_listen()
            pending = False
        return {"answer": None, "pending": pending}

    def need_input(self, session: str, source: str, text: str = "", announce: bool = True) -> None:
        """An agent waits on you for something we can't answer here: it joins the "needs you" list."""
        title = self._title(session, source)
        self.attention[session] = {"source": source, "text": text.strip()[:300], "at": time.time()}
        if session:
            self.threads[session] = {"source": source, "last": time.time()}
        self._event("needs", session, source, text)
        self._push("needs", title, text or "Waiting for you")
        log.info("%s needs you", title)
        if not announce or self.muted:
            return
        self._show_banner(text or "Waiting for you", session, source, kind="needs", tag="needs you")
        if self.chime:
            self.play_chime("attention")
        else:
            self._wake_cancel.set()
            self.speaker.speak(f"{title} needs you." + (f" {text}" if text and len(text) < 160 else ""))

    def _settled(self, session: str) -> None:
        """The conversation moved on (it talks, works, or you wrote to it): it no longer waits on you."""
        self.attention.pop(session, None)

    def _needs_status(self) -> list[dict]:
        """Everything waiting on you, oldest first: approvals, a question, agents waiting for input."""
        items = []
        for p in sorted(self._perms.values(), key=lambda p: p["at"]):
            items.append({"kind": "permission", "id": p["id"], "session": p["session"], "source": p["source"],
                          "text": p["summary"], "at": p["at"]})
        q = self.question
        if q:
            items.append({"kind": "question", "session": q["session"], "source": q["agent"], "text": q["question"],
                          "at": q["at"]})
        now = time.time()
        self.attention = {s: a for s, a in self.attention.items() if now - a["at"] < 3600}
        for session, a in self.attention.items():
            items.append({"kind": "input", "session": session, "source": a["source"],
                          "text": a["text"] or "Waiting for you", "at": a["at"]})
        for session, info in self._limited.items():
            when = ("about " if info.get("guess") else "") + _clock(info["resets"], "five_hour")
            items.append({"kind": "limit", "session": session, "source": info["source"], "resume": info["resume"],
                          "text": f"Out of quota · resumes {when}" if info["resume"] else f"Out of quota until {when}",
                          "at": info["at"]})
        for item in items:
            item["title"] = self._title(item["session"], item.pop("source"))
            item["color"] = thread_color(item["session"] or item["title"])
            item["at"] = round(item["at"])
        return items

    def _labels(self, session: str) -> list[str]:
        """What a conversation can be called: its title, or its agent's name."""
        source = self.threads.get(session, {}).get("source", "")
        return [self._title(session, source), source]

    def _find_thread(self, name: str, among: set[str] | None = None) -> str | None:
        """The most recent conversation called `name` ("Codex", "the notch redesign")."""
        for session, _info in sorted(self.threads.items(), key=lambda kv: -kv[1]["last"]):
            if among is not None and session not in among:
                continue
            if any(label and names_match(name, label) for label in self._labels(session)):
                return session
        return None

    def _mentioned(self, text: str, among: set[str]) -> str | None:
        """A conversation named somewhere in `text` ("approve Codex", "deny the notch one")."""
        words = _names(text)
        for session in among:
            for label in self._labels(session):
                if label and _names(label) and _names(label) <= words:
                    return session
        return None

    def _voice_approval(self, text: str) -> bool:
        """'Yes' / 'approve Codex' / 'deny the notch one' while approvals wait: answer the one named, else the one shown."""
        if not self._perms:
            return False
        allow = match_yes_no(text)
        if allow is None:
            return False
        perms = sorted(self._perms.values(), key=lambda p: p["at"])  # the order of the "needs you" rows
        words = set(_plain(text).split())
        if words & _ALL or "les deux" in _plain(text):  # "approve all", "deny both"
            for p in perms:
                self.answer_permission(p["id"], allow)
            return True
        named = self._mentioned(text, {p["session"] for p in perms})
        number = None if named else spoken_number(words, len(perms))
        if named:
            perm = next((p for p in perms if p["session"] == named), None)
        elif number:  # "approve two", "deny the first one"
            perm = perms[number - 1]
        else:
            perm = self.permission
        if perm is None:
            return False
        self.answer_permission(perm["id"], allow)
        return True

    def _routed(self, text: str) -> tuple[str, str] | None:
        """'Tell Codex to run the tests' -> (Codex's session, 'Run the tests'), when we know that conversation."""
        parsed = parse_route(text)
        if not parsed:
            return None
        session = self._find_thread(parsed[0])
        return (session, parsed[1]) if session is not None else None

    def _expand(self, text: str) -> tuple[str, bool]:
        """A saved prompt's full text when `text` is its name ("run the tests"); (text, expanded?)."""
        full = textclean.expand_shortcut(text, self.config.get("shortcuts") or {})
        return (full, True) if full else (text, False)

    def _send_to(self, session: str, message: str, lang: str) -> None:
        """Type a message into a conversation's own window (and send it), then come back."""
        message, expanded = self._expand(message)
        hwnd = self._session_hwnd.get(session, 0)
        title = self._title(session, self.threads.get(session, {}).get("source", ""))
        if not (hwnd and winfocus.is_window(hwnd)):
            log.info("no window known for %r: message not sent", title)
            self._show_banner(f"I don't know where {title} is yet: send it one message from its window first.",
                              session, "", kind="you", tag="not sent")
            _beep(300, 150)
            return
        self._heard(message, lang, session, tag="shortcut" if expanded else "sent")
        self._paste(message, hwnd, submit=True, session=session)

    # -- handoff ----------------------------------------------------------------
    def _grab_selection(self) -> str:
        """The text selected in the app in front (copied, then the clipboard put back)."""
        import pyperclip

        self._wait_modifiers_released()
        previous = pyperclip.paste()
        pyperclip.copy("")
        keys.copy()
        time.sleep(0.25)
        selected = pyperclip.paste()
        pyperclip.copy(previous)
        return selected.strip()

    def handoff(self, name: str, message: str, lang: str) -> None:
        """'Send this to Codex': the selection (or the screenshot on the clipboard) goes to that conversation.

        With a message ("... and ask it to fix it") it's sent; without, it waits in the box for your question.
        """
        session = self._find_thread(name)
        if session is None:
            self._show_banner(f"I don't know a thread called {name}.", "", "", kind="note", tag="not sent")
            _beep(300, 150)
            return
        content = self._grab_selection()
        if not content:
            png = winfocus.clipboard_png()
            if png:
                shots = HOME / "shots"
                shots.mkdir(parents=True, exist_ok=True)
                for old in sorted(shots.glob("*.png"))[:-20]:  # keep the last 20
                    old.unlink(missing_ok=True)
                path = shots / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
                path.write_bytes(png)
                content = str(path).replace("\\", "/")
        title = self._title(session, self.threads.get(session, {}).get("source", ""))
        if not content:
            self._show_banner(f"Nothing to send to {title}: select some text or copy a screenshot first.", "", "",
                              kind="note", tag="not sent")
            _beep(300, 150)
            return
        message, _ = self._expand(message) if message else ("", False)
        text = f"{message}\n\n{content}" if message else content
        log.info("handoff to %r (%d chars, %s)", title, len(content), "sent" if message else "not sent")
        self._heard(message or f"(to {title}) {content[:120]}", lang, session, tag="sent" if message else "handoff")
        self._paste(text if message else text + " ", self._hwnd_for(session), submit=bool(message), session=session)

    # -- out of quota -------------------------------------------------------------
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
        self._push("limit", title, f"Out of quota until {'about ' if guess else ''}{when}"
                   + (": it gets “continue” then." if resume else "."))
        if source == "claude" and resets and not guess:
            window = "seven_day" if resets - time.time() > 6 * 3600 else "five_hour"
            self._set_limits("claude", {window: {"pct": 100, "resets": int(resets)}})
        if before.get("sent") and resume:
            return  # our "continue" came too early: quietly try again at the next reset
        if resume:
            self._show_banner(f"{title} is out of quota until {'about ' if guess else ''}{when}: I'll send it "
                              f"\"continue\" then.", session, source, kind="note", tag="limit")
            return
        self._show_banner(f"{title} is out of quota until {'about ' if guess else ''}{when}. Say \"resume when it "
                          "resets\" or click Resume, and I'll send it \"continue\" then.", session, source,
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
        self._show_banner(f"I'll send {title} \"continue\" at {when}." if on else f"{title} won't be resumed.",
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
                self._push("resumed", self._title(session, info["source"]), f"Sent “{message}” after its limit reset.")
                self.jobs.put(lambda s=session, m=message: self._send_to(s, m, "en"))

    def _not_limited(self, session: str) -> None:
        """The thread talks again: it isn't out of quota any more."""
        if session in self._limited:
            sent = self._limited.pop(session).get("sent")
            self._save_limited()
            if sent:
                log.info("%s resumed", self._title(session))

    # -- phone notifications (ntfy) ----------------------------------------------------
    def _ntfy_settings(self) -> dict | None:
        """Where notifications go, or None while they're off (nothing is ever sent then)."""
        phone = self.state.get("ntfy") or {}
        if not phone.get("enabled") or not phone.get("topic"):
            return None
        cfg = self.config.get("ntfy", {})
        return {"server": cfg.get("server") or ntfy.DEFAULT_SERVER, "token": cfg.get("token") or "",
                "topic": phone["topic"], "content": cfg.get("content", "summary")}

    def _push(self, kind: str, title: str, text: str = "", full: str = "", force: bool = False) -> None:
        """Send it to the phone if that's on, this kind is wanted, and (by default) you're away from the PC."""
        if not self._ntfy_settings():
            return
        cfg = self.config.get("ntfy", {})
        group = {"permission": "needs", "resumed": "limit"}.get(kind, kind)
        if not force and group not in (cfg.get("events") or ["needs", "done", "limit", "recap", "scheduled"]):
            return
        if not force and cfg.get("when", "away") == "away" and not self._away_for_phone():
            return
        content = cfg.get("content", "summary")
        self.notifier.push(kind, title, full if content == "full" and full else text)

    def _away_for_phone(self) -> bool:
        idle = winfocus.idle_seconds()
        return bool(self._away_since or self._holding()) or idle >= 60 * float(
            self.config.get("ntfy", {}).get("away_minutes", 2))

    def set_phone(self, on: bool) -> dict:
        """Turn phone notifications on (a private topic is made the first time) or off."""
        phone = dict(self.state.get("ntfy") or {})
        phone["enabled"] = on
        if on and not phone.get("topic"):
            phone["topic"] = ntfy.new_topic()
        self.state["ntfy"] = phone
        save_state(self.state)
        log.info("phone notifications %s", "on" if on else "off")
        return self._phone_status()

    def new_phone_topic(self) -> dict:
        """A new private topic: the old QR code stops receiving anything (scan the new one)."""
        self.state["ntfy"] = {**(self.state.get("ntfy") or {}), "topic": ntfy.new_topic()}
        save_state(self.state)
        return self._phone_status()

    def phone_test(self) -> bool:
        cfg = self._ntfy_settings()
        return bool(cfg) and self.notifier.send(ntfy.message("test", "agent-voice", "", "titles"), cfg)

    def _phone_status(self) -> dict:
        cfg = self._ntfy_settings()
        if not cfg:
            return {"enabled": False}
        return {"enabled": True, "url": ntfy.subscribe_url(cfg["server"], cfg["topic"]), "sent": self.notifier.sent,
                "error": self.notifier.last_error, "when": self.config.get("ntfy", {}).get("when", "away"),
                "content": cfg["content"]}

    # -- scheduled prompts ------------------------------------------------------------
    def add_schedule(self, prompt: str, at: float, session: str = "", new: dict | None = None,
                     repeat: str = "") -> dict:
        """Send `prompt` at `at`: to a conversation (`session`), or as a new thread (`new`: target, cwd)."""
        prompt = prompt.strip()
        if not prompt or not at:
            raise ValueError("a prompt and a time")
        title = (f"New thread ({TARGET_NAMES.get((new or {}).get('target', ''), 'where you last worked')})"
                 if new is not None else self._title(session, self.threads.get(session, {}).get("source", "")))
        item = {"id": max((s["id"] for s in self._schedules), default=0) + 1, "at": int(at), "prompt": prompt[:4000],
                "session": session, "new": new, "repeat": repeat if repeat in schedule.REPEATS else "",
                "title": title}
        self._schedules.append(item)
        self._save_schedules()
        log.info("scheduled #%d for %s: %s", item["id"], _clock(at, "seven_day"), title)
        self._show_banner(f"At {_clock(at, 'seven_day')}" + (f" ({item['repeat']})" if item["repeat"] else "")
                          + f", {title} gets: {prompt[:160]}", "", "", kind="note", tag="scheduled")
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
            self.add_schedule(new[1], at, new={"target": {"claude": "", "": ""}.get(new[0], new[0]), "cwd": ""})
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
                self._show_banner(f"Missed while the PC was off: {item['title']}: {item['prompt'][:120]}", "", "",
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

    # -- the day ----------------------------------------------------------------
    def _day(self) -> dict:
        """Today's log (turns, tokens, what each thread did), started afresh each morning."""
        today = time.strftime("%Y-%m-%d")
        day = self.state.get("day")
        if not isinstance(day, dict) or day.get("date") != today:
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
        """(shown, spoken): what each thread did today, what's waiting, the limits and the tokens."""
        day, stats = self._day(), self._day_status()
        if not stats["turns"]:
            return "Nothing yet today.", "Nothing yet today."
        threads = sorted(day["threads"].items(), key=lambda kv: -kv[1]["turns"])
        lines = [f"{stats['turns']} turn{'s' if stats['turns'] != 1 else ''} in {stats['threads']} "
                 f"thread{'s' if stats['threads'] != 1 else ''}" +
                 (f" · {_tokens_label(stats['tokens'])} tokens" if stats["tokens"] else "") + "."]
        for session, t in threads[:5]:
            title = self._title(session, t.get("source", "")) if session in self.threads else t.get("title") or "?"
            lines.append(f"{title} ({t['turns']}): {textclean.truncate(t['last'], 110) or 'done'}")
        waiting = [n["title"] for n in self._needs_status()]
        if waiting:
            lines.append(f"Still waiting on you: {_join(sorted(set(waiting)))}.")
        limits = []
        for source, windows in self._limits_status().items():
            parts = [f"{'5h' if k == 'five_hour' else 'week'} "
                     + (f"{'~' if w.get('est') else ''}{w['pct']}%" if w.get("pct") is not None
                        else f"{_tokens_label(w.get('tokens', 0))} tokens") for k, w in sorted(windows.items())]
            limits.append(f"{AGENT_TITLES.get(source, source.capitalize())} {', '.join(parts)}")
        if limits:
            lines.append("Limits: " + "; ".join(limits) + ".")
        top = threads[0][1]
        said = (f"Today: {stats['turns']} turns in {stats['threads']} threads, most of it in "
                f"{top.get('title') or 'one thread'}." + (f" {_join(sorted(set(waiting)))} still "
                                                           f"need{'s' if len(set(waiting)) == 1 else ''} you."
                                                           if waiting else ""))
        week = [w["pct"] for windows in self._limits_status().values() for k, w in windows.items()
                if k == "seven_day" and w.get("pct") is not None]
        if week:
            said += f" You've used {max(week)}% of your weekly limit."
        return " ".join(lines), said

    def recap(self, spoken: bool = True) -> None:
        shown, said = self.recap_text()
        log.info("recap: %s", said)
        self._show_banner(shown, "", "", kind="recap")
        self._push("recap", "Today", shown, full=shown)
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

    # -- away -------------------------------------------------------------------
    def _event(self, kind: str, session: str, source: str, text: str = "") -> None:
        self._events = [*self._events[-49:], {"kind": kind, "session": session, "source": source,
                                              "text": " ".join(text.split())[:200], "at": time.time()}]

    def _away_watch(self) -> None:
        """Every few seconds: have you been away (no input, or in a call)? Tell you what happened when you're back."""
        while True:
            time.sleep(5)
            try:
                now = time.time()
                self._check_away(winfocus.idle_seconds(), now)
                self._check_resumes(now)
                self._check_schedules(now)
                self._check_recap(now)
            except Exception:
                log.exception("away check failed")

    def _check_away(self, idle: float, now: float) -> None:
        cfg = self.config.get("catch_up", {})
        if not cfg.get("enabled", True):
            return
        away = idle >= 60 * float(cfg.get("away_minutes", 5)) or bool(self.in_call) or self._holding()
        if away and not self._away_since:
            self._away_since = now - (0 if (self.in_call or self._holding()) and idle < 60 else idle)
        elif not away and self._away_since:
            since, self._away_since = self._away_since, 0.0
            self._catch_up([e for e in self._events if e["at"] >= since])

    def _catch_up(self, events: list[dict]) -> None:
        """'While you were away: Notch redesign finished; Codex needs you.' Once, as you come back."""
        if not events:
            return
        waiting = {item["session"] for item in self._needs_status()}
        done, needs, summaries = [], [], {}
        for e in events:
            title = self._title(e["session"], e["source"])
            if e["kind"] == "done":
                if title not in done:
                    done.append(title)
                if e["text"]:
                    summaries[title] = e["text"]  # its latest turn
            elif e["kind"] == "needs" and e["session"] in waiting and title not in needs:
                needs.append(title)
        parts = ([f"{_join(done)} finished"] if done else []) + (
            [f"{_join(needs)} need{'s' if len(needs) == 1 else ''} you"] if needs else [])
        if not parts:
            return
        said = f"While you were away: {'; '.join(parts)}."
        log.info("catch-up: %s", said)
        # The banner's header already says "While you were away": the body goes straight to the news. One
        # conversation finished: its summary right after its name, not the name twice.
        if len(done) == 1 and done[0] in summaries:
            body = [f"{done[0]} finished: {summaries[done[0]]}"] + (
                [f"{_join(needs)} need{'s' if len(needs) == 1 else ''} you."] if needs else [])
        else:
            body = [f"{'; '.join(parts)}."] + [f"{t}: {x}" for t, x in list(summaries.items())[-3:]]
        self._show_banner(" ".join(body), "", "", kind="catchup")
        if not self.quiet:
            self._wake_cancel.set()
            self.speaker.speak(said, "en")

    # -- desktop apps -----------------------------------------------------------
    @property
    def desktop_apps(self) -> bool:
        if "desktop_apps" in self.state:
            return bool(self.state["desktop_apps"])
        return bool(self.config.get("desktop_apps", {}).get("enabled", False))

    def set_desktop_apps(self, on: bool) -> None:
        self.state["desktop_apps"] = on
        save_state(self.state)
        log.info("desktop_apps=%s", on)
        self._confirm("Desktop apps on." if on else "Desktop apps off.")

    def _apps_watch(self) -> None:
        """ChatGPT, Copilot... have no hooks: their notifications are their replies (opt-in)."""
        from agent_voice import apps

        watcher = None
        while True:
            time.sleep(3)
            if not self.desktop_apps or not apps.available():
                watcher = None
                continue
            try:
                if watcher is None:
                    watcher = apps.Watcher(self.config.get("desktop_apps", {}).get("apps"))
                for toast in watcher.poll():
                    self._app_reply(toast)
            except Exception:
                log.exception("desktop apps check failed")
                time.sleep(60)

    def _app_reply(self, toast: dict) -> None:
        app = toast["app"]
        label = AGENT_TITLES.get(app, app.capitalize())
        log.info("%s notification (%d chars)", label, len(toast["text"]))
        hwnd = winfocus.find_window(label)
        self.jobs.put(lambda: self.speak(toast["text"], source=app, final=True, session=f"app-{app}", hwnd=hwnd))

    # -- usage ------------------------------------------------------------------
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
                span = "5-hour" if window == "five_hour" else "weekly"
                when = f", it resets at {_clock(w['resets'], window)}" if w["resets"] else ""
                log.info("%s %s limit at %d%%", source, window, w["pct"])
                about = "about " if w.get("est") else ""
                self._show_banner(f"{name} has used {about}{w['pct']}% of its {span} limit{when}.", "", "",
                                  kind="note", tag="limits")

    def _quota_watch(self) -> None:
        """Codex writes its rate limits in its session logs, Claude caches its own: re-read them when they change."""
        seen = (None, 0.0)
        claude_seen = 0.0
        claude_json = Path.home() / ".claude.json"
        while True:
            try:
                self._ledger.refresh()  # first: a reading calibrates against the requests before it
                mtime = claude_json.stat().st_mtime if claude_json.exists() else 0.0
                if mtime != claude_seen:
                    claude_seen = mtime
                    fetched, found = quota.claude_cached(claude_json)
                    if found:
                        self._set_limits("claude", found, at=fetched)
            except Exception:
                log.exception("claude limits check failed")
            try:
                if self._ask_claude_usage():
                    fetched, found = quota.claude_cached(claude_json)
                    if found:
                        claude_seen = claude_json.stat().st_mtime
                        self._set_limits("claude", found, at=fetched)
            except Exception:
                log.exception("claude usage refresh failed")
            try:
                self._estimate_claude()
            except Exception:
                log.exception("claude usage estimate failed")
            try:
                self._read_usage_drops()
            except Exception:
                log.exception("usage folder check failed")
            try:
                path = quota.latest_codex_log()
                if path is not None and (path, path.stat().st_mtime) != seen:
                    seen = (path, path.stat().st_mtime)
                    found = quota.codex_limits(path)
                    if found:
                        self._set_limits("codex", found)
            except Exception:
                log.exception("codex limits check failed")
            time.sleep(60)

    def _ask_claude_usage(self, now: float | None = None) -> bool:
        """Have Claude Code fetch its own usage (`claude -p /usage`: a local command, no model request), which it
        saves to ~/.claude.json. Every few minutes while Claude is in use, and once at start."""
        from agent_voice import launch

        now = time.time() if now is None else now
        minutes = float(self.config.get("limits", {}).get("claude_refresh_minutes", 10) or 0)
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
        return {source: windows for source, windows in out.items() if windows}

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
            text = "Claude Code already has your own status line: agent-voice leaves it alone."
        elif result.startswith("failed"):
            text = f"Couldn't change Claude Code's status line ({result[8:]})."
        elif enable:
            text = "Claude Code's limits will show here after its next reply (from its status line)."
        else:
            self.limits.pop("claude", None)
            self.state["limits"] = self.limits
            save_state(self.state)
            text = "Claude Code's limits are off; its status line is back to normal."
        self._show_banner(text, "", "", kind="note", tag="limits")

    def set_muted(self, muted: bool) -> None:
        self.state["muted"] = muted
        save_state(self.state)
        if muted:
            self.stop()
        _beep(440 if muted else 880)
        log.info("muted=%s", muted)

    # -- actions ----------------------------------------------------------------
    def speak(
        self,
        text: str,
        lang: str | None = None,
        source: str = "",
        interrupt: bool = False,
        final: bool = False,
        voice: str | None = None,
        session: str = "",
        hwnd: int = 0,
        result: dict | None = None,
    ) -> int:
        """Queue text; `final` marks the end of an agent's turn and arms auto-listen.

        `hwnd`: the agent's window when its hook knows it (a terminal), so replies and messages can reach it.
        `result`: what the turn changed ({"files": 3, "tests": "passed"}), shown in the notch.
        """
        if session and self.question and self.question["session"] == session and not self.question.get("id"):
            self.question = None  # the agent moved on: the question was answered
        if session:
            self.threads[session] = {"source": source, "last": time.time()}
            if text.strip() or final:
                self._settled(session)
            if hwnd and not self._session_hwnd.get(session):
                self._remember_hwnd(session, hwnd)
        with self._lock:
            if session and self._voice_session and session != self._voice_session and not interrupt and (
                    self.speaker.busy or self._listen_armed or self._listening):
                # Another thread is talking (or waiting for your reply): this one waits its turn.
                self._queue.append((text, lang, source, interrupt, final, voice, session, 0, result))
                log.info("queued speech from another thread (%d waiting)", len(self._queue))
                return 0
        if final and session and textclean.limit_reset(text) is not None:
            self.limit_hit(session, source, text)  # "5-hour limit reached ∙ resets 3pm": not a reply
            self.activity.pop(session, None)
            return 0
        if session and text.strip():
            self._not_limited(session)
        self._record_turn(session, source, text, lang, voice, final)
        if final:
            self.activity.pop(session, None)  # the agent is done working
            self._finished(session, source, text, result)
        if self._holding():
            # Focus (a call, presenting, full screen): nothing now; the catch-up tells you afterwards.
            log.info("held (%s): source=%s final=%s", self.focus, source, final)
            return 0
        if self.quiet:
            # Chimes or silent: no voice and no open mic; the end of a turn shows as a banner.
            log.info("speak source=%s output=%s final=%s", source, self.output, final)
            if final:
                turn = self._turn_text(session)
                shown = text.strip() if self.summary and text.strip() else turn or text.strip()
                if shown:
                    self._show_banner(shown, session, source, tag=result_label(result))
                self.play_chime("done")  # silent: no sound either
            return 0
        if interrupt:
            self.speaker.stop()
        if text.strip() or final:
            self._voice_session, self._voice_source = session, source  # whose voice this is, for the widget
            self._wake_cancel.set()  # never let the wake word hear our own voice
        if text.strip():
            # Read along: a new speech replaces the text, one queued behind the current one adds to it.
            busy = self.speaker.busy and session == self._voice_session
            self._reading = f"{self._reading}\n\n{text.strip()}" if busy and self._reading else text.strip()
        n = self.speaker.speak(text, lang, voice, pitch=self._pitch(session)) if text.strip() else 0
        log.info("speak source=%s sentences=%d final=%s", source, n, final)
        if final and self.config["auto_listen"].get("enabled") and self.transcriber is not None:
            self._reply_session = session
            self._listen_armed = True
        return n

    def _record_turn(self, session: str, source: str, text: str, lang, voice, final: bool) -> None:
        """Collect what a conversation says in one turn (progress, then the end), for replay and copy."""
        turn = self._turns.get(session)
        if turn is None or (turn["done"] and text.strip()):
            turn = {"texts": [], "done": False, "lang": lang, "voice": voice, "source": source}
            self._turns[session] = turn
            self._turns = dict(list(self._turns.items())[-20:])
        if text.strip():
            turn["texts"].append(text.strip())
            turn["lang"], turn["voice"] = lang, voice
            self._last_session = session
        if final:
            turn["done"] = True

    def _finished(self, session: str, source: str, text: str, result: dict | None) -> None:
        """A turn ended: keep its outcome for the notch, and note it for when you come back."""
        turn = self._turns.get(session)
        if turn is not None and result:
            turn["result"] = result
        summary = text.strip() or self._turn_text(session)
        self._event("done", session, source, textclean.clean(summary) if summary else "")
        said_line = re.search(r"voice summary:\s*(.+)", summary or "", re.I)
        full = textclean.clean(strip_summary(self._turn_text(session) or summary or ""))
        self._push("done", self._title(session, source),
                   said_line.group(1).strip() if said_line else full, full=full)
        said = re.search(r"voice summary:\s*(.+)", summary or "", re.I)  # its own one-line summary, if it wrote one
        self._log_turn(session, source, said.group(1).strip() if said else textclean.clean(summary) if summary else "")
        if result and (result.get("files") or result.get("tests")):
            title = self._title(session, source)
            self.last_result = {"session": session, "title": title, "color": thread_color(session or title),
                                "files": int(result.get("files") or 0), "tests": result.get("tests") or "",
                                "at": round(time.time())}

    def _remember_hwnd(self, session: str, hwnd: int) -> None:
        self._session_hwnd[session] = hwnd
        self._session_hwnd = dict(list(self._session_hwnd.items())[-50:])
        self.state["session_hwnds"] = self._session_hwnd
        save_state(self.state)

    def _turn_text(self, session: str) -> str:
        return "\n\n".join((self._turns.get(session) or {}).get("texts") or [])

    def _pitch(self, session: str) -> float:
        """This conversation's voice: the first one natural, the next ones a little lower or higher."""
        if not session or not self.config.get("thread_voices", True):
            return 1.0
        if session not in self._pitches:
            self._pitches[session] = THREAD_PITCHES[len(self._pitches) % len(THREAD_PITCHES)]
        return self._pitches[session]

    def copy_reply(self, session: str = "") -> bool:
        import pyperclip

        text = self._turn_text(session or self._last_session)
        if text:
            pyperclip.copy(text)
        return bool(text)

    def _reading_status(self) -> dict | None:
        """Voice mode: the text being read out, for the widget to show while (and only while) it plays."""
        if not (self.speaker.busy and self._reading and self.config.get("read_along", True)) or self.quiet:
            return None
        session = self._voice_session
        title = self._title(session, self._voice_source)
        return {"text": textclean.clean(self._reading)[:1500], "title": title,
                "color": thread_color(session or title), "session": session}

    def _show_banner(self, text: str, session: str, source: str, kind: str = "reply", tag: str = "") -> None:
        """A notification under the notch: a reply in chime/silent mode, or ("you") what you just said."""
        title = self._title(session, source) if session or source else ""
        self.banner = {"id": (self.banner or {}).get("id", 0) + 1, "text": textclean.clean(text)[:800],
                       "title": title, "color": thread_color(session or title), "session": session,
                       "kind": kind, "tag": tag, "at": time.time()}

    def agent_states(self) -> dict[str, str]:
        """Which agents are connected; re-read every 30 s at most (status is polled 8 times a second).

        The browser is only "on" once its extension has talked to us: until then the bridge is ready but the
        extension isn't loaded ("setup").
        """
        if time.time() - self._agents_at > 30:
            self._agents, self._agents_at = install.agent_states(), time.time()
            self._usage_line = install.usage_line_state()
            if self._agents.get("browser") == "on" and not self.state.get("browser_seen"):
                self._agents["browser"] = "setup"
        return self._agents

    def bridge_seen(self) -> None:
        """The browser extension started its bridge: it's loaded and working."""
        if not self.state.get("browser_seen"):
            log.info("browser extension connected")
            self.state["browser_seen"] = round(time.time())
            save_state(self.state)
            self._show_banner("The extension is connected: replies on ChatGPT, Claude and Gemini will be read out.",
                              "", "", kind="note", tag="browser")
        self._agents_at = 0.0

    # What to do after connecting each kind of agent, said in the banner.
    _NEXT_STEP = {"claude": "Open a new session to load it.", "codex": "Restart Codex to load it.",
                  "gemini": "Restart Gemini CLI to load it.", "cursor": "Restart Cursor to load it.",
                  "cline": "In Cline, turn on Settings > Features > Enable Hooks.",
                  "browser": "Now in Chrome or Edge: open chrome://extensions, turn on Developer mode, click "
                             "Load unpacked and pick the folder that just opened."}

    def install_agent(self, agent: str) -> None:
        """Connect an agent from the widget (same as `agent-voice install <agent>`), and say how it went."""
        name = install.AGENT_TITLES.get(agent, agent)
        try:
            result = install.INSTALLERS[agent]()
            log.info("install %s: %s", agent, result)
            if result.startswith("skipped"):
                text = f"{name}: {result}"
            else:
                text = f"{name} connected. " + self._NEXT_STEP.get(agent, f"Restart {name} to load agent-voice.")
        except Exception as exc:  # a malformed settings file must not take the daemon down
            log.exception("install %s failed", agent)
            text = f"Couldn't connect {name}: {exc}"
        self._agents_at = 0.0
        if agent == "browser":
            self.state.pop("browser_seen", None)  # "on" again once the extension says hello
            save_state(self.state)
            if sys.platform == "win32":
                os.startfile(install.extension_dir())  # the folder to pick in "Load unpacked"
        self._show_banner(text, "", "", kind="note", tag="agents")

    # -- new threads ------------------------------------------------------------
    def _desktop_window(self) -> int:
        """The Claude desktop app's window: one a conversation was prompted from, else any open one."""
        for hwnd in [*self._session_hwnd.values(), self._target_hwnd]:
            if hwnd and winfocus.is_window(hwnd) and claudeapp.is_desktop_app(hwnd):
                return hwnd
        found = winfocus.find_window("Claude")
        return found if found and claudeapp.is_desktop_app(found) else 0

    def new_targets(self) -> list[str]:
        """Where a new thread can start: the desktop app (if open), then the agent CLIs found. Cached 30 s."""
        from agent_voice import launch

        if time.time() - self._targets_at > 30:
            self._targets, self._targets_at = launch.targets(bool(self._desktop_window())), time.time()
        return self._targets

    def _default_target(self) -> str:
        """Where your last conversation was: the desktop app or a terminal."""
        targets = self.new_targets()
        last = self._session_hwnd.get(self._last_session) or self._target_hwnd
        if "claude-app" in targets and last and claudeapp.is_desktop_app(last):
            return "claude-app"
        return next((t for t in ("claude", "claude-app", "codex", "gemini") if t in targets), "")

    def new_thread(self, prompt: str, target: str = "", cwd: str = "", lang: str = "") -> bool:
        """Start a new conversation with `prompt`: in the Claude desktop app, or a CLI agent in a new terminal tab."""
        from agent_voice import lang as langmod, launch

        prompt = prompt.strip()
        if not prompt:
            return False
        target = target or self._default_target()
        cwd = launch.default_cwd(cwd or self.state.get("last_cwd", ""))
        label = launch.LABELS.get(target, target or "an agent")
        log.info("new thread in %s (%s)", label, cwd)
        ok = False
        try:
            if target == "claude-app":
                hwnd = self._desktop_window()
                ok = bool(hwnd) and claudeapp.new_session(hwnd, prompt, Path(cwd).name)
                if ok:
                    self._new_window = {"hwnd": hwnd, "at": time.time()}
            elif target in launch.CLIS:
                launch.open_terminal(target, prompt, cwd)
                ok = True
        except Exception:
            log.exception("new thread failed")
        if ok:
            self._show_banner(prompt, "", "", kind="you", tag=f"new · {label}")
            self.history = [{"text": prompt, "lang": lang or langmod.detect(prompt), "at": round(time.time())},
                            *self.history][:5]
        else:
            self._show_banner(f"Couldn't start a new thread in {label}. Your prompt is on the clipboard.", "", "",
                              kind="note", tag="new thread")
            import pyperclip

            pyperclip.copy(prompt)
            _beep(300, 150)
        return ok

    def listen_new_thread(self, target: str = "", cwd: str = "") -> None:
        """Dictate a new thread's first prompt: what you say next starts it."""
        self._new_pending = {"target": target, "cwd": cwd}
        self.reply_now(NEW_THREAD)

    def send_text(self, session: str, text: str) -> None:
        """A reply typed in the notch: into that conversation's window, sent, and back to where you were."""
        from agent_voice import lang

        text = text.strip()
        if not text:
            return
        session = session or self._last_session
        text, expanded = self._expand(text)
        self._heard(text, lang.detect(text), session, tag="shortcut" if expanded else "sent")
        self._paste(text, self._hwnd_for(session), submit=True, session=session)

    def replay(self, session: str = "") -> None:
        """Say a conversation's last turn again (the last one heard by default); the mic stays closed."""
        session = session or self._last_session
        turn = self._turns.get(session)
        if not turn or not turn["texts"]:
            return
        if self.quiet:  # no voice now: show it instead
            self._show_banner(self._turn_text(session), session, turn["source"])
            return
        self.speaker.stop()
        self._wake_cancel.set()
        self._voice_session, self._voice_source = session, turn["source"]
        self._reading = self._turn_text(session)
        self.speaker.speak(self._turn_text(session), turn["lang"], turn["voice"], pitch=self._pitch(session))

    def stop(self) -> None:
        self.speaker.stop()
        self.cancel_listen()

    def cancel_listen(self) -> None:
        self._listen_armed = False
        self._listen_cancel.set()
        # Closing the mic can take ~1 s on some drivers; show "idle" right away.
        if self._listening and not self._ptt_active:
            self._mic_phase = ""
            self.level = 0.0

    def reply_now(self, session: str | None = None) -> None:
        """Skip the rest of the speech and listen for your answer right away.

        `session`: talk to that conversation (its thread row, its banner): what you say is sent to it.
        """
        self.speaker.stop()
        self._listen_armed = False
        if self._listening or self._ptt_active or self.transcriber is None:
            return
        if session:
            self._reply_session = session
            self._force_submit = True  # you picked the thread: send it, there's no prompt in front of you
            if session != NEW_THREAD:
                self._new_pending = {}
        threading.Thread(target=self._auto_listen, daemon=True).start()

    def _set_level(self, value: float) -> None:
        self.level = value

    def _heard(self, text: str, lang: str, session: str | None = None, tag: str = "typed") -> None:
        """You said something: keep it, and show it under the notch, marked as yours, before it goes out."""
        self.last_heard, self.last_heard_lang = text, lang
        session = self._reply_session if session is None else session
        self._show_banner(text, session, self.threads.get(session, {}).get("source", "") if session else "",
                          kind="you", tag=tag)
        self.history = [{"text": text, "lang": lang, "at": round(time.time())}, *self.history][:5]
        self.state["history"] = self.history
        save_state(self.state)

    def prompt_submitted(self, hwnd: int, session: str = "", source: str = "", title: str = "", cwd: str = "") -> None:
        """The user sent a new prompt: remember its window (and conversation), and stop talking/listening.

        `title`: the conversation's name when the caller knows it (a browser tab's title).
        """
        if session and title:
            self._file_titles[session] = title
        if cwd and cwd != self.state.get("last_cwd"):
            self.state["last_cwd"] = cwd  # where a new thread starts by default
            save_state(self.state)
        pending = self._new_window
        if session and pending and session not in self._session_hwnd and time.time() - pending["at"] < 60:
            hwnd = pending["hwnd"]  # a session we just started in the desktop app: it lives in that window
            self._new_window = {}
        if hwnd and hwnd != self._target_hwnd:
            self._target_hwnd = hwnd
            self.state["target_hwnd"] = hwnd
            save_state(self.state)
        if hwnd and session and claudeapp.is_desktop_app(hwnd):
            threading.Thread(target=self._learn_title, args=(hwnd, session), daemon=True).start()
        if session:
            with self._lock:
                self._queue = [job for job in self._queue if job[6] != session]  # you moved on in that thread
            source = source or self.threads.get(session, {}).get("source", "")
            self.threads[session] = {"source": source, "last": time.time()}
            if hwnd and self._session_hwnd.get(session) != hwnd:
                self._remember_hwnd(session, hwnd)
            self._settled(session)
            if session in self._turns:
                self._turns[session]["done"] = True  # what comes next is a new turn
            now = time.time()
            self.activity[session] = {"source": source, "started": now, "last": now, "steps": 0, "label": "Thinking"}
        self.stop()

    def _hwnd_for(self, session: str) -> int:
        """The window a conversation was prompted from; the last prompted window otherwise."""
        hwnd = self._session_hwnd.get(session, 0)
        return hwnd if hwnd and winfocus.is_window(hwnd) else self._target_hwnd

    def tool_used(self, session: str, source: str, label: str) -> None:
        """An agent is working: one more step, and what it's doing now."""
        now = time.time()
        act = self.activity.get(session)
        if act is None or now - act["last"] > 900:
            act = {"source": source, "started": now, "steps": 0}
            self.activity[session] = act
        act.update(last=now, label=label[:80], steps=act["steps"] + 1)
        self._settled(session)
        if session:
            self.threads[session] = {"source": source or self.threads.get(session, {}).get("source", ""),
                                     "last": now}

    def activity_done(self, session: str) -> None:
        """An agent says it finished working (MCP set_status done): no reply to speak, just stop the timer."""
        self.activity.pop(session, None)

    def _activity_status(self) -> dict | None:
        """The busiest-looking conversation's live activity (the most recent), and how many others work."""
        now = time.time()
        live = {s: a for s, a in self.activity.items() if now - a["last"] < 900}
        self.activity = live
        if not live:
            return None
        session, act = max(live.items(), key=lambda kv: kv[1]["last"])
        title = self._title(session, act["source"])
        return {"session": session, "title": title, "color": thread_color(session or title),
                "label": act.get("label", ""), "steps": act["steps"], "elapsed": int(now - act["started"]),
                "others": len(live) - 1, "context": (self.usage.get(session) or {}).get("context")}

    def _call_watch(self) -> None:
        """Every few seconds: is another app on the mic? Then chimes until the call ends."""
        while True:
            time.sleep(4)
            try:
                busy = winfocus.busy_state()
            except Exception:
                busy = ""
            if busy != self._fullscreen:
                log.info("focus: %s", busy or "off")
                if not busy and not self.in_call:
                    self._call_override = False
                self._fullscreen = busy
            if not self.config.get("auto_chime_in_calls", True):
                continue
            try:
                apps = calls.apps_using_mic()
            except Exception:
                log.exception("call check failed")
                time.sleep(60)
                continue
            label = calls.app_label(apps[0]) if apps else ""
            if label and not self.in_call:
                log.info("call started (%s): chimes until it ends", label)
                self.in_call, self._call_override = label, False
                if not self.state.get("chime"):
                    self.stop()  # stop talking over your call
            elif not label and self.in_call:
                log.info("call ended")
                self.in_call, self._call_override = "", False

    def _learn_title(self, hwnd: int, session: str) -> None:
        """You type in the conversation on screen, so its title belongs to this session.

        Not when another session already owns that title: a prompt sent by Claude to a side
        session it started also fires this hook, while you are still looking at your own.
        """
        title = claudeapp.current_title(hwnd)
        if not title or any(t == title for s, t in self._titles.items() if s != session):
            return
        if self._titles.get(session) != title:
            self._titles[session] = title
            self._titles = dict(list(self._titles.items())[-50:])
            self.state["desktop_titles"] = self._titles
            save_state(self.state)
            log.info("conversation %r is session %s", title, session[:8])

    # -- auto-listen ------------------------------------------------------------
    def _play_next_thread(self) -> None:
        """Speak the next waiting thread, announced by its name."""
        with self._lock:
            if not self._queue:
                return
            session = self._queue[0][6]
            jobs = [job for job in self._queue if job[6] == session]
            self._queue = [job for job in self._queue if job[6] != session]
        title = self._title(session, jobs[0][2])
        log.info("playing queued thread %r (%d left)", title, len(self._queue))
        self._voice_session, self._voice_source = session, jobs[0][2]
        if not self.quiet:
            self.speaker.speak(f"{title}.", pitch=self._pitch(session))
        for job in jobs:
            self.speak(*job)

    def _listen_monitor(self) -> None:
        idle_since = None
        queue_idle = None
        while True:
            time.sleep(0.1)
            free = not (self.speaker.busy or self._listen_armed or self._listening or self._ptt_active
                        or self.question or self.permission)
            if free and self._queue and not self.muted:
                queue_idle = queue_idle or time.time()
                if time.time() - queue_idle >= 0.6:
                    queue_idle = None
                    self._play_next_thread()
                continue
            queue_idle = None
            if not self._listen_armed or self._ptt_active or self.muted:
                idle_since = None
                continue
            if self.speaker.busy:
                idle_since = None
                continue
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= 0.4:  # truly done speaking, not a gap between sentences
                self._listen_armed = False
                idle_since = None
                self._auto_listen()

    def _auto_listen(self) -> None:
        from agent_voice.stt import is_hallucination, listen_for_speech

        cfg = self.config["auto_listen"]
        session = self._reply_session
        target = self._hwnd_for(session) or winfocus.foreground()
        question = self.question
        permission = self.permission
        force_submit, self._force_submit = self._force_submit, False
        self._wake_cancel.set()
        self._listen_cancel.clear()
        self._listening = True
        self._mic_phase = "waiting"
        self.speaker.hold()
        try:
            _beep(700, 70)
            time.sleep(0.15)  # don't record our own beep
            audio = listen_for_speech(
                float(cfg["window_seconds"]),
                float(cfg["silence_seconds"]),
                float(cfg["max_seconds"]),
                self._listen_cancel,
                on_level=self._set_level,
                on_start=lambda: setattr(self, "_mic_phase", "recording"),
            )
            if audio is None or self._listen_cancel.is_set():
                log.info("auto-listen: nothing heard")
                _beep(400, 60)
                return
            _beep(520, 60)
            self._mic_phase = "transcribing"
            text, lang = self.transcriber.transcribe(audio)
            log.info("auto-listen lang=%s chars=%d", lang, len(text))
            if self._listen_cancel.is_set() or is_hallucination(text):
                return
            if session == NEW_THREAD:  # dictating a new thread's first prompt
                pending, self._new_pending = self._new_pending, {}
                self.new_thread(text, pending.get("target", ""), pending.get("cwd", ""), lang)
                return
            answering = (permission is not None and permission is self.permission) or (
                question is not None and question is self.question)
            routed = None if answering or match_command(text) else self._routed(text)
            if routed and routed[0] != session:
                self._send_to(routed[0], routed[1], lang)  # "Tell Codex to ...": goes to Codex, not here
                return
            tag = ("answer" if answering else "command" if match_command(text)
                   else "sent" if self.hands_free or force_submit else "typed")
            self._heard(text, lang, session, tag=tag)
            if permission is not None and permission is self.permission:
                if not self._voice_approval(text):
                    log.info("permission answer unclear: %r", text)
                    _beep(300, 150)
            elif question is not None and question is self.question:
                self.answer(text=text)
            elif match_command(text):
                self._run_command(match_command(text), session)
            elif session in self._limited and match_resume(text):
                self.set_resume(session)
            elif self._expand(text)[1]:  # a saved prompt's name: its full text, sent
                self._paste(self._expand(text)[0], target, submit=True, session=session)
            elif self.wake and strip_wake(text, list(self.config.get("wake_word", {}).get("phrases") or [])) is not None:
                # "Hey Claude, ..." while the reply mic was open: the rest is the reply; the phrase alone is nothing.
                rest = strip_wake(text, list(self.config.get("wake_word", {}).get("phrases") or []))
                if rest.strip(" .!?,"):
                    self._paste(rest, target, submit=self.hands_free or force_submit, session=session)
                else:
                    log.info("reply was only the wake phrase: nothing sent")
            else:
                self._paste(text, target, submit=self.hands_free or force_submit, session=session)
        except Exception:
            log.exception("auto-listen failed")
        finally:
            self._listening = False
            self.speaker.release()
            self._mic_phase = ""
            self.level = 0.0

    def _run_command(self, command: str, session: str) -> None:
        """A spoken command in the reply window: handled here, nothing is sent to the agent."""
        log.info("voice command: %s", command)
        if command == "repeat":
            self.replay(session)
        elif command in ("slower", "faster"):
            self.set_speed(self.speed_x + (0.25 if command == "faster" else -0.25))
        else:
            _beep(400, 60)

    def _paste(self, text: str, hwnd: int = 0, submit: bool = False, session: str = "") -> None:
        import pyperclip

        self._wait_modifiers_released()
        title = self._title_for(hwnd, session)
        if title and submit and self.config.get("background_send", True):
            # Desktop app: type and send it in place; the app stays wherever it is.
            if claudeapp.send_message(hwnd, title, text):
                pyperclip.copy(text)
                log.info("sent to %r in the background", title)
                return
            log.warning("background send to %r failed; typing it instead", title)
        # Where you were: once the reply is sent we hand the screen back to it.
        came_from = winfocus.foreground()
        was_minimized = winfocus.is_minimized(hwnd)
        switched = bool(hwnd) and came_from != hwnd
        if hwnd and not winfocus.focus(hwnd):
            # Don't type into whatever app is in front: leave the text on the clipboard instead.
            log.warning("could not focus target window %s (%s); text left on clipboard", hwnd, winfocus.title(hwnd))
            pyperclip.copy(text)
            _beep(300, 150)
            return
        # Desktop app: several conversations share one window; open the one this reply answers.
        if title and not claudeapp.select_session(hwnd, title):
            log.warning("could not open conversation %r; text left on clipboard", title)
            pyperclip.copy(text)
            _beep(300, 150)
            return
        time.sleep(0.1)
        # The text stays on the clipboard afterwards, so a reply that landed in the wrong place
        # can always be pasted again by hand.
        pyperclip.copy(text)
        keys.paste()
        time.sleep(0.3)
        if submit:
            keys.enter()
        if submit and switched and came_from:
            time.sleep(0.2)  # let the agent window register the Enter
            if was_minimized:
                winfocus.minimize(hwnd)
            winfocus.focus(came_from)

    def phase(self) -> str:
        if self._mic_phase:
            return self._mic_phase
        if self.speaker.busy:
            return "speaking"
        return "muted" if self.muted else "idle"

    def _title(self, session: str, source: str = "") -> str:
        """A conversation's name: its desktop-app title when known, else the agent's name."""
        title = self._titles.get(session, "")
        if session and not title:
            if session not in self._file_titles:  # app session files: read once per session, not per poll
                self._file_titles[session] = claudeapp.session_title(session)
            title = self._file_titles[session]
        return title or AGENT_TITLES.get(source, source.capitalize())

    def _thread_status(self) -> dict | None:
        """The conversation the voice (or the open mic) belongs to: its title and a colour of its own."""
        q, perm = self.question, self.permission
        if perm:
            session, source = perm["session"], perm["source"]
        elif q:
            session, source = q["session"], q["agent"]
        elif self._mic_phase:
            session, source = self._reply_session, self._voice_source
        elif self.speaker.busy:
            session, source = self._voice_session, self._voice_source
        else:
            return None
        title = self._title(session, source)
        if not title:
            return None
        return {"title": title, "color": thread_color(session or title)}

    def _threads_status(self) -> list[dict]:
        """The last few conversations heard from, newest first, with what each is doing."""
        queued = {job[6] for job in self._queue}
        asking = {p["session"] for p in self._perms.values()}
        out = []
        for session, info in sorted(self.threads.items(), key=lambda kv: -kv[1]["last"])[:4]:
            if session in asking:
                state = "permission"
            elif session == (self.question or {}).get("session"):
                state = "question"
            elif session in self.attention:
                state = "input"
            elif session == self._voice_session and self.speaker.busy:
                state = "speaking"
            elif session == self._reply_session and self._mic_phase:
                state = "listening"
            elif session in queued:
                state = "queued"
            else:
                state = ""
            title = self._title(session, info["source"])
            if not state and session in self.activity:
                state = "working"
            reply = self._turn_text(session)
            turn = self._turns.get(session) or {}
            out.append({"session": session, "title": title, "color": thread_color(session or title),
                        "state": state, "ago": int(time.time() - info["last"]),
                        "reply": " ".join(reply.split())[-160:],
                        "context": (self.usage.get(session) or {}).get("context"),
                        "tokens": (self.usage.get(session) or {}).get("tokens"),
                        "result": turn.get("result") if turn.get("done") else None})
        return out

    def thread_detail(self, session: str) -> dict:
        """A conversation's last turn in full (markdown), what it changed, and its numbers today."""
        info = self.threads.get(session)
        if info is None:
            return {}
        turn = self._turns.get(session) or {}
        title = self._title(session, info["source"])
        day = self._day()["threads"].get(session) or {}
        return {"session": session, "title": title, "color": thread_color(session or title),
                "text": strip_summary(self._turn_text(session))[-12000:], "result": turn.get("result"),
                "ago": int(time.time() - info["last"]), "turns": day.get("turns", 0),
                "tokens": (self.usage.get(session) or {}).get("tokens") or day.get("tokens", 0),
                "context": (self.usage.get(session) or {}).get("context")}

    def attach(self, paths: list[str], text: str = "", session: str = "") -> None:
        """Files (or text) dropped on the notch: typed into the conversation's input, not sent, so you can add to it."""
        session = session or self._last_session
        quoted = " ".join(f'"{p}"' if " " in p else p for p in paths)
        message = " ".join(x for x in (quoted, text.strip()) if x)
        if not message:
            return
        hwnd = self._hwnd_for(session)
        log.info("dropped %d file(s) into %r", len(paths), self._title(session))
        self._paste(message + " ", hwnd, submit=False, session=session)

    def open_thread(self, session: str) -> None:
        """Bring a conversation to the front (widget thread list)."""
        hwnd = self._hwnd_for(session)
        title = self._title_for(hwnd, session)
        if hwnd:
            winfocus.focus(hwnd)
            if title:
                claudeapp.select_session(hwnd, title)

    # -- permissions ------------------------------------------------------------
    def request_permission(self, session: str, tool: str, summary: str, source: str = "claude") -> int:
        """An agent wants to use a tool: ask out loud and in the widget; the hook polls the answer.

        Several can wait at once (parallel agents): the first is asked, the others queue in "needs you".
        """
        self._perm_seq += 1
        perm = {"id": self._perm_seq, "session": session, "tool": tool, "summary": summary, "source": source,
                "at": time.time()}
        self._perms[perm["id"]] = perm
        if session:
            self.threads[session] = {"source": source, "last": time.time()}
        self._event("needs", session, source, summary)
        self._push("permission", self._title(session, source), f"Wants to {summary}.")
        log.info("permission #%d: %s (%d waiting)", self._perm_seq, summary, len(self._perms))
        if self.permission is None:
            self._present_permission(perm)
        elif self.chime:
            self.play_chime("attention")
        return self._perm_seq

    def _present_permission(self, perm: dict) -> None:
        self.permission = perm
        if self.muted:
            return
        if self.chime:
            self.play_chime("attention")
            return
        self.speaker.stop()
        self._wake_cancel.set()
        self.speaker.speak(f"{self._title(perm['session'], perm['source'])} wants to {perm['summary']}. Allow?", "en")
        if self.transcriber is not None:
            self._reply_session = perm["session"]
            self._listen_armed = True

    def _drop_permission(self, perm_id: int) -> None:
        """Answered or given up: the next waiting request (if any) is asked."""
        self._perms.pop(perm_id, None)
        if self.permission and self.permission["id"] == perm_id:
            self.permission = None
            waiting = sorted(self._perms.values(), key=lambda p: p["at"])
            if waiting:
                self._present_permission(waiting[0])

    def answer_permission(self, perm_id: int, allow: bool) -> None:
        self._perm_decisions[perm_id] = "allow" if allow else "deny"
        log.info("permission #%d: %s", perm_id, "allowed" if allow else "denied")
        _beep(880 if allow else 440, 80)
        self._drop_permission(perm_id)

    def permission_state(self, perm_id: int, cancel: bool = False) -> str:
        """The decision for a request ("" while pending). `cancel`: the hook gave up waiting."""
        if cancel and perm_id in self._perms:
            if self.permission and self.permission["id"] == perm_id:
                self.cancel_listen()
            self._drop_permission(perm_id)
        return self._perm_decisions.pop(perm_id, "")

    def _permission_status(self) -> dict | None:
        for perm_id, p in list(self._perms.items()):
            if time.time() - p["at"] > 900:  # the hook stopped asking long ago
                self._drop_permission(perm_id)
        p = self.permission
        return {k: p[k] for k in ("id", "tool", "summary")} if p else None

    # -- updates ----------------------------------------------------------------
    def _check_update(self) -> None:
        """Tell the widget when GitHub has a newer release: a few seconds after start, then once a day."""
        if not self.config.get("update_check", True):
            return
        time.sleep(5)
        self._update_result()
        while True:
            self._fetch_release()
            time.sleep(UPDATE_EVERY)

    def _fetch_release(self) -> None:
        import urllib.request

        try:
            req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                                "User-Agent": "agent-voice"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                latest = json.loads(resp.read().decode("utf-8"))
        except Exception:  # offline, rate-limited, private repo: no news is fine
            return
        tag = str(latest.get("tag_name") or "")
        if not updater.is_newer(tag, __version__):
            return
        if self.update and self.update.get("state"):  # already downloading or installing one
            return
        asset = updater.pick_asset(latest)
        how = ("auto" if asset else "page") if updater.self_updatable() else ("page" if getattr(sys, "frozen", False) else "git")
        self._release = {"asset": asset, "tag": tag}
        self.update = {"version": tag.lstrip("v"), "url": str(latest.get("html_url") or RELEASES_PAGE),
                       "how": how, "state": ""}
        log.info("update available: %s (%s)", tag, how)

    def _update_result(self) -> None:
        """Right after an update: say how it went, and clear what it left behind."""
        result = updater.read_result()
        if updater.self_updatable():
            updater.cleanup(updater.install_dir())
        if not result:
            return
        if result.get("ok"):
            text = f"Updated to version {result.get('version')}. Your settings and voices are unchanged."
        else:
            text = (f"The update to version {result.get('version')} didn't go through: {result.get('error')}. "
                    "Nothing was changed; try again from the tray.")
        self._show_banner(text, "", "", kind="note", tag="update")

    def install_update(self) -> None:
        """The widget's Update button: download, verify and hand off to the updater (release build only)."""
        up = self.update
        if not up:
            self._show_banner("agent-voice is up to date.", "", "", kind="note", tag="update")
            return
        version = up["version"]
        if up.get("how") == "git":
            self._show_banner(f"Version {version} is out. In your agent-voice folder: git pull, then "
                              "pip install -e . and agent-voice shutdown, then agent-voice start.",
                              "", "", kind="note", tag="update")
            return
        if up.get("how") != "auto":
            self._show_banner(f"Version {version} is out: download it from the release page.",
                              "", "", kind="note", tag="update")
            return
        if up.get("state"):
            return
        up["state"] = "downloading"
        self._show_banner(f"Downloading version {version}...", "", "", kind="note", tag="update")
        try:
            def progress(frac: float) -> None:
                self._show_banner(f"Downloading version {version}... {int(frac * 100)}%", "", "",
                                  kind="note", tag="update")

            zip_path = updater.download(self._release["asset"], progress)
            self._show_banner(f"Checking version {version}...", "", "", kind="note", tag="update")
            root = updater.extract(zip_path, zip_path.parent / "build")
        except Exception as e:
            log.exception("update download failed")
            up["state"] = ""
            self._show_banner(f"Couldn't download version {version} ({e}). Nothing was changed.",
                              "", "", kind="note", tag="update")
            return
        up["state"] = "installing"  # the widget quits when it sees this
        self._show_banner(f"Installing version {version}: agent-voice restarts in a few seconds.",
                          "", "", kind="note", tag="update")
        try:
            updater.launch_apply(root, updater.install_dir(), os.getpid(), version)
        except OSError as e:
            log.exception("could not start the updater")
            up["state"] = ""
            self._show_banner(f"Couldn't start the update ({e}). Nothing was changed.", "", "",
                              kind="note", tag="update")

    @property
    def approvals(self) -> bool:
        return bool(self.state.get("approvals", True))

    def set_approvals(self, on: bool) -> None:
        # The PermissionRequest hook reads this from state.json.
        self.state["approvals"] = on
        save_state(self.state)
        log.info("approvals=%s", on)
        self._confirm("Approvals on." if on else "Approvals off.")

    # -- wake word --------------------------------------------------------------
    @property
    def wake(self) -> bool:
        return bool(self.state.get("wake"))

    def set_wake(self, on: bool) -> None:
        self.state["wake"] = on
        save_state(self.state)
        log.info("wake=%s", on)
        self._wake_cancel.set()
        self._confirm("Wake word on. Say: Hey Claude." if on else "Wake word off.")

    def _wake_loop(self) -> None:
        """While idle, listen for "Hey Claude ..." and send what follows as a new prompt.

        A tiny keyword spotter does the listening (only while someone speaks); Whisper only runs once
        it fires, to confirm the phrase and read the command.
        """
        from agent_voice.stt import is_hallucination

        phrases = list(self.config.get("wake_word", {}).get("phrases") or ["hey claude"])
        listener = None
        while True:
            time.sleep(0.3)
            if not self.wake or self.muted or self.chime or self.transcriber is None:
                continue
            # Approvals waiting don't stop it: "Hey Claude, approve Codex" answers them once the reply mic closed.
            if (self.speaker.busy or self._listening or self._listen_armed or self._ptt_active
                    or self.question or self._queue):
                continue
            try:
                if listener is None:
                    from agent_voice.stt import Transcriber
                    from agent_voice.wakeword import WakeListener

                    listener = WakeListener()
                    # Load (first time: download) the fallback model now, not on the first "Hey Claude".
                    self._wake_transcriber = Transcriber({"stt": {**self.config["stt"], "model": "tiny"}})
                    threading.Thread(target=self._wake_transcriber.warm_up, daemon=True).start()
                self._wake_cancel.clear()
                heard = listener.listen(self._wake_cancel)
            except Exception:
                log.exception("wake word unavailable")
                time.sleep(30)
                continue
            if heard is None or self._wake_cancel.is_set():
                continue
            audio, spotted = heard
            if not spotted:
                # A short phrase the spotter missed (an accent it doesn't know): the smallest Whisper
                # decides, ~0.2 s of CPU. Only a confirmed wake word goes on to the accurate model.
                if self._wake_transcriber is None:
                    from agent_voice.stt import Transcriber

                    self._wake_transcriber = Transcriber({"stt": {**self.config["stt"], "model": "tiny"}})
                # The wake phrase is English: say so, a 1-second clip is easily taken for another language.
                quick, _ = self._wake_transcriber.transcribe(audio, language="en")
                woke = strip_wake(quick, phrases) is not None
                log.info("wake fallback heard %r: %s", quick[:60], "wake" if woke else "no")
                if not woke:
                    continue
            text, lang = self.transcriber.transcribe(audio)
            command = strip_wake(text, phrases)
            if command is None or is_hallucination(text):
                log.info("wake word: not confirmed (heard %r)", text[:60])
                continue
            log.info("wake word heard (%d chars follow)", len(command))
            session = self._voice_session or self._reply_session
            if self._perms and self._voice_approval(command):  # "Hey Claude, approve Codex"
                self._heard(command, lang, "", tag="answer")
                continue
            if self.schedule_said(command, lang, session):  # "Hey Claude, at 2 pm tell Codex to run the tests"
                self._heard(command, lang, "", tag="scheduled")
                continue
            if match_recap(command):  # "Hey Claude, daily recap"
                self._heard(command, lang, "", tag="command")
                self.recap()
                continue
            if self._limited and match_resume(command):  # "Hey Claude, resume Codex when it resets"
                named = self._mentioned(command, set(self._limited))
                if self.set_resume(named or ""):
                    self._heard(command, lang, named or "", tag="command")
                continue
            handoff = parse_handoff(command)
            if handoff:  # "Hey Claude, send this to Codex and ask it to explain it"
                self.handoff(handoff[0], handoff[1], lang)
                continue
            new = parse_new_thread(command)
            if new:  # "Hey Claude, new thread: add a health check"
                self.new_thread(new[1], {"claude": "", "": ""}.get(new[0], new[0]), lang=lang)
                continue
            routed = self._routed(command)
            if routed:  # "Hey Claude, tell the notch redesign to run the tests"
                self._send_to(routed[0], routed[1], lang)
                continue
            full, expanded = self._expand(command)
            if expanded:  # "Hey Claude, commit this": a saved prompt, to the current thread
                self._heard(full, lang, session, tag="shortcut")
                _beep(880, 60)
                self._paste(full, self._hwnd_for(session), submit=True, session=session)
            elif len(command.split()) >= 3:
                self._heard(command, lang, session, tag="sent")
                _beep(880, 60)
                self._paste(command, self._hwnd_for(session), submit=True, session=session)
            else:
                # Just "Hey Claude": open the mic for the prompt, and send it.
                _beep(880, 60)
                self._reply_session = session
                self._force_submit = True
                self._auto_listen()

    def _question_status(self) -> dict | None:
        q = self.question
        if q and time.time() - q["at"] > 1800:  # stale: answered elsewhere, or abandoned
            self.question = q = None
        if not q:
            return None
        return {k: q[k] for k in ("question", "header", "options", "more", "agent")}

    def status(self) -> dict:
        return {
            "version": __version__,
            "phase": self.phase(),
            "level": round(self.level, 4),
            "speaking_lang": self.speaker.current_spec[:2] if self.speaker.busy else "",
            "caption": self.speaker.current_text if self.speaker.busy else "",
            "awaiting_reply": self._listen_armed,
            "widget_toggles": self.widget_toggles,
            "new_thread_opens": self.new_thread_opens,
            "new_targets": self.new_targets(),
            "new_default": {"target": self._default_target(), "cwd": self.state.get("last_cwd", "")},
            "last_heard": self.last_heard[-160:],
            "last_heard_lang": self.last_heard_lang,
            "history": self.history,
            "agents": self.agent_states(),
            "activity": self._activity_status(),
            "reading": self._reading_status(),
            "output": self.output,
            "in_call": self.in_call,
            "focus": self.focus if self._holding() else "",
            "phone": self._phone_status(),
            "schedules": self._schedules_status(),
            "day": self._day_status(),
            "banner": {**self.banner, "age": round(time.time() - self.banner["at"], 1)}
            if self.banner and time.time() - self.banner["at"] < 60 else None,
            "last_reply": {"session": self._last_session} if self._turn_text(self._last_session) else None,
            "question": self._question_status(),
            "needs": self._needs_status(),
            "limits": self._limits_status(),
            "usage_line": self._usage_line,
            "result": self.last_result if self.last_result and time.time() - self.last_result["at"] < 120 else None,
            "thread": self._thread_status(),
            "threads": self._threads_status(),
            "queued": len(self._queue),
            "permission": self._permission_status(),
            "update": self.update,
            "wake": self.wake,
            "desktop_apps": self.desktop_apps,
            "approvals": self.approvals,
            "muted": self.muted,
            "hands_free": self.hands_free,
            "chime": self.chime,
            "summary": self.summary,
            "speed_x": self.speed_x,
            "speaking": self.speaker.busy,
            "listening": self._listening,
            "voices": self.config["voices"],
            "hotkeys": self.config["hotkeys"],
        }

    # -- hotkeys ----------------------------------------------------------------
    def start_hotkeys(self) -> None:
        hk = self.config["hotkeys"]
        if hk.get("push_to_talk") and self.config["stt"].get("enabled"):
            from agent_voice.stt import Transcriber

            self.transcriber = Transcriber(self.config)
            threading.Thread(target=self.transcriber.warm_up, daemon=True).start()
        actions = {
            "stop": self.stop,
            "reply": self.reply_now,
            "widget": lambda: setattr(self, "widget_toggles", self.widget_toggles + 1),
            "new_thread": lambda: setattr(self, "new_thread_opens", self.new_thread_opens + 1),
            "hands_free": lambda: self.set_hands_free(not self.hands_free),
            "mute": lambda: self.set_muted(not self.muted),
            "chime": lambda: self.set_chime(not self.chime),
            "read_selection": lambda: threading.Thread(target=self._read_selection, daemon=True).start(),
            "push_to_talk": self._ptt_pressed if self.transcriber is not None else None,
        }
        try:
            for name, action in actions.items():
                if hk.get(name) and action is not None:
                    keys.add_hotkey(hk[name], action)
        except Exception:  # no `keyboard` / pynput, or macOS without the Accessibility permission
            log.exception("global hotkeys unavailable")
            return
        log.info("hotkeys registered: %s", hk)

    @staticmethod
    def _wait_modifiers_released(timeout: float = 3.0) -> None:
        end = time.time() + timeout
        while time.time() < end and keys.modifiers_down():
            time.sleep(0.02)

    def _read_selection(self) -> None:
        """Speak the selected text (any app, e.g. Antigravity), or the clipboard if nothing is selected."""
        import pyperclip

        self._wait_modifiers_released()
        previous = pyperclip.paste()
        pyperclip.copy("")
        keys.copy()
        time.sleep(0.2)
        selected = pyperclip.paste()
        pyperclip.copy(previous)
        text = selected or previous
        if text.strip():
            self.speaker.stop()
            # Explicit request: speak even when muted.
            self.speaker.speak(text)

    def _ptt_pressed(self) -> None:
        """Hotkey: record while the key is held."""
        if not self.ptt_start():  # key auto-repeat
            return
        trigger = self.config["hotkeys"]["push_to_talk"].split("+")[-1].strip()

        def watch_release():
            while keys.is_pressed(trigger):
                time.sleep(0.02)
            self.ptt_stop()

        threading.Thread(target=watch_release, daemon=True).start()

    def ptt_start(self) -> bool:
        """Start a push-to-talk recording (hotkey or widget). False if one is already running."""
        if self._ptt_active or self.transcriber is None:
            return False
        self._ptt_active = True
        self._wake_cancel.set()
        self._ptt_release.clear()
        threading.Thread(target=self._ptt_session, daemon=True).start()
        return True

    def ptt_stop(self) -> None:
        self._ptt_release.set()

    def _ptt_session(self) -> None:
        from agent_voice.stt import Recorder

        try:
            self.speaker.stop()  # you talk, the agent shuts up
            self.speaker.hold()  # ...and stays quiet until you're done
            self.cancel_listen()
            rec = Recorder(on_level=self._set_level)
            rec.start()
            self._mic_phase = "recording"
            _beep(660, 60)
            # Safety net: a lost "release" (widget closed mid-press) must not record forever.
            self._ptt_release.wait(timeout=600)
            audio = rec.stop()
            _beep(520, 60)
            self._mic_phase = "transcribing"
            text, lang = self.transcriber.transcribe(audio)
            log.info("dictation lang=%s chars=%d", lang, len(text))
            if not text:
                return
            submit = self.hands_free or bool(self.config["stt"].get("auto_submit"))
            self._heard(text, lang, "", tag="sent" if submit else "typed")  # push-to-talk goes to the focused window
            self._paste(text, submit=submit)
        except Exception:
            log.exception("push-to-talk failed")
        finally:
            self._ptt_active = False
            self.speaker.release()
            self._mic_phase = ""
            self.level = 0.0


def _clock(at: float, window: str) -> str:
    """A reset time: '15:20' today, 'Mon 09:00' further away."""
    from datetime import datetime

    when = datetime.fromtimestamp(at)
    return when.strftime("%H:%M" if window == "five_hour" and at - time.time() < 20 * 3600 else "%a %H:%M")


def status_tag(st: dict) -> str:
    """What the widget shows, as a short tag: it changes when something visible changes.

    Not the mic level (a widget that animates polls normally) nor the banner's age.
    """
    shown = {k: v for k, v in st.items() if k != "level"}
    if shown.get("banner"):
        shown["banner"] = {k: v for k, v in shown["banner"].items() if k != "age"}
    shown["threads"] = [{**t, "ago": t["ago"] // 60} for t in shown.get("threads") or []]  # "N min ago"
    return f"{zlib.crc32(json.dumps(shown, sort_keys=True, default=str).encode()):08x}"


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _handler(daemon: Daemon):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence default stderr logging
            log.debug(fmt, *args)

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if not 0 < length <= 1_000_000:  # replies are capped anyway; refuse huge bodies
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _allowed(self, post: bool) -> bool:
            """Only local programs (hooks, CLI) may talk to the daemon, never a web page.

            - Browsers always send Origin on cross-site POSTs; our client never does.
            - The Host check defeats DNS rebinding (evil.com resolving to 127.0.0.1).
            - Requiring application/json forces a CORS preflight, which we never answer.
            """
            port = self.server.server_address[1]
            host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")
            json_ok = not post or self.headers.get("Content-Type", "").startswith("application/json")
            if host_ok and json_ok and "Origin" not in self.headers:
                return True
            self._json(403, {"error": "forbidden"})
            return False

        def do_GET(self):
            if not self._allowed(post=False):
                return None
            url = urlparse(self.path)
            if url.path == "/status":
                query = parse_qs(url.query)
                if "wait" not in query:
                    st = daemon.status()
                    return self._json(200, {**st, "tag": status_tag(st)})
                # Long poll: answer as soon as something changes (or after `timeout`), so a widget at
                # rest makes one request every 20 s instead of several a second.
                wait, end = query["wait"][0], time.time() + min(30.0, float(query.get("timeout", ["20"])[0]))
                while True:
                    st = daemon.status()
                    tag = status_tag(st)
                    if tag != wait or time.time() >= end:
                        return self._json(200, {**st, "tag": tag})
                    time.sleep(0.25)
            if url.path == "/thread":  # a thread's last reply in full, for the preview on hover
                return self._json(200, daemon.thread_detail(parse_qs(url.query).get("session", [""])[0]))
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._allowed(post=True):
                return None
            data = self._body()
            if self.path == "/speak":
                # Answer immediately so hooks never wait on text processing.
                args = (
                    str(data.get("text", "")),
                    data.get("lang"),
                    str(data.get("source", "")),
                    bool(data.get("interrupt")),
                    bool(data.get("final")),
                    data.get("voice"),
                    str(data.get("session") or ""),
                    _int(data.get("hwnd")),
                    data.get("result") if isinstance(data.get("result"), dict) else None,
                )
                daemon.jobs.put(lambda: daemon.speak(*args))
                return self._json(200, {"queued": True, "muted": daemon.muted})
            if self.path == "/ask":
                options = [str(o) for o in data.get("options") or [] if str(o).strip()][:8]
                ask_id = daemon.ask(str(data.get("session") or ""), str(data.get("source") or ""),
                                    str(data.get("question") or "")[:500], options)
                return self._json(200, {"id": ask_id})
            if self.path == "/ask_state":
                return self._json(200, daemon.ask_state(_int(data.get("id")), cancel=bool(data.get("cancel"))))
            if self.path == "/usage":
                daemon.set_usage(data)
                return self._json(200, {"ok": True})
            if self.path == "/attach":
                paths = [str(p) for p in data.get("paths") or []][:20]
                args = (paths, str(data.get("text") or "")[:20000], str(data.get("session") or ""))
                threading.Thread(target=daemon.attach, args=args, daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/reply":
                daemon.reply_now(str(data.get("session") or "") or None)
                return self._json(200, {"ok": True})
            if self.path == "/send":
                args = (str(data.get("session") or ""), str(data.get("text") or "")[:20000])
                threading.Thread(target=daemon.send_text, args=args, daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/new_thread":
                target, cwd = str(data.get("target") or ""), str(data.get("cwd") or "")
                if data.get("listen"):
                    daemon.listen_new_thread(target, cwd)
                else:
                    args = (str(data.get("text") or "")[:20000], target, cwd)
                    threading.Thread(target=daemon.new_thread, args=args, daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/listen_cancel":  # typing a reply: the open mic would hear the keyboard
                daemon.cancel_listen()
                return self._json(200, {"ok": True})
            if self.path == "/bridge_seen":
                daemon.bridge_seen()
                return self._json(200, {"ok": True})
            if self.path == "/ptt":
                if data.get("action") == "stop":
                    daemon.ptt_stop()
                else:
                    daemon.ptt_start()
                return self._json(200, {"ok": True})
            if self.path == "/phone":
                action = str(data.get("action") or "")
                if action == "test":
                    return self._json(200, {"ok": daemon.phone_test(), **daemon._phone_status()})
                if action == "new":
                    return self._json(200, daemon.new_phone_topic())
                return self._json(200, daemon.set_phone(bool(data.get("on", True))))
            if self.path == "/schedule":
                at = data.get("at")
                if not isinstance(at, (int, float)):
                    at = schedule.parse_when(str(data.get("when") or ""), time.time())
                session = str(data.get("session") or "")
                if data.get("thread") and not session:
                    session = daemon._find_thread(str(data["thread"])) or ""
                    if not session:
                        return self._json(400, {"error": f"no thread called {data['thread']}"})
                new = data.get("new") if isinstance(data.get("new"), dict) else None
                if not at:
                    return self._json(400, {"error": "when? (14:00, 2pm, in 20 minutes, tomorrow at 9)"})
                if new is None and not session:
                    session = daemon._last_session
                try:
                    item = daemon.add_schedule(str(data.get("prompt") or ""), float(at), session,
                                               {"target": str(new.get("target") or ""), "cwd": str(new.get("cwd") or "")}
                                               if new is not None else None, str(data.get("repeat") or ""))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                return self._json(200, item)
            if self.path == "/schedule_cancel":
                return self._json(200, {"ok": daemon.cancel_schedule(_int(data.get("id")))})
            if self.path == "/recap":
                daemon.recap(spoken=bool(data.get("speak", True)))
                return self._json(200, {"text": daemon.recap_text()[0]})
            if self.path == "/resume":
                ok = daemon.set_resume(str(data.get("session") or ""), bool(data.get("on", True)))
                return self._json(200, {"ok": ok})
            if self.path == "/limit_hit":
                daemon.limit_hit(str(data.get("session") or ""), str(data.get("source") or ""),
                                 str(data.get("text") or "")[:400])
                return self._json(200, {"ok": True})
            if self.path == "/usage_line":
                threading.Thread(target=daemon.usage_line, args=(bool(data.get("enable", True)),), daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/install":
                agent = str(data.get("agent") or "")
                if agent not in install.INSTALLERS:
                    return self._json(400, {"error": "unknown agent"})
                threading.Thread(target=daemon.install_agent, args=(agent,), daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/replay":
                session = str(data.get("session") or "")
                daemon.jobs.put(lambda: daemon.replay(session))
                return self._json(200, {"ok": True})
            if self.path == "/read":
                threading.Thread(target=daemon._read_selection, daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/prompt":
                daemon.prompt_submitted(_int(data.get("hwnd")), str(data.get("session") or ""),
                                        str(data.get("source") or ""), str(data.get("title") or "")[:80],
                                        str(data.get("cwd") or ""))
                return self._json(200, {"ok": True})
            if self.path == "/activity":
                if data.get("done"):
                    daemon.activity_done(str(data.get("session") or ""))
                else:
                    daemon.tool_used(str(data.get("session") or ""), str(data.get("source") or ""),
                                     str(data.get("label") or ""))
                return self._json(200, {"ok": True})
            if self.path == "/output":
                mode = str(data.get("mode") or "")
                if mode not in ("voice", "chime", "silent"):
                    return self._json(400, {"error": "mode must be voice, chime or silent"})
                daemon.set_output(mode)
                return self._json(200, {"output": daemon.output})
            if self.path == "/copy_reply":
                return self._json(200, {"ok": daemon.copy_reply(str(data.get("session") or ""))})
            if self.path == "/stop":
                daemon.stop()
                return self._json(200, {"ok": True})
            if self.path == "/speed":
                try:
                    daemon.set_speed(float(data.get("value")))
                except (TypeError, ValueError):
                    return self._json(400, {"error": "value must be a number"})
                return self._json(200, {"speed_x": daemon.speed_x})
            if self.path == "/summary":
                mode = data.get("mode", "toggle")
                daemon.set_summary(not daemon.summary if mode == "toggle" else mode == "on")
                return self._json(200, {"summary": daemon.summary})
            if self.path == "/handsfree":
                mode = data.get("mode", "toggle")
                daemon.set_hands_free(not daemon.hands_free if mode == "toggle" else mode == "on")
                return self._json(200, {"hands_free": daemon.hands_free})
            if self.path == "/chime":
                mode = data.get("mode", "toggle")
                daemon.set_chime(not daemon.chime if mode == "toggle" else mode == "on")
                return self._json(200, {"chime": daemon.chime})
            if self.path == "/notify":
                # Queued behind speech, so a question is never cleared by an older progress message.
                kind = str(data.get("kind", "attention"))
                daemon.jobs.put(lambda: daemon.notify(kind, data))
                return self._json(200, {"ok": True})
            if self.path == "/permission":
                perm_id = daemon.request_permission(str(data.get("session") or ""), str(data.get("tool") or ""),
                                                    str(data.get("summary") or "use a tool"),
                                                    str(data.get("source") or "claude"))
                return self._json(200, {"id": perm_id})
            if self.path == "/permission_state":
                decision = daemon.permission_state(int(data.get("id") or 0), cancel=bool(data.get("cancel")))
                return self._json(200, {"decision": decision})
            if self.path == "/permission_answer":
                daemon.answer_permission(int(data.get("id") or 0), bool(data.get("allow")))
                return self._json(200, {"ok": True})
            if self.path == "/open_thread":
                threading.Thread(target=daemon.open_thread, args=(str(data.get("session") or ""),), daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/approvals":
                mode = data.get("mode", "toggle")
                daemon.set_approvals(not daemon.approvals if mode == "toggle" else mode == "on")
                return self._json(200, {"approvals": daemon.approvals})
            if self.path == "/apps":
                mode = data.get("mode", "toggle")
                daemon.set_desktop_apps(not daemon.desktop_apps if mode == "toggle" else mode == "on")
                return self._json(200, {"desktop_apps": daemon.desktop_apps})
            if self.path == "/wake":
                mode = data.get("mode", "toggle")
                daemon.set_wake(not daemon.wake if mode == "toggle" else mode == "on")
                return self._json(200, {"wake": daemon.wake})
            if self.path == "/answer":
                index = data.get("index")
                kwargs = {"index": int(index)} if isinstance(index, int) else {"text": str(data.get("text") or "")}
                threading.Thread(target=daemon.answer, kwargs=kwargs, daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/mute":
                mode = data.get("mode", "toggle")
                daemon.set_muted(not daemon.muted if mode == "toggle" else mode == "on")
                return self._json(200, {"muted": daemon.muted})
            if self.path == "/update":
                threading.Thread(target=daemon.install_update, name="update-install", daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return None
            self._json(404, {"error": "not found"})

    return Handler


def _launch_widget() -> None:
    """Start the status widget as its own process (it exits on its own if one is already open)."""
    import os
    import subprocess

    from agent_voice.client import self_command

    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(self_command("widget"), **kwargs)
    except OSError:
        log.exception("could not start the widget")


def serve(foreground: bool = True) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_DIR / "server.log", encoding="utf-8")]
    if foreground and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", handlers=handlers)

    # Taken before anything heavy: hooks racing to auto-start the daemon must not leave extra copies
    # holding Whisper and global hotkeys.
    lock = InstanceLock()
    if not _acquire_instance(lock):
        log.info("another agent-voice daemon is running (pid %s): exiting", os.getpid())
        return 1
    config = load_config()
    try:
        httpd = _Server((config["host"], int(config["port"])), None)
    except OSError:
        log.error("port %s busy: agent-voice is probably already running", config["port"])
        lock.release()
        return 1
    daemon = Daemon(config)
    httpd.RequestHandlerClass = _handler(daemon)
    daemon.speaker.warm_up()
    daemon.start_hotkeys()
    if config.get("widget", {}).get("enabled", True):
        _launch_widget()
    log.info("agent-voice %s listening on %s:%s (pid %s)", __version__, config["host"], config["port"], os.getpid())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            keys.unhook_all()
        except Exception:
            pass
        lock.release()
        log.info("agent-voice stopped (pid %s)", os.getpid())
    return 0
