"""The background daemon: a tiny local HTTP API + global hotkeys.

Keeping models loaded here is what makes speech start instantly; agent hooks only POST text to it.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from agent_voice import __version__, companion, devices, install, keys, schedule
from agent_voice.config import HOME, LOG_DIR, LOOPBACK, load_config
from agent_voice.daemon.core import Daemon
from agent_voice.i18n import tr

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
                if self.path == "/calendar" and action == "snooze":  # the meeting's banner: +5 min
                    done = daemon.snooze_meeting(float(data.get("start") or 0), float(data.get("seconds") or 300))
                    return self._json(200 if done else 404, {"ok": done})
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
