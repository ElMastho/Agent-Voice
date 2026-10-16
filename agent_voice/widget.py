"""The notch (or floating island): what every agent is doing, what needs you, and one-click access to every action.

It is a separate process that polls the daemon (`/status`) and calls its endpoints, so the daemon stays headless.
The window never takes keyboard focus (WS_EX_NOACTIVATE): clicking it leaves your agent window focused,
which is where dictated text has to land.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QAction,
    QIcon,
    QPixmap,
    QColor,
    QConicalGradient,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit,
                               QPushButton, QSystemTrayIcon, QTextBrowser, QToolTip, QVBoxLayout, QWidget)

from agent_voice import client
from agent_voice.config import HOME, load_config
from agent_voice.launch import LABELS as TARGET_LABELS
from agent_voice.textclean import result_label, tokens_label

WIDTH = 320
BAR_H = 64
SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0)
MODES = (("voice", "Voice"), ("chime", "Chimes"), ("silent", "Do not disturb"))
ENGINES = (("piper", "Light"), ("kokoro", "Natural"))  # the voice: Piper, or Kokoro (more expressive, more CPU)
HIST_ROWS, THREAD_ROWS, ROW_H = 5, 4, 22
LIMIT_H = 22  # a Limits row: the agent, then its 5-hour and weekly bars
LIMIT_NAMES = {"claude": "Claude Code", "codex": "Codex"}
THREAD_H = 36  # a thread row: its title and state, then the start of its last reply
# Tiles that only matter when replies are spoken: hidden in chime mode.
VOICE_ONLY = {"read", "wake", "replay"}
# Agents the panel can connect (install.INSTALLERS has the same keys).
AGENT_LABELS = (("claude", "Claude"), ("codex", "Codex"), ("gemini", "Gemini"), ("cursor", "Cursor"), ("cline", "Cline"),
                ("antigravity", "Antigravity"), ("claude-desktop", "Claude app"), ("windsurf", "Windsurf"),
                ("vscode", "VS Code"), ("browser", "Browser"))
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI", "cursor": "Cursor", "cline": "Cline",
               "antigravity": "Antigravity", "claude-desktop": "the Claude desktop app", "windsurf": "Windsurf",
               "vscode": "VS Code (Copilot agent)", "browser": "ChatGPT, Claude and Gemini in your browser"}
AGENTS_PER_ROW = 6
NEED_ROWS, NEED_H = 3, 28  # "needs you" rows at the top of the panel
# Banner kinds: the accent around it and its header.
BANNER_KINDS = {"you": ("#10E0A0", ""), "pending": ("#10E0A0", ""), "needs": ("#FBBF24", ""), "catchup": ("#8B7CFF", "While you were away"),
                "recap": ("#8B7CFF", "Today")}
GUIDE_URL = "https://github.com/ElMastho/agent-voice/blob/main/docs/guide.md"
COMPACT = 56  # compact mode: just the orb
# Notch mode: docked to the top centre of the screen like a camera notch, tucked away when idle.
NOTCH_W, NOTCH_H = 480, 44  # wide and short: one row of status, caption and chips
# Open, the notch takes a third of the screen (within these bounds); from TWO_COL_MIN its panel has two columns:
# controls and threads on the left, today's numbers, limits, what you said and the agents on the right.
PANEL_MAX_W, TWO_COL_MIN = 900, 600
ANIMATE_MS = 16  # 60 fps while something moves (at rest: no redraws at all, see _tick)
SCREEN_W = 0  # the screen width to lay out for (0: the real one; render_docs and tests set it)
KPI_H = 150  # the "Today" block: three numbers and the activity by hour
PHONE_H = 112  # the phone card: the QR code to scan, and its buttons
PHONE_FOLDED_H = 30  # folded away: one line
SCHED_H = 22  # an upcoming scheduled prompt
TILE_H, TILE_GAP = 30, 6  # the action buttons: an icon and its name on one line (the shortcut is in the tooltip)
NOTCH_RADIUS = 20
NOTCH_EAR = 9  # concave fillets where the island meets the top edge, like a hardware notch
NOTCH_PILL = 0.56  # tucked away, the notch is a pill this fraction of its full width
NOTCH_PEEK = 3  # pixels left showing when tucked under the top edge: hover them to bring it down
NOTCH_HIDE_DELAY = 3.0  # seconds it stays down after the mouse leaves or activity ends
OPTION_ROWS = 5  # question options shown in the panel
POS_FILE = HOME / "widget.json"

MAC = sys.platform == "darwin"
if sys.platform == "win32":
    FONT_TEXT, FONT_DISPLAY, ICON_FONT = "Segoe UI Variable Text", "Segoe UI Variable Display", "Segoe Fluent Icons"
else:  # the system UI font; icons fall back to plain symbols (Segoe Fluent Icons is Windows-only)
    FONT_TEXT = FONT_DISPLAY = ICON_FONT = ".AppleSystemUIFont" if MAC else "sans-serif"


def _font(family: str, size: int) -> QFont:
    """Sizes are Windows points (96 dpi); macOS counts 72 to the inch, so the same text needs a third more."""
    return QFont(family, round(size * 4 / 3) if MAC else size)


ICONS = {
    "headphones": "\ue7f6",
    "phone": "\ue8ea",
    "folder": "\ue838",
    "clock": "\ue823",
    "mic": "",
    "stop": "",
    "skip": "",
    "chat": "",
    "send": "",
    "volume": "",
    "mute": "",
    "read": "",
    "power": "",
    "bell": "",
    "copy": "",
    "check": "",
    "bolt": "",
    "list": "",
    "shield": "",
    "down": "",
    "up": "",
    "replay": "",
    "open": "",
    "help": "",
    "keyboard": "",
    "tuck": "",
}

if sys.platform != "win32":
    ICONS.update({"phone": "☏", "clock": "◷", "folder": "▤", "headphones": "∩"})
    ICONS.update({"mic": "●", "stop": "■", "skip": "⏭", "chat": "✎", "send": "➤", "volume": "♪", "mute": "✕",
                  "read": "☰", "power": "⏻", "bell": "♫", "copy": "⧉", "check": "✓", "bolt": "ϟ", "list": "≡",
                  "shield": "✓", "down": "⌄", "up": "⌃", "replay": "↻", "open": "↗", "help": "?", "keyboard": "⌨", "tuck": "–"})

# phase -> (accent colour, title, subtitle)
PHASES = {
    "idle": ("#94A3B8", "Ready", ""),
    "speaking": ("#8B7CFF", "Speaking", "Skip to answer now"),
    "waiting": ("#34D399", "Listening", "Go ahead, I'm listening"),
    "recording": ("#10E0A0", "Hearing you", "Pause to finish"),
    "transcribing": ("#FBBF24", "Transcribing", "One moment"),
    "muted": ("#F87171", "Do not disturb", "Nothing until you're back"),
    "offline": ("#64748B", "Offline", "Voice service isn't running"),
}



_QR: dict[str, list] = {}


def _paint_qr(p: QPainter, rect: QRectF, text: str) -> None:
    """A QR code for `text`, black on a white tile (with a quiet zone, as scanners like)."""
    if text not in _QR:
        import qrcode

        code = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
        code.add_data(text)
        code.make(fit=True)
        _QR.clear()
        _QR[text] = code.get_matrix()
    matrix = _QR[text]
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255))
    p.drawRoundedRect(rect, 6, 6)
    n = len(matrix)
    cell = (rect.width() - 8) / max(1, n)
    x0, y0 = rect.x() + (rect.width() - cell * n) / 2, rect.y() + (rect.height() - cell * n) / 2
    p.setBrush(QColor(0, 0, 0))
    for r, line in enumerate(matrix):
        for c, dark in enumerate(line):
            if dark:
                p.drawRect(QRectF(x0 + c * cell, y0 + r * cell, cell + 0.35, cell + 0.35))


def _limit_color(pct: int) -> QColor:
    return QColor("#34D399" if pct < 70 else "#FBBF24" if pct < 90 else "#F87171")


def _when_text(at: float, far: str = "%a") -> str:
    """A time coming up: '15:20' within the day, else `far` ('Mon', or 'Mon 09:00' with '%a %H:%M')."""
    from datetime import datetime

    soon = at - time.time() < 20 * 3600
    return datetime.fromtimestamp(at).strftime("%H:%M" if soon else far)


def _round_pen(brush, width: float) -> QPen:
    pen = QPen(brush, width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


FRAME = 1 / 30  # the frame the easing factors below were tuned for; real frames are timed (see _k)


def _ease(current: float, target: float, k: float) -> float:
    return current + (target - current) * k


def _per_frame(k: float, dt: float) -> float:
    """An easing factor tuned per 30 fps frame, for a frame that lasted `dt`: the same speed at any frame rate."""
    return 1 - (1 - k) ** (dt / FRAME)


def _spring(x: float, v: float, target: float, dt: float, damp: float, pull: float) -> tuple[float, float]:
    """The springs the notch and its banner were tuned with (per 30 fps frame), stepped in real time.

    Small sub-steps keep it stable when a frame comes late; the motion is the same whatever the frame rate.
    """
    steps = max(1, min(8, round(dt * 120)))
    h = dt / steps / FRAME
    for _ in range(steps):
        v = v * damp ** h + (target - x) * pull * h
        x += v * h
    return x, v


def _smooth(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _mix(a: QColor, b: QColor, k: float) -> QColor:
    def step(x: int, y: int) -> int:
        d = (y - x) * k
        # At least one unit per frame: truncating would stall a few shades short of the target forever.
        return y if abs(y - x) <= 1 else x + (int(d) or (1 if d > 0 else -1))

    return QColor(step(a.red(), b.red()), step(a.green(), b.green()), step(a.blue(), b.blue()))


def _hotkey_label(combo: str) -> str:
    return "+".join(part.strip().capitalize() for part in combo.split("+")) if combo else ""


# -- Windows window styles ---------------------------------------------------------------
def _keep_on_top(hwnd: int) -> None:
    """Put the window back at the top of the always-on-top band, without activating it.

    Other apps that come to the front (the Claude desktop app, a browser going full screen) can push
    a topmost tool window down; re-asserting it once a second keeps the notch reachable.
    """
    if sys.platform != "win32" or not hwnd:
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]
    HWND_TOPMOST = wintypes.HWND(-1)
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x1, 0x2, 0x10, 0x200
    user32.SetWindowPos(wintypes.HWND(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)


def _apply_windows_style(hwnd: int) -> None:
    """No focus stealing, no taskbar entry, always on top; no system rounding or border (we draw our shape)."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    dwm = ctypes.WinDLL("dwmapi")

    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x08000000, 0x00000080, 0x00000008
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)

    corner = ctypes.c_int(1)  # DWMWCP_DONOTROUND: the island draws its own rounded shape
    dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(corner), ctypes.sizeof(corner))
    # Hide the grey system border.
    no_border = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE
    dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34, ctypes.byref(no_border), ctypes.sizeof(no_border))



MENU_STYLE = (
    "QMenu{background:#161925;color:#E5E7EB;border:1px solid #2A2F3E;border-radius:10px;padding:6px;}"
    "QMenu::item{padding:6px 22px 6px 14px;border-radius:6px;}"
    "QMenu::item:selected{background:#2A2F45;}"
    "QMenu::item:disabled{color:#7C8496;}"
    "QMenu::separator{height:1px;background:#2A2F3E;margin:4px 8px;}"
    "QMenu::indicator{width:14px;height:14px;margin-left:4px;}"
)


def orb_icon(color: QColor, glyph: str, size: int = 64) -> QIcon:
    """The widget's orb as an app/tray icon, tinted with the current phase colour."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QPointF(size / 2, size / 2)
    r = size * 0.44
    core = QRadialGradient(QPointF(c.x() - r * 0.35, c.y() - r * 0.45), r * 1.6)
    core.setColorAt(0, color.lighter(165))
    core.setColorAt(0.55, color)
    core.setColorAt(1, color.darker(170))
    p.setPen(Qt.NoPen)
    p.setBrush(core)
    p.drawEllipse(c, r, r)
    p.setFont(_font(ICON_FONT, int(size * 0.3)))
    p.setPen(QColor(255, 255, 255, 240))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, glyph)
    p.end()
    return QIcon(pm)


# -- background I/O ----------------------------------------------------------------------
class Poller(QObject):
    status = Signal(object)

    def __init__(self, interval: float = 0.12):
        super().__init__()
        self.interval = interval
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        tag = ""
        while True:
            # At rest, wait for the daemon to report a change (one request every 20 s) instead of asking
            # several times a second; while something moves, poll for the mic level and the animations.
            waiting = self.interval >= 0.5 and tag
            path = f"/status?wait={tag}&timeout=20" if waiting else "/status"
            st = client.request(path, timeout=25 if waiting else 0.5)
            tag = (st or {}).get("tag", "")
            try:
                self.status.emit(st)
            except RuntimeError:  # widget closed
                return
            if not waiting or st is None:
                time.sleep(self.interval)


def _post(path: str, payload: dict | None = None) -> None:
    threading.Thread(target=client.request, args=(path, payload or {}), daemon=True).start()


# -- the widget --------------------------------------------------------------------------
# The banner's buttons: (icon, label).
BANNER_BUTTONS = {"type": ("keyboard", "Reply"), "talk": ("mic", "Talk"), "send": ("send", "Send now"),
                  "edit": ("keyboard", "Edit"), "cancel": ("stop", "Cancel")}
# How long a notification stays (Chimes), and how long the read-along text stays once the voice ends.
SHOW_FOR, LINGER = 10.0, 5.0


class Banner(QWidget):
    """Chime mode's notification: the reply drops out from under the notch, stays a few seconds, and
    goes back up. Swipe it up to dismiss it; click it to open the conversation. Hovering keeps it."""

    LINES = 4

    def __init__(self):
        super().__init__(
            None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)  # macOS hides tool windows when another app is active
        self.setMouseTracking(True)
        self.item: dict | None = None
        self.holding = False  # reading along with the voice
        self.dismissed = False
        self.lines: list[str] = []
        self.t, self.v = 0.0, 0.0  # appearance (a spring) and its velocity
        self.until = 0.0
        self.drag = 0.0  # how far it has been pulled up
        self._press: QPointF | None = None
        self._hover = False
        self._hot: str | None = None  # the button under the mouse
        self.on_reply = None  # opens the reply box (set by the widget)
        self._topmost_at = 0.0
        self._shown_left = -1
        self.f_head = _font(FONT_TEXT, 8)
        self.f_icon = _font(ICON_FONT, 9)
        self.f_head.setWeight(QFont.Medium)
        self.f_text = _font(FONT_TEXT, 9)

    def showEvent(self, event):
        super().showEvent(event)
        _apply_windows_style(int(self.winId()))

    def present(self, item: dict, width: int, hold: bool = False) -> None:
        """Show `item`; `hold` keeps it until release() (read along: as long as the voice plays)."""
        self.item, self.drag, self.holding = item, 0.0, hold
        self.LINES = 9 if item.get("kind") in ("recap", "catchup") else Banner.LINES  # a summary needs the room
        self._layout(width)
        words = len(item.get("text", "").split())
        self.until = 1e12 if hold else time.time() + min(30.0, SHOW_FOR + max(0, words - 12) * 0.5)  # more if long
        if item.get("kind") == "pending":  # about to be sent: as long as it waits (what happens next replaces it)
            self.until = float(item.get("until") or time.time()) + 2.0
        if not self.isVisible():
            self.t, self.v = 0.0, 0.0
            self.show()

    def _layout(self, width: int) -> None:
        """Wrap the text for `width` and size the banner to it."""
        fm = QFontMetrics(self.f_text)
        self.lines = self._wrap((self.item or {}).get("text", ""), fm, width - 36)
        self.resize(width, int(40 + len(self.lines) * fm.lineSpacing() + 14 + (26 if self._buttons_shown() else 0)))

    def _can_reply(self) -> bool:
        """An agent's message (a reply, a question waiting on you): it can be answered from here."""
        item = self.item or {}
        return bool(item.get("session")) and item.get("kind") not in ("you", "catchup", "note", "recap")

    def _buttons_shown(self) -> bool:
        return self._can_reply() or (self.item or {}).get("kind") == "pending"

    def _buttons(self) -> dict[str, QRectF]:
        """Reply by typing, or by voice, to the conversation this banner is from; or, for a message about to be
        sent: send it now, keep it to fix it, or drop it."""
        if not self._buttons_shown():
            return {}
        y, h, right = self.height() - 34, 22, self.width() - 16
        if (self.item or {}).get("kind") == "pending":
            out = {}
            for key, w in (("cancel", 74), ("edit", 62), ("send", 90)):
                out[key] = QRectF(right - w, y, w, h)
                right -= w + 8
            return out
        return {"talk": QRectF(right - 62, y, 62, h), "type": QRectF(right - 62 - 8 - 70, y, 70, h)}

    def _wrap(self, text: str, fm: QFontMetrics, width: float) -> list[str]:
        lines, line = [], ""
        words = text.split()
        for i, word in enumerate(words):
            candidate = f"{line} {word}".strip()
            if fm.horizontalAdvance(candidate) <= width or not line:
                line = candidate
                continue
            lines.append(line)
            line = word
            if len(lines) == self.LINES - 1:
                line = " ".join(words[i:])
                break
        if line:
            lines.append(fm.elidedText(line, Qt.ElideRight, int(width)) if len(lines) == self.LINES - 1 else line)
        return lines or [""]

    def _left(self) -> int:
        """Seconds before the message waiting under the notch is sent."""
        return max(0, math.ceil(float((self.item or {}).get("until") or 0) - time.time()))

    def dismiss(self) -> None:
        self.until = 0.0
        self.dismissed = self.holding  # swiped away while reading along: not again for this speech

    def release(self) -> None:
        """The voice stopped: go away with it."""
        if self.holding:
            self.holding = False
            self.until = min(self.until, time.time() + LINGER)  # time to finish reading it

    def step(self, anchor: QRectF, dt: float = FRAME, width: int = 0) -> bool:
        """Move with the notch (or bar), as wide as it is now; returns True while it is animating."""
        if self.item is None:
            return False
        if width and abs(width - self.width()) >= 1:
            self._layout(width)  # the notch opened or closed: the banner keeps its width, frame by frame
        now = time.time()
        if self.item.get("kind") == "pending":
            left = self._left()
            if left != self._shown_left:  # the countdown: one repaint a second
                self._shown_left = left
                self.update()
        elif self._hover and self.until:
            self.until = max(self.until, now + 1.5)  # reading it: don't take it away
        target = 1.0 if now < self.until else 0.0
        self.t, self.v = _spring(self.t, self.v, target, dt, 0.62, 0.14)
        if self._press is None and self.drag:
            self.drag = 0.0 if abs(self.drag) < 0.5 else self.drag * (1 - _per_frame(0.3, dt))  # snap back
        settled = abs(target - self.t) < 0.003 and abs(self.v) < 0.003 and not self.drag
        if settled:
            self.t, self.v = target, 0.0
        if target == 0.0 and self.t <= 0.01:
            self.hide()
            self.item = None
            return False
        geo = QApplication.primaryScreen().geometry()
        y = max(geo.top() + 4, anchor.bottom() + 8)
        if y + self.height() > geo.bottom():  # a bar near the bottom: show it above instead
            y = anchor.top() - 8 - self.height()
        y = int(y - 18 * (1 - self.t) + min(0.0, self.drag))
        x = int(anchor.center().x() - self.width() / 2)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
        opacity = max(0.0, min(1.0, self.t)) * (1 - min(1.0, -min(0.0, self.drag) / 60))
        if abs(self.windowOpacity() - opacity) > 0.004:
            self.setWindowOpacity(opacity)
        if now - self._topmost_at > 1.0:
            self._topmost_at = now
            _keep_on_top(int(self.winId()))
        if not settled:
            self.update()
        return not settled

    def paintEvent(self, _event):
        if not self.item:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        kind = self.item.get("kind")
        yours = kind == "you"
        accent, fixed_title = BANNER_KINDS.get(kind, ("", ""))
        body = QRectF(1, 1, self.width() - 2, self.height() - 2)
        if accent:
            # Your words (green), an agent waiting on you (amber), the catch-up (violet): framed in colour,
            # never mistaken for a reply.
            ring = QColor(accent)
            ring.setAlpha(210 if self._hover else 170)
            p.setPen(QPen(ring, 1.6))
        else:
            p.setPen(QPen(QColor(255, 255, 255, 26 + (14 if self._hover else 0)), 1))
        p.setBrush(QColor(0, 0, 0, 245))
        p.drawRoundedRect(body, 18, 18)
        color = QColor(accent if yours or kind in ("catchup", "recap") else self.item.get("color") or "#94A3B8")
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(21, 20), 3, 3)
        p.setFont(self.f_head)
        p.setPen(QColor(255, 255, 255, 150))
        title = fixed_title or self.item.get("title") or ""
        if kind == "pending":
            title = f"Sending to {title}" if title else "Sending"
        elif yours:
            title = f"You  →  {title}" if title else "You said"
        head = QFontMetrics(self.f_head).elidedText(title or "Reply", Qt.ElideRight, self.width() - 90)
        p.drawText(QRectF(30, 10, self.width() - 90, 20), Qt.AlignLeft | Qt.AlignVCenter, head)
        p.setPen(QColor(255, 255, 255, 90))
        tag = "speaking" if self.holding else self.item.get("tag") or ("" if kind in BANNER_KINDS else "done")
        if kind == "pending":
            tag = f"in {self._left()} s · say “wait” or “cancel”"
        p.drawText(QRectF(0, 10, self.width() - 18, 20), Qt.AlignRight | Qt.AlignVCenter, tag)
        p.setFont(self.f_text)
        p.setPen(QColor(255, 255, 255, 228))
        fm = QFontMetrics(self.f_text)
        y = 34.0
        for line in self.lines:
            p.drawText(QPointF(18, y + fm.ascent()), line)
            y += fm.lineSpacing()
        for key, rect in self._buttons().items():
            hot = key == self._hot
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 48 if hot else 22))
            p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            p.setFont(self.f_icon)
            p.setPen(QColor(255, 255, 255, 235))
            icon, label = BANNER_BUTTONS[key]
            icon_rect = QRectF(rect.x() + 8, rect.y(), 16, rect.height())
            p.drawText(icon_rect, Qt.AlignCenter, ICONS[icon])
            p.setFont(self.f_head)
            p.drawText(rect.adjusted(26, 0, -8, 0), Qt.AlignLeft | Qt.AlignVCenter, label)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 45))  # a grabber: it can be swiped away
        p.drawRoundedRect(QRectF(self.width() / 2 - 16, self.height() - 7, 32, 3), 1.5, 1.5)
        p.end()

    def enterEvent(self, _e):
        self._hover = True
        self.update()

    def leaveEvent(self, _e):
        self._hover, self._hot = False, None
        self.update()

    def _button_at(self, pos: QPointF) -> str | None:
        return next((key for key, rect in self._buttons().items() if rect.contains(pos)), None)

    def mousePressEvent(self, e):
        self._press = e.globalPosition()

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.LeftButton:
            self.drag = e.globalPosition().y() - self._press.y()
            self.update()
            return
        hot = self._button_at(e.position())
        if hot != self._hot:
            self._hot = hot
            self.update()

    def mouseReleaseEvent(self, e):
        if self._press is None:
            return
        moved = e.globalPosition() - self._press
        self._press = None
        if self.drag < -18 or abs(moved.x()) > 60:
            self.dismiss()  # swiped away
        elif abs(moved.x()) + abs(moved.y()) < 5 and self.item:
            button = self._button_at(e.position())
            if self.item.get("kind") == "pending":
                if button:  # what happens to it next replaces this banner
                    _post("/pending", {"id": self.item.get("pending"),
                                       "action": {"send": "send", "edit": "hold", "cancel": "cancel"}[button]})
                return
            if button == "type" and self.on_reply:
                self.on_reply(self.item)
            elif button == "talk":
                _post("/reply", {"session": self.item.get("session", "")})
            else:
                _post("/open_thread", {"session": self.item.get("session", "")})
            self.dismiss()


NEW = "__new__"  # the box's "session" while it starts a new thread


class Preview(QWidget):
    """A thread's last reply in full (markdown: code, lists, tables), beside the panel while you hover its row.

    Like the notch it never takes the focus; it scrolls with the wheel and goes when the mouse leaves it and the row.
    """

    WIDTH, MAX_H = 480, 560

    def __init__(self):
        super().__init__(
            None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.session = ""
        self.color = "#94A3B8"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 10, 12)
        layout.setSpacing(6)
        self.head = QLabel()
        self.head.setFont(_font(FONT_TEXT, 8))
        self.head.setStyleSheet("color: rgba(255,255,255,170); background: transparent;")
        self.body = QTextBrowser()
        self.body.setFocusPolicy(Qt.NoFocus)
        self.body.setOpenLinks(False)
        self.body.setFrameShape(QFrame.NoFrame)
        self.body.setFont(_font(FONT_TEXT, 9))
        self.body.setStyleSheet(
            "QTextBrowser { background: transparent; color: rgba(255,255,255,225); border: none; }"
            "QScrollBar:vertical { width: 6px; background: transparent; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,50); border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }")
        self.body.document().setDefaultStyleSheet(
            "code, pre { font-family: Consolas, Menlo, monospace; background-color: #1E1E24; }"
            "a { color: #8B7CFF; } h1, h2, h3 { color: white; }")
        layout.addWidget(self.head)
        layout.addWidget(self.body)

    def showEvent(self, event):
        super().showEvent(event)
        _apply_windows_style(int(self.winId()))

    def present(self, detail: dict, beside: QRect, row_y: int) -> None:
        """Show `detail` (GET /thread) next to `beside` (the notch), level with the hovered row."""
        self.session = detail.get("session", "")
        self.color = detail.get("color") or "#94A3B8"
        parts = [detail.get("title", "")]
        if detail.get("result"):
            parts.append(result_label(detail["result"]))
        if detail.get("tokens"):
            parts.append(f"{tokens_label(int(detail['tokens']))} tokens today")
        if detail.get("context") is not None:
            parts.append(f"context {detail['context']}%")
        self.head.setText("  ·  ".join(p for p in parts if p))
        self.body.setMarkdown(detail.get("text") or "_No reply yet._")
        self.body.document().setTextWidth(self.WIDTH - 40)
        h = int(min(self.MAX_H, self.body.document().size().height() + 56))
        screen = (QApplication.screenAt(beside.center()) or QApplication.primaryScreen()).availableGeometry()
        x = beside.left() - self.WIDTH - 10
        if x < screen.left() + 8:  # no room on the left: the right
            x = min(beside.right() + 10, screen.right() - self.WIDTH - 8)
        y = max(screen.top() + 8, min(beside.top() + row_y - 20, screen.bottom() - h - 8))
        self.setGeometry(x, y, self.WIDTH, h)
        self.body.verticalScrollBar().setValue(0)
        self.show()
        self.raise_()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(QColor(0, 0, 0, 245))
        p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 16, 16)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.color))
        p.drawEllipse(QPointF(11, 21), 3, 3)


class ReplyBox(QWidget):
    """Type a reply to one conversation (Enter sends it there, Shift+Enter starts a new line, Esc closes), or
    the first prompt of a new thread: where it runs (the Claude app, a CLI agent in a new tab) and in which
    folder, typed or dictated.

    Unlike the notch, this window does take the keyboard (you type in it); it closes as soon as you click
    elsewhere, and what you typed stays for next time.
    """

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.session = ""
        box = QFrame(self)
        box.setObjectName("box")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(16, 10, 16, 8)
        layout.setSpacing(6)
        self.title = QLabel()
        # New thread: where it runs, and in which folder.
        self.options = QWidget()
        row = QHBoxLayout(self.options)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.chips: dict[str, QPushButton] = {}
        for key, label in TARGET_LABELS.items():
            chip = QPushButton(label)
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(lambda _=False, k=key: self._pick(k))
            self.chips[key] = chip
            row.addWidget(chip)
        row.addStretch(1)
        self.talk = QPushButton(f"{ICONS['mic']}  Talk")
        self.talk.setObjectName("talk")
        self.talk.setCursor(Qt.PointingHandCursor)
        self.talk.clicked.connect(self._dictate)
        row.addWidget(self.talk)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Folder: pick one with the button, or type its path")
        self.folder.installEventFilter(self)
        self._picking = False  # the folder dialog is open: losing the focus to it mustn't close the box
        self.recent: list[str] = []
        self._complete_folders()
        self.pick = QPushButton(f"{ICONS['folder']}  Folders")
        self.pick.setObjectName("pick")
        self.pick.setCursor(Qt.PointingHandCursor)
        self.pick.clicked.connect(self._folder_menu)
        self.folder_row = QWidget()
        frow = QHBoxLayout(self.folder_row)
        frow.setContentsMargins(0, 0, 0, 0)
        frow.setSpacing(6)
        frow.addWidget(self.folder, 1)
        frow.addWidget(self.pick)
        self.target = ""
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("Your reply")
        self.edit.installEventFilter(self)
        self.hint = QLabel("Enter to send · Shift+Enter for a new line · Esc to close")
        # Later instead of now: "14:00", "in 30 min", "tomorrow 9am" (a scheduled prompt).
        self.when = QLineEdit()
        self.when.setPlaceholderText("Send now, or at 14:00 / in 30 min")
        self.when.setFixedWidth(200)
        self.when.installEventFilter(self)
        bottom = QWidget()
        foot = QHBoxLayout(bottom)
        foot.setContentsMargins(0, 0, 0, 0)
        foot.addWidget(self.hint, 1)
        foot.addWidget(self.when)
        for w in (self.title, self.options, self.folder_row, self.edit, bottom):
            layout.addWidget(w)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        text, head = f"font-family:'{FONT_TEXT}';", f"font-family:'{FONT_TEXT}';"
        self.setStyleSheet(
            "#box{background:#000;border:1.5px solid #10E0A0;border-radius:18px;}"
            f"QLabel{{color:rgba(255,255,255,150);font-size:{_font(FONT_TEXT, 8).pointSize()}pt;{head}}}"
            f"QPlainTextEdit{{background:transparent;border:none;color:#fff;font-size:"
            f"{_font(FONT_TEXT, 10).pointSize()}pt;{text}selection-background-color:#10E0A0;}}"
            f"QLineEdit{{background:rgba(255,255,255,14);border:none;border-radius:8px;padding:3px 8px;"
            f"color:rgba(255,255,255,200);font-size:{_font(FONT_TEXT, 8).pointSize()}pt;{text}}}"
            f"QPushButton{{background:rgba(255,255,255,18);border:none;border-radius:10px;padding:3px 10px;"
            f"color:rgba(255,255,255,190);font-size:{_font(FONT_TEXT, 8).pointSize()}pt;{text}}}"
            "QPushButton:hover{background:rgba(255,255,255,40);}"
            "QPushButton:checked{background:rgba(16,224,160,70);color:#fff;}"
            f"#talk, #pick{{font-family:'{ICON_FONT}','{FONT_TEXT}';}}")
        self.hint.setStyleSheet("color:rgba(255,255,255,90);")

    def open(self, item: dict, anchor: QRectF, width: int) -> None:
        if item.get("session") != self.session:
            self.edit.clear()  # a draft belongs to its conversation
        self.session = item.get("session", "")
        self.title.setText(f"Reply to {item.get('title') or 'the conversation'}")
        self.options.hide()
        self.folder_row.hide()
        self.edit.setPlaceholderText("Your reply")
        self._show(anchor, width, 132)

    def open_new(self, targets: list[str], default: dict, anchor: QRectF, width: int) -> None:
        """A new thread: pick where it runs (like your last one by default) and its folder, then type or talk."""
        if self.session != NEW:
            self.edit.clear()
            self.folder.setText(default.get("cwd", ""))
        self.recent = [c for c in default.get("recent") or [] if c]
        self.session = NEW
        self.title.setText("New thread")
        for key, chip in self.chips.items():
            chip.setVisible(key in targets)
        self._pick(default.get("target") if default.get("target") in targets else (targets or [""])[0])
        self.options.show()
        self.edit.setPlaceholderText("What should it do?" if targets else "No agent found to start a thread with")
        self._show(anchor, width, 196)

    def _pick(self, key: str) -> None:
        self.target = key
        for k, chip in self.chips.items():
            chip.setChecked(k == key)
        # The desktop app picks the folder's group; a terminal opens in it. Both use the folder.
        self.folder_row.setVisible(bool(key))

    def _complete_folders(self) -> None:
        """Typing a path completes it, folders only, like the address bar of the file explorer."""
        from PySide6.QtCore import QDir
        from PySide6.QtWidgets import QCompleter, QFileSystemModel

        model = QFileSystemModel(self)
        model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot | QDir.Drives)
        model.setRootPath("")
        completer = QCompleter(model, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.folder.setCompleter(completer)

    def _folder_menu(self) -> None:
        """The folders you worked in lately, and Browse… for any other."""
        menu = QMenu(self)
        menu.setStyleSheet(MENU_STYLE)
        for path in self.recent[:8]:
            act = menu.addAction(path)
            act.triggered.connect(lambda _=False, p=path: self._use_folder(p))
        if self.recent:
            menu.addSeparator()
        menu.addAction("Browse…").triggered.connect(self._browse)
        self._picking = True
        menu.exec(self.pick.mapToGlobal(self.pick.rect().bottomLeft()))
        self._picking = False

    def _browse(self) -> None:
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        self._picking = True
        try:
            start = self.folder.text().strip() or str(Path.home())
            chosen = QFileDialog.getExistingDirectory(self, "Where should the new thread work?", start)
        finally:
            self._picking = False
        if chosen:
            self._use_folder(str(Path(chosen)))
        self.show()
        self.activateWindow()
        self.edit.setFocus()

    def _use_folder(self, path: str) -> None:
        self.folder.setText(path)
        self.edit.setFocus()

    def _dictate(self) -> None:
        self.hide()
        _post("/new_thread", {"listen": True, "target": self.target, "cwd": self.folder.text().strip()})

    def _show(self, anchor: QRectF, width: int, height: int) -> None:
        self.resize(width, height)
        geo = QApplication.primaryScreen().geometry()
        y = int(max(geo.top() + 4, anchor.bottom() + 8))
        self.move(int(anchor.center().x() - width / 2), y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()
        _post("/listen_cancel")  # the reply mic would only hear the keyboard

    def send(self) -> None:
        from agent_voice.schedule import parse_when

        text = self.edit.toPlainText().strip()
        if not text:
            return
        when = self.when.text().strip()
        if when:
            at = parse_when(when, time.time())
            if not at:  # not a time we understand: say so, keep everything
                self.when.setStyleSheet("background:rgba(248,113,113,60);")
                self.when.setFocus()
                return
            self.hide()
            target = ({"new": {"target": self.target, "cwd": self.folder.text().strip()}} if self.session == NEW
                      else {"session": self.session})
            _post("/schedule", {"prompt": text, "at": at, **target})
            self.edit.clear()
            self.when.clear()
            self.when.setStyleSheet("")
            return
        self.hide()  # first: the reply goes to the agent's window, then you're brought back
        if self.session == NEW:
            _post("/new_thread", {"text": text, "target": self.target, "cwd": self.folder.text().strip()})
        else:
            _post("/send", {"session": self.session, "text": text})
        self.edit.clear()

    def eventFilter(self, obj, ev):
        if not hasattr(self, "when"):
            return super().eventFilter(obj, ev)  # still being built (the folder completer sends events early)
        if obj is self.when and ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.send()
            return True
        if obj is self.when and ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Escape:
            self.hide()
            return True
        if obj is self.folder and ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.edit.setFocus()  # Enter in the folder line moves on to the prompt
            return True
        if obj is self.folder and ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Escape:
            self.hide()
            return True
        if obj is self.edit and ev.type() == QEvent.KeyPress:
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not ev.modifiers() & Qt.ShiftModifier:
                self.send()
                return True
            if ev.key() == Qt.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, ev)

    def changeEvent(self, ev):
        if (ev.type() == QEvent.ActivationChange and not self.isActiveWindow() and self.isVisible()
                and not self._picking):
            self.hide()  # clicked elsewhere: out of the way (the draft stays)
        super().changeEvent(ev)


class _Fetched(QObject):
    """Answers fetched off the UI thread, delivered on it."""

    done = Signal(object)


def _when(at: float) -> str:
    """'14:20' today, 'Mon 14:20' this week, '12 Oct' before."""
    from datetime import datetime

    then, now = datetime.fromtimestamp(at), datetime.fromtimestamp(time.time())
    if then.date() == now.date():
        return then.strftime("%H:%M")
    return then.strftime("%a %H:%M" if (now - then).days < 6 else "%d %b").lstrip("0")


class HistoryBox(QWidget):
    """Search every conversation: what you asked and what each agent answered, across all of them. Type to search
    (every word, as a prefix); the list shows the best matches, the right side the whole entry. Esc closes.

    Like the reply box, it takes the keyboard (you type in it) and closes when you click elsewhere.
    """

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        from PySide6.QtWidgets import QListWidget

        self.entry: dict = {}
        self._asked = ""
        self._fetched = _Fetched()
        self._fetched.done.connect(self._arrived)
        self._timer = QTimer(self, singleShot=True, interval=220, timeout=self._search)
        box = QFrame(self)
        box.setObjectName("box")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(8)
        self.title = QLabel("History · every conversation, on this PC")
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search what you asked and what they answered (login bug, migration, Codex...)")
        self.query.textChanged.connect(lambda _text: self._timer.start())
        self.query.installEventFilter(self)
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # entries wrap to the list's width
        self.results.setResizeMode(QListWidget.Adjust)
        self.results.currentRowChanged.connect(self._pick)
        self.head = QLabel()
        self.body = QTextBrowser()
        self.body.setOpenLinks(False)
        self.body.document().setDefaultStyleSheet(
            "code, pre { font-family: Consolas, Menlo, monospace; background-color: #1E1E24; } a { color: #8B7CFF; }")
        self.copy = QPushButton(f"{ICONS['copy']}  Copy")
        self.copy.clicked.connect(self._copy)
        self.open_thread = QPushButton(f"{ICONS['open']}  Open the thread")
        self.open_thread.clicked.connect(self._open_thread)
        for button in (self.copy, self.open_thread):
            button.setObjectName("act")
            button.setCursor(Qt.PointingHandCursor)
        right = QWidget()
        side = QVBoxLayout(right)
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(self.head)
        side.addWidget(self.body, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.copy)
        buttons.addWidget(self.open_thread)
        side.addLayout(buttons)
        split = QHBoxLayout()
        split.addWidget(self.results, 4)
        split.addWidget(right, 5)
        for w in (self.title, self.query):
            layout.addWidget(w)
        layout.addLayout(split, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        small, text = _font(FONT_TEXT, 8).pointSize(), _font(FONT_TEXT, 9).pointSize()
        self.setStyleSheet(
            "#box{background:#000;border:1.5px solid #8B7CFF;border-radius:18px;}"
            f"QLabel{{color:rgba(255,255,255,150);font-size:{small}pt;font-family:'{FONT_TEXT}';}}"
            f"QLineEdit{{background:rgba(255,255,255,14);border:none;border-radius:8px;padding:5px 10px;color:#fff;"
            f"font-size:{text}pt;font-family:'{FONT_TEXT}';selection-background-color:#8B7CFF;}}"
            f"QListWidget{{background:transparent;border:none;color:rgba(255,255,255,210);font-size:{small}pt;"
            f"font-family:'{FONT_TEXT}';outline:none;}}"
            "QListWidget::item{padding:6px 8px;border-radius:8px;margin:1px 0;}"
            "QListWidget::item:selected{background:rgba(139,124,255,70);color:#fff;}"
            "QListWidget::item:hover{background:rgba(255,255,255,18);}"
            f"QTextBrowser{{background:transparent;border:none;color:rgba(255,255,255,225);font-size:{text}pt;"
            f"font-family:'{FONT_TEXT}';}}"
            f"QPushButton#act{{background:rgba(255,255,255,18);border:none;border-radius:10px;padding:4px 12px;"
            f"color:rgba(255,255,255,200);font-size:{small}pt;font-family:'{ICON_FONT}','{FONT_TEXT}';}}"
            "QPushButton#act:hover{background:rgba(255,255,255,40);}"
            "QScrollBar:vertical{width:6px;background:transparent;}"
            "QScrollBar::handle:vertical{background:rgba(255,255,255,50);border-radius:3px;min-height:30px;}"
            "QScrollBar::add-line, QScrollBar::sub-line{height:0;}")

    def open(self, anchor: QRectF, width: int) -> None:
        self.resize(max(560, width), 460)
        geo = QApplication.primaryScreen().geometry()
        self.move(int(anchor.center().x() - self.width() / 2), int(max(geo.top() + 4, anchor.bottom() + 8)))
        self.show()
        self.raise_()
        self.activateWindow()
        self.query.setFocus()
        self.query.selectAll()
        self._search()

    def _search(self) -> None:
        from urllib.parse import quote

        asked = self.query.text().strip()
        self._asked = asked
        self._get(f"/history?q={quote(asked)}&limit=40", ("list", asked))

    def _get(self, path: str, tag: tuple) -> None:
        def fetch():
            try:
                self._fetched.done.emit((tag, client.request(path, timeout=3.0)))
            except RuntimeError:  # closed meanwhile
                pass

        threading.Thread(target=fetch, daemon=True).start()

    def _arrived(self, answer) -> None:
        (kind, *rest), data = answer
        if kind == "list":
            if rest[0] != self._asked:
                return  # an older search: you typed on since
            self._fill((data or {}).get("results") or [], off=bool((data or {}).get("off")), offline=data is None)
        elif kind == "entry" and data and data.get("id") == rest[0]:
            self._present(data)

    def _fill(self, rows: list[dict], off: bool = False, offline: bool = False) -> None:
        from PySide6.QtWidgets import QListWidgetItem

        self.results.clear()
        self.rows = rows
        for row in rows:
            who = "You" if row["kind"] == "you" else (row.get("title") or row.get("agent") or "Agent")
            where = f" → {row['title']}" if row["kind"] == "you" and row.get("title") else ""
            snippet = row.get("snippet", "").replace("[", "").replace("]", "")
            QListWidgetItem(f"{who}{where} · {_when(row['at'])}\n{snippet}", self.results)
        if rows:
            self.results.setCurrentRow(0)
            return
        self.entry = {}
        self.head.setText("")
        self.body.setMarkdown("_agent-voice isn't running._" if offline else
                              "_History is off (`history.enabled` in the config)._" if off else
                              "_Nothing found._" if self._asked else "_Nothing yet: your conversations show up here._")

    def _pick(self, row: int) -> None:
        rows = getattr(self, "rows", [])
        if 0 <= row < len(rows):
            self._get(f"/history?id={rows[row]['id']}", ("entry", rows[row]["id"]))

    def _present(self, entry: dict) -> None:
        self.entry = entry
        who = "You asked" if entry["kind"] == "you" else f"{entry.get('agent') or 'The agent'} answered"
        self.head.setText(f"{who} · {entry.get('title') or ''} · {_when(entry['at'])}")
        self.body.setMarkdown(entry.get("text") or "")
        self.body.verticalScrollBar().setValue(0)
        self.open_thread.setEnabled(bool(entry.get("session")))

    def _copy(self) -> None:
        if self.entry:
            QApplication.clipboard().setText(self.entry.get("text", ""))
            self.copy.setText(f"{ICONS['check']}  Copied")
            QTimer.singleShot(1200, lambda: self.copy.setText(f"{ICONS['copy']}  Copy"))

    def _open_thread(self) -> None:
        if self.entry.get("session"):
            self.hide()
            _post("/open_thread", {"session": self.entry["session"]})

    def eventFilter(self, obj, ev):
        if obj is self.query and ev.type() == QEvent.KeyPress:
            if ev.key() == Qt.Key_Escape:
                self.hide()
                return True
            if ev.key() in (Qt.Key_Down, Qt.Key_Up) and self.results.count():
                step = 1 if ev.key() == Qt.Key_Down else -1
                self.results.setCurrentRow(max(0, min(self.results.count() - 1, self.results.currentRow() + step)))
                return True
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._open_thread()
                return True
        return super().eventFilter(obj, ev)

    def changeEvent(self, ev):
        if ev.type() == QEvent.ActivationChange and not self.isActiveWindow() and self.isVisible():
            self.hide()  # clicked elsewhere: out of the way
        super().changeEvent(ev)


class VoiceWidget(QWidget):
    def __init__(self):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setMouseTracking(True)
        self.setWindowTitle("agent-voice")

        cfg = load_config()
        self.hotkeys = cfg["hotkeys"]

        self.st: dict = {}
        self.phase = "offline"
        self.accent = QColor(PHASES["offline"][0])
        self._accent_target = QColor(self.accent)
        self.level = 0.0
        self.t0 = time.time()
        self.energy = 0.0  # how lively the bars and the edge light are, eased per phase
        self.hover_t: dict[str, float] = {}  # eased hover amount per button
        self.speed_pos: float | None = None  # animated index of the speed indicator
        self.mode_pos: float | None = None  # same for the voice/chime switch
        self.expanded = False
        self.compact = False
        self.notch = False
        self.banner = Banner()
        self.reply_box = ReplyBox()
        self.history_box = HistoryBox()
        self.preview = Preview()
        self._preview_for = ""  # the thread row the preview is (or is about to be) for
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._show_preview)
        self._preview_watch = QTimer(self)  # hides it once the mouse is on neither the row nor the preview
        self._preview_watch.timeout.connect(self._check_preview)
        self.banner.on_reply = self.open_reply
        self._tucked: tuple | None = None  # tucked away by hand: what was showing then (news brings it back)
        self._tuck_hover = False  # the mouse that clicked "tuck" is still over it: don't hold it down
        self._banner_seen: int | None = None
        self._boot: int | None = None  # the service's start: when it changes, it was restarted (and so are we)
        self._opens: int | None = None
        self.phone_folded = False  # the phone card folded to one line (kept in widget.json)
        self._quitting = False
        self._reading_shown: tuple | None = None
        self._banner_busy = False
        self._bar_pos: tuple[int, int] | None = None  # where the floating bar was before notch mode
        self._reveal = 1.0  # notch: 0 tucked under the top edge, 1 fully down (a spring: may overshoot)
        self._reveal_v = 0.0
        self._nw = float(NOTCH_W)  # the notch's width now (it widens as the panel opens)
        self._vw = float(NOTCH_W)  # notch: the drawn island's width (the window, its canvas, is resized rarely)
        self._last_tick = time.time()
        self._dt = FRAME
        screen = QApplication.primaryScreen()
        self._screen_w = screen.geometry().width() if screen else 3 * NOTCH_W  # read once per frame, not per call
        self._reveal_until = 0.0
        self._h = float(BAR_H)  # eased window height
        self._painted_still = False  # battery: the still frame has been drawn, skip redraws until a change
        self._topmost_at = 0.0
        self._shown_digest = ""
        self._copied: str | None = None  # history row just copied, for a brief confirmation
        self._copied_at = 0.0
        self.hover: str | None = None
        self.pressed: str | None = None
        self.f_kpi = _font(FONT_TEXT, 12)
        self.f_kpi.setWeight(QFont.DemiBold)
        self._dragged = False
        self._opacity = 1.0
        self._hovering = False
        self._drop: dict | None = None  # files being dragged over: where they'd go
        self.setAcceptDrops(True)

        self.f_title = _font(FONT_DISPLAY, 10)
        self.f_title.setWeight(QFont.DemiBold)
        self.f_sub = _font(FONT_TEXT, 8)
        self.f_tile = _font(FONT_TEXT, 8)
        self.f_tile.setWeight(QFont.Medium)
        self.f_hint = _font(FONT_TEXT, 7)
        self.f_notch_title = _font(FONT_DISPLAY, 9)
        self.f_notch_title.setWeight(QFont.DemiBold)
        self.f_notch_sub = _font(FONT_TEXT, 8)
        self.f_notch_chip = _font(FONT_TEXT, 7)
        self.f_notch_chip.setWeight(QFont.Medium)
        self.f_q = _font(FONT_TEXT, 9)
        self.f_q.setWeight(QFont.DemiBold)
        self.f_chip = _font(FONT_TEXT, 7)
        self.f_chip.setWeight(QFont.Bold)
        self.f_icon = _font(ICON_FONT, 11)
        self.f_icon_small = _font(ICON_FONT, 9)

        self.resize(WIDTH, BAR_H)
        self._restore_position()

        self.poller = Poller()
        self.poller.status.connect(self._on_status)
        self._toggles_seen: int | None = None
        self._opens_seen: int | None = None
        self.tray: Tray | None = None
        self.frame = QTimer(self, interval=33, timeout=self._tick)
        self.frame.start()

    # -- window setup -------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_styled", False):
            self._styled = True
            _apply_windows_style(int(self.winId()))

    def _restore_position(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        try:
            pos = json.loads(POS_FILE.read_text(encoding="utf-8"))
            x, y = int(pos["x"]), int(pos["y"])
            self.expanded = bool(pos.get("expanded", False))
            self.compact = bool(pos.get("compact", False))
            self.notch = bool(pos.get("notch", True))
            self.start_hidden = bool(pos.get("hidden", False))
            self.phone_folded = bool(pos.get("phone_folded", False))
        except (OSError, ValueError, KeyError):
            x, y = screen.right() - NOTCH_W - 8, screen.top() + 64
            self.notch = True  # a fresh install starts as the notch
        # Keep it on a visible screen (monitors may have changed since last time).
        if not any(s.availableGeometry().adjusted(-40, -40, 40, 40).contains(x, y) for s in QApplication.screens()):
            x, y = screen.right() - NOTCH_W - 8, screen.top() + 64
        if self.notch:
            self._bar_pos = (x, y)  # where the floating bar goes if you leave notch mode
        self.move(x, y)
        self._apply_size()

    def _apply_size(self) -> None:
        w, h = self._target_size()
        self._h = float(h)
        self.resize(w, h)

    def _question(self) -> dict | None:
        """The choice waiting on you: a permission request (shown as Allow / Deny), else a question."""
        perm = self.st.get("permission")
        if perm:
            summary = perm["summary"]
            return {"question": summary[:1].upper() + summary[1:] + "?", "header": "Permission",
                    "options": ["Allow once", "Deny"], "more": 0, "agent": "claude", "permission": perm["id"]}
        return self.st.get("question") or None

    @property
    def island(self) -> bool:
        """The black island look and layout: the notch, and the floating widget too (not the compact orb)."""
        return not self._is_compact()

    def _is_compact(self) -> bool:
        # A question needs room: the compact orb opens into the bar until it is answered.
        return self.compact and not self.notch and not self._question()

    @property
    def nw(self) -> int:
        """The island's full width now: one row wide, a third of the screen with the notch's panel open."""
        return int(round(self._nw)) if self.notch else NOTCH_W

    def _canvas_w(self) -> int:
        """The notch's window width: the island at its widest (open, overshooting a little), always."""
        widest = max(NOTCH_W, min(PANEL_MAX_W, (SCREEN_W or self._screen_w) / 3))
        return int(widest * 1.05) + 2

    def _nw_target(self) -> float:
        if not (self.notch and self.expanded):
            return float(NOTCH_W)
        width = SCREEN_W or self._screen_w
        return float(max(NOTCH_W, min(PANEL_MAX_W, width / 3)))

    @property
    def pw(self) -> float:
        """Width of the panel area: the bar, or the island's whole body (between its ears)."""
        return self.nw - 2 * NOTCH_EAR if self.island else WIDTH

    def _two_col(self) -> bool:
        """The open notch is wide enough for two columns (decided on its final width: no jump while it widens)."""
        return self.notch and self._nw_target() - 2 * NOTCH_EAR >= TWO_COL_MIN and not self._question()

    @property
    def cw(self) -> float:
        """The main column's width: controls, needs-you and threads."""
        return round(self.pw * 0.56) if self._two_col() else self.pw

    @property
    def rx(self) -> float:
        """Where the side column starts (today, limits, what you said, agents); under the rest when single."""
        return self.cw + 2 if self._two_col() else 0.0

    @property
    def rw(self) -> float:
        return self.pw - self.rx

    def _cols(self) -> int:
        return 3 if self._two_col() or not self.island else 4

    def _agents_per_row(self) -> int:
        return 3 if self._two_col() else AGENTS_PER_ROW

    def _to_layout(self, pos: QPointF) -> QPointF:
        """Notch: window coordinates -> notch-row coordinates (the island is centred while it widens)."""
        if not self.island:
            return pos
        return QPointF(pos.x() - (self.width() - self.nw) / 2, pos.y())

    def _target_size(self) -> tuple[int, int]:
        if self._is_compact():
            return COMPACT, COMPACT
        if self._question():
            w, h = WIDTH, BAR_H + int(self._question_layout()[3])
        else:
            w, h = WIDTH, self._expanded_h() if self.expanded else BAR_H
        if self.island:  # the island row on top, the same panels underneath
            return self.nw, NOTCH_H + h - BAR_H
        return w, h

    def _panel_h(self) -> float:
        """Height of the open panel under the bar (or under the notch row)."""
        return self._vis_h() - (NOTCH_H if self.island else BAR_H)

    def _vis_h(self) -> float:
        """The island's drawn height: the notch draws inside a canvas that is only resized when it must be."""
        return round(self._h) if self.notch else self.height()

    def visual_rect(self) -> QRect:
        """Where the island is on screen (the notch's canvas can be larger while it animates)."""
        if not self.notch:
            return self.geometry()
        vw = int(round(self._vw))
        return QRect(self.x() + (self.width() - vw) // 2, self.y(), vw, int(self._vis_h()))

    def _follow_size(self) -> None:
        """Glide towards the size the content needs (panel opening, a question arriving...)."""
        if self._dragged:
            return
        w, h = self._target_size()
        self._h = h if abs(self._h - h) < 0.6 else _ease(self._h, h, self._k(0.28))
        if self.notch:
            return  # _place_notch sizes its canvas and moves it
        if w != self.width():  # compact <-> bar: keep the right edge where it is
            right = self.x() + self.width()
            self.resize(w, self.height())
            self.move(right - w, self.y())
        if int(round(self._h)) != self.height():
            self.resize(w, int(round(self._h)))

    def _place_notch(self) -> None:
        """Top centre of the primary screen, sliding down when wanted and back up when not."""
        now = time.time()
        busy = (self.phase in ("speaking", "waiting", "recording", "transcribing") or bool(self._activity())
                or bool(self._drop))
        dnd = self._output() == "silent"  # Do not disturb: it stays up until you hover it
        if self._tucked is not None and self._news() != self._tucked:
            self._tucked = None  # something new happened: show it
        hovering = self._hovering and not self._tuck_hover
        if self._tucked is not None:  # tucked by hand: only the mouse (or a drag) brings it down for a look
            if hovering or self.pressed or self._drop:
                self._reveal_until = now + NOTCH_HIDE_DELAY
        elif hovering or self.expanded or self.pressed or self._drop or (not dnd and (busy or self._question())):
            self._reveal_until = now + NOTCH_HIDE_DELAY
        # A lightly damped spring: it drops down, overshoots a hair in width, and settles.
        target = 1.0 if now < self._reveal_until else 0.0
        self._reveal, self._reveal_v = _spring(self._reveal, self._reveal_v, target, self._dt, 0.66, 0.12)
        if abs(target - self._reveal) < 0.002 and abs(self._reveal_v) < 0.002:
            self._reveal, self._reveal_v = target, 0.0
        down = _smooth(self._reveal)  # vertical travel never overshoots: it stays glued to the top edge
        grow = NOTCH_PILL + (1 - NOTCH_PILL) * max(0.0, min(1.04, self._reveal))
        geo = QApplication.primaryScreen().geometry()
        target_w = self._nw_target()
        self._nw = target_w if abs(self._nw - target_w) < 0.5 else _ease(self._nw, target_w, self._k(0.3))
        self._vw = self._nw * grow
        vh = self._vis_h()
        # The window is a canvas the island is drawn in (its see-through pixels let clicks through). It is as wide
        # as the island can ever get, so it never moves sideways: a window that moves and resizes shows its old
        # picture at the new place for a frame (a jolt). Only its height changes, downwards (nothing shifts):
        # grown once for where an animation is going, trimmed once it has settled (never resized every frame).
        end_h = self._target_size()[1]
        settled = abs(vh - end_h) < 0.6 and abs(self._nw - target_w) < 0.5
        cw = self._canvas_w()
        ch = int(vh) if settled else max(self.height(), int(max(vh, end_h)) + 2)
        hidden = geo.top() - vh + NOTCH_PEEK
        x = geo.center().x() - cw // 2
        y = int(round(hidden + (geo.top() - hidden) * down))
        if (cw, ch) != (self.width(), self.height()):
            self.setGeometry(x, y, cw, ch)
        elif (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _save_position(self) -> None:
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            x, y = self._bar_pos if self.notch and self._bar_pos else (self.x(), self.y())
            state = {"x": x, "y": y, "expanded": self.expanded, "compact": self.compact, "notch": self.notch,
                     "hidden": not self.isVisible(), "phone_folded": self.phone_folded}
            POS_FILE.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass

    def _snap(self) -> None:
        """Snap to a screen edge when released close to it."""
        geo = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        x, y, m, snap = self.x(), self.y(), 16, 28
        if abs(x - geo.left()) < snap + m:
            x = geo.left() + m
        if abs(geo.right() - (x + self.width())) < snap + m:
            x = geo.right() - self.width() - m
        if abs(y - geo.top()) < snap + m:
            y = geo.top() + m
        self.move(x, y)

    # -- state ----------------------------------------------------------------------
    def _on_status(self, st) -> None:
        # Redraw a still widget only when something it shows has changed (not the mic level, nor
        # the "N min ago" of the thread list until the minute turns).
        shown = {k: v for k, v in (st or {}).items() if k not in ("level", "threads")}
        shown["threads"] = [(t.get("session"), t.get("state"), t.get("ago", 0) // 60) for t in (st or {}).get("threads") or []]
        digest = json.dumps(shown, sort_keys=True, default=str)
        if digest != self._shown_digest:
            self._shown_digest, self._painted_still = digest, False
            self._kick()
        self.st = st or {}
        boot = self.st.get("boot")
        if boot and self._boot and boot != self._boot:
            self._relaunch()  # the service restarted (an update, `agent-voice restart`): come back with its code
            return
        self._boot = boot or self._boot
        opens = self.st.get("widget_opens")
        if opens is not None and self._opens is not None and opens != self._opens:
            self.show_panel()  # `agent-voice open`, the Start menu
        self._opens = opens if opens is not None else self._opens
        self._read_along(self.st.get("reading"))
        banner = self.st.get("banner")
        if banner and banner["id"] != self._banner_seen:
            self._banner_seen = banner["id"]
            if banner.get("age", 99) < 10:  # not an old one, after a restart
                self.banner.present(banner, int(self.pw))
        phase = st["phase"] if st else "offline"
        self.phase = phase
        self._accent_target = QColor(PHASES.get(phase, PHASES["idle"])[0])
        # Show/hide hotkey (handled by the daemon, which owns the global hotkeys).
        toggles = self.st.get("widget_toggles")
        if toggles is not None:
            if self._toggles_seen is not None and toggles != self._toggles_seen:
                self.toggle_visible()
            self._toggles_seen = toggles
        opens = self.st.get("new_thread_opens")  # the new-thread hotkey (the daemon owns global hotkeys)
        if opens is not None:
            if self._opens_seen is not None and opens != self._opens_seen:
                self.open_new_thread()
            self._opens_seen = opens
        if (self.st.get("update") or {}).get("state") == "installing" and not self._quitting:
            # The updater is about to replace our files: get out of its way (it restarts everything).
            self._quitting = True
            QTimer.singleShot(1000, QApplication.quit)
        if self.tray:
            self.tray.refresh()

    def _read_along(self, reading: dict | None) -> None:
        """Voice mode: the text being spoken, under the notch, from the sentence being read onwards."""
        if not reading:
            self.banner.release()
            self.banner.dismissed = False
            self._reading_shown = None
            return
        if self.banner.dismissed:
            return
        text = reading["text"]
        caption = " ".join((self.st.get("caption") or "").split())
        flat = " ".join(text.split())
        at = flat.find(caption[:48]) if caption else -1
        shown = flat[at:] if at > 0 else flat  # a teleprompter: what's left to hear, starting now
        key = (reading.get("session"), shown)
        if key != self._reading_shown:
            self._reading_shown = key
            self.banner.present({**reading, "text": shown}, int(self.pw), hold=True)

    def _k(self, k: float) -> float:
        """`k` (tuned per 30 fps frame) for the frame being drawn: animations keep their speed when frames vary."""
        return _per_frame(k, self._dt)

    def _kick(self) -> None:
        """Something moves (a click, the mouse, news): full frame rate now, not at the next idle tick (250 ms)."""
        if self.frame.interval() != ANIMATE_MS:
            self._last_tick = time.time() - ANIMATE_MS / 1000
            self._painted_still = False
            self.frame.setInterval(ANIMATE_MS)

    def _tick(self) -> None:
        now = time.time()
        self._dt = min(0.1, max(0.001, now - self._last_tick))  # a frame that came late moves further
        self._last_tick = now
        screen = QApplication.primaryScreen()
        if screen is not None:
            self._screen_w = screen.geometry().width()
        # Ease colour and mic level so transitions glide instead of jumping.
        self.accent = _mix(self.accent, self._accent_target, self._k(0.18))
        target = min(1.0, float(self.st.get("level", 0.0)) * 14.0)
        self.level += (target - self.level) * self._k(0.45 if target > self.level else 0.12)
        want_energy = {"speaking": 1.0, "recording": 0.35 + 0.65 * self.level, "waiting": 0.45,
                       "transcribing": 0.6}.get(self.phase, 0.12)
        self.energy = _ease(self.energy, want_energy, self._k(0.08))
        target_idx = self._speed_index()
        self.speed_pos = target_idx if self.speed_pos is None else _ease(self.speed_pos, target_idx, self._k(0.22))
        mode_idx = float([k for k, _ in MODES].index(self._output()))
        self.mode_pos = mode_idx if self.mode_pos is None else _ease(self.mode_pos, mode_idx, self._k(0.22))
        for key in set(self.hover_t) | ({self.hover} if self.hover else set()):
            self.hover_t[key] = _ease(self.hover_t.get(key, 0.0), 1.0 if key == self.hover else 0.0, self._k(0.25))

        self._follow_size()
        if self.notch:
            self._place_notch()
        if time.time() - self._topmost_at > 1.0:
            self._topmost_at = time.time()
            _keep_on_top(int(self.winId()))
        # While you type in the reply box, notifications drop below it instead of over it.
        boxed = self.reply_box.isVisible()
        anchor = self.reply_box.geometry() if boxed else self.visual_rect()
        width = anchor.width() if boxed else anchor.width() - (2 * NOTCH_EAR if self.island else 0)
        self._banner_busy = self.banner.step(QRectF(anchor), self._dt, max(300, int(width)))
        # Discreet when nothing happens, fully visible when active or hovered (the notch is always solid).
        active = (self.phase not in ("idle", "offline", "muted") or self._hovering or self.expanded
                  or self.notch or self._question())
        want = 1.0 if active else 0.62
        self._opacity += (want - self._opacity) * self._k(0.12)
        if abs(self.windowOpacity() - self._opacity) > 0.004:
            self.setWindowOpacity(self._opacity)
        # Battery: 30 fps only while something moves. At rest, no redraws and a slower status poll.
        still = self._is_still(want)
        gliding = still and self._glow_moving()  # at rest otherwise: the working light alone, at 15 fps
        self.poller.interval = 0.6 if still else 0.12
        interval = 66 if gliding else 250 if still else ANIMATE_MS
        if self.frame.interval() != interval:
            self.frame.setInterval(interval)
        if still and not gliding and self._painted_still:
            return
        self._painted_still = still and not gliding
        self.update()

    def _is_still(self, opacity_target: float) -> bool:
        """Nothing is animating or easing: the last frame drawn is still right."""
        if self.phase not in ("idle", "offline", "muted") or self._hovering or self.pressed or self._dragged:
            return False
        if self._drop:
            return False
        if self._banner_busy:
            return False
        if self._question() or time.time() - self._copied_at < 1.3:
            return False
        if self.expanded and self.st.get("limits_refreshing"):
            return False  # the ↻ turns
        settled = [
            abs(self.energy - 0.12) < 0.01,
            abs(self._opacity - opacity_target) < 0.01,
            abs(self._h - self._target_size()[1]) < 0.5,
            all(v < 0.02 or v > 0.98 for v in self.hover_t.values()),
            self.speed_pos is None or abs(self.speed_pos - self._speed_index()) < 0.01,
            sum(abs(a - b) for a, b in zip(self.accent.getRgb(), self._accent_target.getRgb())) < 4,
        ]
        if self.notch:
            settled.append(abs(self._reveal - round(self._reveal)) < 0.003 and abs(self._reveal_v) < 0.003)
            settled.append(abs(self._nw - self._nw_target()) < 0.5)
        return all(settled)

    # -- layout -----------------------------------------------------------------------
    def _primary(self) -> tuple[str, str, str] | None:
        """Contextual main button: (id, icon, tooltip)."""
        hk = self.hotkeys
        if self.phase == "offline":
            return ("start", ICONS["power"], "Start the voice service")
        if self.phase == "speaking":
            return ("reply", ICONS["skip"], f"Skip and answer now ({_hotkey_label(hk.get('reply', ''))})")
        if self.phase in ("waiting", "recording"):
            return ("stop", ICONS["stop"], f"Cancel ({_hotkey_label(hk.get('stop', ''))})")
        if self.phase == "transcribing":
            return None
        if self._output() == "silent":
            back = "Chimes" if self.st.get("dnd_from") == "chime" else "Voice"
            return ("undnd", ICONS["bell"], f"Do not disturb is on: click to go back to {back}")
        return ("reply", ICONS["mic"], f"Talk now ({_hotkey_label(hk.get('reply', ''))})")

    def _tiles(self) -> list[tuple[str, str, str, str, bool]]:
        """(id, icon, label, hotkey, active)"""
        hk, st = self.hotkeys, self.st
        muted = bool(st.get("muted"))
        tiles = [
            ("reply", ICONS["chat"], "Reply now", hk.get("reply", ""), False),
            ("hold", ICONS["mic"], "Hold to talk", hk.get("push_to_talk", ""), self.pressed == "hold"),
            ("stop", ICONS["stop"], "Stop", hk.get("stop", ""), False),
            ("handsfree", ICONS["send"], "Auto-send", hk.get("hands_free", ""), bool(st.get("hands_free"))),
            ("mute", ICONS["mute"], "Do not disturb", hk.get("mute", ""), muted),
            ("read", ICONS["read"], "Read selection", hk.get("read_selection", ""), False),
            ("wake", ICONS["bolt"], "Hey Claude", "wake word", bool(st.get("wake"))),
            ("approve", ICONS["shield"], "Approvals", "voice or click", bool(st.get("approvals", True))),
            ("summary", ICONS["list"], "Summaries" if st.get("summary", True) else "Full answers",
             "short / full", bool(st.get("summary", True))),
            ("phone", ICONS["phone"], "Phone", "notifications", bool((st.get("phone") or {}).get("enabled"))),
            ("headphones", ICONS["headphones"], "Headphones", "voice in headphones only", bool(st.get("headphones_only"))),
        ]
        if self.island:  # a fourth column: the last reply, on demand
            tiles[3:3] = [("replay", ICONS["replay"], "Replay", "last reply", False)]
            tiles[7:7] = [("copyreply", ICONS["check"] if self._copied == "copyreply" and time.time() - self._copied_at < 1.3
                           else ICONS["copy"], "Copy reply", "to clipboard", False)]
            tiles[11:11] = [("openchat", ICONS["open"], "Open chat", "last thread", False)]
        # Do not disturb is in the Mode row (and on the notch itself while it's on): no tile for it.
        tiles = [t for t in tiles if t[0] != "mute"]
        if self._output() != "voice":
            tiles = [t for t in tiles if t[0] not in VOICE_ONLY]  # nothing to hear: their tiles go
        return tiles

    def _needs(self) -> list[dict]:
        """Everything waiting on you, minus the question shown as a card right now."""
        q = self._question()
        items = list(self.st.get("needs") or [])
        if q and q.get("permission"):
            items = [n for n in items if n.get("id") != q["permission"]]
        elif q:
            items = [n for n in items if n["kind"] != "question"]
        return items

    def _pills(self) -> list[dict]:
        """One dot per conversation that's live (working, waiting on you, talking) or just finished."""
        live = [t for t in self.st.get("threads") or [] if t.get("state") or int(t.get("ago", 9999)) < 300]
        return live[:4] if len(live) >= 2 else []

    def _result(self) -> dict | None:
        """The turn that just ended and what it changed, while nothing else needs the second line."""
        if self.phase != "idle" or self._question() or self._activity():
            return None
        return self.st.get("result") or None

    def _context_warning(self) -> int | None:
        act = self._activity() or {}
        pct = act.get("context")
        return int(pct) if isinstance(pct, (int, float)) and pct >= 80 else None

    def _output(self) -> str:
        st = self.st
        return st.get("output") or ("silent" if st.get("muted") else "chime" if st.get("chime") else "voice")

    def _activity(self) -> dict | None:
        """The agent's live activity, shown while nothing else needs the notch."""
        if self.phase != "idle" or self._question():
            return None
        return self.st.get("activity") or None

    def _rects(self) -> dict[str, QRectF]:
        r = self._bar_rects()
        if not self.island:
            return r
        # Panels keep their bar layout, centred under the notch row.
        dx, dy = NOTCH_EAR, NOTCH_H - BAR_H
        out = {k: v.translated(dx, dy) for k, v in r.items() if k not in ("primary", "chevron")}
        if "primary" in r:
            out["primary"] = QRectF(self.nw - NOTCH_EAR - 68, (NOTCH_H - 28) / 2, 28, 28)
        out["chevron"] = QRectF(self.nw - NOTCH_EAR - 36, (NOTCH_H - 26) / 2, 26, 26)
        if self.notch:
            out["tuck"] = QRectF(self.nw - NOTCH_EAR - 96, (NOTCH_H - 24) / 2, 24, 24)
        out.update(self._pill_rects())
        if self._result():
            out["result"] = QRectF(NOTCH_EAR + 38, 23, self.nw / 2 - 40, 15)
        label = self._update_label()
        for text, rect in self._notch_chips()[0] if label else ():
            if text == label:  # the one chip you can click
                out["update"] = rect
        return out

    def _update_label(self) -> str:
        state = (self.st.get("update") or {}).get("state")
        if not self.st.get("update"):
            return ""
        return {"downloading": "Updating…", "installing": "Restarting…"}.get(state, "Update")

    def start_update(self) -> None:
        """The Update chip / tray item: install it (release build) or say how (source checkout, other OS)."""
        update = self.st.get("update") or {}
        if update.get("how") == "page":
            QDesktopServices.openUrl(QUrl(update["url"]))
        _post("/update")

    def _chips_right(self) -> float:
        """Where the info chips end, left of the buttons (one more, "tuck", in notch mode)."""
        return self.nw - NOTCH_EAR - (104 if self.notch else 78)

    def _pill_rects(self) -> dict[str, QRectF]:
        right = self._chips_right() - 4
        pills = self._pills()
        return {f"pill:{i}": QRectF(right - (len(pills) - i) * 12, NOTCH_H / 2 - 7, 12, 14) for i in range(len(pills))}

    def _bar_rects(self) -> dict[str, QRectF]:
        r: dict[str, QRectF] = {}
        if self._is_compact():
            # The whole orb is the main button.
            return {"primary": QRectF(0, 0, COMPACT, COMPACT)} if self._primary() else {}
        if self._primary():
            r["primary"] = QRectF(WIDTH - 82, BAR_H / 2 - 17, 34, 34)
        r["chevron"] = QRectF(WIDTH - 40, BAR_H / 2 - 13, 26, 26)
        if self._question():
            for i, row in enumerate(self._question_layout()[2]):
                r[f"opt:{i}"] = row
            return r
        if self._panel_h() > 40:
            pad, gap, th = 12, TILE_GAP, TILE_H
            cols = self._cols()
            tw = (self.cw - 2 * pad - (cols - 1) * gap) / cols
            for i, n in enumerate(self._needs()[:NEED_ROWS]):
                row = self._need_row(i)
                if n["kind"] == "permission":
                    r[f"deny:{i}"] = QRectF(row.right() - 46, row.y() + 4, 42, row.height() - 8)
                    r[f"allow:{i}"] = QRectF(row.right() - 94, row.y() + 4, 44, row.height() - 8)
                elif n["kind"] == "limit":  # out of quota: resume at reset time, or not
                    r[f"resume:{i}"] = QRectF(row.right() - 74, row.y() + 4, 70, row.height() - 8)
                r[f"need:{i}"] = row
            top = BAR_H + 10 + self._needs_h()
            for i, tile in enumerate(self._tiles()):
                row, col = divmod(i, cols)
                r[tile[0]] = QRectF(pad + col * (tw + gap), top + row * (th + gap), tw, th)
            if self._show_speed():
                track = self._speed_track()
                seg = track.width() / len(SPEEDS)
                for i, x in enumerate(SPEEDS):
                    r[f"speed:{x}"] = QRectF(track.x() + i * seg, track.y(), seg, track.height())
            track = self._mode_track()
            seg = track.width() / len(MODES)
            for i, (key, _label) in enumerate(MODES):
                r[f"mode:{key}"] = QRectF(track.x() + i * seg, track.y(), seg, track.height())
            if self._show_speed():
                track = self._engine_track()
                seg = track.width() / len(ENGINES)
                for i, (key, _label) in enumerate(ENGINES):
                    r[f"engine:{key}"] = QRectF(track.x() + i * seg, track.y(), seg, track.height())
            for i, t in enumerate(self._threads()):
                for key, rect in self._thread_buttons(i, t).items():  # first: they sit on top of the row
                    r[key] = rect
                r[f"thr:{i}"] = self._thread_row(i)
            for i in range(len(self._history())):
                r[f"hist:{i}"] = self._history_row(i)
            r.update(self._agent_rects())
            r["newthread"] = self._new_rect()
            r["history"] = self._history_rect()
            on = self._limit_on_rect()
            if on:
                r["limits:on"] = on
            if self._limit_rows():
                r["limits:refresh"] = QRectF(self.rx + 70, self._limits_y(), 18, 16)
            r.update(self._phone_rects())
            for i in range(len(self._schedules())):
                row = self._sched_row(i)
                r[f"unsched:{i}"] = QRectF(row.right() - 20, row.y() + 1, 18, row.height() - 2)
        return r

    def _needs_h(self) -> float:
        n = min(NEED_ROWS, len(self._needs()))
        return 18 + n * NEED_H + 8 if n else 0

    def _need_row(self, i: int) -> QRectF:
        return QRectF(10, BAR_H + 10 + 18 + i * NEED_H, self.cw - 20, NEED_H - 4)

    def _show_speed(self) -> bool:
        return self._output() == "voice"  # chimes and silence have no voice to speed up

    def _speed_y(self) -> float:
        rows = -(-len(self._tiles()) // self._cols())
        return BAR_H + 10 + self._needs_h() + rows * TILE_H + (rows - 1) * TILE_GAP + 8

    def _mode_y(self) -> float:
        return self._speed_y() + (34 if self._show_speed() else 0)

    def _engine_y(self) -> float:
        return self._mode_y() + 34

    def _threads_y(self) -> float:
        return (self._engine_y() if self._show_speed() else self._mode_y()) + 36

    def _kpi_h(self) -> float:
        return KPI_H if self._two_col() else 0.0

    def _phone_y(self) -> float:
        if self._two_col():
            return BAR_H + 10 + self._kpi_h()
        return self._hist_y() + 16 + max(1, len(self._history())) * ROW_H + 8

    def _phone_h(self) -> float:
        if not (self.st.get("phone") or {}).get("enabled"):
            return 0.0
        return (PHONE_FOLDED_H if self.phone_folded else PHONE_H) + 10

    def _phone_rects(self) -> dict[str, QRectF]:
        if not self._phone_h():
            return {}
        if self.phone_folded:  # one line: the state, Test, and ⌄ to see the QR code again
            row = QRectF(self.rx + 10, self._phone_y(), self.rw - 20, PHONE_FOLDED_H)
            return {"phone:fold": QRectF(row.right() - 28, row.y() + 4, 24, row.height() - 8),
                    "phone:test": QRectF(row.right() - 90, row.y() + 5, 56, row.height() - 10)}
        card = QRectF(self.rx + 10, self._phone_y(), self.rw - 20, PHONE_H)
        x = card.x() + PHONE_H - 6
        y = card.bottom() - 30
        return {"phone:test": QRectF(x, y, 56, 22), "phone:new": QRectF(x + 62, y, 70, 22),
                "phone:fold": QRectF(card.right() - 30, card.y() + 8, 22, 22),
                "phone:copy": QRectF(x, card.y() + 56, card.right() - x - 8, 14)}

    def _schedules(self) -> list[dict]:
        return list(self.st.get("schedules") or [])[:4]

    def _sched_y(self) -> float:
        return self._phone_y() + self._phone_h()

    def _sched_h(self) -> float:
        n = len(self._schedules())
        return 16 + n * SCHED_H + 8 if n else 0.0

    def _sched_row(self, i: int) -> QRectF:
        return QRectF(self.rx + 10, self._sched_y() + 16 + i * SCHED_H, self.rw - 20, SCHED_H - 2)

    def _limits_y(self) -> float:
        return self._sched_y() + self._sched_h()

    def _limit_h(self) -> float:
        """A Limits row: one line (single column), or the name over a line per window (side column)."""
        return 54 if self._two_col() else LIMIT_H

    def _limit_rows(self) -> list[tuple[str, dict]]:
        """(agent, its windows) per row. Claude Code without numbers still gets a row: to turn its status line on,
        or to say they're on their way."""
        limits = self.st.get("limits") or {}
        rows = [(s, limits[s]) for s in sorted(limits, key=lambda s: (s not in LIMIT_NAMES, s))]
        if "claude" not in limits and self._agent_state("claude") == "on":
            rows.append(("claude", {}))
        return rows

    def _limit_row(self, i: int) -> QRectF:
        return QRectF(self.rx + 10, self._limits_y() + 16 + i * self._limit_h(), self.rw - 20, self._limit_h() - 2)

    def _limits_h(self) -> float:
        n = len(self._limit_rows())
        return 16 + n * self._limit_h() + 8 if n else 0

    def _limit_on_rect(self) -> QRectF | None:
        if self.st.get("usage_line") != "off":
            return None  # on already (numbers on their way), or your own status line
        for i, (_source, windows) in enumerate(self._limit_rows()):
            if not windows:
                row = self._limit_row(i)
                return QRectF(row.right() - 64, row.y() + (min(row.height(), LIMIT_H - 2) - 16) / 2, 60, 16)
        return None

    def _agents_y(self) -> float:
        return self._limits_y() + self._limits_h()

    def _agent_rects(self) -> dict[str, QRectF]:
        keys = [f"agent:{a}" for a, _ in AGENT_LABELS] + ["guide"]
        gap, per = 6, self._agents_per_row()
        w = (self.rw - 20 - (per - 1) * gap) / per
        y = self._agents_y() + 18
        return {k: QRectF(self.rx + 10 + (i % per) * (w + gap), y + (i // per) * 30, w, 24)
                for i, k in enumerate(keys)}

    def _threads(self) -> list[dict]:
        return list(self.st.get("threads") or [])[:THREAD_ROWS]

    def _thread_buttons(self, i: int, t: dict) -> dict[str, QRectF]:
        """Talk to it, type to it, and (once it has replied) hear and copy its last reply."""
        row = self._thread_row(i)
        keys = ["thrtalk", "thrtype"] + (["thrplay", "thrcopy"] if t.get("reply") else [])
        return {f"{k}:{i}": QRectF(row.right() - 22 * (len(keys) - j), row.y() + 17, 18, 16) for j, k in enumerate(keys)}

    def _thread_row(self, i: int) -> QRectF:
        return QRectF(10, self._threads_y() + 16 + i * THREAD_H, self.cw - 20, THREAD_H - 2)

    def _hist_y(self) -> float:
        n = len(self._threads())
        return self._threads_y() + 16 + n * THREAD_H + 8  # the header stays: it holds "+ New"

    def _expanded_h(self) -> int:
        """The open panel fits what it shows (the taller column): no empty rows."""
        rows = -(-(len(AGENT_LABELS) + 1) // self._agents_per_row())
        side = self._agents_y() + 18 + rows * 30 - 6 + 12
        if not self._two_col():
            return int(side)
        main = self._hist_y() + 16 + max(1, len(self._history())) * ROW_H + 12  # what you said ends the main column
        return int(max(main, side))

    def _question_layout(self) -> tuple[QRectF, QRectF, list[QRectF], float]:
        """(header line, question text, option rows, panel height) for the pending question."""
        q = self._question() or {"question": "", "options": []}
        fm = QFontMetrics(self.f_q)
        text_h = fm.boundingRect(0, 0, self.pw - 28, 400, Qt.TextWordWrap, q["question"]).height()
        text_h = min(text_h, 3 * fm.lineSpacing())
        y = BAR_H + 10
        head = QRectF(14, y, self.pw - 28, 15)
        y += 21
        text = QRectF(14, y, self.pw - 28, text_h)
        y += text_h + 8
        rows = []
        for _ in q["options"][:OPTION_ROWS]:
            rows.append(QRectF(10, y, self.pw - 20, 28))
            y += 32
        return head, text, rows, y + 20 - BAR_H

    def _history(self) -> list[dict]:
        return list(self.st.get("history") or [])[:HIST_ROWS]

    def _history_row(self, i: int) -> QRectF:
        return QRectF(10, self._hist_y() + 16 + i * ROW_H, self.cw - 20, ROW_H - 2)

    def _speed_track(self) -> QRectF:
        return QRectF(70, self._speed_y(), self.cw - 70 - 12, 26)

    def _mode_track(self) -> QRectF:
        return QRectF(70, self._mode_y(), self.cw - 70 - 12, 26)

    def _engine_track(self) -> QRectF:
        return QRectF(70, self._engine_y(), self.cw - 70 - 12, 26)

    def _speed_index(self) -> float:
        x = float(self.st.get("speed_x", 1.5) or 1.5)
        return float(min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - x)))

    @staticmethod
    def _nth(key: str, items: list):
        """The item a "kind:i" key points at (a row, a pill), or None once the list has changed under it."""
        i = int(key.rsplit(":", 1)[1])
        return items[i] if 0 <= i < len(items) else None

    def _hit(self, pos: QPointF) -> str | None:
        pos = self._to_layout(pos)
        for key, rect in self._rects().items():
            if rect.contains(pos):
                return key
        return None

    # -- painting -----------------------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        if self.notch:  # drawn at its own size, centred in the canvas
            w, h = round(self._vw), self._vis_h()
            p.translate((self.width() - w) / 2, 0)
        path = self._body_path(w, h)
        p.setClipPath(path)
        p.fillPath(path, QColor(0, 0, 0))  # pure black, like a hardware notch
        if self._is_compact():
            self._paint_compact(p, path)
        else:
            self._paint_island(p, path, w, h)
        p.setClipping(False)
        if not self.notch:
            # Floating, it can sit over dark windows: a faint hairline keeps its outline.
            p.setPen(QPen(QColor(255, 255, 255, 24), 1))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        self._paint_edge_light(p, w, h)
        p.end()

    def _body_path(self, w: float, h: float) -> QPainterPath:
        """The notch hangs from the screen edge; floating, the same island is rounded all round (an orb if compact)."""
        if self.notch:
            return self._notch_path(w, h)
        path = QPainterPath()
        if self._is_compact():
            path.addEllipse(QRectF(0.5, 0.5, w - 1, h - 1))
        else:
            r = min(NOTCH_RADIUS + 2, NOTCH_H / 2)
            path.addRoundedRect(QRectF(NOTCH_EAR + 0.5, 0.5, w - 2 * NOTCH_EAR - 1, h - 1), r, r)
        return path

    @staticmethod
    def _notch_path(w: float, h: float) -> QPainterPath:
        """Square top flush with the screen edge, flared into it by two concave ears; round bottom."""
        e, r = NOTCH_EAR, min(NOTCH_RADIUS, h / 2)
        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(w, 0)
        path.quadTo(w - e, 0, w - e, e)
        path.lineTo(w - e, h - r)
        path.quadTo(w - e, h, w - e - r, h)
        path.lineTo(e + r, h)
        path.quadTo(e, h, e, h - r)
        path.lineTo(e, e)
        path.quadTo(e, 0, 0, 0)
        path.closeSubpath()
        return path

    def _edge_live(self) -> float:
        """How strongly the edge light shows: bright while the voice or the mic is live, soft while an agent
        works (in its conversation's colour), and a faint, still rim when ready."""
        live = {"speaking": 1.0, "recording": 1.0, "waiting": 0.75, "transcribing": 0.6}.get(self.phase, 0.0)
        if live:
            return live
        if self._activity():
            return 0.85
        return 0.5 if self.phase == "idle" else 0.0

    def _glow_moving(self) -> bool:
        """The working light glides around the edge: worth a gentle 15 fps, but only while it can be seen."""
        return bool(self._activity()) and self._reveal > 0.3 and self.isVisible()

    def _paint_edge_light(self, p: QPainter, w: float, h: float) -> None:
        """A thin band of light around the island: running while live, gliding while working, still when ready."""
        live = self._edge_live() * _smooth((self._reveal - 0.3) / 0.6)
        if live < 0.02:
            return
        t = time.time() - self.t0
        act = self._activity() if self.phase == "idle" else None
        # Ready: a cool silver rim, like light on a polished edge; otherwise the state's (or thread's) colour.
        color = QColor(act["color"]) if act else QColor("#CBD5E1") if self.phase == "idle" else QColor(self.accent)
        if self.phase != "idle":
            angle = -t * (40 + 70 * self.energy)  # live: the light runs around with the voice
        elif act:
            angle = -t * 30  # working: a slow glide
        else:
            angle = 270.0  # ready: still, brightest along the bottom edge
        grad = QConicalGradient(QPointF(w / 2, h * 0.4), angle)
        # One colour; the motion comes from a brighter stretch travelling around. Working: a single comet
        # (a bright head, a long fading tail) rather than two highlights, calmer than a live voice.
        stops = (((0.0, 255), (0.08, 230), (0.35, 70), (0.7, 25), (0.92, 40), (1.0, 255)) if act
                 else ((0.0, 255), (0.25, 120), (0.5, 70), (0.75, 120), (1.0, 255)))
        for pos, alpha in stops:
            c = QColor(color)
            c.setAlpha(int(alpha * live))
            grad.setColorAt(pos, c)
        e, r, k = NOTCH_EAR, min(NOTCH_RADIUS, h / 2), 0.75
        if self.notch:
            edge = QPainterPath()  # sides and bottom only: the top belongs to the screen edge
            edge.moveTo(e + k, e)
            edge.lineTo(e + k, h - r)
            edge.quadTo(e + k, h - k, e + r, h - k)
            edge.lineTo(w - e - r, h - k)
            edge.quadTo(w - e - k, h - k, w - e - k, h - r)
            edge.lineTo(w - e - k, e)
        else:
            edge = self._body_path(w, h)  # floating: all the way round
        p.setBrush(Qt.NoBrush)
        p.setClipPath(self._body_path(w, h))
        bloom = QColor(color)
        bloom.setAlpha(int((30 + 40 * self.energy) * live))
        p.setPen(_round_pen(bloom, 5.0))  # a soft inner bloom, clipped to the island
        p.drawPath(edge)
        p.setClipping(False)
        p.setPen(_round_pen(grad, 1.5))
        p.drawPath(edge)

    def _paint_island(self, p: QPainter, path: QPainterPath, w: float, h: float) -> None:
        """Notch: the content stays centred while the island widens, and fades in once there is room."""
        show = _smooth((self._reveal - 0.55) / 0.4)
        off = (w - self.nw) / 2
        if show > 0.01:
            p.save()
            p.translate(off, 0)
            p.setOpacity(show)
            self._paint_notch_row(p)
            if h > NOTCH_H + 4:
                p.translate(NOTCH_EAR, NOTCH_H - BAR_H)
                self._paint_panel(p)
            p.restore()

    def _notch_chips(self) -> tuple[list[tuple[str, QRectF]], float]:
        """The notch row's info chips, right to left before the buttons (and the pills); where they end."""
        st, cy = self.st, NOTCH_H / 2
        chips = []
        if st.get("permission"):
            chips.append("Approve?")
        elif self._question():
            chips.append("Choose")
        waiting = len(self._needs())
        if waiting:
            chips.append(f"{waiting} need{'s' if waiting == 1 else ''} you")
        ctx = self._context_warning()
        if ctx:
            chips.append(f"Context {ctx}%")
        if st.get("queued"):
            chips.append(f"+{st['queued']} waiting")
        update_chip = self._update_label()
        if update_chip:
            chips.append(update_chip)
        lang = st.get("speaking_lang") if self.phase == "speaking" else ""
        if lang:
            chips.append(lang.upper())
        act = self._activity()
        pills = self._pills()
        if act:
            if act.get("others") and not pills:  # the pills already show who else is working
                chips.append(f"+{act['others']} working")
            chips.append(f"{act['elapsed'] // 60}:{act['elapsed'] % 60:02d}")
        if st.get("focus") and not st.get("in_call"):
            chips.append("Focus")
        elif st.get("in_call"):
            chips.append("In a call")
        elif st.get("speakers"):
            chips.append("Speakers")
        elif self._output() == "chime":
            chips.append("Chime")
        elif self._output() == "silent":
            chips.append("Do not disturb")
        elif st.get("hands_free"):
            chips.append("Auto")
        if st.get("speed_x") and self.phase == "speaking":  # only while there's a voice to speed up
            chips.append(f"{float(st['speed_x']):g}×")
        fm_chip = QFontMetrics(self.f_notch_chip)
        right = self._chips_right()
        if pills:
            right -= len(pills) * 12 + 8
        out = []
        for text in chips:
            cw = fm_chip.horizontalAdvance(text) + 12
            right -= cw
            out.append((text, QRectF(right, cy - 8, cw, 16)))
            right -= 4
        return out, right

    def _paint_notch_row(self, p: QPainter) -> None:
        """One dense row: orb, status + conversation, live caption, info chips, buttons."""
        cy = NOTCH_H / 2
        st = self.st
        self._paint_indicator(p, QPointF(NOTCH_EAR + 21, cy))

        # Info chips, right to left, before the buttons.
        act = self._activity()
        pills = self._pills()
        if pills:
            self._paint_pills(p, pills)
        chips, right = self._notch_chips()
        for text, rect in chips:
            self._flat_chip(p, rect, text)

        # Status and conversation on the first line, caption (or hint) on the second.
        x = NOTCH_EAR + 38.0
        avail = right - x - 6
        _, title, sub = PHASES.get(self.phase, PHASES["idle"])
        if self._question() and self.phase in ("idle", "waiting", "recording"):
            title = "Your choice"
        if act:
            title = "Working"
        p.setFont(self.f_notch_title)
        fm = QFontMetrics(self.f_notch_title)
        p.setPen(QColor(255, 255, 255, 240))
        p.drawText(QPointF(x, 20), fm.elidedText(title, Qt.ElideRight, int(avail)))
        tx = x + min(fm.horizontalAdvance(title), avail) + 8
        thread = st.get("thread") or act or self._result()
        if thread and tx < x + avail - 30:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(thread["color"]))
            p.drawEllipse(QPointF(tx + 3, 16), 2.5, 2.5)
            p.setFont(self.f_notch_sub)
            p.setPen(QColor(255, 255, 255, 120))
            p.drawText(QPointF(tx + 10, 20), QFontMetrics(self.f_notch_sub).elidedText(
                thread["title"], Qt.ElideRight, int(x + avail - tx - 10)))
        line = st.get("caption") or ""
        if st.get("reading") and not self.banner.dismissed:
            line = ""  # the text is in the banner below: the hint instead of the same sentence twice
        if self._drop:
            line = f"Drop to add to {self._drop['title']}"
        result = self._result()
        if not line and result:
            line = f"Done · {result_label(result)}"
            if self.hover == "result":
                line += "  ·  open"
        if not line and act:
            steps = act["steps"]
            line = act.get("label") or "Thinking"
            if steps:
                line += f" · {steps} step{'s' if steps != 1 else ''}"
        if not line:
            q = self._question()
            if q:
                line = q["question"]
            elif self.phase == "idle" and st.get("last_heard"):
                line = f"You: “{st['last_heard']}”"
            elif self.phase == "idle":
                line = f"{_hotkey_label(self.hotkeys.get('reply', ''))} to talk"
            else:
                line = sub
        p.setFont(self.f_notch_sub)
        p.setPen(QColor(255, 255, 255, 140))
        p.drawText(QRectF(x, 23, avail, 15), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(self.f_notch_sub).elidedText(line, Qt.ElideRight, int(avail)))
        self._paint_notch_buttons(p)

    def _paint_pills(self, p: QPainter, pills: list[dict]) -> None:
        """A dot per live conversation: filled while it works or talks, ringed in amber when it waits on you."""
        for (key, rect), t in zip(self._pill_rects().items(), pills):
            c = rect.center()
            color = QColor(t["color"])
            state = t.get("state", "")
            ht = self.hover_t.get(key, 0.0)
            p.setPen(Qt.NoPen)
            if state in ("permission", "question", "input"):
                p.setPen(QPen(QColor("#FBBF24"), 1.4))
            if not state:
                color.setAlpha(110)  # finished a moment ago
            p.setBrush(color)
            p.drawEllipse(c, 3.2 + ht, 3.2 + ht)

    def _paint_indicator(self, p: QPainter, c: QPointF) -> None:
        """Flat state mark: live bars for a voice, a spinning arc while transcribing, a dot otherwise."""
        t = time.time() - self.t0
        acc = QColor(self.accent)
        p.setPen(Qt.NoPen)
        p.setBrush(acc)
        if self.phase in ("speaking", "recording"):
            for i in range(4):
                if self.phase == "recording":
                    k = 0.15 + self.level * (0.55 + 0.45 * abs(math.sin(t * 7 + i * 1.7)))
                else:
                    k = 0.25 + 0.75 * abs(math.sin(t * (3.1 + i * 0.6) + i * 1.3)) * self.energy
                bh = 3 + 13 * min(1.0, k)
                p.drawRoundedRect(QRectF(c.x() - 7.5 + i * 4.4, c.y() - bh / 2, 2.6, bh), 1.3, 1.3)
        elif self.phase == "transcribing":
            p.setPen(_round_pen(acc, 2.0))
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(c.x() - 6, c.y() - 6, 12, 12), int(-t * 360 * 16) % (360 * 16), 250 * 16)
        elif self._activity():
            # Working: three dots, one lit, moving on each second (the status changes once a second,
            # so this costs one repaint per second, not a 30 fps animation).
            act = self._activity()
            color = QColor(act["color"])
            for i in range(3):
                dot = QColor(color)
                dot.setAlpha(255 if i == act["elapsed"] % 3 else 80)
                p.setBrush(dot)
                p.drawEllipse(QPointF(c.x() - 6 + i * 6, c.y()), 2.2, 2.2)
        else:
            r = 4.0 + (0.8 * math.sin(t * 2.2) if self.phase == "waiting" else 0.0)
            p.drawEllipse(c, r, r)

    def _flat_chip(self, p: QPainter, rect: QRectF, text: str) -> None:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 22))
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        p.setFont(self.f_notch_chip)
        p.setPen(QColor(255, 255, 255, 200))
        p.drawText(rect, Qt.AlignCenter, text)

    def _paint_notch_buttons(self, p: QPainter) -> None:
        """Quiet round buttons: a faint disc, brighter on hover."""
        rects = self._rects()
        prim = self._primary()
        buttons = [("primary", prim[1])] if prim else []
        buttons.append(("chevron", ICONS["up" if self.expanded else "down"]))
        if self.notch:
            buttons.append(("tuck", ICONS["tuck"]))
        for key, icon in buttons:
            rect = rects[key]
            ht = self.hover_t.get(key, 0.0)
            base = 16 if key == "primary" else 0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, int(base + 26 * ht + (24 if self.pressed == key else 0))))
            p.drawEllipse(rect)
            p.setFont(self.f_icon_small)
            p.setPen(QColor(255, 255, 255, int(175 + 80 * ht)))
            p.drawText(rect, Qt.AlignCenter, icon)

    def _paint_compact(self, p: QPainter, path: QPainterPath) -> None:
        """Compact mode: a black orb with the island's state mark; a click talks, cancels or skips."""
        ht = self.hover_t.get("primary", 0.0)
        if ht > 0.01:
            p.fillPath(path, QColor(255, 255, 255, int(18 * ht)))
        p.save()
        p.translate(COMPACT / 2, COMPACT / 2)
        p.scale(1.35, 1.35)
        self._paint_indicator(p, QPointF(0, 0))
        p.restore()

    def _chip(self, p: QPainter, x: float, y: float, text: str) -> None:
        text = text if len(text) <= 3 else text.capitalize()
        self._flat_chip(p, QRectF(x, y, QFontMetrics(self.f_notch_chip).horizontalAdvance(text) + 12, 16), text)

    def _paint_question(self, p: QPainter) -> None:
        """The agent's question and its options, each one clickable."""
        q = self._question()
        head, text, rows, _ = self._question_layout()
        self._chip(p, head.x(), head.y(), (q.get("header") or "Question").upper())
        if q.get("more"):
            p.setFont(self.f_hint)
            p.setPen(QColor(226, 232, 240, 120))
            p.drawText(head, Qt.AlignRight | Qt.AlignVCenter, f"+{q['more']} more in {q.get('agent') or 'the app'}")
        p.setFont(self.f_q)
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(text, Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, q["question"])
        fm = QFontMetrics(self.f_tile)
        for i, (label, row) in enumerate(zip(q["options"], rows)):
            ht = self.hover_t.get(f"opt:{i}", 0.0)
            fill = QColor(self.accent)
            fill.setAlpha(int(30 + 60 * ht))
            p.setPen(QPen(QColor(255, 255, 255, int(28 + 50 * ht)), 1))
            p.setBrush(fill)
            p.drawRoundedRect(row, 8, 8)
            badge = QRectF(row.x() + 6, row.y() + 5, 18, 18)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 40))
            p.drawEllipse(badge)
            p.setFont(self.f_chip)
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(badge, Qt.AlignCenter, str(i + 1))
            p.setFont(self.f_tile)
            label_rect = row.adjusted(32, 0, -8, 0)
            p.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(label, Qt.ElideRight, int(label_rect.width())))
        p.setFont(self.f_hint)
        p.setPen(QColor(226, 232, 240, 110))
        y = rows[-1].bottom() + 4 if rows else text.bottom() + 4
        hint = ("Or say “yes” or “no” · no answer in a minute: Claude asks you itself" if q.get("permission")
                else "Or say it: “the second one”, or the option’s name")
        p.drawText(QRectF(14, y, self.pw - 28, 14), Qt.AlignLeft | Qt.AlignVCenter, hint)

    def _paint_panel(self, p: QPainter) -> None:
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.drawLine(QPointF(14, BAR_H), QPointF(self.pw - 14, BAR_H))
        if self._question():
            self._paint_question(p)
            return
        rects = self._bar_rects()  # drawn in bar coordinates, also under the notch
        progress = self._panel_h() / max(1, self._expanded_h() - BAR_H)
        self._paint_needs(p, rects)
        for i, (tid, icon, label, _hk, active) in enumerate(self._tiles()):
            rect = rects.get(tid)
            if rect is None:
                continue
            appear = max(0.0, min(1.0, (progress - i * 0.05) / 0.5))
            if appear <= 0:
                continue
            p.save()
            p.setOpacity(appear)
            ht = self.hover_t.get(tid, 0.0)
            rect = rect.translated(0, 10 * (1 - appear) - 1.5 * ht)
            if active:
                fill = QColor(self.accent if tid == "hold" else "#8B7CFF")
                if tid == "mute":
                    fill = QColor("#F87171")
                if tid in ("handsfree", "approve"):
                    fill = QColor("#34D399")
                if tid == "wake":
                    fill = QColor("#FBBF24")
                fill.setAlpha(70)
                border = QColor(fill)
                border.setAlpha(170)
            else:
                fill = QColor(255, 255, 255, int(14 + 18 * ht))
                border = QColor(255, 255, 255, int(22 + 30 * ht))
            if self.pressed == tid:
                fill.setAlpha(min(255, fill.alpha() + 40))
            p.setPen(QPen(border, 1))
            p.setBrush(fill)
            p.drawRoundedRect(rect, 8, 8)
            p.setFont(self.f_icon_small)
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(QRectF(rect.x() + 8, rect.y(), 18, rect.height()), Qt.AlignCenter, icon)
            p.setFont(self.f_tile)
            p.setPen(QColor(255, 255, 255, 220))
            room = int(rect.width() - 34)
            p.drawText(QRectF(rect.x() + 30, rect.y(), room, rect.height()), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(self.f_tile).elidedText(label, Qt.ElideRight, room))
            p.restore()

        # Speed and mode switches, then what you said recently (so you can trust or reuse it).
        speed_pos = self.speed_pos if self.speed_pos is not None else self._speed_index()
        if self._show_speed():
            self._paint_segmented(p, progress, 6, self._speed_track(), "Speed",
                                  [f"speed:{x}" for x in SPEEDS], [f"{x:g}×" for x in SPEEDS], speed_pos)
        mode_pos = self.mode_pos if self.mode_pos is not None else float([k for k, _ in MODES].index(self._output()))
        self._paint_segmented(p, progress, 7, self._mode_track(), "Mode",
                              [f"mode:{k}" for k, _ in MODES], [label for _, label in MODES], mode_pos)
        if self._show_speed():
            fetching = self.st.get("engine_state") or ""  # "downloading 40%": the natural voice, once
            labels = [f"Natural · {fetching.split()[-1]}" if key == "kokoro" and fetching else label
                      for key, label in ENGINES]
            engine = self.st.get("engine") or "piper"
            self._paint_segmented(p, progress, 8, self._engine_track(), "Voice", [f"engine:{k}" for k, _ in ENGINES],
                                  labels, float(next((i for i, (k, _) in enumerate(ENGINES) if k == engine), 0)))

        self._paint_threads(p, progress)
        self._paint_history(p, progress)
        self._paint_today(p, progress)
        self._paint_phone(p, progress)
        self._paint_schedules(p, progress)
        self._paint_limits(p, progress)
        self._paint_agents(p, progress)

    def _paint_needs(self, p: QPainter, rects: dict[str, QRectF]) -> None:
        """Every agent waiting on you: approve or deny here, or open it."""
        needs = self._needs()[:NEED_ROWS]
        if not needs:
            return
        p.setFont(self.f_tile)
        p.setPen(QColor("#FBBF24"))
        p.drawText(QRectF(14, BAR_H + 10, self.cw - 28, 16), Qt.AlignLeft | Qt.AlignVCenter, "Needs you")
        p.setFont(self.f_hint)
        p.setPen(QColor(226, 232, 240, 105))
        more = len(self._needs()) - len(needs)
        perms = sum(1 for n in needs if n["kind"] == "permission")
        p.drawText(QRectF(14, BAR_H + 10, self.cw - 28, 16), Qt.AlignRight | Qt.AlignVCenter,
                   f"+{more} more" if more > 0 else "or say “approve all”, “deny two”" if perms > 1
                   else "or say “approve Codex”")
        fm = QFontMetrics(self.f_sub)
        for i, n in enumerate(needs):
            row = self._need_row(i)
            ht = self.hover_t.get(f"need:{i}", 0.0)
            p.setPen(QPen(QColor(251, 191, 36, int(60 + 60 * ht)), 1))
            p.setBrush(QColor(251, 191, 36, int(14 + 14 * ht)))
            p.drawRoundedRect(row, 8, 8)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(n["color"]))
            p.drawEllipse(QPointF(row.x() + 11, row.center().y()), 3.5, 3.5)
            buttons = n["kind"] == "permission"
            text_rect = row.adjusted(22, 0, -(100 if buttons else 80 if n["kind"] == "limit" else 46), 0)
            p.setFont(self.f_sub)
            p.setPen(QColor(255, 255, 255, 230))
            # Several approvals: numbered, so "approve two" says which.
            number = f"{sum(1 for m in needs[:i + 1] if m['kind'] == 'permission')} · " if buttons and sum(
                1 for m in needs if m["kind"] == "permission") > 1 else ""
            label = number + f"{n['title']}: {n['text'][:1].upper()}{n['text'][1:]}" + ("?" if buttons else "")
            p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(label, Qt.ElideRight, int(text_rect.width())))
            if buttons:
                for key, text, fill in ((f"allow:{i}", "Allow", "#34D399"), (f"deny:{i}", "Deny", "#F87171")):
                    if key in rects:
                        c = QColor(fill)
                        c.setAlpha(int(60 + 90 * self.hover_t.get(key, 0.0)))
                        self._pill(p, rects[key], text, c)
            elif n["kind"] == "limit":
                key = f"resume:{i}"
                if key in rects:
                    on = bool(n.get("resume"))
                    c = QColor("#34D399" if on else "#FBBF24")
                    c.setAlpha(int((110 if on else 60) + 80 * self.hover_t.get(key, 0.0)))
                    self._pill(p, rects[key], "✓ Resume" if on else "Resume", c)
            else:
                p.setFont(self.f_hint)
                p.setPen(QColor(255, 255, 255, int(150 + 90 * ht)))
                p.drawText(row.adjusted(0, 0, -10, 0), Qt.AlignRight | Qt.AlignVCenter, "Open")

    def _section_head(self, p: QPainter, rect: QRectF, title: str, hint: str = "",
                      hint_color: QColor | None = None) -> None:
        """A section's name on the left, and a short hint on the right."""
        p.setFont(self.f_tile)
        p.setPen(QColor(226, 232, 240, 170))
        p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, title)
        if hint:
            p.setFont(self.f_hint)
            p.setPen(hint_color or QColor(226, 232, 240, 105))
            p.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, hint)

    def _pill(self, p: QPainter, rect: QRectF, text: str, fill: QColor) -> None:
        """A small round-ended button: its colour (hover already in its alpha), its label."""
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        p.setFont(self.f_chip)
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(rect, Qt.AlignCenter, text)

    def _go_button(self, key: str) -> QColor:
        """The green of the buttons that do something (Test, + New, Turn on), brighter under the mouse."""
        return QColor(16, 224, 160, int(40 + 70 * self.hover_t.get(key, 0.0)))

    def _note(self, text: str) -> None:
        """A short message under the notch from the widget itself (no conversation behind it)."""
        self.banner.present({"id": -1, "kind": "note", "title": "Agents", "tag": "agents", "session": "",
                             "text": text}, int(self.pw))

    def _agent_state(self, agent: str) -> str:
        """'on' (hooks installed), 'off' (agent found, not connected) or 'missing' (not on this PC)."""
        return (self.st.get("agents") or {}).get(agent, "missing")

    def _paint_agents(self, p: QPainter, progress: float) -> None:
        """Which agents speak through agent-voice; click one to connect it, or open the guide."""
        appear = max(0.0, min(1.0, (progress - 0.6) / 0.4))
        if appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        self._section_head(p, QRectF(self.rx + 14, self._agents_y(), self.rw - 28, 16), "Agents", "click to connect")
        for key, rect in self._agent_rects().items():
            ht = self.hover_t.get(key, 0.0)
            state = "guide" if key == "guide" else self._agent_state(key.split(":", 1)[1])
            p.setPen(QPen(QColor(255, 255, 255, int(20 + 30 * ht)), 1))
            p.setBrush(QColor(255, 255, 255, int((16 if state == "on" else 6) + 16 * ht)))
            p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            if key == "guide":
                p.setFont(self.f_icon_small)
                p.setPen(QColor(255, 255, 255, int(170 + 80 * ht)))
                p.drawText(rect, Qt.AlignCenter, ICONS["help"])
                continue
            label = dict(AGENT_LABELS)[key.split(":", 1)[1]]
            fm = QFontMetrics(self.f_hint)
            tw = fm.horizontalAdvance(label)
            x = max(rect.x() + 6, rect.center().x() - (tw + 9) / 2)
            if state == "on":
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#34D399"))
                p.drawEllipse(QPointF(x + 2.5, rect.center().y()), 2.5, 2.5)
            elif state in ("off", "setup"):
                p.setPen(QPen(QColor("#FBBF24") if state == "setup" else QColor(255, 255, 255, 120), 1))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(x + 2.5, rect.center().y()), 2.5, 2.5)
            p.setFont(self.f_hint)
            p.setPen(QColor(255, 255, 255, {"on": 225, "off": 170, "setup": 200}.get(state, 70)))
            room = rect.right() - (x + 9) - 4
            p.drawText(QRectF(x + 9 if state != "missing" else x + 4.5, rect.y(), min(tw + 2, room + 6), rect.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(label, Qt.ElideRight, int(room + 6)))
        p.restore()

    def _new_rect(self) -> QRectF:
        return QRectF(self.cw - 14 - 52, self._threads_y() + 1, 52, 15)

    def _history_rect(self) -> QRectF:
        new = self._new_rect()
        return QRectF(new.x() - 8 - 62, new.y(), 62, new.height())

    def _paint_threads(self, p: QPainter, progress: float) -> None:
        """Recent conversations with what each is doing; click one to open it. "+ New" starts another."""
        threads = self._threads()
        appear = max(0.0, min(1.0, (progress - 0.5) / 0.5))
        if appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        p.setFont(self.f_tile)
        p.setPen(QColor(226, 232, 240, 170))
        p.drawText(QRectF(14, self._threads_y(), self.cw - 28, 16), Qt.AlignLeft | Qt.AlignVCenter, "Threads")
        p.setFont(self.f_hint)
        p.setPen(QColor(226, 232, 240, 105))
        queued = int(self.st.get("queued") or 0)
        new, past = self._new_rect(), self._history_rect()
        p.drawText(QRectF(14, self._threads_y(), past.x() - 22, 16), Qt.AlignRight | Qt.AlignVCenter,
                   f"{queued} waiting to speak" if queued else "click to open" if threads else "none yet")
        self._pill(p, past, "History", QColor(139, 124, 255, int(40 + 70 * self.hover_t.get("history", 0.0))))
        self._pill(p, new, "+ New", self._go_button("newthread"))
        labels = {"speaking": "speaking", "listening": "listening", "queued": "next up",
                  "question": "asks you", "permission": "needs approval", "working": "working",
                  "input": "waiting for you"}
        fm = QFontMetrics(self.f_sub)
        for i, t in enumerate(threads):
            row = self._thread_row(i)
            ht = self.hover_t.get(f"thr:{i}", 0.0)
            if ht > 0.01:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, int(20 * ht)))
                p.drawRoundedRect(row, 6, 6)
            color = QColor(t["color"])
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            line1 = QRectF(row.x(), row.y() + 1, row.width(), 16)
            p.drawEllipse(QPointF(row.x() + 9, line1.center().y()), 3.5, 3.5)
            state = labels.get(t.get("state", ""), "")
            if not state:
                ago = int(t.get("ago", 0))
                state = "now" if ago < 60 else f"{ago // 60} min ago" if ago < 3600 else f"{ago // 3600} h ago"
            p.setFont(self.f_hint)
            p.setPen(color.lighter(130) if t.get("state") else QColor(226, 232, 240, 110))
            sw = QFontMetrics(self.f_hint).horizontalAdvance(state)
            p.drawText(QRectF(row.right() - sw - 6, line1.y(), sw + 2, line1.height()), Qt.AlignRight | Qt.AlignVCenter, state)
            text_rect = QRectF(row.x() + 20, line1.y(), row.width() - sw - 32, line1.height())
            p.setFont(self.f_sub)
            p.setPen(QColor(226, 232, 240, 215))
            p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(t["title"], Qt.ElideRight, int(text_rect.width())))
            reply = t.get("reply") or ""
            extras = [result_label(t["result"])] if t.get("result") else []
            if isinstance(t.get("context"), (int, float)):
                extras.append(f"context {int(t['context'])}%")
            if extras:
                reply = " · ".join(extras) + ("  —  " + reply if reply else "")
            buttons = self._thread_buttons(i, t)
            if reply:
                # The conversation's last reply, with replay and copy at hand.
                p.setFont(self.f_hint)
                p.setPen(QColor(226, 232, 240, 120))
                line2 = QRectF(row.x() + 20, row.y() + 17, row.width() - 28 - 22 * len(buttons), 16)
                p.drawText(line2, Qt.AlignLeft | Qt.AlignVCenter,
                           QFontMetrics(self.f_hint).elidedText(reply, Qt.ElideRight, int(line2.width())))
            icons = {"thrtalk": "mic", "thrtype": "keyboard", "thrplay": "replay", "thrcopy": "copy"}
            for key, rect in buttons.items():
                done = key.startswith("thrcopy") and self._copied == key and time.time() - self._copied_at < 1.2
                kt = self.hover_t.get(key, 0.0)
                p.setFont(self.f_icon_small)
                p.setPen(QColor(255, 255, 255, 235 if done else int(110 + 140 * kt)))
                p.drawText(rect, Qt.AlignCenter, ICONS["check"] if done else ICONS[icons[key.split(":")[0]]])
        p.restore()

    def _paint_today(self, p: QPainter, progress: float) -> None:
        """The side column's top: today's turns, threads and tokens, and the turns by hour."""
        if not self._two_col():
            return
        appear = max(0.0, min(1.0, (progress - 0.45) / 0.45))
        if appear <= 0:
            return
        day = self.st.get("day") or {}
        p.save()
        p.setOpacity(appear)
        x, y, w = self.rx + 10, BAR_H + 10, self.rw - 20
        focus = self.st.get("focus")
        self._section_head(p, QRectF(x + 4, y, w - 8, 16), "Today",
                           f"focus: {focus}, holding replies" if focus else "since midnight",
                           QColor(139, 124, 255, 230) if focus else None)
        cards = (("Turns", str(day.get("turns", 0))), ("Threads", str(day.get("threads", 0))),
                 ("Tokens", tokens_label(int(day.get("tokens") or 0))))
        gap = 6
        cw = (w - 2 * gap) / 3
        for i, (label, value) in enumerate(cards):
            card = QRectF(x + i * (cw + gap), y + 20, cw, 46)
            p.setPen(QPen(QColor(255, 255, 255, 22), 1))
            p.setBrush(QColor(255, 255, 255, 10))
            p.drawRoundedRect(card, 10, 10)
            p.setFont(self.f_kpi)
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(card.adjusted(10, 4, -6, -18), Qt.AlignLeft | Qt.AlignVCenter, value)
            p.setFont(self.f_hint)
            p.setPen(QColor(226, 232, 240, 120))
            p.drawText(card.adjusted(10, 26, -6, -4), Qt.AlignLeft | Qt.AlignVCenter, label)
        # Turns by hour: a bar per hour, this hour in the accent colour.
        hours = list(day.get("hours") or [0] * 24)[:24]
        chart = QRectF(x + 2, y + 76, w - 4, KPI_H - 76 - 22)
        top = max(1, max(hours or [0]))
        from datetime import datetime

        now = datetime.fromtimestamp(time.time()).hour  # through `time`: render_docs drives the clock
        bw = chart.width() / 24
        p.setPen(Qt.NoPen)
        for h, n in enumerate(hours):
            bh = max(2.0, chart.height() * n / top) if n else 2.0
            c = QColor(self.accent if h == now else "#8B7CFF")
            c.setAlpha(230 if h == now else 150 if n else 40)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(chart.x() + h * bw + 1, chart.bottom() - bh, bw - 2, bh), 1.5, 1.5)
        p.setFont(self.f_hint)
        p.setPen(QColor(226, 232, 240, 90))
        for h in (0, 6, 12, 18):
            p.drawText(QRectF(chart.x() + h * bw, chart.bottom() + 2, 30, 14), Qt.AlignLeft | Qt.AlignVCenter, f"{h}h")
        p.drawText(QRectF(chart.x(), chart.y() - 2, chart.width(), 14), Qt.AlignRight | Qt.AlignVCenter,
                   "turns by hour")
        p.restore()

    def _paint_phone(self, p: QPainter, progress: float) -> None:
        """Phone notifications on: the QR code to scan with the ntfy app, a test, a new private link."""
        phone = self.st.get("phone") or {}
        appear = max(0.0, min(1.0, (progress - 0.5) / 0.45))
        if not phone.get("enabled") or appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        if self.phone_folded:
            self._paint_phone_folded(p, phone)
            p.restore()
            return
        card = QRectF(self.rx + 10, self._phone_y(), self.rw - 20, PHONE_H)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(QColor(255, 255, 255, 10))
        p.drawRoundedRect(card, 12, 12)
        qr = QRectF(card.x() + 8, card.y() + 8, PHONE_H - 16, PHONE_H - 16)
        _paint_qr(p, qr, phone.get("url", ""))
        x = qr.right() + 10
        p.setFont(self.f_tile)
        p.setPen(QColor(255, 255, 255, 230))
        p.drawText(QRectF(x, card.y() + 8, card.right() - x - 34, 16), Qt.AlignLeft | Qt.AlignVCenter, "Phone")
        p.setFont(self.f_hint)
        p.setPen(QColor(226, 232, 240, 140))
        when = "When you're away" if phone.get("when", "away") == "away" else "Always"
        detail = when + (f" · {phone['sent']} sent" if phone.get("sent") else "") + (
            f" · not sent: {phone['error']}" if phone.get("error") else "")
        fm = QFontMetrics(self.f_hint)
        width = int(card.right() - x - 8)
        for i, (line, alpha, elide) in enumerate((("Scan it with the ntfy app", 150, Qt.ElideRight),
                                                  (detail, 150, Qt.ElideRight),
                                                  (phone.get("url", "").split("://", 1)[-1], 90, Qt.ElideMiddle))):
            p.setPen(QColor(226, 232, 240, alpha) if not (i == 1 and phone.get("error")) else QColor("#F87171"))
            p.drawText(QRectF(x, card.y() + 26 + 15 * i, width - (30 if i == 0 else 0), 14),
                       Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(line, elide, width - (30 if i == 0 else 0)))
        if self._copied == "phone:copy" and time.time() - self._copied_at < 1.3:
            p.setFont(self.f_hint)
            p.setPen(QColor("#34D399"))
            p.fillRect(self._phone_rects()["phone:copy"], QColor(0, 0, 0))
            p.drawText(self._phone_rects()["phone:copy"], Qt.AlignLeft | Qt.AlignVCenter, "Link copied: paste it in ntfy")
        self._phone_buttons(p, (("phone:test", "Test"), ("phone:new", "New link"), ("phone:fold", "⌃")))
        p.restore()

    def _phone_buttons(self, p: QPainter, buttons: tuple) -> None:
        rects = self._phone_rects()
        for key, label in buttons:
            fill = (self._go_button(key) if key == "phone:test"
                    else QColor(255, 255, 255, int(16 + 30 * self.hover_t.get(key, 0.0))))
            self._pill(p, rects[key], label, fill)

    def _paint_phone_folded(self, p: QPainter, phone: dict) -> None:
        row = QRectF(self.rx + 10, self._phone_y(), self.rw - 20, PHONE_FOLDED_H)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(QColor(255, 255, 255, 10))
        p.drawRoundedRect(row, row.height() / 2, row.height() / 2)
        p.setFont(self.f_icon_small)
        p.setPen(QColor("#34D399"))
        p.drawText(QRectF(row.x() + 8, row.y(), 18, row.height()), Qt.AlignCenter, ICONS["phone"])
        p.setFont(self.f_hint)
        p.setPen(QColor(226, 232, 240, 170) if not phone.get("error") else QColor("#F87171"))
        text = "Phone notifications on" + (f" · {phone['sent']} sent" if phone.get("sent") else "") + (
            f" · not sent: {phone['error']}" if phone.get("error") else "")
        rects = self._phone_rects()
        room = int(rects["phone:test"].x() - row.x() - 34)
        p.drawText(QRectF(row.x() + 30, row.y(), room, row.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(self.f_hint).elidedText(text, Qt.ElideRight, room))
        self._phone_buttons(p, (("phone:test", "Test"), ("phone:fold", "⌄")))

    def _paint_schedules(self, p: QPainter, progress: float) -> None:
        """Prompts waiting for their time; × cancels one."""
        items = self._schedules()
        appear = max(0.0, min(1.0, (progress - 0.55) / 0.45))
        if not items or appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        self._section_head(p, QRectF(self.rx + 14, self._sched_y(), self.rw - 28, 16), "Upcoming", "scheduled prompts")
        fm = QFontMetrics(self.f_sub)
        for i, item in enumerate(items):
            row = self._sched_row(i)
            stamp = _when_text(item["at"], "%a %H:%M") + (" ↻" if item.get("repeat") else "")
            p.setFont(self.f_chip)
            p.setPen(QColor("#8B7CFF"))
            p.drawText(QRectF(row.x() + 4, row.y(), 64, row.height()), Qt.AlignLeft | Qt.AlignVCenter, stamp)
            p.setFont(self.f_sub)
            p.setPen(QColor(226, 232, 240, 200))
            text_rect = QRectF(row.x() + 70, row.y(), row.width() - 96, row.height())
            p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(f"{item['title']}: {item['prompt']}", Qt.ElideRight, int(text_rect.width())))
            key = f"unsched:{i}"
            p.setFont(self.f_icon_small)
            p.setPen(QColor(255, 255, 255, int(90 + 140 * self.hover_t.get(key, 0.0))))
            p.drawText(QRectF(row.right() - 20, row.y(), 18, row.height()), Qt.AlignCenter, "×")
        p.restore()

    def _paint_limits(self, p: QPainter, progress: float) -> None:
        """How much of each agent's 5-hour and weekly limits is used, and when they reset."""
        rows = self._limit_rows()
        appear = max(0.0, min(1.0, (progress - 0.58) / 0.42))
        if not rows or appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        y = self._limits_y()
        estimated = any(w.get("est") for _s, windows in rows for w in windows.values())
        self._section_head(p, QRectF(self.rx + 14, y, self.rw - 28, 16), "Limits",
                           "≈ from your transcripts · resets" if estimated else "used · resets")
        spin = bool(self.st.get("limits_refreshing"))
        p.save()  # ↻: ask for fresh numbers now (it turns while it does)
        p.translate(self.rx + 79, y + 8)
        if spin:
            p.rotate((time.time() * 360) % 360)
        p.setFont(self.f_icon_small)
        p.setPen(QColor(255, 255, 255, int(110 + 130 * max(self.hover_t.get("limits:refresh", 0.0), float(spin)))))
        p.drawText(QRectF(-9, -8, 18, 16), Qt.AlignCenter, ICONS["replay"])
        p.restore()
        fm = QFontMetrics(self.f_hint)
        for i, (source, windows) in enumerate(rows):
            row = self._limit_row(i)
            stacked = self._two_col()  # the side column: the name, then a line per window
            name_w = row.width() - 70 if stacked else 84
            line = LIMIT_H - 2
            p.setFont(self.f_sub)
            p.setPen(QColor(226, 232, 240, 210))
            p.drawText(QRectF(row.x() + 4, row.y(), name_w - 6, line), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(self.f_sub).elidedText(LIMIT_NAMES.get(source, source.capitalize()),
                                                           Qt.ElideRight, name_w - 6))
            if not windows:  # Claude Code without numbers yet
                on = self._limit_on_rect()
                p.setFont(self.f_hint)
                p.setPen(QColor(226, 232, 240, 120))
                hint = QRectF(row.x() + 4, row.y() + line, row.width() - 8, line) if stacked else QRectF(
                    row.x() + name_w, row.y(), (on.x() if on else row.right()) - row.x() - name_w - 6, line)
                text = ("Shows after Claude Code's next reply in a terminal, or its /usage" if on is None
                        else "Show its 5-hour and weekly limits")
                p.drawText(hint, Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(text, Qt.ElideRight, int(hint.width())))
                if on is None:
                    continue
                self._pill(p, on, "Turn on", self._go_button("limits:on"))
                continue
            half = row.width() - 8 if stacked else (row.width() - name_w - 8) / 2
            for j, (key, label) in enumerate((("five_hour", "5h"), ("seven_day", "week"))):
                x = row.x() + 4 if stacked else row.x() + name_w + j * (half + 8)
                ly = row.y() + (1 + j) * (line - 4) + 2 if stacked else row.y()
                w = windows.get(key)
                p.setFont(self.f_hint)
                p.setPen(QColor(226, 232, 240, 120))
                p.drawText(QRectF(x, ly, 28, line), Qt.AlignLeft | Qt.AlignVCenter, label)
                pct = w.get("pct") if w else None
                # Estimated from the transcripts: "≈"; not calibrated yet: the tokens used in the window instead.
                amount = (f"{'≈' if w.get('est') else ''}{pct}%" if pct is not None
                          else f"{tokens_label(int(w.get('tokens') or 0))} tok") if w else ""
                text = amount + (f" · {_when_text(w['resets'])}" if w.get("resets") else "") if w else "–"
                tw = fm.horizontalAdvance("≈100% · 00:00") + 2  # the same for every row: the bars line up
                bar = QRectF(x + 30, ly + line / 2 - 2, max(10.0, half - 30 - tw - 6), 4)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 26))
                p.drawRoundedRect(bar, 2, 2)
                if pct is not None:
                    fill = _limit_color(pct)
                    if w.get("est"):
                        fill.setAlpha(170)  # an estimate: a touch softer than a reading
                    p.setBrush(fill)
                    p.drawRoundedRect(QRectF(bar.x(), bar.y(), max(4.0, bar.width() * pct / 100), 4), 2, 2)
                p.setPen(QColor(226, 232, 240, 200 if w else 90))
                p.drawText(QRectF(bar.right() + 6, ly, tw, line), Qt.AlignLeft | Qt.AlignVCenter, text)
        p.restore()

    def _paint_history(self, p: QPainter, progress: float) -> None:
        """The last few things you said, newest first; click one to copy it."""
        appear = max(0.0, min(1.0, (progress - 0.55) / 0.45))
        if appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        dy = 10 * (1 - appear)
        self._section_head(p, QRectF(14, self._hist_y() + dy, self.cw - 28, 16), "You said", "click to copy")
        items = self._history()
        if not items:
            p.setFont(self.f_sub)
            p.setPen(QColor(226, 232, 240, 95))
            p.drawText(self._history_row(0).adjusted(4, dy, 0, dy), Qt.AlignLeft | Qt.AlignVCenter,
                       "What you say will show up here")
        fm = QFontMetrics(self.f_sub)
        for i, item in enumerate(items):
            key = f"hist:{i}"
            rect = self._history_row(i).translated(0, dy)
            ht = self.hover_t.get(key, 0.0)
            copied = self._copied == key and time.time() - self._copied_at < 1.2
            if ht > 0.01 or copied:
                fill = QColor("#34D399") if copied else QColor(255, 255, 255)
                fill.setAlpha(55 if copied else int(20 * ht))
                p.setPen(Qt.NoPen)
                p.setBrush(fill)
                p.drawRoundedRect(rect, 6, 6)
            text_rect = rect.adjusted(6, 0, -52, 0)
            p.setFont(self.f_sub)
            p.setPen(QColor(226, 232, 240, 210 if i == 0 else 150))
            p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(item.get("text", ""), Qt.ElideRight, int(text_rect.width())))
            lang = item.get("lang", "")
            if lang:
                self._chip(p, rect.right() - 46, rect.y() + (rect.height() - 15) / 2, lang.upper())
            p.setFont(self.f_icon_small)
            p.setPen(QColor(255, 255, 255, 235 if copied else int(110 + 120 * ht)))
            p.drawText(QRectF(rect.right() - 20, rect.y(), 16, rect.height()), Qt.AlignCenter,
                       ICONS["check" if copied else "copy"])
        p.restore()

    def _paint_segmented(self, p: QPainter, progress: float, order: int, track: QRectF, label: str,
                         keys: list[str], texts: list[str], pos: float) -> None:
        """Segmented control with a sliding indicator (speed, mode)."""
        appear = max(0.0, min(1.0, (progress - order * 0.07) / 0.5))
        if appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        track = track.translated(0, 10 * (1 - appear))
        p.setFont(self.f_tile)
        p.setPen(QColor(226, 232, 240, 170))
        p.drawText(QRectF(14, track.y(), 56, track.height()), Qt.AlignLeft | Qt.AlignVCenter, label)

        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(QColor(255, 255, 255, 12))
        p.drawRoundedRect(track, 8, 8)

        seg = track.width() / len(keys)
        ind = QRectF(track.x() + pos * seg + 2, track.y() + 2, seg - 4, track.height() - 4)
        glow = QColor(self.accent)
        glow.setAlpha(110)
        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        p.setBrush(glow)
        p.drawRoundedRect(ind, 6, 6)

        active = round(pos)
        for i, (key, text) in enumerate(zip(keys, texts)):
            rect = QRectF(track.x() + i * seg, track.y(), seg, track.height())
            ht = self.hover_t.get(key, 0.0)
            if i != active and ht > 0.01:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, int(18 * ht)))
                p.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)
            p.setFont(self.f_chip if i == active else self.f_hint)
            p.setPen(QColor(255, 255, 255, 245 if i == active else int(150 + 60 * ht)))
            p.drawText(rect, Qt.AlignCenter, text)
        p.restore()

    # -- interaction ----------------------------------------------------------------------
    def enterEvent(self, _e):
        self._hovering = True
        self._kick()

    def leaveEvent(self, _e):
        self._hovering = False
        self._kick()
        self._tuck_hover = False
        self.hover = None
        QToolTip.hideText()

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._drag_from is not None and e.buttons() & Qt.LeftButton:
            delta = e.globalPosition() - self._drag_from
            if self._dragged or abs(delta.x()) + abs(delta.y()) > 4:
                self._dragged = True
                self.pressed = None
                self.move(int(self._win_from.x() + delta.x()), int(self._win_from.y() + delta.y()))
            return
        hit = self._hit(pos)
        if hit != self.hover:
            self.hover = hit
            self._kick()  # its highlight eases in
            self._hover_preview(hit)
            tip = self._tooltip(hit)
            if tip:
                at = e.globalPosition().toPoint()
                below = self.mapToGlobal(QPointF(0, self.height())).toPoint().y() - 8
                QToolTip.showText(QPoint(at.x(), max(at.y(), below)), tip, self)
            else:
                QToolTip.hideText()

    # -- reply preview ----------------------------------------------------------------
    def _hover_preview(self, key: str | None) -> None:
        """Resting on a thread row opens its last reply beside the panel."""
        t = self._nth(key, self._threads()) if key and key.startswith("thr:") else None
        if t and t["session"] != self._preview_for:
            self._preview_for = t["session"]
            self._preview_row = self._thread_row(int(key.split(":", 1)[1]))
            self._preview_timer.start(450)

    def _show_preview(self) -> None:
        session = self._preview_for
        if not session or not (self.hover or "").startswith("thr:"):
            self._preview_for = ""
            return

        def fetch():
            from urllib.parse import quote

            detail = client.request(f"/thread?session={quote(session)}", timeout=1.0)
            if detail:
                self._preview_detail = detail
                QTimer.singleShot(0, self._present_preview)

        threading.Thread(target=fetch, daemon=True).start()

    def _present_preview(self) -> None:
        detail = getattr(self, "_preview_detail", None)
        if not detail or detail.get("session") != self._preview_for:
            return
        row_y = int(self._preview_row.y() + (NOTCH_H - BAR_H if self.island else 0))
        self.preview.present(detail, self.visual_rect(), row_y)
        self._preview_watch.start(200)

    def _check_preview(self) -> None:
        from PySide6.QtGui import QCursor

        at = QCursor.pos()
        on_row = (self.hover or "").startswith("thr:") and self.visual_rect().contains(at)
        if on_row or self.preview.geometry().adjusted(-12, -12, 12, 12).contains(at):
            if on_row:
                t = self._nth(self.hover, self._threads())
                if t and t["session"] != self.preview.session:
                    self._preview_for = ""  # another row: its own preview
                    self._hover_preview(self.hover)
            return
        self._hide_preview()

    def _hide_preview(self) -> None:
        self._preview_watch.stop()
        self._preview_timer.stop()
        self._preview_for = ""
        self.preview.hide()

    def _tooltip(self, key: str | None) -> str:
        """What a control does; for the action buttons, their shortcut first (it's no longer on them)."""
        tip = self._tooltip_text(key)
        tile = next((t for t in self._tiles() if t[0] == key), None) if key else None
        if tile:
            head = tile[2] + (f" · {_hotkey_label(tile[3])}" if "+" in tile[3] else f" · {tile[3]}" if tile[3] else "")
            return f"{head}\n{tip}" if tip else head
        return tip

    def _tooltip_text(self, key: str | None) -> str:
        if key == "phone":
            return ("Phone notifications (ntfy): replies, what needs you, limits and the recap on your phone while "
                    "you're away. Sends them over the internet: off until you turn it on")
        if key == "headphones":
            return ("The voice only in headphones: through the speakers (an open office), chimes and the reply as a "
                    "banner instead. It switches by itself when you plug them in or take them off")
        if key == "phone:test":
            return "Send a test notification"
        if key == "phone:copy":
            return ("Copy the link. In the ntfy app: + (Subscribe to topic), paste it. Scanning the QR code with "
                    "the camera opens it in the browser instead: subscribe from the app")
        if key == "phone:new":
            return "Make a new private link (the old QR code stops getting anything)"
        if key == "phone:fold":
            return ("Show the QR code" if self.phone_folded else "Fold it away (notifications stay on; the Phone "
                    "button turns them off)")
        if key and key.startswith("unsched:"):
            return "Cancel this scheduled prompt"
        if key == "limits:refresh":
            return "Refresh now: asks Claude Code for its usage (claude -p /usage) and re-reads the others"
        if key == "limits:on":
            return ("Adds agent-voice's status line to Claude Code: it reports the limits after each reply. "
                    "Undo: agent-voice usage off")
        if key == "primary":
            prim = self._primary()
            return prim[2] if prim else ""
        if key == "chevron":
            return "Collapse" if self.expanded else "All actions"
        if key == "tuck":
            return "Tuck away until something new happens"
        if key == "history":
            return "Search every conversation: what you asked and what each agent answered (kept on this PC)"
        if key == "newthread":
            return f"Start a new thread: type or say its first prompt ({_hotkey_label(self.hotkeys.get('new_thread', ''))})"
        if key == "guide":
            return "Setup guide: every agent, step by step"
        if key and key.startswith("pill:"):
            t = self._nth(key, self._pills())
            if t:
                state = {"permission": "needs approval", "question": "asks you", "input": "waiting for you",
                         "working": "working", "speaking": "speaking", "listening": "listening",
                         "queued": "next up"}.get(t.get("state", ""), "done")
                return f"{t['title']}: {state} · click to open"
            return ""
        if key == "result":
            return "Open the conversation to see the changes"
        if key == "update":
            up = self.st.get("update") or {}
            if up.get("state"):
                return f"Updating to version {up.get('version')}"
            if up.get("how") == "auto":
                return f"Install version {up.get('version')}: downloads it, then restarts agent-voice"
            return f"Version {up.get('version')} is out: click for how to update"
        if key and (key.startswith("need:") or key.startswith("allow:") or key.startswith("deny:")
                    or key.startswith("resume:")):
            n = self._nth(key, self._needs())
            if n is None:
                return ""
            if key.startswith("resume:"):
                return ("Won't send it anything: click to send it “continue” when its limit resets"
                        if not n.get("resume") else "Sends it “continue” once its limit resets: click to cancel")
            if key.startswith("allow:"):
                return f"Let {n['title']} {n['text']}"
            if key.startswith("deny:"):
                return f"Don't let {n['title']} {n['text']}"
            return f"{n['title']}: {n['text']}\n\nClick to open it"
        if key and key.startswith("agent:"):
            agent = key.split(":", 1)[1]
            name = AGENT_NAMES.get(agent, agent)
            state = self._agent_state(agent)
            if state == "on":
                extra = " (in Cline, turn on Settings > Features > Enable Hooks)" if agent == "cline" else ""
                return f"{name} speaks through agent-voice{extra}"
            if state == "off":
                return f"Connect {name}: adds agent-voice to its settings"
            if state == "setup":
                return "Bridge ready, extension not loaded yet: click for the steps (chrome://extensions, Load unpacked)"
            return f"{name} isn't installed on this PC"
        if key and key.startswith("speed:"):
            return f"Voice speed {float(key.split(':', 1)[1]):g}×"
        if key and key.startswith("hist:"):
            item = self._nth(key, self._history())
            if item:
                text = item.get("text", "")
                return (text if len(text) <= 400 else text[:400] + "…") + "\n\nClick to copy"
        if key and key.startswith("thr:"):
            return ""  # resting on it shows its last reply in full; a click opens it
        if key == "wake":
            return "Hey Claude: while idle, say “Hey Claude, …” to send a new prompt to your last thread"
        if key == "approve":
            return "Voice approvals: permission requests are read out; answer yes / no or click"
        if key == "summary":
            return "Final answers as a one-sentence summary, or read in full"
        if key and key.startswith("opt:"):
            return self._nth(key, (self._question() or {}).get("options") or []) or ""
        if key == "engine:piper":
            return "Light voice (Piper): quick, and easy on the battery"
        if key == "engine:kokoro":
            return ("Natural voice (Kokoro): more expressive, about three times the CPU. Downloaded the first time "
                    "(about 120 MB), then offline like the light one")
        if key == "mode:voice":
            return "Voice: replies are read aloud and the mic opens"
        if key == "mode:chime":
            hk = _hotkey_label(self.hotkeys.get("chime", ""))
            return f"Chimes: no voice, a soft sound and the reply as a banner ({hk})"
        if key == "mode:silent":
            hk = _hotkey_label(self.hotkeys.get("mute", ""))
            return (f"Do not disturb: no voice, no sound, no banner, the notch stays up. What happens meanwhile is "
                    f"told when you're back ({hk})")
        if key and key.startswith("thrtalk:"):
            return "Talk to this conversation: what you say is sent to it"
        if key and key.startswith("thrtype:"):
            return "Type a reply to this conversation"
        if key and key.startswith("thrplay:"):
            return "Hear this conversation's last reply again"
        if key and key.startswith("thrcopy:"):
            return "Copy this conversation's last reply"
        return ""

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            return
        self._kick()
        self._drag_from = None if self.notch else e.globalPosition()  # the notch stays docked
        self._win_from = self.pos()
        self._dragged = False
        self.pressed = self._hit(e.position())
        if self.pressed == "hold":
            _post("/ptt", {"action": "start"})

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.RightButton:
            return
        pressed, self.pressed = self.pressed, None
        if pressed == "hold":
            _post("/ptt", {"action": "stop"})
        elif self._dragged:
            self._snap()
            self._save_position()
        elif pressed and pressed == self._hit(e.position()):
            self._activate(pressed)
        self._drag_from = None
        self._dragged = False

    def _activate(self, key: str) -> None:
        if key == "primary":
            prim = self._primary()
            key = prim[0] if prim else ""
        if key == "undnd":  # back to Voice or Chimes, as it was before
            mode = self.st.get("dnd_from") or "voice"
            self.st["output"] = mode
            _post("/output", {"mode": mode})
        elif key == "chevron":
            self.toggle_expanded()
        elif key == "tuck":
            self.tuck()
        elif key == "history":
            self.open_history()
        elif key == "newthread":
            self.open_new_thread()
        elif key.startswith("pill:"):
            t = self._nth(key, self._pills())
            if t:
                _post("/open_thread", {"session": t["session"]})
        elif key == "update":
            self.start_update()
        elif key == "result":
            _post("/open_thread", {"session": (self.st.get("result") or {}).get("session", "")})
        elif key == "headphones":
            _post("/headphones", {"mode": "toggle"})
        elif key == "phone":
            on = not (self.st.get("phone") or {}).get("enabled")
            _post("/phone", {"on": on})
            if on:
                self.phone_folded = False  # just turned on: the QR code, to scan it
                self._note("Phone notifications on: scan the QR code in the panel with the ntfy app (iPhone or "
                           "Android). Whoever has that link can read them, so keep it to yourself.")
        elif key == "phone:test":
            _post("/phone", {"action": "test"})
        elif key == "phone:copy":
            QApplication.clipboard().setText((self.st.get("phone") or {}).get("url", ""))
            self._copied, self._copied_at = key, time.time()
        elif key == "phone:new":
            _post("/phone", {"action": "new"})
            self._note("New private link: scan the new QR code; the old one gets nothing any more.")
        elif key == "phone:fold":
            self.phone_folded = not self.phone_folded  # the QR code only when you need it
            self._save_position()
        elif key.startswith("unsched:"):
            item = self._nth(key, self._schedules())
            if item:
                _post("/schedule_cancel", {"id": item["id"]})
                self.st["schedules"] = [s for s in self.st.get("schedules") or [] if s is not item]
        elif key.startswith("resume:"):
            n = self._nth(key, self._needs())
            if n:
                _post("/resume", {"session": n["session"], "on": not n.get("resume")})
                n["resume"] = not n.get("resume")
        elif key.startswith(("allow:", "deny:", "need:")):
            n = self._nth(key, self._needs())
            if n:
                if key.startswith("need:"):
                    _post("/open_thread", {"session": n["session"]})
                else:
                    _post("/permission_answer", {"id": n["id"], "allow": key.startswith("allow:")})
                    self.st["needs"] = [x for x in self.st.get("needs") or [] if x is not n]
        elif key == "start":
            threading.Thread(target=client.start_detached, daemon=True).start()
        elif key == "reply":
            _post("/reply")
        elif key == "stop":
            _post("/stop")
        elif key == "handsfree":
            _post("/handsfree", {"mode": "toggle"})
        elif key == "mute":
            _post("/mute", {"mode": "toggle"})
        elif key == "read":
            _post("/read")
        elif key == "replay":
            _post("/replay")
        elif key == "copyreply":
            if self.st.get("last_reply"):
                _post("/copy_reply")
                self._copied, self._copied_at = key, time.time()
        elif key.startswith(("thrtalk:", "thrtype:", "thrplay:", "thrcopy:", "thr:")):
            t = self._nth(key, self._threads())
            if t is None:
                pass
            elif key.startswith("thrtalk:"):
                _post("/reply", {"session": t["session"]})
            elif key.startswith("thrtype:"):
                self.open_reply(t)
            elif key.startswith("thrplay:"):
                _post("/replay", {"session": t["session"]})
            elif key.startswith("thrcopy:"):
                _post("/copy_reply", {"session": t["session"]})
                self._copied, self._copied_at = key, time.time()
            else:
                _post("/open_thread", {"session": t["session"]})
        elif key.startswith("agent:"):
            agent = key.split(":", 1)[1]
            state, name = self._agent_state(agent), AGENT_NAMES.get(agent, agent)
            if state in ("off", "setup"):  # connect it (again: the browser re-opens the extension's folder)
                _post("/install", {"agent": agent})
                if state == "off":
                    self.st.setdefault("agents", {})[agent] = "setup" if agent == "browser" else "on"
            elif state == "on":
                self._note(f"{name[:1].upper()}{name[1:]} is connected. Not hearing it? Restart it once; to "
                           f"disconnect it: agent-voice uninstall {agent}")
            else:
                self._note(f"{name[:1].upper()}{name[1:]} isn't installed on this PC. Install it, then click "
                           "here to connect it. The ? opens the setup guide.")
        elif key == "limits:refresh":
            _post("/limits_refresh")
            self.st["limits_refreshing"] = True
        elif key == "limits:on":
            _post("/usage_line", {"enable": True})
            self.st["usage_line"] = "on"  # the row goes until the first reply brings the numbers
        elif key == "guide":
            QDesktopServices.openUrl(QUrl(GUIDE_URL))
        elif key == "openchat":
            session = (self.st.get("last_reply") or {}).get("session", "")
            _post("/open_thread", {"session": session})
        elif key.startswith("speed:"):
            value = float(key.split(":", 1)[1])
            self.st["speed_x"] = value  # move the indicator now, don't wait for the next poll
            _post("/speed", {"value": value})
        elif key.startswith("hist:"):
            item = self._nth(key, self._history())
            if item:
                QApplication.clipboard().setText(item.get("text", ""))
                self._copied, self._copied_at = key, time.time()
        elif key.startswith("opt:"):
            i = int(key.split(":", 1)[1])
            q = self._question() or {}
            if q.get("permission"):
                _post("/permission_answer", {"id": q["permission"], "allow": i == 0})
                self.st["permission"] = None
            else:
                _post("/answer", {"index": i})
                self.st["question"] = None  # close the card now; the daemon answers in the background
        elif key == "wake":
            _post("/wake", {"mode": "toggle"})
        elif key == "approve":
            _post("/approvals", {"mode": "toggle"})
        elif key == "summary":
            _post("/summary", {"mode": "toggle"})
        elif key.startswith("engine:"):
            name = key.split(":", 1)[1]
            self.st["engine"] = name
            _post("/engine", {"engine": name})
        elif key.startswith("mode:"):
            mode = key.split(":", 1)[1]
            self.st["output"] = mode  # move the indicator now
            _post("/output", {"mode": mode})

    # -- drag and drop: files, screenshots or text dropped on the notch go to a conversation -----------------
    def _drop_target(self, pos: QPointF) -> dict:
        """The thread row under the cursor (panel open), else the conversation that spoke last."""
        hit = self._hit(pos) or ""
        threads = self._threads()
        t = self._nth(hit, threads) if hit.startswith("thr") and ":" in hit else None
        if t:
            return {"session": t["session"], "title": t["title"]}
        last = (self.st.get("last_reply") or {}).get("session", "")
        title = next((t["title"] for t in threads if t["session"] == last), threads[0]["title"] if threads else "")
        return {"session": last, "title": title or "the last conversation"}

    def dragEnterEvent(self, e):
        mime = e.mimeData()
        if mime.hasUrls() or mime.hasImage() or mime.hasText():
            e.acceptProposedAction()
            self._drop = self._drop_target(e.position())
            self._reveal_until = time.time() + NOTCH_HIDE_DELAY

    def dragMoveEvent(self, e):
        self._drop = self._drop_target(e.position())
        e.acceptProposedAction()

    def dragLeaveEvent(self, _e):
        self._drop = None

    def dropEvent(self, e):
        target = self._drop or self._drop_target(e.position())
        self._drop = None
        mime = e.mimeData()
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        text = ""
        if not paths and mime.hasImage():  # a screenshot dragged from a browser or a viewer: save it first
            folder = HOME / "drops"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / time.strftime("drop-%Y%m%d-%H%M%S.png")
            if mime.imageData().save(str(path), "PNG"):
                paths = [str(path)]
        if not paths:
            text = mime.text() if mime.hasText() else ""
        if paths or text.strip():
            e.acceptProposedAction()
            _post("/attach", {"paths": paths, "text": text, "session": target["session"]})

    def open_new_thread(self) -> None:
        """Start a new thread: the box under the notch, with where it can run."""
        self.reply_box.open_new(list(self.st.get("new_targets") or []), self.st.get("new_default") or {},
                                QRectF(self.visual_rect()), int(self.pw))

    def open_history(self) -> None:
        """Search every conversation, in a box under the notch."""
        self.history_box.open(QRectF(self.visual_rect()), int(self.pw))

    def open_reply(self, item: dict) -> None:
        """Type a reply to a conversation (from its banner or its thread row)."""
        self.reply_box.open(item, QRectF(self.visual_rect()), int(self.pw))

    def _news(self) -> tuple:
        """What would bring the notch down by itself: a voice or the mic, a question, someone needing you, a banner."""
        st = self.st
        live = self.phase if self.phase in ("speaking", "waiting", "recording", "transcribing") else ""
        q = self._question() or {}
        return (live, q.get("permission") or q.get("question"), len(st.get("needs") or []),
                (st.get("banner") or {}).get("id"))

    def tuck(self) -> None:
        """Out of the way now (it was covering something): up to its 3-pixel line until there's news."""
        self._tucked = self._news()
        self._tuck_hover = True
        self.expanded = False
        self._reveal_until = 0.0
        QToolTip.hideText()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
        self._save_position()
        if self.tray:
            self.tray.refresh(force=True)

    def toggle_compact(self) -> None:
        """Switch between the full bar and a small orb, keeping the right edge where it was."""
        if self.notch:
            self.toggle_notch()
        right = self.x() + self.width()
        self.compact = not self.compact
        self._apply_size()
        self.move(right - self.width(), self.y())
        self._snap()
        self._save_position()

    def toggle_notch(self) -> None:
        """Dock at the top centre of the screen like a camera notch, or float again where it was."""
        self.notch = not self.notch
        if self.notch:
            self._bar_pos = (self.x(), self.y())
            self.compact = False
            self._reveal, self._reveal_until = 1.0, time.time() + NOTCH_HIDE_DELAY
        else:
            self._reveal, self._reveal_v = 1.0, 0.0  # floating: always fully shown
            if self._bar_pos:
                self.move(*self._bar_pos)
        self._apply_size()
        _apply_windows_style(int(self.winId()))
        self._save_position()

    def show_panel(self) -> None:
        """Opened from the Start menu: visible, and the panel open."""
        if not self.isVisible():
            self.toggle_visible()
        if not self.expanded:
            self.toggle_expanded()
        self._reveal_until = time.time() + NOTCH_HIDE_DELAY

    def _relaunch(self) -> None:
        """Start a fresh widget (the code may have changed) and leave: our lock goes first so it can take it."""
        if self._quitting:
            return
        self._quitting = True
        global _LOCK
        if _LOCK is not None:
            _LOCK.unlock()
            _LOCK = None
        import subprocess

        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        try:
            subprocess.Popen(client.self_command("widget"), **kwargs)
        except OSError:
            pass
        QApplication.quit()

    def toggle_expanded(self) -> None:
        if self.compact:
            self.toggle_compact()
        self.expanded = not self.expanded  # _follow_size() glides to the new height
        self._kick()
        self._save_position()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.setStyleSheet(MENU_STYLE)
        geo = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        if self.compact:
            actions = [("Full widget", self.toggle_compact), ("Show all actions", self.toggle_expanded), (None, None)]
        elif self.notch:
            actions = [("Collapse" if self.expanded else "Show all actions", self.toggle_expanded),
                       ("Float the widget again", self.toggle_notch), (None, None)]
        else:
            actions = [
                ("Collapse" if self.expanded else "Show all actions", self.toggle_expanded),
                ("Compact (orb only)", self.toggle_compact),
                ("Notch (top of the screen)", self.toggle_notch),
                (None, None),
            ]
        if not self.notch:
            actions += [
                ("Move to top right", lambda: self._place(geo.right() - NOTCH_W - 8, geo.top() + 64)),
                ("Move to top left", lambda: self._place(geo.left() + 16, geo.top() + 64)),
                (None, None),
            ]
        actions += [
            (f"Hide widget ({_hotkey_label(self.hotkeys.get('widget', ''))})", self.toggle_visible),
            ("Stop voice service", lambda: (_post("/shutdown"))),
        ]
        for label, fn in actions:
            if label is None:
                menu.addSeparator()
                continue
            act = QAction(label, menu)
            act.triggered.connect(fn)
            menu.addAction(act)
        menu.exec(e.globalPos())

    def _place(self, x: int, y: int) -> None:
        self.move(x, y)
        self._save_position()


class Tray(QSystemTrayIcon):
    """Notification-area icon: live state colour, left-click shows/hides the widget, right-click for actions."""

    GLYPHS = {"speaking": "volume", "waiting": "mic", "recording": "mic", "muted": "mute",
              "offline": "power", "transcribing": "chat"}

    def __init__(self, widget: VoiceWidget):
        super().__init__(widget)
        self.w = widget
        self._shown_phase = None
        self.menu = QMenu()
        self.menu.setStyleSheet(MENU_STYLE)
        self.menu.aboutToShow.connect(self._build_menu)
        self.setContextMenu(self.menu)
        self.activated.connect(self._on_activated)
        self.refresh(force=True)
        self.show()

    def refresh(self, force: bool = False) -> None:
        phase = self.w.phase
        if phase == self._shown_phase and not force:
            return
        self._shown_phase = phase
        color = QColor(PHASES.get(phase, PHASES["idle"])[0])
        self.setIcon(orb_icon(color, ICONS[self.GLYPHS.get(phase, "mic")]))
        title = PHASES.get(phase, PHASES["idle"])[1]
        lang = self.w.st.get("speaking_lang", "")
        extra = " · auto-send" if self.w.st.get("hands_free") else ""
        if self.w.st.get("chime"):
            extra += " · chime mode"
        self.setToolTip(f"agent-voice · {title}{f' ({lang.upper()})' if lang else ''}{extra}")

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.w.toggle_visible()

    def _build_menu(self) -> None:
        m, w, st = self.menu, self.w, self.w.st
        hk = w.hotkeys
        m.clear()
        online = w.phase != "offline"

        def add(label, fn, enabled=True, checked=None, key=""):
            text = f"{label}\t{_hotkey_label(key)}" if key else label
            act = QAction(text, m)
            act.setEnabled(enabled)
            if checked is not None:
                act.setCheckable(True)
                act.setChecked(checked)
            act.triggered.connect(fn)
            m.addAction(act)

        update = st.get("update")
        if update:
            how, state = update.get("how"), update.get("state")
            label = (f"Updating to v{update['version']}…" if state else
                     f"Update to v{update['version']} (restarts agent-voice)" if how == "auto" else
                     f"Update available: v{update['version']} (download)" if how == "page" else
                     f"Update available: v{update['version']} (how to)")
            add(label, w.start_update, enabled=not state)
            m.addSeparator()
        add("Today's recap", lambda: _post("/recap"), enabled=online)
        m.addSeparator()
        add("Hide widget" if w.isVisible() else "Show widget", w.toggle_visible, key=hk.get("widget", ""))
        add("Compact widget (orb only)", w.toggle_compact, checked=w.compact)
        add("Notch (top of the screen)", w.toggle_notch, checked=w.notch)
        m.addSeparator()
        add("New thread…", w.open_new_thread, online, key=hk.get("new_thread", ""))
        add("Search history…", w.open_history, online)
        add("Reply now", lambda: _post("/reply"), online, key=hk.get("reply", ""))
        add("Stop", lambda: _post("/stop"), online, key=hk.get("stop", ""))
        add("Read selection", lambda: _post("/read"), online, key=hk.get("read_selection", ""))
        m.addSeparator()
        add("Auto-send replies", lambda: _post("/handsfree", {"mode": "toggle"}), online,
            bool(st.get("hands_free")), hk.get("hands_free", ""))
        add("Do not disturb", lambda: _post("/mute", {"mode": "toggle"}), online, bool(st.get("muted")), hk.get("mute", ""))
        add("Chime mode (sounds only)", lambda: _post("/chime", {"mode": "toggle"}), online,
            bool(st.get("chime")), hk.get("chime", ""))
        add("Summarize answers", lambda: _post("/summary", {"mode": "toggle"}), online, bool(st.get("summary", True)))
        add("Approve permissions by voice", lambda: _post("/approvals", {"mode": "toggle"}), online,
            bool(st.get("approvals", True)))
        add("Wake word (Hey Claude)", lambda: _post("/wake", {"mode": "toggle"}), online, bool(st.get("wake")))
        add("Voice in headphones only", lambda: _post("/headphones", {"mode": "toggle"}), online,
            bool(st.get("headphones_only")))
        add("Read ChatGPT / Copilot notifications", lambda: _post("/apps", {"mode": "toggle"}), online,
            bool(st.get("desktop_apps")))
        speed_menu = m.addMenu("Voice speed")
        speed_menu.setStyleSheet(MENU_STYLE)
        speed_menu.setEnabled(online)
        current = float(st.get("speed_x", 1.5) or 1.5)
        for x in SPEEDS:
            act = QAction(f"{x:g}×", speed_menu)
            act.setCheckable(True)
            act.setChecked(abs(x - current) < 0.01)
            act.triggered.connect(lambda _=False, v=x: _post("/speed", {"value": v}))
            speed_menu.addAction(act)
        m.addSeparator()
        if not online:
            add("Start agent-voice", lambda: threading.Thread(target=client.start_detached, daemon=True).start())
        add("Restart agent-voice", lambda: threading.Thread(target=client.restart, daemon=True).start())
        add("Start with macOS" if MAC else "Start with Windows" if sys.platform == "win32" else "Start at login",
            self._toggle_autostart, checked=_autostart_enabled())
        m.addSeparator()
        add("Quit agent-voice", self._quit_all)

    @staticmethod
    def _quit_all() -> None:
        """Everything stops: the voice service and the notch (start it again from the Start menu)."""
        client.request("/shutdown", {}, timeout=1.0)
        QApplication.quit()

    @staticmethod
    def _toggle_autostart() -> None:
        from agent_voice.install import autostart

        autostart(not _autostart_enabled())


def _autostart_enabled() -> bool:
    if not (sys.platform in ("win32", "darwin") or sys.platform.startswith("linux")):
        return False
    from agent_voice.install import _startup_file

    return _startup_file().exists()


_LOCK = None  # the widget's single-instance lock (released early by _relaunch)


def run(demo: str | None = None) -> int:
    from PySide6.QtCore import QLockFile

    global _LOCK

    if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY") and "QT_QPA_PLATFORM" not in os.environ:
        # Wayland doesn't let a window place itself or stay on top: the notch runs through XWayland, where it can.
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    app = QApplication(sys.argv)
    # Hiding the widget must not quit: the tray icon keeps running.
    app.setQuitOnLastWindowClosed(False)
    HOME.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(HOME / "widget.lock"))
    if not demo and not lock.tryLock(3000):  # a widget relaunching itself lets go of it first
        return 0  # already showing
    _LOCK = lock
    w = VoiceWidget()
    if demo:  # design preview: fake a status instead of polling the daemon
        w.poller.status.disconnect()
        fake = {"phase": demo, "level": 0.03, "speaking_lang": "fr", "hands_free": demo == "waiting",
                "last_heard": "Can you add tests for the server?", "last_heard_lang": "en"}
        QTimer.singleShot(0, lambda: w._on_status(fake))
    app.setWindowIcon(orb_icon(QColor(PHASES["speaking"][0]), ICONS["volume"]))
    if not getattr(w, "start_hidden", False):
        w.show()
    if not demo and QSystemTrayIcon.isSystemTrayAvailable():
        w.tray = Tray(w)
    return app.exec()
