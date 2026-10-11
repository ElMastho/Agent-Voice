"""Remember and restore the window to type into (Windows only; no-ops elsewhere)."""

from __future__ import annotations

import sys

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

    def foreground() -> int:
        return int(_user32.GetForegroundWindow() or 0)

    def title(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value

    def focus(hwnd: int) -> bool:
        """Bring hwnd to the front. Returns True if it is the foreground window afterwards."""
        if not hwnd or not _user32.IsWindow(hwnd):
            return False
        if foreground() == hwnd:
            return True
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # Windows only lets the process that received the last input steal focus;
        # a synthetic Alt press satisfies that rule.
        _user32.keybd_event(0x12, 0, 0, None)
        _user32.keybd_event(0x12, 0, 2, None)
        _user32.SetForegroundWindow(hwnd)
        return foreground() == hwnd

else:

    def foreground() -> int:
        return 0

    def title(hwnd: int) -> str:  # noqa: ARG001
        return ""

    def focus(hwnd: int) -> bool:  # noqa: ARG001
        return False
