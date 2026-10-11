"""Claude desktop app: type a reply into the right conversation, not just the right window.

The desktop app shows many Claude Code sessions in one window. Hooks know the CLI session id
of each turn; we map it to the conversation title (the app's session files when readable, else
the title on screen when you send a prompt), and UI Automation lets us click that sidebar entry
and focus its prompt box (Windows only).

Microsoft Store Python cannot see %APPDATA%/Claude (MSIX file virtualization), hence the
on-screen fallback.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("agent_voice.claudeapp")

SESSIONS_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude-code-sessions"


def session_title(cli_session_id: str, root: Path = SESSIONS_DIR) -> str:
    """Sidebar title of the desktop session running this CLI session, or "" if it isn't one."""
    if not cli_session_id or not root.is_dir():
        return ""
    for path in root.glob("*/*/local_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("cliSessionId") == cli_session_id:
            return str(data.get("title") or "")
    return ""


PAGE_SUFFIX = " - Claude Code"


def title_from_page(document_name: str) -> str:
    """The page is named "<session title> - Claude Code"."""
    return document_name[: -len(PAGE_SUFFIX)] if document_name.endswith(PAGE_SUFFIX) else document_name


def is_current(document_name: str, title: str) -> bool:
    return bool(title) and title_from_page(document_name) == title


def pick_entry(names: list[str], title: str) -> int:
    """Index of the clickable entry that opens `title`, or -1.

    Sidebar entries read "<status> <title>", sometimes followed by a note ("<status> <title> Started
    by ..."). Only pass invokable buttons: "More options for <title>" isn't one. The header's
    "<title>, rename" button never matches (no space before the title).
    """
    for i, name in enumerate(names):
        if name == title or name.endswith(" " + title):
            return i
        # A trailing note is a sentence ("Started by ..."), unlike "<title> (fork)", another conversation.
        _, sep, note = name.partition(f" {title} ")
        if sep and note[:1].isupper() and note.endswith("."):
            return i
    return -1


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def is_desktop_app(hwnd: int) -> bool:
        """True if the window belongs to the Claude desktop app (claude.exe owns the window itself)."""
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return False
            return Path(buf.value).name.lower() == "claude.exe"
        finally:
            _kernel32.CloseHandle(handle)

    def _uia():
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        return UIA, comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)

    def _find(uia, UIA, root, control_type: int) -> list:
        cond = uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, control_type)
        found = root.FindAll(UIA.TreeScope_Descendants, cond)
        return [found.GetElement(i) for i in range(found.Length)]

    def _document_name(uia, UIA, root) -> str:
        docs = _find(uia, UIA, root, UIA.UIA_DocumentControlTypeId)
        return docs[0].CurrentName if docs else ""

    def current_title(hwnd: int, timeout: float = 2.0) -> str:
        """Title of the conversation shown in the desktop app right now ("" if unknown)."""
        try:
            UIA, uia = _uia()
            root = uia.ElementFromHandle(hwnd)
            deadline = time.time() + timeout
            while True:
                name = _document_name(uia, UIA, root)
                if name or time.time() >= deadline:
                    return title_from_page(name)
                time.sleep(0.2)  # accessibility tree still being built
        except Exception:
            log.exception("could not read the current conversation")
            return ""

    def select_session(hwnd: int, title: str, timeout: float = 3.0) -> bool:
        """Open the sidebar session `title` and put the caret in its prompt box. True when done."""
        try:
            UIA, uia = _uia()
            root = uia.ElementFromHandle(hwnd)
            deadline = time.time() + timeout
            # Chromium builds its accessibility tree lazily, on the first queries: retry briefly.
            while True:
                if is_current(_document_name(uia, UIA, root), title):
                    break
                buttons = [b for b in _find(uia, UIA, root, UIA.UIA_ButtonControlTypeId)
                           if b.GetCurrentPattern(UIA.UIA_InvokePatternId)]
                i = pick_entry([b.CurrentName or "" for b in buttons], title)
                if i >= 0:
                    pattern = buttons[i].GetCurrentPattern(UIA.UIA_InvokePatternId)
                    pattern.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
                    while time.time() < deadline and not is_current(_document_name(uia, UIA, root), title):
                        time.sleep(0.05)
                    break
                if time.time() >= deadline:
                    log.warning("session %r not found in the sidebar (collapsed group or hidden sidebar?)", title)
                    return False
                time.sleep(0.25)
            if not is_current(_document_name(uia, UIA, root), title):
                log.warning("could not open session %r", title)
                return False
            # The prompt box (a ProseMirror editor exposed as an Edit named "Prompt").
            edits = [e for e in _find(uia, UIA, root, UIA.UIA_EditControlTypeId) if e.CurrentIsKeyboardFocusable]
            prompt = next((e for e in edits if e.CurrentName == "Prompt"), edits[-1] if edits else None)
            if prompt is None:
                log.warning("no prompt box found in session %r", title)
                return False
            prompt.SetFocus()
            time.sleep(0.1)
            return True
        except Exception:
            log.exception("could not select session %r", title)
            return False

else:

    def is_desktop_app(hwnd: int) -> bool:  # noqa: ARG001
        return False

    def current_title(hwnd: int, timeout: float = 2.0) -> str:  # noqa: ARG001
        return ""

    def select_session(hwnd: int, title: str, timeout: float = 3.0) -> bool:  # noqa: ARG001
        return False
