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

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (
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
EXPANDED_H = 274
SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0)
SPEED_Y = BAR_H + 10 + 2 * 58 + 8 + 6  # top of the speed row
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


def _mix(a: QColor, b: QColor, k: float) -> QColor:
    return QColor(
        int(a.red() + (b.red() - a.red()) * k),
        int(a.green() + (b.green() - a.green()) * k),
        int(a.blue() + (b.blue() - a.blue()) * k),
    )


def _hotkey_label(combo: str) -> str:
    return "+".join(part.strip().capitalize() for part in combo.split("+")) if combo else ""


# -- Windows glass ---------------------------------------------------------------------
def _apply_windows_style(hwnd: int, blur: str) -> bool:
    """Acrylic blur behind the window, rounded corners, no focus stealing, no taskbar entry."""
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
    corner = ctypes.c_int(2)  # DWMWCP_ROUND
    dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(corner), ctypes.sizeof(corner))
    # Hide the grey system border; we draw our own hairline.
    no_border = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE
    dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34, ctypes.byref(no_border), ctypes.sizeof(no_border))

    if blur == "none":
        return False

    class AccentPolicy(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]

    class WinCompAttrData(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.POINTER(AccentPolicy)), ("SizeOfData", ctypes.c_size_t)]

    # 4 = acrylic, 3 = plain blur. Tint is AABBGGRR: a dark, slightly blue glass.
    state = 4 if blur == "acrylic" else 3
    accent = AccentPolicy(state, 2, 0x66201812, 0)
    data = WinCompAttrData(19, ctypes.pointer(accent), ctypes.sizeof(accent))  # WCA_ACCENT_POLICY
    try:
        return bool(user32.SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data)))
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
        self.prev_title = ""
        self.title_t = 1.0  # title crossfade progress
        self.expanded = False
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
            self.glass = _apply_windows_style(int(self.winId()), self.blur_mode)

    def _restore_position(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        try:
            pos = json.loads(POS_FILE.read_text(encoding="utf-8"))
            x, y = int(pos["x"]), int(pos["y"])
            self.expanded = bool(pos.get("expanded", False))
            self.start_hidden = bool(pos.get("hidden", False))
        except (OSError, ValueError, KeyError):
            x, y = screen.right() - WIDTH - 16, screen.top() + 64
        # Keep it on a visible screen (monitors may have changed since last time).
        if not any(s.availableGeometry().adjusted(-40, -40, 40, 40).contains(x, y) for s in QApplication.screens()):
            x, y = screen.right() - WIDTH - 16, screen.top() + 64
        self.move(x, y)
        self.resize(WIDTH, EXPANDED_H if self.expanded else BAR_H)

    def _save_position(self) -> None:
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            state = {"x": self.x(), "y": self.y(), "expanded": self.expanded, "hidden": not self.isVisible()}
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
        self.title_t = min(1.0, self.title_t + 0.09)
        for key in set(self.hover_t) | ({self.hover} if self.hover else set()):
            self.hover_t[key] = _ease(self.hover_t.get(key, 0.0), 1.0 if key == self.hover else 0.0, 0.25)

        # Discreet when nothing happens, fully visible when active or hovered.
        active = self.phase not in ("idle", "offline", "muted") or self._hovering or self.expanded
        want = 1.0 if active else 0.62
        self._opacity += (want - self._opacity) * 0.12
        if abs(self.windowOpacity() - self._opacity) > 0.004:
            self.setWindowOpacity(self._opacity)
        self.update()

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
        ]

    def _rects(self) -> dict[str, QRectF]:
        r: dict[str, QRectF] = {}
        if self._primary():
            r["primary"] = QRectF(WIDTH - 82, BAR_H / 2 - 17, 34, 34)
        r["chevron"] = QRectF(WIDTH - 40, BAR_H / 2 - 13, 26, 26)
        if self.height() > BAR_H + 40:
            pad, gap, th = 12, 8, 58
            tw = (WIDTH - 2 * pad - 2 * gap) / 3
            for i, tile in enumerate(self._tiles()):
                row, col = divmod(i, 3)
                r[tile[0]] = QRectF(pad + col * (tw + gap), BAR_H + 10 + row * (th + gap), tw, th)
            track = self._speed_track()
            seg = track.width() / len(SPEEDS)
            for i, x in enumerate(SPEEDS):
                r[f"speed:{x}"] = QRectF(track.x() + i * seg, track.y(), seg, track.height())
        return r

    @staticmethod
    def _speed_track() -> QRectF:
        return QRectF(70, SPEED_Y, WIDTH - 70 - 12, 26)

    def _speed_index(self) -> float:
        x = float(self.st.get("speed_x", 1.5) or 1.5)
        return float(min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - x)))

    def _hit(self, pos: QPointF) -> str | None:
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
        body = QRectF(0.5, 0.5, w - 1, h - 1)
        path = QPainterPath()
        path.addRoundedRect(body, RADIUS, RADIUS)
        p.setClipPath(path)

        # Glass body: tinted fill (denser when there is no real blur behind it).
        p.fillPath(path, QColor(14, 16, 24, 120 if self.glass else 228))
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
        self._paint_buttons(p)
        if h > BAR_H + 4:
            self._paint_panel(p)

        # Hairline border, brighter at the top like light catching glass.
        p.setClipping(False)
        edge = QLinearGradient(0, 0, 0, h)
        edge.setColorAt(0, QColor(255, 255, 255, 70))
        edge.setColorAt(0.5, QColor(255, 255, 255, 26))
        edge.setColorAt(1, QColor(255, 255, 255, 40))
        p.setPen(QPen(edge, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(body, RADIUS, RADIUS)
        p.end()

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

    def _paint_wave(self, p: QPainter) -> None:
        """A live waveform along the bottom of the bar: the voice when speaking, your mic when listening."""
        t = time.time() - self.t0
        amp = 0.8 + 4.5 * self.energy
        if self.phase == "recording":
            amp = 1.2 + 7.5 * self.level
        base = BAR_H - 6
        x0, x1 = 62.0, WIDTH - 16.0
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

    def _paint_orb(self, p: QPainter) -> None:
        t = time.time() - self.t0
        center = QPointF(34, BAR_H / 2)
        base_r = 12.0
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
                 "offline": "power", "transcribing": "chat"}.get(phase, "mic")
        p.setFont(self.f_icon_small)
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r), Qt.AlignCenter, ICONS[glyph])

    def _paint_text(self, p: QPainter) -> None:
        _, title, sub = PHASES.get(self.phase, PHASES["idle"])
        st = self.st
        if self.phase == "idle":
            sub = "Auto-send on · replies are sent" if st.get("hands_free") else f"{_hotkey_label(self.hotkeys.get('reply', ''))} to talk"
        if self.phase == "speaking" and st.get("hands_free"):
            sub = "Skip to answer · auto-send on"
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
        p.setPen(QColor(226, 232, 240, 150))
        p.drawText(QRectF(x, 34, right - x, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(self.f_sub).elidedText(sub, Qt.ElideRight, int(right - x)))

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

    def _paint_panel(self, p: QPainter) -> None:
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.drawLine(QPointF(14, BAR_H), QPointF(WIDTH - 14, BAR_H))
        rects = self._rects()
        progress = (self.height() - BAR_H) / (EXPANDED_H - BAR_H)
        for i, (tid, icon, label, hk, active) in enumerate(self._tiles()):
            rect = rects.get(tid)
            if rect is None:
                continue
            appear = max(0.0, min(1.0, (progress - i * 0.07) / 0.5))
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
                if tid == "handsfree":
                    fill = QColor("#34D399")
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

        # Footer: what was last heard, so you can trust the transcription at a glance.
        self._paint_speed(p, progress)

        y = SPEED_Y + 26 + 10
        heard = self.st.get("last_heard", "")
        p.setFont(self.f_sub)
        if heard:
            text = QFontMetrics(self.f_sub).elidedText(f"“{heard}”", Qt.ElideLeft, WIDTH - 64)
            p.setPen(QColor(226, 232, 240, 165))
            p.drawText(QRectF(14, y, WIDTH - 64, 20), Qt.AlignLeft | Qt.AlignVCenter, text)
            lang = self.st.get("last_heard_lang", "")
            if lang:
                self._chip(p, WIDTH - 44, y + 2.5, lang.upper())
        else:
            p.setPen(QColor(226, 232, 240, 95))
            p.drawText(QRectF(14, y, WIDTH - 28, 20), Qt.AlignLeft | Qt.AlignVCenter,
                       "Drag to move · right-click for options")

    def _paint_speed(self, p: QPainter, progress: float) -> None:
        """Segmented 1x..2x control with a sliding indicator."""
        appear = max(0.0, min(1.0, (progress - 6 * 0.07) / 0.5))
        if appear <= 0:
            return
        p.save()
        p.setOpacity(appear)
        track = self._speed_track().translated(0, 10 * (1 - appear))
        p.setFont(self.f_tile)
        p.setPen(QColor(226, 232, 240, 170))
        p.drawText(QRectF(14, track.y(), 56, track.height()), Qt.AlignLeft | Qt.AlignVCenter, "Speed")

        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(QColor(255, 255, 255, 12))
        p.drawRoundedRect(track, 8, 8)

        seg = track.width() / len(SPEEDS)
        pos = self.speed_pos if self.speed_pos is not None else self._speed_index()
        ind = QRectF(track.x() + pos * seg + 2, track.y() + 2, seg - 4, track.height() - 4)
        glow = QColor(self.accent)
        glow.setAlpha(110)
        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        p.setBrush(glow)
        p.drawRoundedRect(ind, 6, 6)

        active = round(pos)
        for i, x in enumerate(SPEEDS):
            rect = QRectF(track.x() + i * seg, track.y(), seg, track.height())
            ht = self.hover_t.get(f"speed:{x}", 0.0)
            if i != active and ht > 0.01:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, int(18 * ht)))
                p.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)
            p.setFont(self.f_chip if i == active else self.f_hint)
            p.setPen(QColor(255, 255, 255, 245 if i == active else int(150 + 60 * ht)))
            p.drawText(rect, Qt.AlignCenter, f"{x:g}×")
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
        return ""

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            return
        self._drag_from = e.globalPosition()
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

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
        self._save_position()
        if self.tray:
            self.tray.refresh(force=True)

    def toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self._height_anim.stop()
        self._height_anim.setStartValue(self.height())
        self._height_anim.setEndValue(EXPANDED_H if self.expanded else BAR_H)
        self._height_anim.start()
        self._save_position()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.setStyleSheet(MENU_STYLE)
        geo = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        actions = [
            ("Collapse" if self.expanded else "Show all actions", self.toggle_expanded),
            (None, None),
            ("Move to top right", lambda: self._place(geo.right() - WIDTH - 16, geo.top() + 64)),
            ("Move to top left", lambda: self._place(geo.left() + 16, geo.top() + 64)),
            (None, None),
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

        add("Hide widget" if w.isVisible() else "Show widget", w.toggle_visible, key=hk.get("widget", ""))
        m.addSeparator()
        add("Reply now", lambda: _post("/reply"), online, key=hk.get("reply", ""))
        add("Stop", lambda: _post("/stop"), online, key=hk.get("stop", ""))
        add("Read selection", lambda: _post("/read"), online, key=hk.get("read_selection", ""))
        m.addSeparator()
        add("Auto-send replies", lambda: _post("/handsfree", {"mode": "toggle"}), online,
            bool(st.get("hands_free")), hk.get("hands_free", ""))
        add("Mute voice", lambda: _post("/mute", {"mode": "toggle"}), online, bool(st.get("muted")), hk.get("mute", ""))
        add("Summarize answers", lambda: _post("/summary", {"mode": "toggle"}), online, bool(st.get("summary", True)))
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
