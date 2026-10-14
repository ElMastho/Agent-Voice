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

    def idle_seconds() -> float:
        return 0.0

    def console_window() -> int:
        return 0

    def app_window(pid: int) -> int:  # noqa: ARG001
        return 0
