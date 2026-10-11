"""Remember and restore the window to type into (Windows only; no-ops elsewhere)."""

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
