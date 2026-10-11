"""The background daemon: a tiny local HTTP API + global hotkeys.

Keeping models loaded here is what makes speech start instantly; agent hooks only POST text to it.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_voice import __version__, winfocus
from agent_voice.config import LOG_DIR, load_config, load_state, save_state
from agent_voice.tts import Speaker

log = logging.getLogger("agent_voice.server")


def _beep(freq: int, ms: int = 90) -> None:
    if sys.platform == "win32":
        import winsound

        threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()


class Daemon:
    def __init__(self, config: dict):
        self.config = config
        self.speaker = Speaker(config)
        self.state = load_state()
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
        self._target_hwnd = 0
        # What the mic is doing, for the widget: "" | waiting | recording | transcribing
        self._mic_phase = ""
        self.level = 0.0
        self.last_heard = ""
        self.last_heard_lang = ""
        self._ptt_release = threading.Event()
        # Bumped by the show/hide hotkey; the widget toggles when it sees the number change.
        self.widget_toggles = 0
        threading.Thread(target=self._listen_monitor, name="listen", daemon=True).start()

    def _job_loop(self) -> None:
        while True:
            args = self.jobs.get()
            try:
                self.speak(*args)
            except Exception:
                log.exception("speak failed")

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
    ) -> int:
        """Queue text; `final` marks the end of an agent's turn and arms auto-listen."""
        if self.muted:
            return 0
        if interrupt:
            self.speaker.stop()
        n = self.speaker.speak(text, lang, voice) if text.strip() else 0
        log.info("speak source=%s sentences=%d final=%s", source, n, final)
        if final and self.config["auto_listen"].get("enabled") and self.transcriber is not None:
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

    def prompt_submitted(self, hwnd: int) -> None:
        """The user sent a new prompt: remember its window, and stop talking/listening."""
        if hwnd:
            self._target_hwnd = hwnd
        self.stop()

    # -- auto-listen ------------------------------------------------------------
    def _listen_monitor(self) -> None:
        idle_since = None
        while True:
            time.sleep(0.1)
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
        self._listen_cancel.clear()
        self._listening = True
        self._mic_phase = "waiting"
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
            self._paste(text, target, submit=self.hands_free)
        except Exception:
            log.exception("auto-listen failed")
        finally:
            self._listening = False
            self._mic_phase = ""
            self.level = 0.0

    def _paste(self, text: str, hwnd: int = 0, submit: bool = False) -> None:
        import keyboard
        import pyperclip

        self._wait_modifiers_released()
        if hwnd and not winfocus.focus(hwnd):
            log.warning("could not focus target window %s (%s)", hwnd, winfocus.title(hwnd))
        time.sleep(0.1)
        previous = pyperclip.paste()
        pyperclip.copy(text)
        keyboard.send("ctrl+v")
        time.sleep(0.3)
        if submit:
            keyboard.send("enter")
        pyperclip.copy(previous)

    def phase(self) -> str:
        if self._mic_phase:
            return self._mic_phase
        if self.speaker.busy:
            return "speaking"
        return "muted" if self.muted else "idle"

    def status(self) -> dict:
        return {
            "version": __version__,
            "phase": self.phase(),
            "level": round(self.level, 4),
            "speaking_lang": self.speaker.current_spec[:2] if self.speaker.busy else "",
            "awaiting_reply": self._listen_armed,
            "widget_toggles": self.widget_toggles,
            "last_heard": self.last_heard[-160:],
            "last_heard_lang": self.last_heard_lang,
            "muted": self.muted,
            "hands_free": self.hands_free,
            "summary": self.summary,
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
        self._ptt_release.clear()
        threading.Thread(target=self._ptt_session, daemon=True).start()
        return True

    def ptt_stop(self) -> None:
        self._ptt_release.set()

    def _ptt_session(self) -> None:
        from agent_voice.stt import Recorder

        try:
            self.speaker.stop()  # you talk, the agent shuts up
            self.cancel_listen()
            rec = Recorder(on_level=self._set_level)
            rec.start()
            self._mic_phase = "recording"
            _beep(660, 60)
            # Safety net: a lost "release" (widget closed mid-press) must not record forever.
            self._ptt_release.wait(timeout=120)
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
                )
                daemon.jobs.put(args)
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
                daemon.prompt_submitted(int(data.get("hwnd") or 0))
                return self._json(200, {"ok": True})
            if self.path == "/stop":
                daemon.stop()
                return self._json(200, {"ok": True})
            if self.path == "/summary":
                mode = data.get("mode", "toggle")
                daemon.set_summary(not daemon.summary if mode == "toggle" else mode == "on")
                return self._json(200, {"summary": daemon.summary})
            if self.path == "/handsfree":
                mode = data.get("mode", "toggle")
                daemon.set_hands_free(not daemon.hands_free if mode == "toggle" else mode == "on")
                return self._json(200, {"hands_free": daemon.hands_free})
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

    from agent_voice.client import pythonw

    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen([pythonw(), "-m", "agent_voice", "widget"], **kwargs)
    except OSError:
        log.exception("could not start the widget")


def serve(foreground: bool = True) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_DIR / "server.log", encoding="utf-8")]
    if foreground and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", handlers=handlers)

    config = load_config()
    try:
        httpd = ThreadingHTTPServer((config["host"], int(config["port"])), None)
    except OSError:
        log.error("port %s busy: agent-voice is probably already running", config["port"])
        return 1
    daemon = Daemon(config)
    httpd.RequestHandlerClass = _handler(daemon)
    daemon.speaker.warm_up()
    daemon.start_hotkeys()
    if config.get("widget", {}).get("enabled", True):
        _launch_widget()
    log.info("agent-voice %s listening on %s:%s", __version__, config["host"], config["port"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
