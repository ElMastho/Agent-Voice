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


# The send button's accessible name, as the app labels it in each UI language.
SEND_NAMES = {"send", "envoyer", "senden", "enviar", "invia", "verzenden", "wyślij", "送信", "发送", "傳送", "전송"}


def is_send(name: str) -> bool:
    return name.strip().lower() in SEND_NAMES


def pick_option(names: list[str], label: str) -> int:
    """Index of a question-card option: named "<label> <description> <n>" (description optional)."""
    for i, name in enumerate(names):
        if name == label or (name.startswith(label + " ") and name.rsplit(" ", 1)[-1].isdigit()):
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
        return _elements(root.FindAll(UIA.TreeScope_Descendants, cond))

    def _elements(found) -> list:
        out = []
        for i in range(found.Length):
            try:
                out.append(found.GetElement(i))
            except Exception:  # the page changed under us
                pass
        return out

    def _controls(uia, UIA, root) -> list[tuple]:
        """(element, is_edit, name) for every button and edit box, in page order."""
        cond = uia.CreateOrCondition(
            uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_ButtonControlTypeId),
            uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_EditControlTypeId),
        )
        out = []
        for e in _elements(root.FindAll(UIA.TreeScope_Descendants, cond)):
            try:
                out.append((e, e.CurrentControlType == UIA.UIA_EditControlTypeId, e.CurrentName or ""))
            except Exception:
                pass
        return out

    def _invoke(UIA, element) -> bool:
        pattern = element.GetCurrentPattern(UIA.UIA_InvokePatternId)
        if not pattern:
            return False
        pattern.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
        return True

    def _set_text(UIA, element, text: str) -> None:
        element.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern).SetValue(text)

    def _text(UIA, element) -> str:
        pattern = element.GetCurrentPattern(UIA.UIA_ValuePatternId)
        return pattern.QueryInterface(UIA.IUIAutomationValuePattern).CurrentValue if pattern else ""

    def _prompt(controls: list[tuple]) -> int:
        """Index of the conversation's prompt box (an Edit named "Prompt") in `controls`."""
        return next((i for i, (_, edit, name) in enumerate(controls) if edit and name == "Prompt"), -1)

    def _document_name(uia, UIA, root) -> str:
        # The window can hold several web documents (side panes); the conversation is the one
        # named "<title> - Claude Code".
        names = [d.CurrentName or "" for d in _find(uia, UIA, root, UIA.UIA_DocumentControlTypeId)]
        return next((n for n in names if n.endswith(PAGE_SUFFIX)), next((n for n in names if n), ""))

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

    def _open(uia, UIA, root, title: str, timeout: float) -> bool:
        """Show conversation `title`: a click on its sidebar entry (the window stays where it is)."""
        deadline = time.time() + timeout
        # Chromium builds its accessibility tree lazily, on the first queries: retry briefly.
        while not is_current(_document_name(uia, UIA, root), title):
            buttons = [b for b in _find(uia, UIA, root, UIA.UIA_ButtonControlTypeId)
                       if b.GetCurrentPattern(UIA.UIA_InvokePatternId)]
            i = pick_entry([b.CurrentName or "" for b in buttons], title)
            if i >= 0:
                _invoke(UIA, buttons[i])
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
        return True

    def select_session(hwnd: int, title: str, timeout: float = 3.0) -> bool:
        """Open the sidebar session `title` and put the caret in its prompt box. True when done."""
        try:
            UIA, uia = _uia()
            root = uia.ElementFromHandle(hwnd)
            if not _open(uia, UIA, root, title, timeout):
                return False
            controls = _controls(uia, UIA, root)
            i = _prompt(controls)
            if i < 0:
                log.warning("no prompt box found in session %r", title)
                return False
            controls[i][0].SetFocus()
            time.sleep(0.1)
            return True
        except Exception:
            log.exception("could not select session %r", title)
            return False

    def send_message(hwnd: int, title: str, text: str, timeout: float = 3.0) -> bool:
        """Type `text` into conversation `title` and send it, without bringing the window forward.

        True once the prompt box has emptied (the app took the message).
        """
        try:
            UIA, uia = _uia()
            root = uia.ElementFromHandle(hwnd)
            if not _open(uia, UIA, root, title, timeout):
                return False
            controls = _controls(uia, UIA, root)
            i = _prompt(controls)
            if i < 0:
                log.warning("no prompt box found in session %r", title)
                return False
            prompt = controls[i][0]
            _set_text(UIA, prompt, text)
            deadline = time.time() + 2.0
            sent = False
            while not sent and time.time() < deadline:  # the send button enables once the editor has text
                after = _controls(uia, UIA, root)
                j = _prompt(after)
                send = next((e for e, edit, name in after[j + 1:]
                             if not edit and is_send(name) and e.CurrentIsEnabled), None)
                sent = send is not None and _invoke(UIA, send)
                if not sent:
                    time.sleep(0.1)
            if not sent:
                log.warning("no enabled send button in session %r", title)
                return False
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if not _text(UIA, prompt).strip():
                    return True
                time.sleep(0.1)
            log.warning("message to %r was not taken", title)
            return False
        except Exception:
            log.exception("could not send to session %r", title)
            return False

    def _card(uia, UIA, root) -> list[tuple]:
        """Controls of the question card, which sits just above the prompt box."""
        controls = _controls(uia, UIA, root)
        return controls[: max(_prompt(controls), 0)]

    def answer_question(hwnd: int, title: str, label: str = "", other: str = "", timeout: float = 3.0) -> bool:
        """Answer the question card in conversation `title`: pick option `label`, or type `other`.

        Works without bringing the window forward. True once the card has taken the answer.
        """
        try:
            UIA, uia = _uia()
            root = uia.ElementFromHandle(hwnd)
            if not _open(uia, UIA, root, title, timeout):
                return False
            card = _card(uia, UIA, root)
            if label:
                buttons = [e for e, edit, _ in card if not edit]
                i = pick_option([n for _, edit, n in card if not edit], label)
                if i < 0:
                    log.warning("option %r not found in %r", label, title)
                    return False
                _invoke(UIA, buttons[i])
            else:
                # "Other": the card's free-text box, the last edit box above the prompt.
                box = next((e for e, edit, _ in reversed(card) if edit), None)
                if box is None:
                    log.warning("no free-text answer box in %r", title)
                    return False
                _set_text(UIA, box, other)

            def pending() -> bool:
                card = _card(uia, UIA, root)
                if label:
                    return pick_option([n for _, edit, n in card if not edit], label) >= 0
                return any(edit for _, edit, _ in card)

            deadline = time.time() + 3.0
            while time.time() < deadline:
                time.sleep(0.2)
                if not pending():
                    return True  # picking the option was enough
                send = next((e for e, edit, n in _card(uia, UIA, root)
                             if not edit and is_send(n) and e.CurrentIsEnabled), None)
                if send is not None:
                    _invoke(UIA, send)
                    time.sleep(0.5)
                    return not pending()
            log.warning("question card in %r did not take the answer", title)
            return False
        except Exception:
            log.exception("could not answer in session %r", title)
            return False

else:

    def is_desktop_app(hwnd: int) -> bool:  # noqa: ARG001
        return False

    def current_title(hwnd: int, timeout: float = 2.0) -> str:  # noqa: ARG001
        return ""

    def select_session(hwnd: int, title: str, timeout: float = 3.0) -> bool:  # noqa: ARG001
        return False

    def send_message(hwnd: int, title: str, text: str, timeout: float = 3.0) -> bool:  # noqa: ARG001
        return False

    def answer_question(hwnd: int, title: str, label: str = "", other: str = "",  # noqa: ARG001
                        timeout: float = 3.0) -> bool:
        return False
