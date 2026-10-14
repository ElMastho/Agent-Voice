"""`agent-voice watch <command>`: a voice for any terminal agent (Aider, OpenCode, Goose, Copilot CLI...).

The agent runs in a pseudo-terminal that you keep using exactly as before; agent-voice reads the screen it
draws (through a terminal emulator, so full-screen TUIs work too). Press Enter and the notch shows it working;
once the screen goes quiet, what's new on it is the reply: spoken, or shown under the notch. A last line that
asks something ("Apply these edits? (y/n)") goes to "needs you" instead.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from agent_voice import client, textclean

QUIET = 2.5  # seconds without output that end a reply
# A last line that waits on you.
ASKS = re.compile(r"\?\s*(?:\(.{0,40}\)|\[.{0,20}\])?\s*:?\s*$|\((?:y/n|yes/no)\)|\[(?:y/n|Y/n|y/N)\]|"
                  r"press enter|\b(?:allow|approve|confirm|continue)\b.*[?:]\s*$", re.I)
# Screen furniture that is never part of a reply: borders, spinners, empty prompts.
CHROME = re.compile(r"^[\s│┃|─━═╭╮╰╯┌┐└┘├┤┬┴┼▌▐█░▒▓⠁-⣿·•>❯$#%-]*$")


# ConPTY asks the terminal for "win32-input-mode": each key then arrives as ESC [ Vk;Sc;Uc;Kd;Cs;Rc _
WIN32_KEY = re.compile(r"\x1b\[(\d*);(\d*);(\d*);(\d*);(\d*);(\d*)_")
# Arrows, function keys, the terminal's own replies: keys, not text.
ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.|.)")


def _win32_char(m: re.Match) -> str:
    char, down = int(m[3] or 0), m[4] == "1"
    return chr(char) * max(1, int(m[6] or 1)) if down and char else ""


class Tracker:
    """What the agent is doing, from what it draws and what you type. No I/O: `post(path, payload)` reports."""

    def __init__(self, name: str, post, rows: int = 40, cols: int = 120, now=time.time):
        import pyte

        self.name, self.post, self.now = name, post, now
        self.session = f"watch-{name}-{os.getpid()}"
        self.screen = pyte.HistoryScreen(cols, rows, history=3000)
        self.stream = pyte.Stream(self.screen)
        self.lock = threading.Lock()
        self.working = False
        self.before: list[str] = []  # the screen when you pressed Enter
        self.typed = ""
        self.prompt = ""  # what you sent: echoed on screen, never part of the reply
        self.last_out = 0.0
        self.out_since_enter = False
        self.last_label_at = -1e9
        self.hwnd = 0

    def lines(self) -> list[str]:
        cols = self.screen.columns
        top = ["".join(line[x].data for x in range(cols)).rstrip() for line in self.screen.history.top]
        return top + [line.rstrip() for line in self.screen.display]

    def resize(self, rows: int, cols: int) -> None:
        with self.lock:
            self.screen.resize(rows, cols)

    def output(self, text: str) -> None:
        with self.lock:
            self.stream.feed(text)
            self.last_out = self.now()
            if self.working:
                self.out_since_enter = True

    def typed_input(self, data: str) -> None:
        """Keys you pressed; Enter sends the line to the agent (so it starts working)."""
        data = ESCAPES.sub("", WIN32_KEY.sub(_win32_char, data))
        for ch in data:
            if ch in "\r\n":
                self._sent()
            elif ch in "\x7f\b":
                self.typed = self.typed[:-1]
            elif ch.isprintable():
                self.typed += ch

    def _sent(self) -> None:
        with self.lock:
            self.before = self.lines()
        prompt, self.typed = self.typed.strip(), ""
        self.prompt = prompt
        self.working, self.out_since_enter = True, False
        self.post("/prompt", {"hwnd": self.hwnd, "session": self.session, "source": self.name})  # starts "Thinking"

    def tick(self) -> None:
        """Called a few times a second: a live label while it works, the reply once it goes quiet."""
        now = self.now()
        if not self.working or not self.out_since_enter:
            return
        with self.lock:
            lines = self.lines()
        if now - self.last_out < QUIET:
            if now - self.last_label_at > 2.0:
                self.last_label_at = now
                fresh = new_text(self.before, lines, self.prompt).splitlines()
                label = fresh[-1] if fresh else ""
                if label:
                    self.post("/activity", {"session": self.session, "source": self.name, "label": label[:80]})
            return
        self.working = False
        reply = new_text(self.before, lines, self.prompt)
        last = next((l.strip() for l in reversed(lines) if l.strip() and not CHROME.match(l)), "")
        if last and ASKS.search(last):
            self.post("/notify", {"kind": "input", "session": self.session, "agent": self.name, "text": last[:300]})
            self.post("/activity", {"session": self.session, "done": True})
            return
        self.post("/speak", {"text": spoken(reply), "source": self.name, "final": True, "session": self.session,
                             "hwnd": self.hwnd})


def new_text(before: list[str], after: list[str], prompt: str = "") -> str:
    """Lines on screen now that weren't there when you pressed Enter, without the furniture."""
    seen: dict[str, int] = {}
    for line in before:
        seen[line] = seen.get(line, 0) + 1
    out = []
    for line in after:
        if seen.get(line):
            seen[line] -= 1
            continue
        if line.strip() and not CHROME.match(line) and not (prompt and line.strip().endswith(prompt)):
            out.append(line.strip())
    return "\n".join(out)


def spoken(reply: str) -> str:
    from agent_voice.hooks import strip_summary, summarize, summary_enabled
    from agent_voice.config import load_config

    if not summary_enabled():
        return strip_summary(textclean.clean(reply))
    limit = int(load_config().get("summary", {}).get("max_chars", 280))
    if "voice summary:" in reply.lower():
        return summarize(reply, limit)
    # A terminal agent ends with its conclusion (the steps scroll by above it): the last lines.
    out: list[str] = []
    for line in reversed([l for l in reply.splitlines() if l.strip()]):
        if out and len(" ".join([line, *out])) > limit:
            break
        out.insert(0, line.strip())
    return textclean.clean(" ".join(out))


# -- the pseudo-terminal ---------------------------------------------------------------------
class _WinConsole:
    """Raw keys in (VT sequences, so arrows and Ctrl+C reach the agent), VT out; restored on exit."""

    def __enter__(self):
        import ctypes

        self.k32 = ctypes.windll.kernel32
        self.hin, self.hout = self.k32.GetStdHandle(-10), self.k32.GetStdHandle(-11)
        self.modes = []
        for handle in (self.hin, self.hout):
            mode = ctypes.c_uint()
            self.k32.GetConsoleMode(handle, ctypes.byref(mode))
            self.modes.append(mode.value)
        self.cp = (self.k32.GetConsoleCP(), self.k32.GetConsoleOutputCP())
        self.k32.SetConsoleCP(65001)
        self.k32.SetConsoleOutputCP(65001)
        # in: no line editing, no echo, Ctrl+C as a key, VT input; out: VT processing
        self.k32.SetConsoleMode(self.hin, (self.modes[0] & ~0x7) | 0x200)
        self.k32.SetConsoleMode(self.hout, self.modes[1] | 0x4)
        return self

    def __exit__(self, *exc):
        self.k32.SetConsoleMode(self.hin, self.modes[0])
        self.k32.SetConsoleMode(self.hout, self.modes[1])
        self.k32.SetConsoleCP(self.cp[0])
        self.k32.SetConsoleOutputCP(self.cp[1])

    @staticmethod
    def read() -> str:
        return os.read(0, 1024).decode("utf-8", "replace")


def _run_windows(argv: list[str], tracker: Tracker) -> int:
    from winpty import PtyProcess

    cols, rows = shutil.get_terminal_size()
    proc = PtyProcess.spawn(argv, dimensions=(rows, cols))
    tracker.resize(rows, cols)
    out = sys.stdout.buffer

    def pump_out():
        while True:
            try:
                data = proc.read(4096)
            except EOFError:
                return
            if data:
                out.write(data.encode("utf-8", "replace"))
                out.flush()
                tracker.output(data)

    with _WinConsole() as console:
        threading.Thread(target=pump_out, daemon=True).start()

        def pump_in():
            while proc.isalive():
                data = console.read()
                if data:
                    proc.write(data)
                    tracker.typed_input(data)

        threading.Thread(target=pump_in, daemon=True).start()
        size = (cols, rows)
        while proc.isalive():
            time.sleep(0.25)
            tracker.tick()
            now = tuple(shutil.get_terminal_size())
            if now != size:
                size = now
                proc.setwinsize(now[1], now[0])
                tracker.resize(now[1], now[0])
    return proc.exitstatus or 0


def _run_posix(argv: list[str], tracker: Tracker) -> int:
    import fcntl
    import pty
    import select
    import struct
    import termios
    import tty

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
    def set_size():
        cols, rows = shutil.get_terminal_size()
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        tracker.resize(rows, cols)

    set_size()
    old = termios.tcgetattr(0)
    tty.setraw(0)
    size = shutil.get_terminal_size()
    try:
        while True:
            ready, _, _ = select.select([0, fd], [], [], 0.25)
            if fd in ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(1, data)
                tracker.output(data.decode("utf-8", "replace"))
            if 0 in ready:
                data = os.read(0, 1024)
                os.write(fd, data)
                tracker.typed_input(data.decode("utf-8", "replace"))
            tracker.tick()
            if shutil.get_terminal_size() != size:
                size = shutil.get_terminal_size()
                set_size()
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def run(argv: list[str]) -> int:
    if not argv:
        print("usage: agent-voice watch <command> [args...]   e.g. agent-voice watch aider", file=sys.stderr)
        return 2
    try:
        import pyte  # noqa: F401
    except ImportError:
        print("agent-voice watch needs pyte (and pywinpty on Windows): pip install pyte pywinpty", file=sys.stderr)
        return 2
    if argv[0] == "--":
        argv = argv[1:]
    from agent_voice import winfocus

    if not client.is_running():
        threading.Thread(target=client.start_detached, daemon=True).start()
    name = Path(argv[0]).stem.lower()

    def post(path: str, payload: dict) -> None:
        threading.Thread(target=client.request, args=(path, payload), daemon=True).start()

    tracker = Tracker(name, post)
    tracker.hwnd = winfocus.console_window()
    if sys.platform == "win32":
        return _run_windows(argv, tracker)
    return _run_posix(argv, tracker)
