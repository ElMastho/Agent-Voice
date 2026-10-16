"""Remember and restore the window to type into: Win32 on Windows, System Events on macOS, no-ops elsewhere."""

from __future__ import annotations

import sys
import time

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def foreground() -> int:
        return int(_user32.GetForegroundWindow() or 0)

    def title(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value

    def is_window(hwnd: int) -> bool:
        return bool(hwnd and _user32.IsWindow(hwnd))

    def is_minimized(hwnd: int) -> bool:
        return bool(hwnd and _user32.IsIconic(hwnd))

    def minimize(hwnd: int) -> None:
        if hwnd and _user32.IsWindow(hwnd):
            _user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE

    def focus(hwnd: int, timeout: float = 1.5) -> bool:
        """Bring hwnd to the front, restoring it if minimized or hidden to the tray.

        Returns True only if it is the foreground window afterwards.
        """
        if not hwnd or not _user32.IsWindow(hwnd):
            return False
        if foreground() == hwnd:
            return True
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        elif not _user32.IsWindowVisible(hwnd):
            _user32.ShowWindow(hwnd, 5)  # SW_SHOW
        deadline = time.time() + timeout
        attempt = 0
        while True:
            _bring_forward(hwnd, attach=attempt > 0)
            time.sleep(0.05)
            if foreground() == hwnd:
                time.sleep(0.15)  # let the restore animation finish and the input regain its caret
                return foreground() == hwnd
            if time.time() >= deadline:
                return False
            attempt += 1
            time.sleep(0.1)

    class _LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInput)]
    _kernel32.GetTickCount.restype = wintypes.DWORD
    _kernel32.GetConsoleWindow.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]

    def idle_seconds() -> float:
        """Seconds since the last keyboard or mouse input, anywhere."""
        info = _LastInput(ctypes.sizeof(_LastInput), 0)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0

    def console_window() -> int:
        """The terminal window this process runs in (Windows Terminal hosts a hidden console it owns)."""
        hwnd = _kernel32.GetConsoleWindow()
        if not hwnd:
            return 0
        root = _user32.GetAncestor(hwnd, 3)  # GA_ROOTOWNER: Windows Terminal's window, not its pseudo console
        return int(root or hwnd) if _user32.IsWindowVisible(root or hwnd) else 0

    def _parents() -> dict[int, int]:
        """pid -> parent pid, for every running process."""
        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        out: dict[int, int] = {}
        if not snap or snap == wintypes.HANDLE(-1).value:
            return out
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = _kernel32.Process32FirstW(wintypes.HANDLE(snap), ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = _kernel32.Process32NextW(wintypes.HANDLE(snap), ctypes.byref(entry))
        _kernel32.CloseHandle(wintypes.HANDLE(snap))
        return out

    def app_window(pid: int) -> int:
        """The app window behind a helper process (an MCP server, a browser's native host).

        Walks up the parent processes until one owns a visible, titled top-level window; the one in front
        wins when the app has several.
        """
        parents = _parents()
        chain, p = [], parents.get(pid, 0)
        while p and p not in chain and len(chain) < 12:
            chain.append(p)
            p = parents.get(p, 0)
        windows: dict[int, list[int]] = {}

        def collect(hwnd, _lparam):
            if _user32.IsWindowVisible(hwnd) and not _user32.GetWindow(hwnd, 4) and _user32.GetWindowTextLengthW(hwnd):
                owner = wintypes.DWORD()
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                windows.setdefault(int(owner.value), []).append(int(hwnd))
            return True

        _user32.EnumWindows(_EnumProc(collect), 0)
        front = foreground()
        for p in chain:
            if p in windows:
                return front if front in windows[p] else windows[p][0]
        return 0

    def find_window(text: str) -> int:
        """A visible top-level window whose title contains `text` (case-insensitive), 0 if none."""
        found: list[int] = []
        want = text.lower()

        def check(hwnd, _lparam):
            if _user32.IsWindowVisible(hwnd) and not _user32.GetWindow(hwnd, 4) and want in title(hwnd).lower():
                found.append(int(hwnd))
                return False
            return True

        _user32.EnumWindows(_EnumProc(check), 0)
        return found[0] if found else 0

    def _bring_forward(hwnd: int, attach: bool) -> None:
        # Windows only lets the process that received the last input steal focus;
        # a synthetic Alt press satisfies that rule.
        _user32.keybd_event(0x12, 0, 0, None)
        _user32.keybd_event(0x12, 0, 2, None)
        if not attach:
            _user32.SetForegroundWindow(hwnd)
            return
        # Fallback: share input state with the current foreground thread, which may move focus.
        me = _kernel32.GetCurrentThreadId()
        fg = _user32.GetWindowThreadProcessId(foreground(), None)
        attached = bool(fg and fg != me and _user32.AttachThreadInput(me, fg, True))
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(me, fg, False)

elif sys.platform == "darwin":
    # macOS: a "window" is the app's process id (System Events brings apps forward, not windows).
    import os
    import re
    import subprocess

    def _osa(script: str) -> str:
        try:
            out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return out.stdout.strip()

    def foreground() -> int:
        pid = _osa('tell application "System Events" to get unix id of first process whose frontmost is true')
        return int(pid) if pid.isdigit() else 0

    def title(hwnd: int) -> str:
        return _osa(f'tell application "System Events" to get name of first process whose unix id is {int(hwnd)}')

    def is_window(hwnd: int) -> bool:
        if not hwnd:
            return False
        try:
            os.kill(hwnd, 0)
        except OSError:
            return False
        return True

    def is_minimized(hwnd: int) -> bool:  # noqa: ARG001
        return False

    def minimize(hwnd: int) -> None:  # noqa: ARG001
        pass

    def focus(hwnd: int, timeout: float = 1.5) -> bool:
        if not is_window(hwnd):
            return False
        _osa(f'tell application "System Events" to set frontmost of (first process whose unix id is {int(hwnd)}) to true')
        deadline = time.time() + timeout
        while time.time() < deadline:
            if foreground() == hwnd:
                return True
            time.sleep(0.1)
        return False

    def idle_seconds() -> float:
        try:
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return 0.0
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        return int(m.group(1)) / 1e9 if m else 0.0

    def app_window(pid: int) -> int:
        """The app (Terminal, iTerm, VS Code...) a helper process runs under: its first GUI ancestor."""
        gui = set(_osa('tell application "System Events" to get unix id of every process whose background only '
                       'is false').replace(" ", "").split(","))
        p = pid
        for _ in range(12):
            try:
                p = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(p)], capture_output=True, text=True,
                                       timeout=2).stdout.strip() or 0)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                return 0
            if p <= 1:
                return 0
            if str(p) in gui:
                return p
        return 0

    def console_window() -> int:
        return app_window(os.getpid())

    def find_window(text: str) -> int:
        pid = _osa('tell application "System Events" to get unix id of first process whose name contains '
                   f'"{text.replace(chr(34), "")}"')
        return int(pid) if pid.isdigit() else 0

else:

    def foreground() -> int:
        return 0

    def title(hwnd: int) -> str:  # noqa: ARG001
        return ""

    def is_window(hwnd: int) -> bool:  # noqa: ARG001
        return False

    def is_minimized(hwnd: int) -> bool:  # noqa: ARG001
        return False

    def minimize(hwnd: int) -> None:  # noqa: ARG001
        pass

    def focus(hwnd: int, timeout: float = 1.5) -> bool:  # noqa: ARG001
        return False

    def idle_seconds() -> float:
        return 0.0

    def console_window() -> int:
        return 0

    def app_window(pid: int) -> int:  # noqa: ARG001
        return 0

    def find_window(text: str) -> int:  # noqa: ARG001
        return 0


def busy_state() -> str:
    """'presenting' or 'full screen' while Windows itself holds its notifications back; '' otherwise."""
    if sys.platform != "win32":
        return ""
    state = ctypes.c_int(0)
    try:
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return ""
    except (AttributeError, OSError):
        return ""
    # QUNS_BUSY (a full-screen app), QUNS_RUNNING_D3D_FULL_SCREEN (a game), QUNS_PRESENTATION_MODE.
    return {2: "full screen", 3: "full screen", 4: "presenting"}.get(state.value, "")


def dib_to_png(dib: bytes) -> bytes:
    """A clipboard bitmap (CF_DIB: BITMAPINFOHEADER + pixels, 24 or 32 bits) as a PNG file, with the stdlib only."""
    import struct
    import zlib

    size, width, height, _planes, bits, compression = struct.unpack_from("<IiiHHI", dib, 0)
    if bits not in (24, 32) or compression not in (0, 3) or width <= 0 or height == 0:
        return b""
    offset = size + (12 if compression == 3 and size == 40 else 0)  # BI_BITFIELDS: three masks follow
    stride = (width * bits // 8 + 3) & ~3
    rows = range(abs(height) - 1, -1, -1) if height > 0 else range(abs(height))  # bottom-up unless negative
    step = bits // 8
    raw = bytearray()
    rgb = bytearray(width * 3)
    for y in rows:
        line = dib[offset + y * stride: offset + y * stride + width * step]
        if len(line) != width * step:
            return b""  # cut short
        rgb[0::3], rgb[1::3], rgb[2::3] = line[2::step], line[1::step], line[0::step]  # BGR(A) -> RGB, a row at once
        raw.append(0)
        raw += rgb

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, abs(height), 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


def clipboard_png() -> bytes:
    """The image on the clipboard (a screenshot) as PNG bytes; b'' when it holds none."""
    if sys.platform == "darwin":
        import re
        import subprocess

        out = subprocess.run(["osascript", "-e", "the clipboard as «class PNGf»"], capture_output=True, text=True,
                             timeout=5).stdout
        m = re.search(r"«data PNGf([0-9A-Fa-f]+)»", out)
        return bytes.fromhex(m.group(1)) if m else b""
    if sys.platform != "win32":
        return b""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    CF_DIB = 8
    if not user32.IsClipboardFormatAvailable(CF_DIB) or not user32.OpenClipboard(None):
        return b""
    try:
        handle = user32.GetClipboardData(CF_DIB)
        ptr = kernel32.GlobalLock(handle) if handle else None
        if not ptr:
            return b""
        try:
            data = ctypes.string_at(ptr, kernel32.GlobalSize(handle))
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    return dib_to_png(data)


def window_png(hwnd: int) -> bytes:
    """A picture of a window as it is on screen right now (what you're looking at), as PNG; b'' if it can't be had.

    Copied from the screen rather than asked of the window: browsers and GPU-drawn apps answer that with black.
    """
    if sys.platform != "win32" or not hwnd:
        return b""
    import struct

    user32, gdi32, dwm = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32"), ctypes.WinDLL("dwmapi")
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    # Real pixels on a scaled display (the daemon itself isn't DPI-aware): for this thread, for this capture.
    before = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    try:
        rect = wintypes.RECT()
        # The visible frame, without the invisible resize borders around it.
        if dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0 or width * height > 50_000_000:
            return b""
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        old = gdi32.SelectObject(mem, bitmap)
        try:
            gdi32.BitBlt(mem, 0, 0, width, height, screen, rect.left, rect.top, 0x00CC0020 | 0x40000000)  # SRCCOPY
            header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down, 32 bits
            info = ctypes.create_string_buffer(header, 40 + 12)
            pixels = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(mem, bitmap, 0, height, pixels, info, 0):
                return b""
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
    finally:
        if before:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(before))
    return dib_to_png(header + pixels.raw)
