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

from agent_voice import (__version__, agenda, calls, claudeapp, companion, devices, history, media, install, keys, launch, ntfy,
                         outputs, quota, schedule, textclean, updater, usage, winfocus)
from agent_voice.config import HOME, LOG_DIR, LOOPBACK, load_config, load_state, save_state
from agent_voice.hooks import strip_summary
from agent_voice.textclean import result_label
from agent_voice.i18n import action, day_time, plural, tr
from agent_voice.i18n import set_language as set_ui_language
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


def _beep(freq: int) -> None:
    """A short, soft cue (the mic opens, it heard you, it didn't): a gentle tone, not the system's square-wave beep.

    High asks rise (the mic opens, yes), low ones fall (nothing heard, no, not sent); played on a stream of
    its own, so a cue never cuts the voice off.
    """
    if freq >= 800:
        notes = ((freq * 0.75, 0.0), (freq, 0.06))  # up: listening, done, yes
    elif freq >= 500:
        notes = ((freq, 0.0),)
    else:
        notes = ((max(freq, 330.0) * 1.25, 0.0), (max(freq, 330.0), 0.07))  # down: nothing, no, not sent
    try:
        audio, rate = tone_audio(notes, 0.16, decay=14.0, tail=0.18)
    except Exception:
        return
    threading.Thread(target=_play_apart, args=(audio, rate), daemon=True).start()


def _play_apart(audio, rate: int) -> None:
    """Play on a stream of its own (sounddevice's play() would stop the voice playing through it)."""
    try:
        import sounddevice as sd

        with sd.OutputStream(samplerate=rate, channels=1, dtype="float32", device=devices.output_device()) as stream:
            stream.write(audio.reshape(-1, 1))
    except Exception:
        log.debug("cue not played", exc_info=True)


SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0)
RELEASES_API = f"https://api.github.com/repos/{updater.REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{updater.REPO}/releases/latest"
UPDATE_EVERY = 3600  # re-check for a release every hour (daemons stay up for weeks; one small request)

# Chime mode: (frequency Hz, start s) notes, a soft mallet sound. "done" settles on a fifth (C5, G5); "attention"
# asks, a little higher and twice (G5 then C6, repeated).
CHIMES = {
    "done": ((523.25, 0.0), (783.99, 0.11)),
    "attention": ((783.99, 0.0), (1046.5, 0.12), (783.99, 0.42), (1046.5, 0.54)),
}


def tone_audio(notes, volume: float = 0.35, rate: int = 44100, decay: float = 7.0, tail: float = 0.6):
    """Soft mallet tones: a sine with a gentle attack, a warm low partial that fades first, a smooth tail.

    No harmonics above the note and no clicks (eased in and out): nothing shrill, even on laptop speakers.
    """
    import numpy as np

    length = notes[-1][1] + tail
    t = np.arange(int(rate * length)) / rate
    out = np.zeros_like(t, dtype=np.float32)
    for freq, start in notes:
        u = t - start
        on = u >= 0
        uu = np.clip(u, 0, None)
        attack = np.minimum(uu / 0.012, 1.0) ** 2
        body = np.sin(2 * np.pi * freq * uu) * np.exp(-uu * decay)
        warmth = 0.18 * np.sin(np.pi * freq * uu) * np.exp(-uu * decay * 2.2)  # an octave below, brief
        out += np.where(on, attack * (body + warmth), 0.0).astype(np.float32)
    fade = int(rate * 0.03)
    out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * max(0.0, min(1.0, volume))).astype(np.float32), rate


def chime_audio(kind: str, volume: float = 0.35, rate: int = 44100):
    return tone_audio(CHIMES.get(kind, CHIMES["done"]), volume, rate)


# Spoken answers to a question: "the second one", "option 2", "la deuxième"...
_ORDINALS = {
    1: ("1", "one", "first", "un", "une", "premier", "première", "premiere"),
    2: ("2", "two", "second", "deux", "deuxième", "deuxieme", "seconde"),
    3: ("3", "three", "third", "trois", "troisième", "troisieme"),
    4: ("4", "four", "fourth", "quatre", "quatrième", "quatrieme"),
    5: ("5", "five", "fifth", "cinq", "cinquième", "cinquieme"),
}


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
COMPANION = "__companion__"  # ... and what's said into the companion's mic


def parse_new_thread(text: str) -> tuple[str, str] | None:
    """'New thread with Codex: add a health check' -> ('codex', 'Add a health check'); None otherwise."""
    m = _NEW_THREAD.match(text.strip())
    if not m or len(m["prompt"].split()) < 2:
        return None
    agent = (m["agent"] or "").lower().split()[0] if m["agent"] else ""
    prompt = m["prompt"].strip()
    return agent, prompt[:1].upper() + prompt[1:]


def _new_target(agent: str) -> str:
    """Where a dictated new thread starts: the agent it names; Claude (or none) means where you last worked."""
    return "" if agent in ("", "claude") else agent


def _voice_line(text: str) -> str:
    """The `Voice summary:` line an agent ended its reply with, '' when it wrote none."""
    m = re.search(r"voice summary:\s*(.+)", text or "", re.I)
    return m.group(1).strip() if m else ""


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


# Said while a dictated message waits to be sent: keep it to fix it, drop it, or send it now.
_TAKE_BACK = {
    "hold": re.compile(r"^(?:wait|hold on|hold it|hang on|stop|edit|edit it|let me (?:fix|edit|change) (?:it|that)|"
                       r"attends?|attendez|pas encore|modifie|je corrige)$"),
    "cancel": re.compile(r"^(?:cancel(?: that| it)?|never ?mind|don'?t send(?: it| that)?|scratch that|delete (?:it|that)|"
                         r"forget it|annule|annule ça|laisse tomber|n'envoie pas|oublie)$"),
    "send": re.compile(r"^(?:send(?: it)?(?: now)?|go|go ahead|yes|ok send it|envoie(?:-le)?|vas-y|oui)$"),
}


# Said over the voice to make it stop (and then correct it): not a reply in itself.
_INTERRUPT = re.compile(r"^(?:stop|stop it|stop talking|wait|no|no no|no wait|wait wait|hold on|hang on|enough|that's enough|"
                        r"shut up|quiet|be quiet|ok stop|okay stop|actually|arrête|arrête-toi|stop stop|attends|non|"
                        r"non attends|chut|tais-toi|ça suffit|en fait)$")


def match_interrupt(text: str) -> bool:
    """'No, wait!' -> True: you cut the voice off to say something else, so the mic opens for it."""
    plain = re.sub(r"[^\w\s'-]", "", text.lower().replace("’", "'"))
    return bool(_INTERRUPT.match(" ".join(plain.split())))


def match_take_back(text: str) -> str | None:
    """'Wait!' -> 'hold', 'cancel that' -> 'cancel', 'send it' -> 'send'; None for anything else (more words)."""
    plain = re.sub(r"[^\w\s'-]", "", text.lower().replace("’", "'")).strip()
    plain = re.sub(r"^(?:no|non|oh|uh|euh|please|ok|okay)\s+|\s+(?:please|s'il te pla[iî]t)$", "", plain)
    return next((name for name, rx in _TAKE_BACK.items() if rx.match(plain)), None)


# "Look at this", "check this error and tell me what's wrong", "regarde ça" -> a picture of the window in front.
_LOOK = (
    re.compile(r"^(?:please\s+)?(?:look at|have a look at|take a look at|check(?: out)?|see)\s+(?:this|that|my screen|"
               r"the screen|this window|this page|this error|that error|what i'?m looking at)\b(?P<rest>.*)$", re.I | re.S),
    re.compile(r"^(?:regarde|vois|jette un (?:œil|oeil) (?:à|a))\s+(?:ça|ca|ceci|cela|mon écran|l'écran|cette fenêtre|"
               r"cette page|cette erreur)\b(?P<rest>.*)$", re.I | re.S),
)


def parse_look(text: str) -> str | None:
    """'Look at this and tell me why it fails' -> 'Tell me why it fails'; 'look at this' -> ''; None otherwise."""
    text = text.strip()
    for rx in _LOOK:
        m = rx.match(text)
        if m:
            rest = re.sub(r"^[\s,.:;!?-]*(?:(?:and|et)\s+)?", "", m["rest"]).strip()
            return rest[:1].upper() + rest[1:]
    return None


def match_resume(text: str) -> bool:
    return bool(_RESUME.search(text)) and len(text.split()) <= 12


def match_recap(text: str) -> bool:
    return bool(_RECAP.match(_plain(text).replace(" s ", "'s ")) or _RECAP.match(text.strip().lower()))


_BRIEF = re.compile(r"^(?:give me\s+)?(?:the\s+|my\s+)?(?:morning\s+brief(?:ing)?|brief(?:ing)?|what'?s (?:on )?my day|"
                    r"what does my day look like|(?:my )?plan for today|(?:le |mon )?brief(?: du matin)?|"
                    r"(?:c'?est )?quoi (?:ma journée|mon programme)|(?:le |mon )?programme du jour)\W*$", re.I)
_JOIN = re.compile(r"^(?:please\s+)?(?:(?:join|open|start)\s+(?:the\s+|my\s+)?(?:next\s+)?(?:meeting|call|teams|zoom|meet)"
                   r"(?:\s+(?:call|meeting))?|(?:rejoin(?:dre|s)?|lance(?:r)?|ouvr(?:e|ir))\s+(?:la\s+|ma\s+)?"
                   r"(?:prochaine\s+)?(?:réunion|reunion|visio|teams|zoom))\W*$", re.I)


def match_brief(text: str) -> bool:
    """'Morning brief', "what's my day", 'brief du matin'."""
    return bool(_BRIEF.match(_plain(text).replace(" s ", "'s ")) or _BRIEF.match(text.strip().lower()))


def match_join(text: str) -> bool:
    """'Join my meeting', 'rejoins la réunion'."""
    return bool(_JOIN.match(" ".join(text.strip().split())))


def spoken_number(words: set[str], most: int) -> int | None:
    """'two', '2', 'the second one' -> 2 (up to `most`); 'one' alone only when nothing else is said."""
    found = [n for n, keys in _ORDINALS.items() if n <= most and any(k in words for k in keys)]
    if len(found) > 1:
        found = [n for n in found if n != 1 or not words & {"one", "un", "une"}]
    return found[0] if found else None


_ALL = {"all", "everything", "everyone", "both", "tout", "tous", "toutes", "les deux"}


def _names(label: str) -> set[str]:
    return {w for w in _plain(label).split() if w not in _FILLER and len(w) > 1}


def names_match(said: str, label: str) -> bool:
    """Does `said` ("codex", "the notch one") name the conversation called `label`?"""
    want, have = _names(said), _names(label)
    return bool(want) and want <= have


def _join(items: list[str], in_lang: str | None = None) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + tr(" and ", in_lang=in_lang) + items[-1]


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
        n = spoken_number(set(words), len(options))
        if n:
            return n - 1
    return None


class Daemon:
    def __init__(self, config: dict, start_threads: bool = True):
        self.config = config
        self.state = load_state()
        if self.state.get("speed_x"):
            # The speaker reads config["speed"] (Piper length_scale) for every sentence.
            config["speed"] = 1.0 / float(self.state["speed_x"])
        # The voice engine: Piper (light, the default) or Kokoro (natural); Piper's voices are kept to go back to.
        self._piper_voices = dict(config["voices"])
        self._engine_state = ""  # "downloading 40%" while the natural voice is fetched
        self._apply_engine()
        # Your language (Settings): an unsure detection falls back to it, for the voice and for what you say.
        self._apply_language(self.state.get("language") or config.get("default_lang", "en"))
        set_ui_language(self.state.get("ui_language") or config.get("ui_language") or "en")
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
        self._transcript_titles = claudeapp.TranscriptTitles()
        self._recent_said: dict[str, tuple[float, str]] = {}  # what was just said (and by whom): never twice
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
        self._call_paused = False  # we paused the music when the call started: play it again when it ends
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
        # Other apps' sound, lowered under the voice and while you reply (a worker: fades never hold the mic loop).
        self._ducker = None
        self._duck_state = ""
        self._duck_idle = 0.0
        self._duck_jobs: queue.Queue = queue.Queue()
        self._schedules: list[dict] = [s for s in self.state.get("schedules") or [] if isinstance(s, dict)]
        self._limits_at: dict[str, float] = {}  # when each agent's numbers were measured (newest wins)
        # Claude's usage from its transcripts: an estimate between real readings (the desktop app reports none).
        self._ledger = usage.Ledger()
        self._estimator = usage.Estimator(self._ledger, self.state.get("claude_usage"))
        self._claude_est: dict[str, dict] = {}
        self._pace: dict[tuple[str, str], list[tuple[float, float, int]]] = {}  # (at, pct, reset) per window: its pace
        self._claude_asked = 0.0  # the last time we ran `claude -p /usage`
        self._claude_seen = 0.0  # ~/.claude.json's mtime when last read
        self._codex_seen: tuple = (None, 0.0)  # Codex's latest log and its mtime when last read
        self._limits_lock = threading.Lock()
        self._limits_refreshing = False
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
        self.widget_opens = 0  # bumped by `agent-voice open` (the Start menu): the widget shows its panel
        self.boot = int(time.time() * 1000)
        # A dictated message waiting a few seconds before it's sent (the take-back), and what was said about it.
        self._pending: dict | None = None
        self._pending_seq = 0
        self._pending_event = threading.Event()
        # Headphones or speakers: on speakers (if you asked), chimes instead of the voice. You can still pick Voice.
        self._output = outputs.OutputWatch()
        self._speaker_override = False
        # Which mic and speaker (Settings), "" for the system's. A mic that sends nothing is skipped, and said.
        devices.mic, devices.speaker = self.mic, self.speaker_device
        self._output.preferred = self.speaker_device
        devices.on_silent = self._mic_silent
        devices.safe_to_refresh = lambda: not self.speaker.busy
        self._mic_silent_at = 0.0
        # Talking over the voice (barge-in): the mic listens while it speaks, in headphones (no echo to hear).
        # Every prompt and reply, searchable (a local SQLite file); None when `history.enabled` is off.
        hist = config.get("history", {})
        self.history_db = history.History(days=float(hist.get("days", 90))) if hist.get("enabled", True) else None
        self._barge_cancel = threading.Event()
        self._barging = False  # listening for you while it speaks
        self._barged = False  # ... and you spoke
        # The companion beside the panel: timers, the stopwatch, the to-do list, reminders (kept in state.json).
        self.companion = companion.Companion(self.state.get("companion"), self._save_companion,
                                             config.get("pomodoro"))
        self.media: dict | None = None  # what's playing (Spotify, YouTube...): the companion's player
        self.agenda: list[dict] = []  # today's meetings (calendar links you added, Outlook while it's open)
        self._agenda_error = ""
        self._agenda_wake = threading.Event()
        if start_threads:
            threading.Thread(target=self._job_loop, name="jobs", daemon=True).start()
            threading.Thread(target=self._listen_monitor, name="listen", daemon=True).start()
            threading.Thread(target=self._wake_loop, name="wake", daemon=True).start()
            threading.Thread(target=self._check_update, name="update", daemon=True).start()
            threading.Thread(target=self._call_watch, name="calls", daemon=True).start()
            threading.Thread(target=self._away_watch, name="away", daemon=True).start()
            threading.Thread(target=self._apps_watch, name="apps", daemon=True).start()
            threading.Thread(target=self._quota_watch, name="quota", daemon=True).start()
            threading.Thread(target=self._duck_loop, name="ducking", daemon=True).start()
            threading.Thread(target=self._barge_loop, name="barge-in", daemon=True).start()
            threading.Thread(target=self._ensure_start_menu, name="start-menu", daemon=True).start()
            threading.Thread(target=self._companion_loop, name="companion", daemon=True).start()
            threading.Thread(target=self._media_loop, name="media", daemon=True).start()
            threading.Thread(target=self._agenda_loop, name="agenda", daemon=True).start()

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
            self._confirm("Speed {x}.", x=f"{x:g}")

    @property
    def chime(self) -> bool:
        """Chimes instead of the voice: chosen, automatic while you're in a call or in focus, or on speakers."""
        return bool(self.state.get("chime")) or bool(self.focus) or self.on_speakers

    @property
    def engine(self) -> str:
        """'piper' (light) or 'kokoro' (natural)."""
        return str(self.state.get("engine") or self.config.get("engine") or "piper")

    def _apply_engine(self) -> None:
        """The voices the speaker uses: Kokoro's for the languages it has (when it's on and ready), Piper's else."""
        from agent_voice import kokoro
        from agent_voice.tts import KOKORO

        voices = dict(self._piper_voices)
        if self.engine == "kokoro" and kokoro.available() and kokoro.ready():
            chosen = {**kokoro.DEFAULT_VOICES, **(self.config.get("kokoro", {}).get("voices") or {})}
            voices.update({lang: KOKORO + name for lang, name in chosen.items() if lang in voices})
        self.config["voices"] = voices

    @property
    def language(self) -> str:
        return self.config.get("default_lang", "en")

    def _apply_language(self, code: str) -> None:
        from agent_voice import lang as langmod

        code = code if code in ("en", "fr") else "en"
        self.config["default_lang"] = code  # the voice's fallback (tts reads it for every reply)
        self.config.setdefault("stt", {})["prefer"] = code  # what an unsure transcription takes
        langmod.prefer(code)  # text detection's fallback

    def set_language(self, code: str) -> None:
        """English or French: the language you speak and hear most."""
        self._apply_language(code)
        self.state["language"] = self.language
        save_state(self.state)
        log.info("language=%s", self.language)
        self._confirm("English it is.")

    @property
    def ui_language(self) -> str:
        return self.state.get("ui_language") or self.config.get("ui_language") or "en"

    def set_ui_language(self, code: str) -> None:
        """English or French for what's shown: the notch, its notifications, the phone's (the widget relaunches)."""
        self.state["ui_language"] = "fr" if code == "fr" else "en"
        save_state(self.state)
        set_ui_language(self.ui_language)
        log.info("ui_language=%s", self.ui_language)

    def set_engine(self, name: str) -> None:
        """Light (Piper) or natural (Kokoro): the natural voice is downloaded the first time (~120 MB)."""
        from agent_voice import kokoro

        if name == "kokoro" and not kokoro.available():
            self._show_banner(tr("The natural voice needs the kokoro-onnx package: pip install kokoro-onnx."), "", "",
                              kind="note", tag="voice")
            return
        self.state["engine"] = name
        save_state(self.state)
        log.info("engine=%s", name)
        if name == "kokoro" and not kokoro.ready():
            if not self._engine_state:
                threading.Thread(target=self._fetch_kokoro, name="kokoro", daemon=True).start()
            return
        self._apply_engine()
        self.speaker.stop()
        self._confirm("This is the natural voice." if name == "kokoro" else "This is the light voice.")

    def _fetch_kokoro(self) -> None:
        from agent_voice import kokoro

        def progress(frac: float) -> None:
            self._engine_state = f"downloading {int(frac * 100)}%"

        self._engine_state = "downloading 0%"
        self._show_banner(tr("Downloading the natural voice (about 120 MB, once)..."), "", "", kind="note", tag="voice")
        try:
            kokoro.download(progress)
            kokoro.engine()  # loaded now, not on the first sentence
        except Exception as exc:
            log.exception("kokoro download failed")
            self.state["engine"] = "piper"
            save_state(self.state)
            self._show_banner(tr("Couldn't get the natural voice ({why}). Still the light one.", why=exc), "", "", kind="note",
                              tag="voice")
            return
        finally:
            self._engine_state = ""
        if self.engine == "kokoro":
            self._apply_engine()
            self._confirm("This is the natural voice.")

    @property
    def mic(self) -> str:
        """The microphone picked in Settings (its name), "" for the system's default."""
        return str(self.state.get("mic") or "")

    @property
    def speaker_device(self) -> str:
        """The speaker picked in Settings (its name), "" for the system's default."""
        return str(self.state.get("speaker") or "")

    def set_device(self, kind: str, name: str) -> None:
        """kind "mic" or "speaker"; name "" for the system's default."""
        self.state[kind] = name
        save_state(self.state)
        if kind == "mic":
            devices.mic = name
        else:
            devices.speaker = self._output.preferred = name
            self._check_output()  # headphones or speakers: the new device's
        log.info("%s=%s", kind, name or "system default")

    def _mic_silent(self) -> None:
        """No mic sent any sound: say so on the notch, at most every 10 minutes (the wake word retries quietly)."""
        if time.time() - self._mic_silent_at < 600:
            return
        self._mic_silent_at = time.time()
        self._show_banner(tr("The microphone sends no sound. Check it isn't muted or held by another app, or pick "
                             "another one in Settings."), "", "", kind="note", tag="mic")

    @property
    def headphones_only(self) -> bool:
        """The voice only in headphones: through speakers, chimes and banners instead (everyone around would hear)."""
        if "headphones_only" in self.state:
            return bool(self.state["headphones_only"])
        return bool(self.config.get("headphones", {}).get("only", False))

    def set_headphones_only(self, on: bool) -> None:
        self.state["headphones_only"] = on
        save_state(self.state)
        self._speaker_override = False
        log.info("headphones_only=%s", on)
        if self.on_speakers:
            self.stop()
            self.play_chime("done")
        else:
            self._confirm("Voice in headphones only." if on else "Voice on any speaker.")

    @property
    def on_speakers(self) -> bool:
        """The voice is kept off right now because the sound would come out of speakers."""
        return self.headphones_only and self._output.kind == "speakers" and not self._speaker_override

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
        if cfg.get("enabled", True) and self._focus_session():
            return "focus session"
        if cfg.get("enabled", True) and self.config.get("calendar", {}).get("quiet_in_meetings") and self._meeting():
            return "meeting"
        return ""

    def _focus_session(self) -> bool:
        """The Pomodoro's focus is running (and holds replies until the break)."""
        pomo = self.companion.data.get("pomodoro") or {}
        return bool(self.config.get("pomodoro", {}).get("hold", True) and pomo.get("phase") == "focus"
                    and pomo.get("end"))

    def _meeting(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        return next((e for e in self.agenda if not e["all_day"] and e["start"] <= now < e["end"]), None)

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

    def _confirm(self, text: str, **values) -> None:
        """Say a setting change out loud, in your speech language, unless the voice is off (chimes, silent)."""
        if not self.quiet:
            self.speaker.stop()
            self.speaker.speak(tr(text, in_lang=self.language, **values), self.language)

    def _say(self, text: str, **values) -> None:
        """Something the app says itself (not an agent's reply): in your speech language."""
        self.speaker.speak(tr(text, in_lang=self.language, **values), self.language)

    def set_chime(self, on: bool) -> None:
        self.state["chime"] = on
        save_state(self.state)
        log.info("chime=%s", on)
        if self.focus and not on:
            self._call_override = True  # you asked for the voice during a call (or full screen): keep it
        if self.on_speakers and not on:
            self._speaker_override = True  # the voice on speakers anyway, until the device changes
        self.stop()  # no voice and no open mic in chime mode
        if on:
            self.play_chime("done")
        else:
            self._confirm("Voice mode.")

    def set_output(self, mode: str) -> None:
        """voice | chime | silent (Do not disturb): one choice instead of separate mute and chime switches.

        Do not disturb keeps the chime choice, so turning it off goes back to Voice or Chimes as you had it."""
        if mode == "silent":
            self.set_muted(True)
            return
        if self.muted:
            self.state["muted"] = False
        self.set_chime(mode == "chime")

    def play_chime(self, kind: str) -> None:
        if self.muted:
            return
        try:
            audio, rate = chime_audio(kind, float(self.config.get("chime", {}).get("volume", 0.35)))
            threading.Thread(target=_play_apart, args=(audio, rate), daemon=True).start()
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
            _beep(300)
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
        self._push("needs", title, text or tr("Waiting for you"))
        log.info("%s needs you", title)
        if not announce or self.muted:
            return
        self._show_banner(text or tr("Waiting for you"), session, source, kind="needs", tag="needs you")
        if self.chime:
            self.play_chime("attention")
        else:
            self._wake_cancel.set()
            self._say("{title} needs you.", title=title)
            if text and len(text) < 160:
                self.speaker.speak(text)

    def _settled(self, session: str) -> None:
        """The conversation moved on (it talks, works, or you wrote to it): it no longer waits on you."""
        self.attention.pop(session, None)

    def _needs_status(self) -> list[dict]:
        """Everything waiting on you, oldest first: approvals, a question, agents waiting for input."""
        items = []
        for p in sorted(self._perms.values(), key=lambda p: p["at"]):
            items.append({"kind": "permission", "id": p["id"], "session": p["session"], "source": p["source"],
                          "text": action(p["summary"]), "at": p["at"]})
        q = self.question
        if q:
            items.append({"kind": "question", "session": q["session"], "source": q["agent"], "text": q["question"],
                          "at": q["at"]})
        now = time.time()
        self.attention = {s: a for s, a in self.attention.items() if now - a["at"] < 3600}
        for session, a in self.attention.items():
            items.append({"kind": "input", "session": session, "source": a["source"],
                          "text": a["text"] or tr("Waiting for you"), "at": a["at"]})
        for session, info in self._limited.items():
            when = (tr("about ") if info.get("guess") else "") + _clock(info["resets"], "five_hour")
            items.append({"kind": "limit", "session": session, "source": info["source"], "resume": info["resume"],
                          "text": tr("Out of quota · resumes {when}" if info["resume"] else "Out of quota until {when}",
                                     when=when),
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

    def _send_spoken(self, text: str, hwnd: int, session: str, lang: str) -> None:
        """A dictated message that sends itself: shown under the notch for a few seconds first, while the mic
        listens for "wait" (it's typed in, not sent, to fix it), "cancel that" or "send it"; more words are added."""
        seconds = float(self.config.get("take_back", {}).get("seconds", 5) or 0)
        if seconds <= 0:
            self._paste(text, hwnd, submit=True, session=session)
            return
        self._pending_seq += 1
        p = {"id": self._pending_seq, "text": text, "session": session, "until": time.time() + seconds, "action": ""}
        self._pending = p
        self._pending_event.clear()
        self._show_pending(p)
        try:
            self._await_take_back(p, seconds)
        finally:
            self._pending = None
        action = p["action"] or ("keep" if self._pending_event.is_set() else "send")
        source = self.threads.get(session, {}).get("source", "") if session else ""
        log.info("take-back: %s", action)
        if action == "send":
            self._show_banner(p["text"], session, source, kind="you", tag="sent")
            self._paste(p["text"], hwnd, submit=True, session=session)
        elif action == "hold":  # typed into its input, not sent: fix it there and press Enter
            self._show_banner(p["text"], session, source, kind="you", tag="not sent: fix it, then Enter")
            self._paste(p["text"], hwnd, submit=False, session=session)
        elif action == "cancel":
            self._show_banner(p["text"], session, source, kind="you", tag="cancelled")
            _beep(400)
        else:  # interrupted (Stop, a prompt you typed yourself): nothing sent, nothing lost
            import pyperclip

            pyperclip.copy(p["text"])
            self._show_banner(p["text"], session, source, kind="you", tag="not sent · on the clipboard")

    def _show_pending(self, p: dict) -> None:
        source = self.threads.get(p["session"], {}).get("source", "") if p["session"] else ""
        self._show_banner(p["text"], p["session"], source, kind="pending", tag="sending")
        if self.banner:
            self.banner.update(pending=p["id"], until=p["until"])

    def _await_take_back(self, p: dict, seconds: float) -> None:
        """Until the message goes: listen for what you say about it (or a click on Send, Edit, Cancel)."""
        from agent_voice.stt import is_hallucination, listen_for_speech

        if self.transcriber is None:  # no mic: the buttons only
            self._pending_event.wait(max(0.0, p["until"] - time.time()))
            return
        cfg = self.config["auto_listen"]
        listening, self._listening = self._listening, True
        self.speaker.hold()
        try:
            while not p["action"] and not self._pending_event.is_set():
                left = p["until"] - time.time()
                if left <= 0:
                    return
                self._mic_phase = "waiting"
                audio = listen_for_speech(left, float(cfg["silence_seconds"]), float(cfg["max_seconds"]),
                                          self._pending_event, on_level=self._set_level,
                                          on_start=lambda: setattr(self, "_mic_phase", "recording"))
                if audio is None or p["action"] or self._pending_event.is_set():
                    return
                self._mic_phase = "transcribing"
                said, _ = self.transcriber.transcribe(audio)
                if not said or is_hallucination(said):
                    continue
                what = match_take_back(said)
                if what:
                    p["action"] = what
                    return
                p["text"] = f"{p['text']} {said}".strip()  # you went on: it's part of the message
                p["until"] = time.time() + seconds
                self._show_pending(p)
        finally:
            self.speaker.release()
            self._listening = listening
            self._mic_phase = ""
            self.level = 0.0

    def pending_action(self, pending_id: int, action: str) -> bool:
        """The banner's Send, Edit and Cancel."""
        p = self._pending
        if not p or p["id"] != pending_id or action not in ("send", "hold", "cancel"):
            return False
        p["action"] = action
        self._pending_event.set()
        return True

    def _send_to(self, session: str, message: str, lang: str, spoken: bool = False) -> None:
        """Type a message into a conversation's own window (and send it), then come back.

        `spoken`: you said it, so it waits a moment under the notch first (scheduled prompts and resumes don't)."""
        message, expanded = self._expand(message)
        hwnd = self._session_hwnd.get(session, 0)
        title = self._title(session, self.threads.get(session, {}).get("source", ""))
        if not (hwnd and winfocus.is_window(hwnd)):
            log.info("no window known for %r: message not sent", title)
            self._show_banner(tr("I don't know where {title} is yet: send it one message from its window first.",
                                 title=title),
                              session, "", kind="you", tag="not sent")
            _beep(300)
            return
        self._heard(message, lang, session, tag="shortcut" if expanded else "sent")
        if spoken:
            self._send_spoken(message, hwnd, session, lang)
        else:
            self._paste(message, hwnd, submit=True, session=session)

    # -- handoff ----------------------------------------------------------------
    def _copy_selection(self, settle: float = 0.25) -> tuple[str, str]:
        """(the text selected in the app in front, what the clipboard held): copied, then the clipboard put back."""
        import pyperclip

        self._wait_modifiers_released()
        previous = pyperclip.paste()
        pyperclip.copy("")
        keys.copy()
        time.sleep(settle)
        selected = pyperclip.paste()
        pyperclip.copy(previous)
        return selected, previous

    def handoff(self, name: str, message: str, lang: str) -> None:
        """'Send this to Codex': the selection (or the screenshot on the clipboard) goes to that conversation.

        With a message ("... and ask it to fix it") it's sent; without, it waits in the box for your question.
        """
        session = self._find_thread(name)
        if session is None:
            self._show_banner(tr("I don't know a thread called {name}.", name=name), "", "", kind="note", tag="not sent")
            _beep(300)
            return
        window = winfocus.foreground()  # what you're looking at, before anything moves
        content = self._copy_selection()[0].strip()
        if not content:
            # A screenshot you copied, else a picture of the window you're looking at.
            content = self._save_shot(winfocus.clipboard_png() or winfocus.window_png(window))
        title = self._title(session, self.threads.get(session, {}).get("source", ""))
        if not content:
            self._show_banner(tr("Nothing to send to {title}: select some text or copy a screenshot first.", title=title), "", "",
                              kind="note", tag="not sent")
            _beep(300)
            return
        self._deliver(session, message, content, lang, tr("(to {title}) {text}", title=title, text=content[:120]))

    def look_at(self, session: str, message: str, lang: str) -> None:
        """'Look at this': a picture of the window in front goes to the conversation, with what you asked."""
        window = winfocus.foreground()
        path = self._save_shot(winfocus.window_png(window))
        if not path:
            self._show_banner(tr("Couldn't take a picture of that window."), "", "", kind="note", tag="not sent")
            _beep(300)
            return
        log.info("look at %r", winfocus.title(window)[:60])
        self._deliver(session, message, path, lang,
                      tr("(a picture of {window})", window=winfocus.title(window)[:60] or tr("the window")))

    def _deliver(self, session: str, message: str, content: str, lang: str, shown: str) -> None:
        """Content for a conversation: with a message, sent (after the take-back); alone, typed in for your question."""
        message, _ = self._expand(message) if message else ("", False)
        log.info("to %r: %d chars, %s", self._title(session), len(content), "sent" if message else "not sent")
        self._heard(message or shown, lang, session, tag="sent" if message else "handoff")
        hwnd = self._hwnd_for(session)
        if message:
            self._send_spoken(f"{message}\n\n{content}", hwnd, session, lang)
        else:
            self._paste(content + " ", hwnd, submit=False, session=session)

    @staticmethod
    def _save_shot(png: bytes) -> str:
        """Keep a picture for an agent to open (the last 20 stay); its path, or '' with no picture."""
        if not png:
            return ""
        shots = HOME / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        for old in sorted(shots.glob("*.png"))[:-20]:
            old.unlink(missing_ok=True)
        path = shots / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        path.write_bytes(png)
        return str(path).replace("\\", "/")

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
        return bool(self._away_since or self._holding() or self.muted) or idle >= 60 * float(
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

    # -- the companion: timers, stopwatch, to-do, reminders ------------------------
    def _save_companion(self, data: dict) -> None:
        self.state["companion"] = data
        save_state(self.state)

    def _companion_loop(self) -> None:
        """Sleep until the next timer or reminder is due (woken by any change): no polling, nothing per second."""
        while True:
            due = self.companion.next_due()
            self.companion.wake.wait(None if due is None else max(0.05, min(3600.0, due - time.time())))
            self.companion.wake.clear()
            try:
                for kind, item in self.companion.due():
                    self._companion_due(kind, item)
            except Exception:
                log.exception("companion check failed")

    def _companion_due(self, kind: str, item: dict, now: float | None = None) -> None:
        """A timer ended or a reminder's time came: the banner (even in Do not disturb, silently), a chime, the voice."""
        now = time.time() if now is None else now
        late = now - float(item.get("end") or item.get("at") or now) > 120  # the PC was asleep or off
        if kind == "pomodoro":
            self._pomodoro_due(item, late)
            return
        if kind == "timer":
            what = item.get("label") or companion.duration_words(int(item.get("total") or 0))
            title, text = "Timer done", what
            said = tr("Time's up: {what}.", in_lang=self.language, what=what) if item.get("label") else tr(
                "Time's up.", in_lang=self.language)
            snooze = {"kind": "timer", "seconds": 300, "label": item.get("label") or ""}
        else:
            title, text = "Reminder", item["text"]
            said = tr("Reminder: {text}", in_lang=self.language, text=item["text"])
            snooze = {"kind": "reminder", "seconds": 600, "text": item["text"]}
        if late:
            text += tr(" (due at {time})", time=time.strftime('%H:%M', time.localtime(item.get('end') or item.get('at'))))
        log.info("companion: %s due", kind)
        self._show_banner(text, "", "", kind=kind, tag=title, extra={"snooze": snooze})
        self._push("scheduled", tr(title), text)
        if self.muted or late:
            return
        self.play_chime("attention")
        if not self.chime:
            self.speaker.speak(said, self.language)

    def _pomodoro_due(self, ended: dict, late: bool) -> None:
        """A focus session or a break ended: the next one has started. Say so, softly."""
        nxt = ended["next"]
        minutes = companion.duration_words(int(nxt["length"]))
        long = nxt["phase"] == "long"
        if ended["phase"] == "focus":
            text = tr("Focus done ({n} today): {minutes} long break." if long else
                      "Focus done ({n} today): {minutes} break.", n=nxt['today'], minutes=minutes)
            said = tr("Time for a long break: {minutes}." if long else "Time for a break: {minutes}.",
                      in_lang=self.language, minutes=minutes)
        else:
            text = tr("Break over: {minutes} of focus.", minutes=minutes)
            said = tr("Back to focus.", in_lang=self.language)
        self._show_banner(text, "", "", kind="pomodoro", tag="Pomodoro")
        if self.muted or late:
            return
        self.play_chime("done" if ended["phase"] == "focus" else "attention")
        if not self.chime:
            self.speaker.speak(said, self.language)

    def companion_said(self, text: str, lang: str = "") -> bool:
        """'Timer 10 minutes', 'add milk to my list', 'remind me at 4 to call Paul': done here; False if it isn't one."""
        action = companion.parse_command(text, time.time())
        if action is None:
            return False
        self._heard(text, lang, "", tag="companion")
        kind = action["kind"]
        try:
            if kind == "timer":
                self.companion.timer("start", seconds=action["seconds"], label=action["label"])
            elif kind == "timer_cancel":
                self.companion.timer("cancel")
            elif kind == "stopwatch":
                self.companion.stopwatch(action["action"])
            elif kind == "pomodoro":
                self.companion.pomodoro(action["action"])
            elif kind == "note":
                self.add_note(action["text"])
            elif kind == "media":
                media.control(action["action"])
            elif kind == "todo":
                self.companion.todo("add", text=action["text"], source="you")
            elif kind == "reminder":
                self.companion.reminder("add", text=action["text"], at=action["at"])
        except ValueError as exc:
            self._show_banner(tr("Not done: {why}.", why=tr(str(exc))), "", "", kind="note", tag="companion")
            _beep(300)
            return True
        _beep(880)
        return True

    def add_note(self, text: str) -> dict:
        item = self.companion.note("add", text=text)
        if self.history_db is not None:  # searchable from History, with everything else
            try:
                self.history_db.add("note", item["text"], title=tr("Note"))
            except Exception:
                log.exception("note not kept in the history")
        return item

    # -- the companion: what's playing, today's meetings --------------------------
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
        log.info("joining the meeting %r", meeting["title"][:60])
        webbrowser.open(link)
        return meeting["title"]

    def listen_companion(self) -> None:
        """The companion's mic: what you say next is a timer, a to-do, a reminder (else it joins the to-do list)."""
        self.reply_now(COMPANION)

    # -- the day ----------------------------------------------------------------
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

    # -- the morning brief ------------------------------------------------------
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
                idle = winfocus.idle_seconds()
                self._check_away(idle, now)
                self._check_brief(now, idle)
                self._check_resumes(now)
                self._check_schedules(now)
                self._check_recap(now)
            except Exception:
                log.exception("away check failed")

    def _check_away(self, idle: float, now: float) -> None:
        cfg = self.config.get("catch_up", {})
        if not cfg.get("enabled", True):
            return
        # Do not disturb counts as away: what happened meanwhile is told when you turn it off.
        held = bool(self.in_call) or self._holding() or self.muted
        away = idle >= 60 * float(cfg.get("away_minutes", 5)) or held
        if away and not self._away_since:
            self._away_since = now - (0 if held and idle < 60 else idle)
        elif not away and self._away_since:
            since, self._away_since = self._away_since, 0.0
            events = [e for e in self._events if e["at"] >= since]
            if self._brief_due(now):  # the first time you're back in the morning: the brief tells it all
                self.brief(away=events)
            else:
                self._catch_up(events)

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
        def news(in_lang: str | None = None) -> list[str]:
            return ([plural(len(done), "{who} finished", "{who} finished", in_lang=in_lang, who=_join(done, in_lang))]
                    if done else []) + ([plural(len(needs), "{who} needs you", "{who} need you", in_lang=in_lang,
                                                who=_join(needs, in_lang))] if needs else [])

        parts = news()
        if not parts:
            return
        said = tr("While you were away: {news}.", in_lang=self.language, news="; ".join(news(self.language)))
        log.info("catch-up: %s", said)
        # The banner's header already says "While you were away": the body goes straight to the news. One
        # conversation finished: its summary right after its name, not the name twice.
        if len(done) == 1 and done[0] in summaries:
            body = [tr("{who} finished: {summary}", who=done[0], summary=summaries[done[0]])] + (
                [plural(len(needs), "{who} needs you.", "{who} need you.", who=_join(needs))] if needs else [])
        else:
            body = [f"{'; '.join(parts)}."] + [f"{t}: {x}" for t, x in list(summaries.items())[-3:]]
        self._show_banner(" ".join(body), "", "", kind="catchup")
        if not self.quiet:
            self._wake_cancel.set()
            self.speaker.speak(said, self.language)

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

    def set_muted(self, muted: bool) -> None:
        """Do not disturb: no voice, no sound, no banner; what happens is kept for when you're back."""
        self.state["muted"] = muted
        save_state(self.state)
        if muted:
            self.stop()  # and no cue: you may be walking into a meeting
        else:
            _beep(880)
        log.info("do not disturb=%s", muted)

    @property
    def dnd_from(self) -> str:
        """What Do not disturb goes back to: 'chime' or 'voice'."""
        return "chime" if self.state.get("chime") else "voice"

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
        full: str = "",
    ) -> int:
        """Queue text; `final` marks the end of an agent's turn and arms auto-listen.

        `hwnd`: the agent's window when its hook knows it (a terminal), so replies and messages can reach it.
        `result`: what the turn changed ({"files": 3, "tests": "passed"}), shown in the notch.
        `full`: the whole reply when `text` is only its summary (for the preview and the history).
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
                self._queue.append((text, lang, source, interrupt, final, voice, session, 0, result, full))
                log.info("queued speech from another thread (%d waiting)", len(self._queue))
                return 0
        if final and session and textclean.limit_reset(text) is not None:
            self.limit_hit(session, source, text)  # "5-hour limit reached ∙ resets 3pm": not a reply
            self.activity.pop(session, None)
            return 0
        if text.strip() and self._said_lately(text, session):
            # The same words from another conversation a moment ago: one reply reaching us twice (an app's MCP
            # `speak` beside its hooks). Said once, and the mic isn't opened a second time.
            log.info("already said a moment ago: source=%s final=%s", source, final)
            return 0
        if session and text.strip():
            self._not_limited(session)
        self._record_turn(session, source, text, lang, voice, final, full)
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

    def _said_lately(self, text: str, session: str, window: float = 120.0) -> bool:
        """Was this said in the last `window` seconds by another conversation? (Remembered either way.) Within one
        conversation the hooks' own dedupe already decides."""
        key = " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())[:300]
        if not key:
            return False
        now = time.time()
        self._recent_said = {k: v for k, v in self._recent_said.items() if now - v[0] < window}
        before = self._recent_said.get(key)
        if before is not None and before[1] != session:
            return True  # still the first one's: it said them
        self._recent_said[key] = (now, session)
        return False

    def _record_turn(self, session: str, source: str, text: str, lang, voice, final: bool, full: str = "") -> None:
        """Collect what a conversation says in one turn (progress, then the end), for replay and copy; and all of
        it (`full`: the reply behind a summary) for the preview and the history."""
        turn = self._turns.get(session)
        if turn is None or (turn["done"] and (text.strip() or full.strip())):
            turn = {"texts": [], "full": [], "done": False, "lang": lang, "voice": voice, "source": source}
            self._turns[session] = turn
            self._turns = dict(list(self._turns.items())[-20:])
        if full.strip() or text.strip():
            turn["full"].append((full or text).strip())
        if text.strip():
            turn["texts"].append(text.strip())
            turn["lang"], turn["voice"] = lang, voice
            self._last_session = session
        if final:
            turn["done"] = True
            self._agent_todo(companion.agent_todos("\n".join(turn["full"])), source, session)

    AGENT_TODOS_PER_THREAD, AGENT_TODOS_OPEN = 2, 8

    def _agent_todo(self, tasks: list[str], source: str, session: str) -> None:
        """A reply's "To-do for you" line -> your list. Only the first of a turn; and not once that thread already has
        2 waiting on you, nor once your agents have 8 open: the list stays yours, not a log of their suggestions."""
        if not tasks:
            return
        mine = [t for t in self.companion.data["todos"] if not t["done"] and (t.get("source") or "you") != "you"]
        if len(mine) >= self.AGENT_TODOS_OPEN:
            return
        if session and sum(1 for t in mine if t.get("session") == session) >= self.AGENT_TODOS_PER_THREAD:
            return
        try:
            self.companion.todo("add", text=tasks[0], source=source or "agent", session=session)
        except ValueError:
            pass

    def _finished(self, session: str, source: str, text: str, result: dict | None) -> None:
        """A turn ended: keep its outcome for the notch, and note it for when you come back."""
        turn = self._turns.get(session)
        if turn is not None and result:
            turn["result"] = result
        summary = text.strip() or self._turn_text(session)
        self._event("done", session, source, textclean.clean(summary) if summary else "")
        said = _voice_line(summary)  # its own one-line summary, if it wrote one
        full = textclean.clean(strip_summary(self._turn_text(session) or summary or ""))
        self._push("done", self._title(session, source), said or full, full=full)
        self._log_turn(session, source, said or (textclean.clean(summary) if summary else ""))
        whole = self._turn_full(session)
        if self.history_db is not None and whole:
            try:
                self.history_db.add("reply", whole, session, source, self._title(session, source))
            except Exception:
                log.exception("history: reply not kept")
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

    def _turn_full(self, session: str) -> str:
        """The turn as the agent wrote it (not only what was read out), without its summary line."""
        turn = self._turns.get(session) or {}
        return strip_summary("\n\n".join(turn.get("full") or turn.get("texts") or [])).strip()

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

    def _show_banner(self, text: str, session: str, source: str, kind: str = "reply", tag: str = "",
                     extra: dict | None = None) -> None:
        """A notification under the notch: a reply in chime mode, or ("you") what you just said.

        Do not disturb: only what you do yourself shows (and your own timers, silently); the rest waits for the
        catch-up when you're back."""
        if self.muted and kind not in ("you", "pending", "timer", "reminder", "pomodoro"):
            return
        title = self._title(session, source) if session or source else ""
        self.banner = {"id": (self.banner or {}).get("id", 0) + 1, "text": textclean.clean(text)[:800],
                       "title": title, "color": thread_color(session or title), "session": session,
                       "kind": kind, "tag": tag, "at": time.time(), **(extra or {})}

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
            self._show_banner(tr("The extension is connected: replies on ChatGPT, Claude and Gemini will be read out."),
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
                text = tr("{name} connected.", name=name) + " " + (
                    tr(self._NEXT_STEP[agent]) if agent in self._NEXT_STEP
                    else tr("Restart {name} to load agent-voice.", name=name))
        except Exception as exc:  # a malformed settings file must not take the daemon down
            log.exception("install %s failed", agent)
            text = tr("Couldn't connect {name}: {why}", name=name, why=exc)
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
        from agent_voice import lang as langmod

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
            self._show_banner(prompt, "", "", kind="you", tag=tr("new · {where}", where=label))
            self.history = [{"text": prompt, "lang": lang or langmod.detect(prompt), "at": round(time.time())},
                            *self.history][:5]
        else:
            self._show_banner(tr("Couldn't start a new thread in {where}. Your prompt is on the clipboard.", where=label), "", "",
                              kind="note", tag="new thread")
            import pyperclip

            pyperclip.copy(prompt)
            _beep(300)
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
        self._pending_event.set()  # a message about to go stays unsent (on the clipboard)
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

    def prompt_submitted(self, hwnd: int, session: str = "", source: str = "", title: str = "", cwd: str = "",
                         prompt: str = "") -> None:
        """The user sent a new prompt: remember its window (and conversation), and stop talking/listening.

        `title`: the conversation's name when the caller knows it (a browser tab's title). `prompt`: what you wrote,
        for the history.
        """
        if session and title:
            self._file_titles[session] = title
        if cwd and cwd != self.state.get("last_cwd"):
            self.state["last_cwd"] = cwd  # where a new thread starts by default
            recent = [c for c in self.state.get("recent_cwds") or [] if c != cwd]
            self.state["recent_cwds"] = [cwd, *recent][:8]  # offered in the new-thread box
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
            if prompt.strip() and self.history_db is not None:
                try:
                    self.history_db.add("you", prompt, session, source, self._title(session, source))
                except Exception:
                    log.exception("history: prompt not kept")
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
        """Every few seconds: is another app on the mic? Then chimes until the call ends. Headphones or speakers?"""
        while True:
            time.sleep(4)
            self._check_output()
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
                self._pause_for_call()
            elif not label and self.in_call:
                log.info("call ended")
                self.in_call, self._call_override = "", False
                self._resume_after_call()

    def _pause_for_call(self) -> None:
        """A song playing when a call starts: paused (the media key is a toggle, so only when it plays)."""
        if not self.config.get("pause_media_in_calls", True):
            return
        now = media.now_playing()
        if now and now.get("playing") and self.media_control("toggle"):
            self._call_paused = True
            log.info("music paused for the call (%s)", now.get("app", ""))

    def _resume_after_call(self) -> None:
        if not self._call_paused:
            return
        self._call_paused = False
        now = media.now_playing()
        if now and not now.get("playing"):  # not if you've played it again yourself meanwhile
            self.media_control("toggle")
            log.info("music resumed after the call")

    def _check_output(self) -> None:
        if self.speaker_device:
            devices.output_device()  # Settings' speaker plugged in since the start: PortAudio learns of it now
        before = self._output.kind
        try:
            now = self._output.poll()
        except Exception:
            log.debug("sound output unknown", exc_info=True)
            return
        if now != before:
            self._speaker_override = False  # a new device: your choice for the old one doesn't carry over
            if self.on_speakers and self.speaker.busy:
                self.stop()  # headphones unplugged mid-sentence: not out loud in the room

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

    def _ensure_start_menu(self) -> None:
        """Once (Windows): agent-voice in the Start menu, so it can be found and started like any app."""
        if not (os.name == "nt" or sys.platform.startswith("linux")) or self.state.get("start_menu"):
            return
        try:
            log.info("start menu: %s", install.start_menu(True))
        except Exception:
            log.exception("start menu entry failed")
        self.state["start_menu"] = True  # once: if you remove it, it stays removed
        save_state(self.state)

    # -- other apps' sound -----------------------------------------------------------
    def _duck_loop(self) -> None:
        """Runs the fades (one at a time, the latest wish wins); first puts back what a crash left lowered."""
        from agent_voice import ducking

        if not ducking.available():
            return

        def save(data: dict) -> None:
            self.state["ducked"] = data
            save_state(self.state)

        self._ducker = ducking.Ducker(self.state.get("ducked"), on_save=save)
        if self.state.get("ducked"):
            self._ducker.restore()
        while True:
            job = self._duck_jobs.get()
            while not self._duck_jobs.empty():
                job = self._duck_jobs.get()  # only the latest matters
            try:
                if job[0] == "duck":
                    self._ducker.duck(job[1], pause=job[2])
                else:
                    self._ducker.restore()
            except Exception:
                log.exception("ducking failed")

    def release_audio(self) -> None:
        """Shutting down: other apps get their volume back now (and a paused video plays), not at the next start."""
        if self._ducker is not None:
            try:
                self._ducker.restore()
            except Exception:
                log.exception("ducking restore failed")

    def _duck_tick(self, now: float) -> None:
        """Every 100 ms: speaking -> others lowered; you speak -> silenced (a video paused); done -> as they were.

        The mic open for your reply isn't you speaking: until you do, the others come back (in headphones all the
        way; through speakers to the voice's level, or the mic would take the video for you)."""
        cfg = self.config.get("ducking", {})
        if not cfg.get("enabled", True) or self._ducker is None:
            return
        if self._ptt_active or self._mic_phase == "recording":
            want = "listen"
        elif self.speaker.busy or self._mic_phase == "waiting" and self._output.kind != "headphones":
            want = "voice"
        else:
            want = ""
        if want:
            self._duck_idle = 0.0
            if want != self._duck_state:
                self._duck_state = want
                level = float(cfg.get("listen", 0.0) if want == "listen" else cfg.get("voice", 0.3))
                self._duck_jobs.put(("duck", level, want == "listen" and bool(cfg.get("pause_media", True))))
        elif self._duck_state:
            # A pause between two sentences, or between the voice and the mic, isn't the end: wait a moment.
            self._duck_idle = self._duck_idle or now
            # While the thread that spoke is still at work, its next progress note is seconds away: the others
            # stay lowered (up to 15 s), instead of coming back up and going down again with each note.
            hold = 15.0 if self._voice_session and self._voice_session in self.activity else 0.4
            if now - self._duck_idle >= hold:
                self._duck_state, self._duck_idle = "", 0.0
                self._duck_jobs.put(("restore",))

    def _listen_monitor(self) -> None:
        idle_since = None
        queue_idle = None
        while True:
            time.sleep(0.1)
            try:
                self._duck_tick(time.time())
            except Exception:
                log.exception("ducking check failed")
            if self._barging and not self._barged and not self.speaker.busy:
                self._barge_cancel.set()  # the voice finished without you cutting in
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
            _beep(700)
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
                _beep(400)
                return
            _beep(520)
            self._mic_phase = "transcribing"
            text, lang = self.transcriber.transcribe(audio)
            log.info("auto-listen lang=%s chars=%d", lang, len(text))
            if self._listen_cancel.is_set() or is_hallucination(text):
                return
            self._handle_heard(text, lang, session, target, question, permission, force_submit)
        except Exception:
            log.exception("auto-listen failed")
        finally:
            self._listening = False
            self.speaker.release()
            self._mic_phase = ""
            self.level = 0.0

    def _handle_heard(self, text: str, lang: str, session: str, target: int, question, permission,
                      force_submit: bool) -> None:
        """What you said back to a conversation: an answer, a command, a message for another thread, or the reply."""
        if session == NEW_THREAD:  # dictating a new thread's first prompt
            pending, self._new_pending = self._new_pending, {}
            self.new_thread(text, pending.get("target", ""), pending.get("cwd", ""), lang)
            return
        if session == COMPANION:  # the companion's mic: a timer, a reminder... or a to-do
            if not self.companion_said(text, lang):
                self._heard(text, lang, "", tag="to-do")
                self.companion.todo("add", text=text.strip(" .")[:300], source="you")
            return
        approving = permission is not None and permission is self.permission
        answering = approving or (question is not None and question is self.question)
        command = None if answering else match_command(text)
        look = None if answering or command else parse_look(text)
        if look is not None:  # "Look at this and tell me what's wrong"
            self.look_at(session or self._last_session, look, lang)
            return
        routed = None if answering or command else self._routed(text)
        if routed and routed[0] != session:
            self._send_to(routed[0], routed[1], lang, spoken=True)  # "Tell Codex to ...": goes to Codex, not here
            return
        tag = ("answer" if answering else "command" if command
               else "sent" if self.hands_free or force_submit else "typed")
        self._heard(text, lang, session, tag=tag)
        full, expanded = self._expand(text)
        rest = strip_wake(text, list(self.config.get("wake_word", {}).get("phrases") or [])) if self.wake else None
        if approving:
            if not self._voice_approval(text):
                log.info("permission answer unclear: %r", text)
                _beep(300)
        elif answering:
            self.answer(text=text)
        elif command:
            self._run_command(command, session)
        elif session in self._limited and match_resume(text):
            self.set_resume(session)
        elif expanded:  # a saved prompt's name: its full text, sent
            self._send_spoken(full, target, session, lang)
        elif rest is not None:
            # "Hey Claude, ..." while the reply mic was open: the rest is the reply; the phrase alone is nothing.
            if rest.strip(" .!?,"):
                self._dictated(rest, target, session, lang, self.hands_free or force_submit)
            else:
                log.info("reply was only the wake phrase: nothing sent")
        else:
            self._dictated(text, target, session, lang, self.hands_free or force_submit)

    # -- talking over the voice ------------------------------------------------------
    def _barge_ok(self) -> bool:
        """Listen while it speaks? Voice on, a mic model, and headphones (speakers would let it hear itself)."""
        cfg = self.config.get("barge_in", {})
        if not cfg.get("enabled", True) or self.transcriber is None or self.quiet:
            return False
        if self._listening or self._ptt_active:
            return False
        return self._output.kind == "headphones" or bool(cfg.get("speakers", False))

    def _barge_loop(self) -> None:
        """While the voice speaks, listen: talk over it and it stops. "Stop" or "no, wait" opens the mic for what
        you want to say instead; anything longer is your reply, as if the mic had opened after it."""
        while True:
            time.sleep(0.2)
            if self.speaker.busy and self._barge_ok():
                try:
                    self._barge_once()
                except Exception:
                    log.exception("barge-in failed")
                    time.sleep(30)

    def _barge_once(self) -> None:
        from agent_voice import stt

        cfg = self.config["auto_listen"]
        session = self._voice_session
        self._barge_cancel.clear()
        self._barging, self._barged = True, False
        try:
            audio = stt.listen_for_speech(3600, float(cfg["silence_seconds"]), float(cfg["max_seconds"]),
                                          self._barge_cancel, on_level=self._set_level, on_start=self._barge_in,
                                          start_blocks=10)  # 300 ms: a cough or a click doesn't cut it off
        finally:
            self._barging = False
        if not self._barged:
            return
        handed_over = False  # the mic opened for your correction: it owns the listening state from here
        try:
            if audio is None:
                return
            self._mic_phase = "transcribing"
            text, lang = self.transcriber.transcribe(audio)
            log.info("barge-in: %d chars", len(text))
            if not text or stt.is_hallucination(text):
                return
            if match_interrupt(text):  # "No, wait": say what you want instead
                self._listening, self._mic_phase, self.level = False, "", 0.0
                handed_over = True
                self.reply_now(session or None)
                return
            target = self._hwnd_for(session) or winfocus.foreground()
            self._handle_heard(text, lang, session, target, self.question, self.permission, False)
        finally:
            self._barged = False
            if not handed_over and not self._ptt_active:
                self._listening, self._mic_phase, self.level = False, "", 0.0

    def _barge_in(self) -> None:
        """You started talking over the voice: it stops at once (and what was queued with it)."""
        self._barged = True
        self.speaker.stop()
        self._listen_armed = False
        self._listening, self._mic_phase = True, "recording"
        log.info("barge-in: you talked over the voice")

    def _dictated(self, text: str, hwnd: int, session: str, lang: str, submit: bool) -> None:
        """What you said: sent (after the take-back), or typed into the input for you to send."""
        if submit:
            self._send_spoken(text, hwnd, session, lang)
        else:
            self._paste(text, hwnd, submit=False, session=session)

    def _run_command(self, command: str, session: str) -> None:
        """A spoken command in the reply window: handled here, nothing is sent to the agent."""
        log.info("voice command: %s", command)
        if command == "repeat":
            self.replay(session)
        elif command in ("slower", "faster"):
            self.set_speed(self.speed_x + (0.25 if command == "faster" else -0.25))
        else:
            _beep(400)

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
            _beep(300)
            return
        # Desktop app: several conversations share one window; open the one this reply answers.
        if title and not claudeapp.select_session(hwnd, title):
            log.warning("could not open conversation %r; text left on clipboard", title)
            pyperclip.copy(text)
            _beep(300)
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
        """A conversation's name: its desktop-app title when known, else the one Claude Code gave the session (its
        tab's), else the agent's name."""
        title = self._titles.get(session, "")
        if session and not title:
            if session not in self._file_titles:  # app session files: read once per session, not per poll
                self._file_titles[session] = claudeapp.session_title(session)
            title = self._file_titles[session]
        if session and not title and source in ("claude", ""):
            title = self._transcript_titles.title(session)
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
                "text": self._turn_full(session)[-12000:], "result": turn.get("result"),
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
        self._push("permission", self._title(session, source), tr("Wants to {action}.", action=action(summary)))
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
        self._say("{title} wants to {action}. Allow?", title=self._title(perm['session'], perm['source']),
                  action=action(perm['summary'], in_lang=self.language))
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
        _beep(880 if allow else 440)
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
                    from agent_voice.wakeword import WakeListener

                    listener = WakeListener()
                    # Load (first time: download) the fallback model now, not on the first "Hey Claude".
                    threading.Thread(target=self._wake_model().warm_up, daemon=True).start()
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
                # The wake phrase is English: say so, a 1-second clip is easily taken for another language.
                quick, _ = self._wake_model().transcribe(audio, language="en")
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
            if self.companion_said(command, lang):  # "Hey Claude, timer 10 minutes", "... add milk to my list"
                continue
            if self.schedule_said(command, lang, session):  # "Hey Claude, at 2 pm tell Codex to run the tests"
                self._heard(command, lang, "", tag="scheduled")
                continue
            if match_recap(command):  # "Hey Claude, daily recap"
                self._heard(command, lang, "", tag="command")
                self.recap()
                continue
            if match_brief(command):  # "Hey Claude, what's my day?"
                self._heard(command, lang, "", tag="command")
                self.brief(asked=True)
                continue
            if match_join(command):  # "Hey Claude, join my meeting"
                self._heard(command, lang, "", tag="command")
                if not self.join_meeting():
                    self._confirm("No meeting to join right now.")
                continue
            if self._limited and match_resume(command):  # "Hey Claude, resume Codex when it resets"
                named = self._mentioned(command, set(self._limited))
                if self.set_resume(named or ""):
                    self._heard(command, lang, named or "", tag="command")
                continue
            look = parse_look(command)
            if look is not None:  # "Hey Claude, look at this and tell me why it's red"
                self.look_at(session or self._last_session, look, lang)
                continue
            handoff = parse_handoff(command)
            if handoff:  # "Hey Claude, send this to Codex and ask it to explain it"
                self.handoff(handoff[0], handoff[1], lang)
                continue
            new = parse_new_thread(command)
            if new:  # "Hey Claude, new thread: add a health check"
                self.new_thread(new[1], _new_target(new[0]), lang=lang)
                continue
            routed = self._routed(command)
            if routed:  # "Hey Claude, tell the notch redesign to run the tests"
                self._send_to(routed[0], routed[1], lang, spoken=True)
                continue
            full, expanded = self._expand(command)
            if expanded:  # "Hey Claude, commit this": a saved prompt, to the current thread
                self._heard(full, lang, session, tag="shortcut")
                _beep(880)
                self._send_spoken(full, self._hwnd_for(session), session, lang)
            elif len(command.split()) >= 3:
                self._heard(command, lang, session, tag="sent")
                _beep(880)
                self._send_spoken(command, self._hwnd_for(session), session, lang)
            else:
                # Just "Hey Claude": open the mic for the prompt, and send it.
                _beep(880)
                self._reply_session = session
                self._force_submit = True
                self._auto_listen()

    def _wake_model(self):
        """Whisper tiny, made once: it confirms the short phrases the keyword spotter missed."""
        if self._wake_transcriber is None:
            from agent_voice.stt import Transcriber

            self._wake_transcriber = Transcriber({"stt": {**self.config["stt"], "model": "tiny"}})
        return self._wake_transcriber

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
            "boot": self.boot,  # a new one: the service restarted, and the widget restarts with it
            "widget_opens": self.widget_opens,
            "phase": self.phase(),
            "level": round(self.level, 4),
            "speaking_lang": self.speaker.current_lang if self.speaker.busy else "",
            "engine": self.engine,
            "engine_state": self._engine_state,
            "caption": self.speaker.current_text if self.speaker.busy else "",
            "awaiting_reply": self._listen_armed,
            "widget_toggles": self.widget_toggles,
            "new_thread_opens": self.new_thread_opens,
            "new_targets": self.new_targets(),
            "new_default": {"target": self._default_target(), "cwd": self.state.get("last_cwd", ""),
                            "recent": self.state.get("recent_cwds") or []},
            "last_heard": self.last_heard[-160:],
            "last_heard_lang": self.last_heard_lang,
            "history": self.history,
            "agents": self.agent_states(),
            "activity": self._activity_status(),
            "reading": self._reading_status(),
            "output": self.output,
            "in_call": self.in_call,
            "focus": self.focus if self._holding() else "",
            "barge_in": self._barging,
            "headphones_only": self.headphones_only,
            "mic": self.mic,
            "speaker_device": self.speaker_device,
            "dnd_from": self.dnd_from,
            "speakers": self.on_speakers,
            "pending": {"id": self._pending["id"], "until": self._pending["until"]} if self._pending else None,
            "phone": self._phone_status(),
            "schedules": self._schedules_status(),
            "companion": self.companion.status(),
            "media": self.media,
            "agenda": self._agenda_status(),
            "day": self._day_status(),
            "banner": {**self.banner, "age": round(time.time() - self.banner["at"], 1)}
            if self.banner and time.time() - self.banner["at"] < 60 else None,
            "last_reply": {"session": self._last_session} if self._turn_text(self._last_session) else None,
            "question": self._question_status(),
            "needs": self._needs_status(),
            "limits": self._limits_status(),
            "usage_line": self._usage_line,
            "limits_refreshing": self._limits_refreshing,
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
            "language": self.language,
            "ui_language": self.ui_language,
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
        selected, previous = self._copy_selection(0.2)
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
            _beep(660)
            # Safety net: a lost "release" (widget closed mid-press) must not record forever.
            self._ptt_release.wait(timeout=600)
            audio = rec.stop()
            _beep(520)
            self._mic_phase = "transcribing"
            text, lang = self.transcriber.transcribe(audio)
            log.info("dictation lang=%s chars=%d", lang, len(text))
            if not text:
                return
            submit = self.hands_free or bool(self.config["stt"].get("auto_submit"))
            self._heard(text, lang, "", tag="sent" if submit else "typed")  # push-to-talk goes to the focused window
            self._dictated(text, winfocus.foreground() if submit else 0, "", lang, submit)
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
    return when.strftime("%H:%M") if window == "five_hour" and at - time.time() < 20 * 3600 else day_time(when)


def status_tag(st: dict) -> str:
    """What the widget shows, as a short tag: it changes when something visible changes.

    Not the mic level (a widget that animates polls normally) nor the banner's age.
    """
    shown = {k: v for k, v in st.items() if k != "level"}
    if shown.get("banner"):
        shown["banner"] = {k: v for k, v in shown["banner"].items() if k != "age"}
    shown["threads"] = [{**t, "ago": t["ago"] // 60} for t in shown.get("threads") or []]  # "N min ago"
    return f"{zlib.crc32(json.dumps(shown, sort_keys=True, default=str).encode()):08x}"


# On/off switches: the endpoint -> the daemon's property (set with set_<name>).
TOGGLES = {"/summary": "summary", "/handsfree": "hands_free", "/chime": "chime", "/approvals": "approvals",
           "/apps": "desktop_apps", "/wake": "wake", "/mute": "muted", "/headphones": "headphones_only"}


def _background(target, *args, **kwargs) -> None:
    """Run it off the request thread: the caller gets its answer at once."""
    threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True).start()


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
                try:
                    timeout = min(30.0, max(0.0, float(query.get("timeout", ["20"])[0])))
                except ValueError:
                    timeout = 20.0
                wait, end = query["wait"][0], time.time() + timeout
                while True:
                    st = daemon.status()
                    tag = status_tag(st)
                    if tag != wait or time.time() >= end:
                        return self._json(200, {**st, "tag": tag})
                    time.sleep(0.25)
            if url.path == "/history":  # search every conversation (?q=), or one entry in full (?id=)
                query = parse_qs(url.query)
                if daemon.history_db is None:
                    return self._json(200, {"results": [], "off": True})
                if "id" in query:
                    return self._json(200, daemon.history_db.get(_int(query["id"][0])))
                limit = min(100, max(1, _int(query.get("limit", ["30"])[0])))
                return self._json(200, {"results": daemon.history_db.search(query.get("q", [""])[0], limit)})
            if url.path == "/devices":  # what Settings' Devices menus list
                return self._json(200, {"inputs": devices.names("input"), "outputs": devices.names("output"),
                                        "mic": daemon.mic, "speaker": daemon.speaker_device})
            if url.path == "/thread":  # a thread's last reply in full, for the preview on hover
                return self._json(200, daemon.thread_detail(parse_qs(url.query).get("session", [""])[0]))
            self._json(404, {"error": "not found"})

        def _companion(self, data: dict):
            """The companion's endpoints: /timer, /stopwatch, /todo (agents use it too, through MCP), /reminder."""
            action, item_id, now = str(data.get("action") or ""), _int(data.get("id")), time.time()
            box = daemon.companion
            try:
                if self.path == "/companion_listen":
                    daemon.listen_companion()
                    return self._json(200, {"ok": True})
                if self.path == "/media":
                    return self._json(200, {"ok": daemon.media_control(action)})
                if self.path == "/calendar" and action == "join":  # the meeting's Teams / Zoom / Meet link
                    title = daemon.join_meeting(float(data.get("start") or 0))
                    return self._json(200 if title else 404, {"title": title} if title else {"error": "no meeting to join"})
                if self.path == "/calendar":
                    return self._json(200, {"calendars": len(daemon.set_calendar(action, str(data.get("source") or "")))})
                if self.path == "/note":
                    out = daemon.add_note(str(data.get("text") or "")) if action == "add" else box.note(action, item_id)
                    if out is None:
                        return self._json(404, {"error": "no such note"})
                    return self._json(200, {"ok": True, "item": out})
                if self.path == "/timer":
                    seconds = data.get("seconds")
                    if not isinstance(seconds, (int, float)):
                        seconds = companion.parse_duration(str(data.get("duration") or "")) or 0
                    out = box.timer(action, item_id, float(seconds), str(data.get("label") or ""), now)
                elif self.path == "/stopwatch":
                    out = box.stopwatch(action, now)
                elif self.path == "/pomodoro":
                    out = box.pomodoro(action, now)
                elif self.path == "/todo":
                    if action == "list":
                        return self._json(200, {"todos": box.status()["todos"]})
                    done = data.get("done")
                    out = box.todo(action, item_id, str(data.get("text") or ""),
                                   None if done is None else bool(done), str(data.get("source") or "you"),
                                   str(data.get("session") or ""), str(data.get("category") or ""))
                else:
                    at = data.get("at")
                    text = str(data.get("text") or "")
                    if action == "add" and isinstance(data.get("in"), (int, float)):
                        at = now + float(data["in"])
                    elif action == "add" and not isinstance(at, (int, float)):
                        found = companion.parse_reminder(text, now)  # "16:00 call Paul", "in 20 minutes stretch"
                        if not found:
                            return self._json(400, {"error": "when? Start with a time: 'at 16:00 ...', "
                                                             "'in 20 minutes ...', 'tomorrow at 9 ...'"})
                        at, text = found
                    out = box.reminder(action, item_id, text, float(at or 0))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            if out is None:
                return self._json(404, {"error": f"nothing to {action or 'do'} there"})
            return self._json(200, {"ok": True, "item": out if isinstance(out, dict) else None})

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
                    str(data.get("full") or "")[:100000],
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
                _background(daemon.attach, *args)
                return self._json(200, {"ok": True})
            if self.path == "/reply":
                daemon.reply_now(str(data.get("session") or "") or None)
                return self._json(200, {"ok": True})
            if self.path == "/send":
                args = (str(data.get("session") or ""), str(data.get("text") or "")[:20000])
                _background(daemon.send_text, *args)
                return self._json(200, {"ok": True})
            if self.path == "/new_thread":
                target, cwd = str(data.get("target") or ""), str(data.get("cwd") or "")
                if data.get("listen"):
                    daemon.listen_new_thread(target, cwd)
                else:
                    args = (str(data.get("text") or "")[:20000], target, cwd)
                    _background(daemon.new_thread, *args)
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
            if self.path == "/brief":
                daemon.brief(spoken=bool(data.get("speak", True)), asked=True)
                return self._json(200, {"text": daemon.brief_text()[0] or tr("Nothing planned today.")})
            if self.path == "/resume":
                ok = daemon.set_resume(str(data.get("session") or ""), bool(data.get("on", True)))
                return self._json(200, {"ok": ok})
            if self.path == "/limit_hit":
                daemon.limit_hit(str(data.get("session") or ""), str(data.get("source") or ""),
                                 str(data.get("text") or "")[:400])
                return self._json(200, {"ok": True})
            if self.path == "/language":  # what you speak and hear (language), what's shown (ui_language)
                codes = {k: str(data.get(k) or "") for k in ("language", "ui_language") if data.get(k)}
                if not codes or any(c not in ("en", "fr") for c in codes.values()):
                    return self._json(400, {"error": "language / ui_language must be en or fr"})
                if "language" in codes:
                    daemon.set_language(codes["language"])
                if "ui_language" in codes:
                    daemon.set_ui_language(codes["ui_language"])
                return self._json(200, {"language": daemon.language, "ui_language": daemon.ui_language})
            if self.path == "/engine":  # the voice: light (Piper) or natural (Kokoro)
                name = str(data.get("engine") or "")
                if name not in ("piper", "kokoro"):
                    return self._json(400, {"error": "engine must be piper or kokoro"})
                _background(daemon.set_engine, name)
                return self._json(200, {"engine": name})
            if self.path in ("/timer", "/stopwatch", "/pomodoro", "/todo", "/reminder", "/companion_listen", "/note",
                             "/media", "/calendar"):
                return self._companion(data)
            if self.path == "/pending":  # the take-back banner's Send, Edit (hold) and Cancel
                return self._json(200, {"ok": daemon.pending_action(_int(data.get("id")), str(data.get("action") or ""))})
            if self.path == "/widget_open":
                daemon.widget_opens += 1
                return self._json(200, {"ok": True})
            if self.path == "/limits_refresh":
                _background(daemon.refresh_limits)
                return self._json(200, {"ok": True})
            if self.path == "/usage_line":
                _background(daemon.usage_line, bool(data.get("enable", True)))
                return self._json(200, {"ok": True})
            if self.path == "/install":
                agent = str(data.get("agent") or "")
                if agent not in install.INSTALLERS:
                    return self._json(400, {"error": "unknown agent"})
                _background(daemon.install_agent, agent)
                return self._json(200, {"ok": True})
            if self.path == "/replay":
                session = str(data.get("session") or "")
                daemon.jobs.put(lambda: daemon.replay(session))
                return self._json(200, {"ok": True})
            if self.path == "/read":
                _background(daemon._read_selection)
                return self._json(200, {"ok": True})
            if self.path == "/prompt":
                daemon.prompt_submitted(_int(data.get("hwnd")), str(data.get("session") or ""),
                                        str(data.get("source") or ""), str(data.get("title") or "")[:80],
                                        str(data.get("cwd") or ""), str(data.get("prompt") or "")[:20000])
                return self._json(200, {"ok": True})
            if self.path == "/activity":
                if data.get("done"):
                    daemon.activity_done(str(data.get("session") or ""))
                else:
                    daemon.tool_used(str(data.get("session") or ""), str(data.get("source") or ""),
                                     str(data.get("label") or ""))
                return self._json(200, {"ok": True})
            if self.path == "/device":  # the mic or speaker to use: a device's name, "" for the system's
                kind, name = str(data.get("kind") or ""), str(data.get("name") or "")
                if kind not in ("mic", "speaker") or len(name) > 300:
                    return self._json(400, {"error": "kind must be mic or speaker"})
                daemon.set_device(kind, name)
                return self._json(200, {"mic": daemon.mic, "speaker": daemon.speaker_device})
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
            if self.path in TOGGLES:  # {"mode": "on" | "off" | "toggle"} -> {its name: on?}
                name = TOGGLES[self.path]
                mode = data.get("mode", "toggle")
                getattr(daemon, f"set_{name}")(not getattr(daemon, name) if mode == "toggle" else mode == "on")
                return self._json(200, {name: getattr(daemon, name)})
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
                decision = daemon.permission_state(_int(data.get("id")), cancel=bool(data.get("cancel")))
                return self._json(200, {"decision": decision})
            if self.path == "/permission_answer":
                daemon.answer_permission(_int(data.get("id")), bool(data.get("allow")))
                return self._json(200, {"ok": True})
            if self.path == "/open_thread":
                _background(daemon.open_thread, str(data.get("session") or ""))
                return self._json(200, {"ok": True})
            if self.path == "/answer":
                index = data.get("index")
                kwargs = {"index": int(index)} if isinstance(index, int) else {"text": str(data.get("text") or "")}
                _background(daemon.answer, **kwargs)
                return self._json(200, {"ok": True})
            if self.path == "/update":
                threading.Thread(target=daemon.install_update, name="update-install", daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/update_check":  # "Check for updates": now, and install it if there's one
                threading.Thread(target=daemon.check_updates, args=(bool(data.get("install", True)),),
                                 name="update-check", daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/shutdown":
                self._json(200, {"ok": True})
                _background(self.server.shutdown)
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
    if config["host"] not in LOOPBACK:
        log.warning("host %r ignored: the API only ever listens on 127.0.0.1", config["host"])
        config["host"] = "127.0.0.1"
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
        daemon.release_audio()
        try:
            keys.unhook_all()
        except Exception:
            pass
        lock.release()
        log.info("agent-voice stopped (pid %s)", os.getpid())
    return 0
