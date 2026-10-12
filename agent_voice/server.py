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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_voice import __version__, claudeapp, winfocus
from agent_voice.config import HOME, LOG_DIR, load_config, load_state, save_state
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


def _version(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


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
    def __init__(self, config: dict):
        self.config = config
        self.state = load_state()
        if self.state.get("speed_x"):
            # The speaker reads config["speed"] (Piper length_scale) for every sentence.
            config["speed"] = 1.0 / float(self.state["speed_x"])
        self.speaker = Speaker(config)
        self.transcriber = None
        self._ptt_active = False
        # Requests are processed in order by one worker so replies are never spoken out of order.
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._job_loop, name="jobs", daemon=True).start()
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
        # Newer release on GitHub, if any (checked once at start).
        self.update: dict | None = None
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
        self._ptt_release = threading.Event()
        # Bumped by the show/hide hotkey; the widget toggles when it sees the number change.
        self.widget_toggles = 0
        threading.Thread(target=self._listen_monitor, name="listen", daemon=True).start()
        threading.Thread(target=self._wake_loop, name="wake", daemon=True).start()
        threading.Thread(target=self._check_update, name="update", daemon=True).start()

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
        # Say it, so you know which mode you are in without looking.
        self.speaker.stop()
        self.speaker.speak("Hands-free on. Your replies will be sent automatically." if on else "Hands-free off.", "en")

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
        self.speaker.stop()
        self.speaker.speak("Summaries on." if on else "Full answers.", "en")

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
        if not self.speaker.busy and not self.muted:
            self.speaker.speak(f"Speed {x:g}.", "en")

    @property
    def chime(self) -> bool:
        return bool(self.state.get("chime"))

    def set_chime(self, on: bool) -> None:
        self.state["chime"] = on
        save_state(self.state)
        log.info("chime=%s", on)
        self.stop()  # no voice and no open mic in chime mode
        if on:
            self.play_chime("done")
        elif not self.muted:
            self.speaker.speak("Voice mode.", "en")

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
        if kind == "question" and options:
            self.question = {
                "session": str(data.get("session") or ""),
                "agent": str(data.get("agent") or ""),
                "question": str(data.get("question") or ""),
                "header": str(data.get("header") or ""),
                "options": options,
                "more": int(data.get("more") or 0),
                "at": time.time(),
            }
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
            self.speaker.speak(f"{self.question['question']} {listed}")
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
        hwnd, session = self._target_hwnd, q["session"]
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

    def set_muted(self, muted: bool) -> None:
        self.state["muted"] = muted
        save_state(self.state)
        if muted:
            self.speaker.stop()
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
    ) -> int:
        """Queue text; `final` marks the end of an agent's turn and arms auto-listen."""
        if session and self.question and self.question["session"] == session:
            self.question = None  # the agent moved on: the question was answered
        if session:
            self.threads[session] = {"source": source, "last": time.time()}
        if session and self._voice_session and session != self._voice_session and not interrupt and (
                self.speaker.busy or self._listen_armed or self._listening):
            # Another thread is talking (or waiting for your reply): this one waits its turn.
            self._queue.append((text, lang, source, interrupt, final, voice, session))
            log.info("queued speech from another thread (%d waiting)", len(self._queue))
            return 0
        if self.muted:
            return 0
        if self.chime:
            # Sounds only: progress is silent, the end of a turn rings, the mic stays closed.
            log.info("speak source=%s chime=%s final=%s", source, final, final)
            if final:
                self.play_chime("done")
            return 0
        if interrupt:
            self.speaker.stop()
        if text.strip() or final:
            self._voice_session, self._voice_source = session, source  # whose voice this is, for the widget
            self._wake_cancel.set()  # never let the wake word hear our own voice
        n = self.speaker.speak(text, lang, voice) if text.strip() else 0
        log.info("speak source=%s sentences=%d final=%s", source, n, final)
        if final and self.config["auto_listen"].get("enabled") and self.transcriber is not None:
            self._reply_session = session
            self._listen_armed = True
        return n

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

    def reply_now(self) -> None:
        """Skip the rest of the speech and listen for your answer right away."""
        self.speaker.stop()
        self._listen_armed = False
        if self._listening or self._ptt_active or self.transcriber is None:
            return
        threading.Thread(target=self._auto_listen, daemon=True).start()

    def _set_level(self, value: float) -> None:
        self.level = value

    def _heard(self, text: str, lang: str) -> None:
        self.last_heard, self.last_heard_lang = text, lang
        self.history = [{"text": text, "lang": lang, "at": round(time.time())}, *self.history][:5]
        self.state["history"] = self.history
        save_state(self.state)

    def prompt_submitted(self, hwnd: int, session: str = "") -> None:
        """The user sent a new prompt: remember its window (and conversation), and stop talking/listening."""
        if hwnd and hwnd != self._target_hwnd:
            self._target_hwnd = hwnd
            self.state["target_hwnd"] = hwnd
            save_state(self.state)
        if hwnd and session and claudeapp.is_desktop_app(hwnd):
            threading.Thread(target=self._learn_title, args=(hwnd, session), daemon=True).start()
        if session:
            self._queue = [job for job in self._queue if job[6] != session]  # you moved on in that thread
            self.threads[session] = {"source": self.threads.get(session, {}).get("source", ""), "last": time.time()}
        self.stop()

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
        session = self._queue[0][6]
        jobs = [job for job in self._queue if job[6] == session]
        self._queue = [job for job in self._queue if job[6] != session]
        title = self._title(session, jobs[0][2])
        log.info("playing queued thread %r (%d left)", title, len(self._queue))
        self._voice_session, self._voice_source = session, jobs[0][2]
        self.speaker.speak(f"{title}.")
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
        target = self._target_hwnd or winfocus.foreground()
        session = self._reply_session
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
            self._heard(text, lang)
            if permission is not None and permission is self.permission:
                allow = match_yes_no(text)
                if allow is None:
                    log.info("permission answer unclear: %r", text)
                    _beep(300, 150)
                else:
                    self.answer_permission(permission["id"], allow)
            elif question is not None and question is self.question:
                self.answer(text=text)
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

    def _paste(self, text: str, hwnd: int = 0, submit: bool = False, session: str = "") -> None:
        import keyboard
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
        keyboard.send("ctrl+v")
        time.sleep(0.3)
        if submit:
            keyboard.send("enter")
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
        return title or {"claude": "Claude Code", "cline": "Cline"}.get(source, source.capitalize())

    def _thread_status(self) -> dict | None:
        """The conversation the voice (or the open mic) belongs to: its title and a colour of its own."""
        q, perm = self.question, self.permission
        if perm:
            session, source = perm["session"], "claude"
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
        out = []
        for session, info in sorted(self.threads.items(), key=lambda kv: -kv[1]["last"])[:4]:
            if session == (self.permission or {}).get("session"):
                state = "permission"
            elif session == (self.question or {}).get("session"):
                state = "question"
            elif session == self._voice_session and self.speaker.busy:
                state = "speaking"
            elif session == self._reply_session and self._mic_phase:
                state = "listening"
            elif session in queued:
                state = "queued"
            else:
                state = ""
            title = self._title(session, info["source"])
            out.append({"session": session, "title": title, "color": thread_color(session or title),
                        "state": state, "ago": int(time.time() - info["last"])})
        return out

    def open_thread(self, session: str) -> None:
        """Bring a conversation to the front (widget thread list)."""
        hwnd = self._target_hwnd
        title = self._title_for(hwnd, session)
        if hwnd:
            winfocus.focus(hwnd)
            if title:
                claudeapp.select_session(hwnd, title)

    # -- permissions ------------------------------------------------------------
    def request_permission(self, session: str, tool: str, summary: str) -> int:
        """Claude wants to use a tool: ask out loud and in the widget; the hook polls the answer."""
        self._perm_seq += 1
        self.permission = {"id": self._perm_seq, "session": session, "tool": tool, "summary": summary,
                           "at": time.time()}
        if session:
            self.threads[session] = {"source": "claude", "last": time.time()}
        log.info("permission #%d: %s", self._perm_seq, summary)
        if not self.muted:
            if self.chime:
                self.play_chime("attention")
            else:
                self.speaker.stop()
                self._wake_cancel.set()
                self.speaker.speak(f"{self._title(session, 'claude')} wants to {summary}. Allow?", "en")
                if self.transcriber is not None:
                    self._reply_session = session
                    self._listen_armed = True
        return self._perm_seq

    def answer_permission(self, perm_id: int, allow: bool) -> None:
        if self.permission and self.permission["id"] == perm_id:
            self.permission = None
        self._perm_decisions[perm_id] = "allow" if allow else "deny"
        log.info("permission #%d: %s", perm_id, "allowed" if allow else "denied")
        _beep(880 if allow else 440, 80)

    def permission_state(self, perm_id: int, cancel: bool = False) -> str:
        """The decision for a request ("" while pending). `cancel`: the hook gave up waiting."""
        if cancel and self.permission and self.permission["id"] == perm_id:
            self.permission = None
            self.cancel_listen()
        return self._perm_decisions.pop(perm_id, "")

    def _permission_status(self) -> dict | None:
        p = self.permission
        if p and time.time() - p["at"] > 900:
            self.permission = p = None
        return {k: p[k] for k in ("id", "tool", "summary")} if p else None

    # -- updates ----------------------------------------------------------------
    def _check_update(self) -> None:
        """Tell the widget when GitHub has a newer release (once, a few seconds after start)."""
        if not self.config.get("update_check", True):
            return
        time.sleep(5)
        import urllib.request

        try:
            req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                                "User-Agent": "agent-voice"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                latest = json.loads(resp.read().decode("utf-8"))
        except Exception:  # offline, rate-limited, private repo: no news is fine
            return
        tag = str(latest.get("tag_name") or "")
        if tag and _version(tag) > _version(__version__):
            self.update = {"version": tag.lstrip("v"), "url": str(latest.get("html_url") or RELEASES_PAGE)}
            log.info("update available: %s", tag)

    @property
    def approvals(self) -> bool:
        return bool(self.state.get("approvals", True))

    def set_approvals(self, on: bool) -> None:
        # The PermissionRequest hook reads this from state.json.
        self.state["approvals"] = on
        save_state(self.state)
        log.info("approvals=%s", on)
        if not self.muted:
            self.speaker.speak("Voice approvals on." if on else "Voice approvals off.", "en")

    # -- wake word --------------------------------------------------------------
    @property
    def wake(self) -> bool:
        return bool(self.state.get("wake"))

    def set_wake(self, on: bool) -> None:
        self.state["wake"] = on
        save_state(self.state)
        log.info("wake=%s", on)
        self._wake_cancel.set()
        if not self.muted:
            self.speaker.speak("Wake word on. Say: Hey Claude." if on else "Wake word off.", "en")

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
            if (self.speaker.busy or self._listening or self._listen_armed or self._ptt_active
                    or self.question or self.permission or self._queue):
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
            if len(command.split()) >= 3:
                self._heard(command, lang)
                _beep(880, 60)
                self._paste(command, self._target_hwnd, submit=True, session=session)
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
            "last_heard": self.last_heard[-160:],
            "last_heard_lang": self.last_heard_lang,
            "history": self.history,
            "question": self._question_status(),
            "thread": self._thread_status(),
            "threads": self._threads_status(),
            "queued": len(self._queue),
            "permission": self._permission_status(),
            "update": self.update,
            "wake": self.wake,
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
        try:
            import keyboard
        except ImportError:
            log.warning("keyboard module missing: hotkeys disabled")
            return
        hk = self.config["hotkeys"]
        if hk.get("stop"):
            keyboard.add_hotkey(hk["stop"], self.stop)
        if hk.get("reply"):
            keyboard.add_hotkey(hk["reply"], self.reply_now)
        if hk.get("widget"):
            keyboard.add_hotkey(hk["widget"], lambda: setattr(self, "widget_toggles", self.widget_toggles + 1))
        if hk.get("hands_free"):
            keyboard.add_hotkey(hk["hands_free"], lambda: self.set_hands_free(not self.hands_free))
        if hk.get("mute"):
            keyboard.add_hotkey(hk["mute"], lambda: self.set_muted(not self.muted))
        if hk.get("chime"):
            keyboard.add_hotkey(hk["chime"], lambda: self.set_chime(not self.chime))
        if hk.get("read_selection"):
            keyboard.add_hotkey(hk["read_selection"], lambda: threading.Thread(target=self._read_selection, daemon=True).start())
        if hk.get("push_to_talk") and self.config["stt"].get("enabled"):
            from agent_voice.stt import Transcriber

            self.transcriber = Transcriber(self.config)
            threading.Thread(target=self.transcriber.warm_up, daemon=True).start()
            keyboard.add_hotkey(hk["push_to_talk"], self._ptt_pressed)
        log.info("hotkeys registered: %s", hk)

    @staticmethod
    def _wait_modifiers_released(timeout: float = 3.0) -> None:
        import keyboard

        end = time.time() + timeout
        while time.time() < end and any(keyboard.is_pressed(k) for k in ("ctrl", "alt", "shift", "windows")):
            time.sleep(0.02)

    def _read_selection(self) -> None:
        """Speak the selected text (any app, e.g. Antigravity), or the clipboard if nothing is selected."""
        import keyboard
        import pyperclip

        self._wait_modifiers_released()
        previous = pyperclip.paste()
        pyperclip.copy("")
        keyboard.send("ctrl+c")
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
        import keyboard

        trigger = self.config["hotkeys"]["push_to_talk"].split("+")[-1].strip()

        def watch_release():
            while keyboard.is_pressed(trigger):
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
            self._heard(text, lang)
            self._paste(text, submit=self.hands_free or bool(self.config["stt"].get("auto_submit")))
        except Exception:
            log.exception("push-to-talk failed")
        finally:
            self._ptt_active = False
            self.speaker.release()
            self._mic_phase = ""
            self.level = 0.0


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
            if self.path == "/status":
                return self._json(200, daemon.status())
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
                )
                daemon.jobs.put(lambda: daemon.speak(*args))
                return self._json(200, {"queued": True, "muted": daemon.muted})
            if self.path == "/reply":
                daemon.reply_now()
                return self._json(200, {"ok": True})
            if self.path == "/ptt":
                if data.get("action") == "stop":
                    daemon.ptt_stop()
                else:
                    daemon.ptt_start()
                return self._json(200, {"ok": True})
            if self.path == "/read":
                threading.Thread(target=daemon._read_selection, daemon=True).start()
                return self._json(200, {"ok": True})
            if self.path == "/prompt":
                daemon.prompt_submitted(int(data.get("hwnd") or 0), str(data.get("session") or ""))
                return self._json(200, {"ok": True})
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
                                                    str(data.get("summary") or "use a tool"))
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
            import keyboard

            keyboard.unhook_all()
        except Exception:
            pass
        lock.release()
        log.info("agent-voice stopped (pid %s)", os.getpid())
    return 0
