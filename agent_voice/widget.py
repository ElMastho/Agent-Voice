"""Floating glass status widget: shows what the voice is doing and gives one-click access to every action.

It is a separate process that polls the daemon (`/status`) and calls its endpoints, so the daemon stays headless.
The window never takes keyboard focus (WS_EX_NOACTIVATE): clicking it leaves your agent window focused,
which is where dictated text has to land.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QAction,
    QIcon,
    QPixmap,
    QColor,
    QConicalGradient,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QToolTip, QWidget

from agent_voice import client
from agent_voice.config import HOME, load_config

WIDTH = 320
BAR_H = 64
SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0)
TILE_ROWS = 3
SPEED_Y = BAR_H + 10 + TILE_ROWS * 58 + (TILE_ROWS - 1) * 8 + 6  # top of the speed row
MODES = (("voice", "Voice"), ("chime", "Chimes"))
MODE_Y = SPEED_Y + 26 + 8  # top of the mode row
THREADS_Y = MODE_Y + 26 + 10  # top of the "Threads" list (then "Recent" under it)
HIST_ROWS, THREAD_ROWS, ROW_H = 5, 4, 22
EXPANDED_H = THREADS_Y + 2 * (16 + 2 * ROW_H + 8)  # a typical panel; the real one fits its content
COMPACT = 56  # compact mode: just the orb
# Notch mode: docked to the top centre of the screen like a camera notch, tucked away when idle.
NOTCH_SCALE = 1.0
NOTCH_W, NOTCH_H = 440, 44  # wide and short: one row of status, caption and chips
NOTCH_RADIUS = 16
NOTCH_PILL = 0.56  # tucked away, the notch is a pill this fraction of its full width
NOTCH_PEEK = 3  # pixels left showing when tucked under the top edge: hover them to bring it down
NOTCH_HIDE_DELAY = 3.0  # seconds it stays down after the mouse leaves or activity ends
OPTION_ROWS = 5  # question options shown in the panel
# Matches Windows 11's native window rounding (DWMWCP_ROUND = 8 px), the only rounding that
# also clips the acrylic blur; any other radius leaves blurred square corners showing.
RADIUS = 8
POS_FILE = HOME / "widget.json"

ICON_FONT = "Segoe Fluent Icons"
ICONS = {
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
}

# phase -> (accent colour, title, subtitle)
PHASES = {
    "idle": ("#94A3B8", "Ready", ""),
    "speaking": ("#8B7CFF", "Speaking", "Skip to answer now"),
    "waiting": ("#34D399", "Listening", "Go ahead, I'm listening"),
    "recording": ("#10E0A0", "Hearing you", "Pause to finish"),
    "transcribing": ("#FBBF24", "Transcribing", "One moment"),
    "muted": ("#F87171", "Muted", "Voice is off"),
    "offline": ("#64748B", "Offline", "Voice service isn't running"),
}


SECONDARY = {
    "idle": "#6366F1",
    "speaking": "#22D3EE",
    "waiting": "#A3E635",
    "recording": "#22D3EE",
    "transcribing": "#F472B6",
    "muted": "#FB7185",
    "offline": "#475569",
}


def _round_pen(brush, width: float) -> QPen:
    pen = QPen(brush, width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _ease(current: float, target: float, k: float) -> float:
    return current + (target - current) * k


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


# -- Windows glass ---------------------------------------------------------------------
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


def _apply_windows_style(hwnd: int, blur: str, notch: bool = False) -> bool:
    """Acrylic blur behind the window, rounded corners, no focus stealing, no taskbar entry.

    Notch mode draws its own shape (square top, rounded bottom): no system rounding and no blur.
    """
    if sys.platform != "win32":
        return False
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

    # Native Windows 11 rounding: it clips the acrylic too (a window region does not). RADIUS matches it.
    corner = ctypes.c_int(1 if notch else 2)  # DWMWCP_DONOTROUND / DWMWCP_ROUND
    dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(corner), ctypes.sizeof(corner))
    # Hide the grey system border; we draw our own hairline.
    no_border = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE
    dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34, ctypes.byref(no_border), ctypes.sizeof(no_border))

    class AccentPolicy(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]

    class WinCompAttrData(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.POINTER(AccentPolicy)), ("SizeOfData", ctypes.c_size_t)]

    # 4 = acrylic, 3 = plain blur, 0 = none. Tint is AABBGGRR: a dark, slightly blue glass.
    state = 0 if notch or blur == "none" else 4 if blur == "acrylic" else 3
    accent = AccentPolicy(state, 2, 0x66201812, 0)
    data = WinCompAttrData(19, ctypes.pointer(accent), ctypes.sizeof(accent))  # WCA_ACCENT_POLICY
    try:
        return bool(user32.SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data))) and state != 0
    except AttributeError:
        return False


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
    p.setFont(QFont(ICON_FONT, int(size * 0.3)))
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
        while True:
            try:
                self.status.emit(client.request("/status", timeout=0.5))
            except RuntimeError:  # widget closed
                return
            time.sleep(self.interval)


def _post(path: str, payload: dict | None = None) -> None:
    threading.Thread(target=client.request, args=(path, payload or {}), daemon=True).start()


# -- the widget --------------------------------------------------------------------------
class VoiceWidget(QWidget):
    def __init__(self):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("agent-voice")

        cfg = load_config()
        self.hotkeys = cfg["hotkeys"]
        self.blur_mode = cfg.get("widget", {}).get("blur", "acrylic")
        self.glass = False

        self.st: dict = {}
        self.phase = "offline"
        self.accent = QColor(PHASES["offline"][0])
        self._accent_target = QColor(self.accent)
        self.second = QColor(SECONDARY["offline"])
        self._second_target = QColor(self.second)
        self.level = 0.0
        self.t0 = time.time()
        self.energy = 0.0  # how lively the waveform/aurora are, eased per phase
        self.flash = 0.0  # ripple when the phase changes
        self.hover_t: dict[str, float] = {}  # eased hover amount per button
        self.speed_pos: float | None = None  # animated index of the speed indicator
        self.mode_pos: float | None = None  # same for the voice/chime switch
        self.prev_title = ""
        self.title_t = 1.0  # title crossfade progress
        self.expanded = False
        self.compact = False
        self.notch = False
        self._bar_pos: tuple[int, int] | None = None  # where the floating bar was before notch mode
        self._reveal = 1.0  # notch: 0 tucked under the top edge, 1 fully down (a spring: may overshoot)
        self._reveal_v = 0.0
        self._reveal_until = 0.0
        self._h = float(BAR_H)  # eased window height
        self._painted_still = False  # battery: the still frame has been drawn, skip redraws until a change
        self._topmost_at = 0.0
        self._shown_digest = ""
        self._copied: str | None = None  # history row just copied, for a brief confirmation
        self._copied_at = 0.0
        self.hover: str | None = None
        self.pressed: str | None = None
        self._drag_from: QPointF | None = None
        self._dragged = False
        self._opacity = 1.0
        self._hovering = False

        self.f_title = QFont("Segoe UI Variable Display", 10)
        self.f_title.setWeight(QFont.DemiBold)
        self.f_sub = QFont("Segoe UI Variable Text", 8)
        self.f_tile = QFont("Segoe UI Variable Text", 8)
        self.f_tile.setWeight(QFont.Medium)
        self.f_hint = QFont("Segoe UI Variable Text", 7)
        self.f_notch_title = QFont("Segoe UI Variable Display", 9)
        self.f_notch_title.setWeight(QFont.DemiBold)
        self.f_notch_sub = QFont("Segoe UI Variable Text", 8)
        self.f_q = QFont("Segoe UI Variable Text", 9)
        self.f_q.setWeight(QFont.DemiBold)
        self.f_chip = QFont("Segoe UI Variable Text", 7)
        self.f_chip.setWeight(QFont.Bold)
        self.f_icon = QFont(ICON_FONT, 11)
        self.f_icon_small = QFont(ICON_FONT, 9)

        self.resize(WIDTH, BAR_H)
        self._restore_position()

        self._height_anim = QVariantAnimation(self, duration=220)
        self._height_anim.valueChanged.connect(lambda h: self.resize(WIDTH, int(h)))

        self.poller = Poller()
        self.poller.status.connect(self._on_status)
        self._toggles_seen: int | None = None
        self.tray: Tray | None = None
        self.frame = QTimer(self, interval=33, timeout=self._tick)
        self.frame.start()

    # -- window setup -------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_styled", False):
            self._styled = True
            self.glass = _apply_windows_style(int(self.winId()), self.blur_mode, self.notch)

    def _restore_position(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        try:
            pos = json.loads(POS_FILE.read_text(encoding="utf-8"))
            x, y = int(pos["x"]), int(pos["y"])
            self.expanded = bool(pos.get("expanded", False))
            self.compact = bool(pos.get("compact", False))
            self.notch = bool(pos.get("notch", False))
            self.start_hidden = bool(pos.get("hidden", False))
        except (OSError, ValueError, KeyError):
            x, y = screen.right() - WIDTH - 16, screen.top() + 64
        # Keep it on a visible screen (monitors may have changed since last time).
        if not any(s.availableGeometry().adjusted(-40, -40, 40, 40).contains(x, y) for s in QApplication.screens()):
            x, y = screen.right() - WIDTH - 16, screen.top() + 64
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

    def _is_compact(self) -> bool:
        # A question needs room: the compact orb opens into the bar until it is answered.
        return self.compact and not self.notch and not self._question()

    def _scale(self) -> float:
        return NOTCH_SCALE if self.notch else 1.0

    def _logical_h(self) -> float:
        """Window height in layout units (the notch is drawn scaled up)."""
        return self.height() / self._scale()

    def _to_layout(self, pos: QPointF) -> QPointF:
        """Notch: window coordinates -> notch-row coordinates (the island is centred while it widens)."""
        if not self.notch:
            return pos
        return QPointF(pos.x() - (self.width() - NOTCH_W) / 2, pos.y())

    def _target_size(self) -> tuple[int, int]:
        if self._is_compact():
            return COMPACT, COMPACT
        if self._question():
            w, h = WIDTH, BAR_H + int(self._question_layout()[3])
        else:
            w, h = WIDTH, self._expanded_h() if self.expanded else BAR_H
        if self.notch:  # its own row on top, the same panels underneath
            return NOTCH_W, NOTCH_H + h - BAR_H
        return w, h

    def _panel_h(self) -> float:
        """Height of the open panel under the bar (or under the notch row)."""
        return self.height() - (NOTCH_H if self.notch else BAR_H)

    def _follow_size(self) -> None:
        """Glide towards the size the content needs (panel opening, a question arriving...)."""
        if self._dragged:
            return
        w, h = self._target_size()
        if self.notch:
            w = self.width()  # the notch animates its own width (_place_notch)
        elif w != self.width():  # compact <-> bar: keep the right edge where it is
            right = self.x() + self.width()
            self.resize(w, self.height())
            self.move(right - w, self.y())
        self._h = h if abs(self._h - h) < 0.6 else _ease(self._h, h, 0.28)
        if int(round(self._h)) != self.height():
            self.resize(w, int(round(self._h)))

    def _place_notch(self) -> None:
        """Top centre of the primary screen, sliding down when wanted and back up when not."""
        now = time.time()
        busy = self.phase in ("speaking", "waiting", "recording", "transcribing")
        if busy or self._hovering or self.expanded or self.pressed or self._question():
            self._reveal_until = now + NOTCH_HIDE_DELAY
        # A lightly damped spring: it drops down, overshoots a hair in width, and settles.
        target = 1.0 if now < self._reveal_until else 0.0
        self._reveal_v = self._reveal_v * 0.66 + (target - self._reveal) * 0.12
        self._reveal += self._reveal_v
        if abs(target - self._reveal) < 0.002 and abs(self._reveal_v) < 0.002:
            self._reveal, self._reveal_v = target, 0.0
        down = _smooth(self._reveal)  # vertical travel never overshoots: it stays glued to the top edge
        grow = NOTCH_PILL + (1 - NOTCH_PILL) * max(0.0, min(1.04, self._reveal))
        geo = QApplication.primaryScreen().geometry()
        w = int(round(NOTCH_W * grow))
        hidden = geo.top() - self.height() + NOTCH_PEEK
        x = geo.center().x() - w // 2
        y = int(round(hidden + (geo.top() - hidden) * down))
        if w != self.width():
            self.resize(w, self.height())
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _save_position(self) -> None:
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            x, y = self._bar_pos if self.notch and self._bar_pos else (self.x(), self.y())
            state = {"x": x, "y": y, "expanded": self.expanded, "compact": self.compact, "notch": self.notch,
                     "hidden": not self.isVisible()}
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
        self.st = st or {}
        phase = st["phase"] if st else "offline"
        if phase != self.phase:
            self.prev_title = PHASES.get(self.phase, PHASES["idle"])[1]
            self.title_t = 0.0
            self.flash = 1.0
        self.phase = phase
        self._accent_target = QColor(PHASES.get(phase, PHASES["idle"])[0])
        # Show/hide hotkey (handled by the daemon, which owns the global hotkeys).
        toggles = self.st.get("widget_toggles")
        if toggles is not None:
            if self._toggles_seen is not None and toggles != self._toggles_seen:
                self.toggle_visible()
            self._toggles_seen = toggles
        if self.tray:
            self.tray.refresh()
        self._second_target = QColor(SECONDARY.get(phase, SECONDARY["idle"]))

    def _tick(self) -> None:
        # Ease colour and mic level so transitions glide instead of jumping.
        self.accent = _mix(self.accent, self._accent_target, 0.18)
        self.second = _mix(self.second, self._second_target, 0.18)
        target = min(1.0, float(self.st.get("level", 0.0)) * 14.0)
        self.level += (target - self.level) * (0.45 if target > self.level else 0.12)
        want_energy = {"speaking": 1.0, "recording": 0.35 + 0.65 * self.level, "waiting": 0.45,
                       "transcribing": 0.6}.get(self.phase, 0.12)
        self.energy = _ease(self.energy, want_energy, 0.08)
        self.flash = max(0.0, self.flash - 0.022)
        target_idx = self._speed_index()
        self.speed_pos = target_idx if self.speed_pos is None else _ease(self.speed_pos, target_idx, 0.22)
        mode_idx = 1.0 if self.st.get("chime") else 0.0
        self.mode_pos = mode_idx if self.mode_pos is None else _ease(self.mode_pos, mode_idx, 0.22)
        self.title_t = min(1.0, self.title_t + 0.09)
        for key in set(self.hover_t) | ({self.hover} if self.hover else set()):
            self.hover_t[key] = _ease(self.hover_t.get(key, 0.0), 1.0 if key == self.hover else 0.0, 0.25)

        self._follow_size()
        if self.notch:
            self._place_notch()
        if time.time() - self._topmost_at > 1.0:
            self._topmost_at = time.time()
            _keep_on_top(int(self.winId()))
        # Discreet when nothing happens, fully visible when active or hovered (the notch is always solid).
        active = (self.phase not in ("idle", "offline", "muted") or self._hovering or self.expanded
                  or self.notch or self._question())
        want = 1.0 if active else 0.62
        self._opacity += (want - self._opacity) * 0.12
        if abs(self.windowOpacity() - self._opacity) > 0.004:
            self.setWindowOpacity(self._opacity)
        # Battery: 30 fps only while something moves. At rest, no redraws and a slower status poll.
        still = self._is_still(want)
        self.poller.interval = 0.6 if still else 0.12
        if self.frame.interval() != (250 if still else 33):
            self.frame.setInterval(250 if still else 33)
        if still and self._painted_still:
            return
        self._painted_still = still
        self.update()

    def _is_still(self, opacity_target: float) -> bool:
        """Nothing is animating or easing: the last frame drawn is still right."""
        if self.phase not in ("idle", "offline", "muted") or self._hovering or self.pressed or self._dragged:
            return False
        if self._question() or self.flash > 0 or self.title_t < 1 or time.time() - self._copied_at < 1.3:
            return False
        settled = [
            abs(self.energy - 0.12) < 0.01,
            abs(self._opacity - opacity_target) < 0.01,
            abs(self._h - self._target_size()[1]) < 0.5,
            all(v < 0.02 or v > 0.98 for v in self.hover_t.values()),
            self.speed_pos is None or abs(self.speed_pos - self._speed_index()) < 0.01,
            sum(abs(a - b) for a, b in zip(self.accent.getRgb(), self._accent_target.getRgb())) < 4,
            sum(abs(a - b) for a, b in zip(self.second.getRgb(), self._second_target.getRgb())) < 4,
        ]
        if self.notch:
            settled.append(abs(self._reveal - round(self._reveal)) < 0.003 and abs(self._reveal_v) < 0.003)
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
        return ("reply", ICONS["mic"], f"Talk now ({_hotkey_label(hk.get('reply', ''))})")

    def _tiles(self) -> list[tuple[str, str, str, str, bool]]:
        """(id, icon, label, hotkey, active)"""
        hk, st = self.hotkeys, self.st
        muted = bool(st.get("muted"))
        return [
            ("reply", ICONS["chat"], "Reply now", hk.get("reply", ""), False),
            ("hold", ICONS["mic"], "Hold to talk", hk.get("push_to_talk", ""), self.pressed == "hold"),
            ("stop", ICONS["stop"], "Stop", hk.get("stop", ""), False),
            ("handsfree", ICONS["send"], "Auto-send", hk.get("hands_free", ""), bool(st.get("hands_free"))),
            ("mute", ICONS["mute"] if muted else ICONS["volume"], "Muted" if muted else "Mute", hk.get("mute", ""), muted),
            ("read", ICONS["read"], "Read selection", hk.get("read_selection", ""), False),
            ("wake", ICONS["bolt"], "Hey Claude", "wake word", bool(st.get("wake"))),
            ("approve", ICONS["shield"], "Voice approvals", "yes / no", bool(st.get("approvals", True))),
            ("summary", ICONS["list"], "Summaries" if st.get("summary", True) else "Full answers",
             "short / full", bool(st.get("summary", True))),
        ]

    def _rects(self) -> dict[str, QRectF]:
        r = self._bar_rects()
        if not self.notch:
            return r
        # Panels keep their bar layout, centred under the notch row.
        dx, dy = (NOTCH_W - WIDTH) / 2, NOTCH_H - BAR_H
        out = {k: v.translated(dx, dy) for k, v in r.items() if k not in ("primary", "chevron")}
        if "primary" in r:
            out["primary"] = QRectF(NOTCH_W - 66, (NOTCH_H - 28) / 2, 28, 28)
        out["chevron"] = QRectF(NOTCH_W - 34, (NOTCH_H - 22) / 2, 22, 22)
        return out

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
            pad, gap, th = 12, 8, 58
            tw = (WIDTH - 2 * pad - 2 * gap) / 3
            for i, tile in enumerate(self._tiles()):
                row, col = divmod(i, 3)
                r[tile[0]] = QRectF(pad + col * (tw + gap), BAR_H + 10 + row * (th + gap), tw, th)
            track = self._speed_track()
            seg = track.width() / len(SPEEDS)
            for i, x in enumerate(SPEEDS):
                r[f"speed:{x}"] = QRectF(track.x() + i * seg, track.y(), seg, track.height())
            track = self._mode_track()
            seg = track.width() / len(MODES)
            for i, (key, _label) in enumerate(MODES):
                r[f"mode:{key}"] = QRectF(track.x() + i * seg, track.y(), seg, track.height())
            for i in range(len(self._threads())):
                r[f"thr:{i}"] = self._thread_row(i)
            for i in range(len(self._history())):
                r[f"hist:{i}"] = self._history_row(i)
        return r

    def _threads(self) -> list[dict]:
        return list(self.st.get("threads") or [])[:THREAD_ROWS]

    def _thread_row(self, i: int) -> QRectF:
        return QRectF(10, THREADS_Y + 16 + i * ROW_H, WIDTH - 20, ROW_H - 2)

    def _hist_y(self) -> float:
        n = len(self._threads())
        return THREADS_Y + (16 + n * ROW_H + 8 if n else 0)

    def _expanded_h(self) -> int:
        """The open panel fits what it shows: no empty rows."""
        return int(self._hist_y() + 16 + max(1, len(self._history())) * ROW_H + 8)

    def _question_layout(self) -> tuple[QRectF, QRectF, list[QRectF], float]:
        """(header line, question text, option rows, panel height) for the pending question."""
        q = self._question() or {"question": "", "options": []}
        fm = QFontMetrics(self.f_q)
        text_h = fm.boundingRect(0, 0, WIDTH - 28, 400, Qt.TextWordWrap, q["question"]).height()
        text_h = min(text_h, 3 * fm.lineSpacing())
        y = BAR_H + 10
        head = QRectF(14, y, WIDTH - 28, 15)
        y += 21
        text = QRectF(14, y, WIDTH - 28, text_h)
        y += text_h + 8
        rows = []
        for _ in q["options"][:OPTION_ROWS]:
            rows.append(QRectF(10, y, WIDTH - 20, 28))
            y += 32
        return head, text, rows, y + 20 - BAR_H

    def _history(self) -> list[dict]:
        return list(self.st.get("history") or [])[:HIST_ROWS]

    def _history_row(self, i: int) -> QRectF:
        return QRectF(10, self._hist_y() + 16 + i * ROW_H, WIDTH - 20, ROW_H - 2)

    @staticmethod
    def _speed_track() -> QRectF:
        return QRectF(70, SPEED_Y, WIDTH - 70 - 12, 26)

    @staticmethod
    def _mode_track() -> QRectF:
        return QRectF(70, MODE_Y, WIDTH - 70 - 12, 26)

    def _speed_index(self) -> float:
        x = float(self.st.get("speed_x", 1.5) or 1.5)
        return float(min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - x)))

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
        s = self._scale()
        if s != 1.0:
            p.scale(s, s)
        w, h = self.width() / s, self.height() / s
        body = QRectF(0.5, 0.5, w - 1, h - 1)
        path = QPainterPath()
        if self.notch:
            # Square top against the screen edge, rounded bottom: a camera notch.
            path.addRoundedRect(body, NOTCH_RADIUS, NOTCH_RADIUS)
            top = QPainterPath()
            top.addRect(QRectF(0.5, 0.5, w - 1, NOTCH_RADIUS))
            path = path.united(top)
        else:
            path.addRoundedRect(body, RADIUS, RADIUS)
        p.setClipPath(path)

        # Glass body: tinted fill (denser when there is no real blur behind it); the notch is near-black.
        p.fillPath(path, QColor(5, 6, 9, 250) if self.notch else QColor(14, 16, 24, 120 if self.glass else 228))
        if self._is_compact():
            self._paint_compact(p, path)
        elif self.notch:
            self._paint_island(p, path, w, h)
        else:
            self._paint_full(p, path, h)

        # Hairline border, brighter at the top like light catching glass.
        p.setClipping(False)
        edge = QLinearGradient(0, 0, 0, h)
        edge.setColorAt(0, QColor(255, 255, 255, 70))
        edge.setColorAt(0.5, QColor(255, 255, 255, 26))
        edge.setColorAt(1, QColor(255, 255, 255, 40))
        p.setPen(QPen(edge, 1))
        p.setBrush(Qt.NoBrush)
        if self.notch:
            p.drawPath(path)
        else:
            p.drawRoundedRect(body, RADIUS, RADIUS)
        p.end()

    def _paint_island(self, p: QPainter, path: QPainterPath, w: float, h: float) -> None:
        """Notch: the content stays centred while the island widens, and fades in once there is room."""
        show = _smooth((self._reveal - 0.55) / 0.4)
        off = (w - NOTCH_W) / 2
        if show > 0.01:
            p.save()
            p.translate(off, 0)
            p.setOpacity(show)
            self._paint_notch_row(p)
            if h > NOTCH_H + 4:
                p.translate((NOTCH_W - WIDTH) / 2, NOTCH_H - BAR_H)
                self._paint_panel(p)
            p.restore()
        # A soft light along the bottom edge, in the accent colour, that breathes with the voice.
        glow = QLinearGradient(0, 0, w, 0)
        c = QColor(self.accent)
        c.setAlpha(0)
        glow.setColorAt(0, c)
        c.setAlpha(int(40 + 150 * self.energy * max(show, 0.35)))
        glow.setColorAt(0.5, c)
        c.setAlpha(0)
        glow.setColorAt(1, c)
        p.fillRect(QRectF(NOTCH_RADIUS, h - 2.2, w - 2 * NOTCH_RADIUS, 1.6), glow)

    def _paint_notch_row(self, p: QPainter) -> None:
        """One dense row: orb, status + conversation, live caption, info chips, buttons."""
        cy = NOTCH_H / 2
        st = self.st
        amb = QRadialGradient(QPointF(22, cy), 110)
        c = QColor(self.accent)
        c.setAlpha(60 if self.phase not in ("idle", "offline") else 20)
        amb.setColorAt(0, c)
        c.setAlpha(0)
        amb.setColorAt(1, c)
        p.fillRect(QRectF(0, 0, NOTCH_W, NOTCH_H + 160), amb)  # past the row: no hard edge above a panel
        self._paint_wave(p, x0=44.0, x1=NOTCH_W - 80.0, base=NOTCH_H - 4, scale=0.55)
        self._paint_orb(p, QPointF(22, cy), 8.5)

        # Info chips, right to left, before the buttons.
        chips = []
        if st.get("permission"):
            chips.append("APPROVE?")
        elif self._question():
            chips.append("CHOOSE")
        if st.get("queued"):
            chips.append(f"+{st['queued']} WAITING")
        if st.get("update"):
            chips.append("UPDATE")
        lang = st.get("speaking_lang") if self.phase == "speaking" else ""
        if lang:
            chips.append(lang.upper())
        if st.get("chime"):
            chips.append("CHIME")
        elif st.get("hands_free"):
            chips.append("AUTO")
        if st.get("speed_x"):
            chips.append(f"{float(st['speed_x']):g}×")
        fm_chip = QFontMetrics(self.f_chip)
        right = NOTCH_W - 74
        for text in chips:
            cw = fm_chip.horizontalAdvance(text) + 10
            right -= cw
            self._chip(p, right, cy - 7.5, text)
            right -= 5

        # Status and conversation on the first line, caption (or hint) on the second.
        x, avail = 40.0, right - 40.0 - 6
        _, title, sub = PHASES.get(self.phase, PHASES["idle"])
        if self._question() and self.phase in ("idle", "waiting", "recording"):
            title = "Your choice"
        p.setFont(self.f_notch_title)
        fm = QFontMetrics(self.f_notch_title)
        p.setPen(QColor(255, 255, 255, 240))
        p.drawText(QPointF(x, 19), fm.elidedText(title, Qt.ElideRight, int(avail)))
        tx = x + min(fm.horizontalAdvance(title), avail) + 8
        thread = st.get("thread")
        if thread and tx < x + avail - 30:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(thread["color"]))
            p.drawEllipse(QPointF(tx + 3, 15), 3, 3)
            p.setFont(self.f_notch_sub)
            p.setPen(QColor(thread["color"]).lighter(125))
            p.drawText(QPointF(tx + 10, 19), QFontMetrics(self.f_notch_sub).elidedText(
                thread["title"], Qt.ElideRight, int(x + avail - tx - 10)))
        line = st.get("caption") or ""
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
        p.setFont(self.f_hint)
        p.setPen(QColor(226, 232, 240, 150))
        p.drawText(QRectF(x, 23, avail, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(self.f_hint).elidedText(line, Qt.ElideRight, int(avail)))
        self._paint_buttons(p)

    def _paint_compact(self, p: QPainter, path: QPainterPath) -> None:
        """Compact mode: only the orb, centred, with its glow and phase animation."""
        c = QColor(self.accent)
        c.setAlpha(80 if self.phase not in ("idle", "offline") else 34)
        amb = QRadialGradient(QPointF(COMPACT / 2, COMPACT / 2), COMPACT * 0.7)
        amb.setColorAt(0, c)
        c2 = QColor(self.accent)
        c2.setAlpha(0)
        amb.setColorAt(1, c2)
        p.fillPath(path, amb)
        ht = self.hover_t.get("primary", 0.0)
        if ht > 0.01:
            p.fillPath(path, QColor(255, 255, 255, int(16 * ht)))
        p.save()
        p.translate(COMPACT / 2 - 34, COMPACT / 2 - BAR_H / 2)  # the orb is drawn around (34, BAR_H / 2)
        self._paint_orb(p)
        p.restore()

    def _paint_full(self, p: QPainter, path: QPainterPath, h: int) -> None:
        # Phase-coloured ambience glowing from behind the orb.
        amb = QRadialGradient(QPointF(34, BAR_H / 2), 150)
        c = QColor(self.accent)
        c.setAlpha(70 if self.phase not in ("idle", "offline") else 28)
        amb.setColorAt(0, c)
        c2 = QColor(self.accent)
        c2.setAlpha(0)
        amb.setColorAt(1, c2)
        p.fillPath(path, amb)
        self._paint_aurora(p, path)
        self._paint_wave(p)
        if self.flash > 0:
            ring = QColor(self.accent)
            ring.setAlpha(int(110 * self.flash))
            rad = 20 + (1 - self.flash) * 300
            p.setPen(QPen(ring, 2.0 * self.flash + 0.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(34, BAR_H / 2), rad, rad)
        # Soft top sheen.
        sheen = QLinearGradient(0, 0, 0, BAR_H)
        sheen.setColorAt(0, QColor(255, 255, 255, 22))
        sheen.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillPath(path, sheen)

        self._paint_orb(p)
        self._paint_text(p)
        self._paint_thread_stripe(p)
        self._paint_buttons(p)
        if h > BAR_H + 4:
            self._paint_panel(p)

    def _paint_aurora(self, p: QPainter, path: QPainterPath) -> None:
        """Two soft colour blobs drifting slowly behind the glass; livelier when something happens."""
        t = time.time() - self.t0
        speed = 0.2 + 0.45 * self.energy
        blobs = [
            (self.accent, 90 + 70 * math.sin(t * speed * 0.9), 30 + 22 * math.cos(t * speed * 1.3), 120),
            (self.second, 230 + 60 * math.cos(t * speed * 0.7), 40 + 26 * math.sin(t * speed * 1.1), 110),
        ]
        for color, x, y, radius in blobs:
            g = QRadialGradient(QPointF(x, y), radius)
            c0 = QColor(color)
            c0.setAlpha(int(18 + 52 * self.energy))
            c1 = QColor(color)
            c1.setAlpha(0)
            g.setColorAt(0, c0)
            g.setColorAt(1, c1)
            p.fillPath(path, g)

    def _paint_wave(self, p: QPainter, x0: float = 62.0, x1: float = WIDTH - 16.0, base: float = BAR_H - 6,
                    scale: float = 1.0) -> None:
        """A live waveform along the bottom of the bar: the voice when speaking, your mic when listening."""
        t = time.time() - self.t0
        amp = (0.8 + 4.5 * self.energy) * scale
        if self.phase == "recording":
            amp = (1.2 + 7.5 * self.level) * scale
        n = 64
        wave = QPainterPath()
        for i in range(n + 1):
            u = i / n
            x = x0 + (x1 - x0) * u
            env = math.sin(math.pi * u) ** 1.5  # fade at both ends
            if self.phase == "transcribing":
                # A pulse travelling left to right.
                c = (t * 0.55) % 1.3 - 0.15
                y = math.exp(-((u - c) ** 2) / 0.006) * amp * 0.9
            else:
                y = (math.sin(u * 18 + t * 2.8) * 0.55 + math.sin(u * 31 - t * 4.2) * 0.3
                     + math.sin(u * 7 + t * 1.1) * 0.35) * amp * env * 0.5
            pt = QPointF(x, base - y)
            wave.moveTo(pt) if i == 0 else wave.lineTo(pt)
        grad = QLinearGradient(x0, 0, x1, 0)
        a = QColor(self.accent)
        b = QColor(self.second)
        a.setAlpha(0)
        b.setAlpha(0)
        mid_a = QColor(self.accent)
        mid_a.setAlpha(int(80 + 150 * self.energy))
        mid_b = QColor(self.second)
        mid_b.setAlpha(int(80 + 150 * self.energy))
        grad.setColorAt(0, a)
        grad.setColorAt(0.3, mid_a)
        grad.setColorAt(0.7, mid_b)
        grad.setColorAt(1, b)
        p.setPen(_round_pen(grad, 1.6))
        p.setBrush(Qt.NoBrush)
        p.drawPath(wave)

    def _paint_orb(self, p: QPainter, center: QPointF | None = None, base_r: float = 12.0) -> None:
        t = time.time() - self.t0
        center = center or QPointF(34, BAR_H / 2)
        phase = self.phase
        acc = QColor(self.accent)

        scale = 1.0
        if phase == "idle":
            scale = 1.0 + 0.03 * math.sin(t * 1.4)
        elif phase == "speaking":
            scale = 1.0 + 0.05 * math.sin(t * 3.2) * (0.6 + 0.4 * math.sin(t * 1.1))
        elif phase == "waiting":
            scale = 1.0 + 0.05 * math.sin(t * 1.8)
        elif phase == "recording":
            scale = 1.0 + 0.32 * self.level
        r = base_r * scale

        # Outer glow.
        glow = QRadialGradient(center, r * 2.3)
        g0 = QColor(acc)
        g0.setAlpha(110 if phase not in ("idle", "offline", "muted") else 45)
        g1 = QColor(acc)
        g1.setAlpha(0)
        glow.setColorAt(0.35, g0)
        glow.setColorAt(1, g1)
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(center, r * 2.3, r * 2.3)

        # Phase-specific motion.
        if phase == "speaking":
            for i in range(2):
                k = ((t * 0.4) + i * 0.5) % 1.0
                ring = QColor(acc)
                ring.setAlpha(int(150 * (1 - k)))
                p.setPen(QPen(ring, 1.4))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(center, r + 3 + k * 11, r + 3 + k * 11)
        elif phase == "waiting":
            k = (t * 0.35) % 1.0
            ring = QColor(acc)
            ring.setAlpha(int(120 * (1 - k)))
            p.setPen(QPen(ring, 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(center, r + 2 + k * 9, r + 2 + k * 9)
        elif phase == "recording":
            ring = QColor(acc)
            ring.setAlpha(int(90 + 140 * self.level))
            p.setPen(QPen(ring, 1.6 + 2.2 * self.level))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(center, r + 4, r + 4)
        elif phase == "transcribing":
            arc = QConicalGradient(center, -t * 360 * 0.6)
            a0 = QColor(acc)
            a0.setAlpha(255)
            a1 = QColor(acc)
            a1.setAlpha(0)
            arc.setColorAt(0, a0)
            arc.setColorAt(0.7, a1)
            arc.setColorAt(1, a0)
            p.setPen(_round_pen(arc, 2.2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(center, r + 4.5, r + 4.5)

        # Core sphere: lit from the top-left.
        core = QRadialGradient(QPointF(center.x() - r * 0.35, center.y() - r * 0.45), r * 1.6)
        core.setColorAt(0, acc.lighter(165))
        core.setColorAt(0.55, acc)
        core.setColorAt(1, acc.darker(170))
        p.setPen(Qt.NoPen)
        p.setBrush(core)
        p.drawEllipse(center, r, r)
        # Specular highlight.
        spec = QRadialGradient(QPointF(center.x() - r * 0.35, center.y() - r * 0.5), r * 0.6)
        spec.setColorAt(0, QColor(255, 255, 255, 150))
        spec.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(spec)
        p.drawEllipse(center, r, r)

        # Glyph inside the orb.
        glyph = {"speaking": "volume", "waiting": "mic", "recording": "mic", "muted": "mute",
                 "offline": "power", "transcribing": "chat"}.get(phase, "bell" if self.st.get("chime") else "mic")
        p.setFont(self.f_icon_small)
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r), Qt.AlignCenter, ICONS[glyph])

    def _paint_text(self, p: QPainter) -> None:
        _, title, sub = PHASES.get(self.phase, PHASES["idle"])
        st = self.st
        if self.phase == "idle":
            sub = "Auto-send on · replies are sent" if st.get("hands_free") else f"{_hotkey_label(self.hotkeys.get('reply', ''))} to talk"
            if st.get("chime"):
                sub = "Chime mode · sounds only, no voice"
        if self.phase == "speaking" and st.get("hands_free"):
            sub = "Skip to answer · auto-send on"
        if self._question() and self.phase in ("idle", "waiting", "recording"):
            title, sub = "Your choice", "Click an option or say it"
        x = 62
        right = WIDTH - 92
        p.setFont(self.f_title)
        fm = QFontMetrics(self.f_title)
        k = self.title_t
        if k < 1 and self.prev_title:
            p.setPen(QColor(255, 255, 255, int(240 * (1 - k))))
            p.drawText(QPointF(x, 28 - 6 * k), self.prev_title)
        p.setPen(QColor(255, 255, 255, int(240 * k)))
        p.drawText(QPointF(x, 28 + 6 * (1 - k)), title)
        chip_x = x + fm.horizontalAdvance(title) + 7

        lang = st.get("speaking_lang") if self.phase == "speaking" else ""
        if lang:
            self._chip(p, chip_x, 17, lang.upper())
        elif self.phase in ("waiting", "recording") and st.get("hands_free"):
            self._chip(p, chip_x, 17, "AUTO")

        p.setFont(self.f_sub)
        thread = st.get("thread")
        if thread:
            # Which conversation is talking (or waiting on you): its colour dot and title.
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(thread["color"]))
            p.drawEllipse(QPointF(x + 4, 43), 3.5, 3.5)
            x += 12
            sub = thread["title"]
            p.setPen(QColor(thread["color"]).lighter(130))
        else:
            p.setPen(QColor(226, 232, 240, 150))
        p.drawText(QRectF(x, 34, right - x, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(self.f_sub).elidedText(sub, Qt.ElideRight, int(right - x)))

    def _paint_thread_stripe(self, p: QPainter) -> None:
        """A thin bar in the conversation's colour along the left edge of the bar."""
        thread = self.st.get("thread")
        if not thread:
            return
        c = QColor(thread["color"])
        grad = QLinearGradient(0, 8, 0, BAR_H - 8)
        c.setAlpha(0)
        grad.setColorAt(0, c)
        c.setAlpha(220)
        grad.setColorAt(0.5, c)
        c.setAlpha(0)
        grad.setColorAt(1, c)
        p.fillRect(QRectF(0, 6, 3, BAR_H - 12), grad)

    def _chip(self, p: QPainter, x: float, y: float, text: str) -> None:
        fm = QFontMetrics(self.f_chip)
        rect = QRectF(x, y, fm.horizontalAdvance(text) + 10, 15)
        fill = QColor(self.accent)
        fill.setAlpha(60)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(fill)
        p.drawRoundedRect(rect, 7.5, 7.5)
        p.setFont(self.f_chip)
        p.setPen(QColor(255, 255, 255, 230))
        p.drawText(rect, Qt.AlignCenter, text)

    def _paint_buttons(self, p: QPainter) -> None:
        rects = self._rects()
        prim = self._primary()
        if prim:
            rect = rects["primary"]
            hot = self.hover == "primary"
            ht = self.hover_t.get("primary", 0.0)
            grow = 1.5 * ht + (1.2 * math.sin((time.time() - self.t0) * 2) if self.phase == "speaking" else 0)
            rect = rect.adjusted(-grow, -grow, grow, grow)
            fill = QColor(self.accent)
            fill.setAlpha(150 if hot else 95)
            if self.pressed == "primary":
                fill.setAlpha(200)
            p.setPen(QPen(QColor(255, 255, 255, 70 if hot else 45), 1))
            p.setBrush(fill)
            p.drawEllipse(rect)
            p.setFont(self.f_icon)
            p.setPen(QColor(255, 255, 255, 245))
            p.drawText(rect, Qt.AlignCenter, prim[1])

        rect = rects["chevron"]
        if self.hover == "chevron":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 26))
            p.drawEllipse(rect)
        p.setFont(self.f_icon_small)
        p.setPen(QColor(255, 255, 255, 170))
        p.drawText(rect, Qt.AlignCenter, ICONS["up" if self.expanded else "down"])

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
        p.drawText(QRectF(14, y, WIDTH - 28, 14), Qt.AlignLeft | Qt.AlignVCenter, hint)

    def _paint_panel(self, p: QPainter) -> None:
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.drawLine(QPointF(14, BAR_H), QPointF(WIDTH - 14, BAR_H))
        if self._question():
            self._paint_question(p)
            return
        rects = self._bar_rects()  # drawn in bar coordinates, also under the notch
        progress = self._panel_h() / max(1, self._expanded_h() - BAR_H)
        for i, (tid, icon, label, hk, active) in enumerate(self._tiles()):
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
            hot = ht > 0.5
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

            p.setFont(self.f_icon)
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(QRectF(rect.x(), rect.y() + 6, rect.width(), 20), Qt.AlignCenter, icon)
            p.setFont(self.f_tile)
            p.setPen(QColor(255, 255, 255, 220))
            p.drawText(QRectF(rect.x(), rect.y() + 27, rect.width(), 14), Qt.AlignCenter, label)
            p.setFont(self.f_hint)
            p.setPen(QColor(226, 232, 240, 105))
            p.drawText(QRectF(rect.x(), rect.y() + 41, rect.width(), 12), Qt.AlignCenter, _hotkey_label(hk))
            p.restore()

        # Speed and mode switches, then what you said recently (so you can trust or reuse it).
        speed_pos = self.speed_pos if self.speed_pos is not None else self._speed_index()
        self._paint_segmented(p, progress, 6, self._speed_track(), "Speed",
                              [f"speed:{x}" for x in SPEEDS], [f"{x:g}×" for x in SPEEDS], speed_pos)
        mode_pos = self.mode_pos if self.mode_pos is not None else (1.0 if self.st.get("chime") else 0.0)
        self._paint_segmented(p, progress, 7, self._mode_track(), "Mode",
                              [f"mode:{k}" for k, _ in MODES], [label for _, label in MODES], mode_pos)

        self._paint_threads(p, progress)
        self._paint_history(p, progress)

    def _paint_threads(self, p: QPainter, progress: float) -> None:
        """Recent conversations with what each is doing; click one to open it."""
        threads = self._threads()
        if not threads:
            return
        appear = max(0.0, min(1.0, (progress - 0.5) / 0.5))
        if appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        p.setFont(self.f_tile)
        p.setPen(QColor(226, 232, 240, 170))
        p.drawText(QRectF(14, THREADS_Y, WIDTH - 28, 16), Qt.AlignLeft | Qt.AlignVCenter, "Threads")
        p.setFont(self.f_hint)
        p.setPen(QColor(226, 232, 240, 105))
        queued = int(self.st.get("queued") or 0)
        p.drawText(QRectF(14, THREADS_Y, WIDTH - 28, 16), Qt.AlignRight | Qt.AlignVCenter,
                   f"{queued} waiting to speak" if queued else "click to open")
        labels = {"speaking": "speaking", "listening": "listening", "queued": "next up",
                  "question": "asks you", "permission": "needs approval"}
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
            p.drawEllipse(QPointF(row.x() + 9, row.center().y()), 3.5, 3.5)
            state = labels.get(t.get("state", ""), "")
            if not state:
                ago = int(t.get("ago", 0))
                state = "now" if ago < 60 else f"{ago // 60} min ago" if ago < 3600 else f"{ago // 3600} h ago"
            p.setFont(self.f_hint)
            p.setPen(color.lighter(130) if t.get("state") else QColor(226, 232, 240, 110))
            sw = QFontMetrics(self.f_hint).horizontalAdvance(state)
            p.drawText(QRectF(row.right() - sw - 6, row.y(), sw + 2, row.height()), Qt.AlignRight | Qt.AlignVCenter, state)
            text_rect = QRectF(row.x() + 20, row.y(), row.width() - sw - 32, row.height())
            p.setFont(self.f_sub)
            p.setPen(QColor(226, 232, 240, 215))
            p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(t["title"], Qt.ElideRight, int(text_rect.width())))
        p.restore()

    def _paint_history(self, p: QPainter, progress: float) -> None:
        """The last few things you said, newest first; click one to copy it."""
        appear = max(0.0, min(1.0, (progress - 0.55) / 0.45))
        if appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        dy = 10 * (1 - appear)
        hist_y = self._hist_y()
        p.setFont(self.f_tile)
        p.setPen(QColor(226, 232, 240, 170))
        p.drawText(QRectF(14, hist_y + dy, WIDTH - 28, 16), Qt.AlignLeft | Qt.AlignVCenter, "You said")
        p.setFont(self.f_hint)
        p.setPen(QColor(226, 232, 240, 105))
        p.drawText(QRectF(14, hist_y + dy, WIDTH - 28, 16), Qt.AlignRight | Qt.AlignVCenter, "click to copy")
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

    def leaveEvent(self, _e):
        self._hovering = False
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
            tip = self._tooltip(hit)
            if tip:
                QToolTip.showText(e.globalPosition().toPoint(), tip, self)
            else:
                QToolTip.hideText()

    def _tooltip(self, key: str | None) -> str:
        if key == "primary":
            prim = self._primary()
            return prim[2] if prim else ""
        if key == "chevron":
            return "Collapse" if self.expanded else "All actions"
        for tid, _icon, label, hk, _active in self._tiles():
            if tid == key:
                return f"{label} ({_hotkey_label(hk)})" if hk else label
        if key and key.startswith("speed:"):
            return f"Voice speed {float(key.split(':', 1)[1]):g}×"
        if key and key.startswith("hist:"):
            items = self._history()
            i = int(key.split(":", 1)[1])
            if i < len(items):
                text = items[i].get("text", "")
                return (text if len(text) <= 400 else text[:400] + "…") + "\n\nClick to copy"
        if key and key.startswith("thr:"):
            threads = self._threads()
            i = int(key.split(":", 1)[1])
            return f"Open “{threads[i]['title']}” in the app" if i < len(threads) else ""
        if key == "wake":
            return "Hey Claude: while idle, say “Hey Claude, …” to send a new prompt to your last thread"
        if key == "approve":
            return "Voice approvals: permission requests are read out; answer yes / no or click"
        if key == "summary":
            return "Final answers as a one-sentence summary, or read in full"
        if key and key.startswith("opt:"):
            q = self._question() or {"options": []}
            i = int(key.split(":", 1)[1])
            return q["options"][i] if i < len(q["options"]) else ""
        if key == "mode:voice":
            return "Voice: replies are read aloud and the mic opens"
        if key == "mode:chime":
            hk = _hotkey_label(self.hotkeys.get("chime", ""))
            return f"Chimes: no voice, a soft sound when done or when you're needed ({hk})"
        return ""

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            return
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
        if key == "chevron":
            self.toggle_expanded()
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
        elif key.startswith("speed:"):
            value = float(key.split(":", 1)[1])
            self.st["speed_x"] = value  # move the indicator now, don't wait for the next poll
            _post("/speed", {"value": value})
        elif key.startswith("hist:"):
            items = self._history()
            i = int(key.split(":", 1)[1])
            if i < len(items):
                QApplication.clipboard().setText(items[i].get("text", ""))
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
        elif key.startswith("thr:"):
            threads = self._threads()
            i = int(key.split(":", 1)[1])
            if i < len(threads):
                _post("/open_thread", {"session": threads[i]["session"]})
        elif key == "wake":
            _post("/wake", {"mode": "toggle"})
        elif key == "approve":
            _post("/approvals", {"mode": "toggle"})
        elif key == "summary":
            _post("/summary", {"mode": "toggle"})
        elif key.startswith("mode:"):
            on = key == "mode:chime"
            self.st["chime"] = on
            _post("/chime", {"mode": "on" if on else "off"})

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
        elif self._bar_pos:
            self.move(*self._bar_pos)
        self._apply_size()
        self.glass = _apply_windows_style(int(self.winId()), self.blur_mode, self.notch)
        self._save_position()

    def toggle_expanded(self) -> None:
        if self.compact:
            self.toggle_compact()
        self.expanded = not self.expanded  # _follow_size() glides to the new height
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
                ("Move to top right", lambda: self._place(geo.right() - WIDTH - 16, geo.top() + 64)),
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
            add(f"Update available: v{update['version']} (download)",
                lambda: QDesktopServices.openUrl(QUrl(update["url"])))
            m.addSeparator()
        add("Hide widget" if w.isVisible() else "Show widget", w.toggle_visible, key=hk.get("widget", ""))
        add("Compact widget (orb only)", w.toggle_compact, checked=w.compact)
        add("Notch (top of the screen)", w.toggle_notch, checked=w.notch)
        m.addSeparator()
        add("Reply now", lambda: _post("/reply"), online, key=hk.get("reply", ""))
        add("Stop", lambda: _post("/stop"), online, key=hk.get("stop", ""))
        add("Read selection", lambda: _post("/read"), online, key=hk.get("read_selection", ""))
        m.addSeparator()
        add("Auto-send replies", lambda: _post("/handsfree", {"mode": "toggle"}), online,
            bool(st.get("hands_free")), hk.get("hands_free", ""))
        add("Mute voice", lambda: _post("/mute", {"mode": "toggle"}), online, bool(st.get("muted")), hk.get("mute", ""))
        add("Chime mode (sounds only)", lambda: _post("/chime", {"mode": "toggle"}), online,
            bool(st.get("chime")), hk.get("chime", ""))
        add("Summarize answers", lambda: _post("/summary", {"mode": "toggle"}), online, bool(st.get("summary", True)))
        add("Approve permissions by voice", lambda: _post("/approvals", {"mode": "toggle"}), online,
            bool(st.get("approvals", True)))
        add("Wake word (Hey Claude)", lambda: _post("/wake", {"mode": "toggle"}), online, bool(st.get("wake")))
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
        if online:
            add("Stop voice service", lambda: _post("/shutdown"))
        else:
            add("Start voice service", lambda: threading.Thread(target=client.start_detached, daemon=True).start())
        add("Start with Windows", self._toggle_autostart, checked=_autostart_enabled())
        m.addSeparator()
        add("Quit widget", QApplication.quit)

    @staticmethod
    def _toggle_autostart() -> None:
        from agent_voice.install import autostart

        autostart(not _autostart_enabled())


def _autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    from agent_voice.install import _startup_file

    return _startup_file().exists()


def run(demo: str | None = None) -> int:
    from PySide6.QtCore import QLockFile

    app = QApplication(sys.argv)
    # Hiding the widget must not quit: the tray icon keeps running.
    app.setQuitOnLastWindowClosed(False)
    HOME.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(HOME / "widget.lock"))
    if not demo and not lock.tryLock(100):
        return 0  # already showing
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
