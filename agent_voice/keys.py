"""Keystrokes and global hotkeys, per platform.

Windows: the `keyboard` package. macOS and Linux: `keyboard` needs root there, so hotkeys go through pynput;
keystrokes through System Events (AppleScript) on macOS, which needs the Accessibility permission (System Settings >
Privacy & Security > Accessibility), and through pynput on Linux (an X11 session; under Wayland only apps running
through XWayland receive them).
"""

from __future__ import annotations

import subprocess
import sys
import threading

MAC = sys.platform == "darwin"
PYNPUT = sys.platform != "win32"  # hotkeys through pynput: macOS and Linux
MODIFIERS = ("ctrl", "alt", "shift", "windows")


def _osa(script: str) -> str:
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    return out.stdout.strip()


def _tap(key: str, ctrl: bool = False) -> None:
    """Linux: one keystroke through pynput (X11)."""
    from pynput.keyboard import Controller, Key

    keyboard = Controller()
    target = getattr(Key, key) if len(key) > 1 else key
    if ctrl:
        with keyboard.pressed(Key.ctrl):
            keyboard.tap(target)
    else:
        keyboard.tap(target)


def paste() -> None:
    if MAC:
        _osa('tell application "System Events" to keystroke "v" using command down')
    elif PYNPUT:
        _tap("v", ctrl=True)
    else:
        import keyboard

        keyboard.send("ctrl+v")


def copy() -> None:
    if MAC:
        _osa('tell application "System Events" to keystroke "c" using command down')
    elif PYNPUT:
        _tap("c", ctrl=True)
    else:
        import keyboard

        keyboard.send("ctrl+c")


def enter() -> None:
    if MAC:
        _osa('tell application "System Events" to key code 36')
    elif PYNPUT:
        _tap("enter")
    else:
        import keyboard

        keyboard.send("enter")


# -- macOS and Linux: one pynput listener for every hotkey, which also knows which keys are down ----------
_PYNPUT = {"ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "windows": "<cmd>", "cmd": "<cmd>",
           "space": "<space>", "enter": "<enter>", "esc": "<esc>", "tab": "<tab>"}
_hotkeys: list = []
_down: set[str] = set()
_listener = None
_lock = threading.Lock()


def pynput_combo(combo: str) -> str:
    """'ctrl+alt+space' -> '<ctrl>+<alt>+<space>' (pynput's notation)."""
    return "+".join(_PYNPUT.get(part.strip().lower(), part.strip().lower()) for part in combo.split("+"))


def _name(key) -> str:
    """A pynput key as `keyboard` names it: 'ctrl', 'alt', 'space', 'a'..."""
    char = getattr(key, "char", None)
    if char:
        return char.lower()
    name = str(getattr(key, "name", "") or "")
    for ours, prefix in (("ctrl", "ctrl"), ("alt", "alt"), ("shift", "shift"), ("windows", "cmd")):
        if name.startswith(prefix):
            return ours
    return name


def _start_listener() -> None:
    global _listener
    from pynput import keyboard as pk

    def on_press(key):
        _down.add(_name(key))
        for hotkey in _hotkeys:
            hotkey.press(_listener.canonical(key))

    def on_release(key):
        _down.discard(_name(key))
        for hotkey in _hotkeys:
            hotkey.release(_listener.canonical(key))

    _listener = pk.Listener(on_press=on_press, on_release=on_release)
    _listener.daemon = True
    _listener.start()


def add_hotkey(combo: str, callback) -> None:
    if not PYNPUT:
        import keyboard

        keyboard.add_hotkey(combo, callback)
        return
    from pynput import keyboard as pk

    with _lock:
        _hotkeys.append(pk.HotKey(pk.HotKey.parse(pynput_combo(combo)),
                                  lambda: threading.Thread(target=callback, daemon=True).start()))
        if _listener is None:
            _start_listener()


def is_pressed(key: str) -> bool:
    if PYNPUT:
        return key.lower() in _down
    import keyboard

    return keyboard.is_pressed(key)


def modifiers_down() -> bool:
    return any(is_pressed(k) for k in MODIFIERS)


def unhook_all() -> None:
    global _listener
    if PYNPUT:
        if _listener is not None:
            _listener.stop()
            _listener = None
        _hotkeys.clear()
        return
    import keyboard

    keyboard.unhook_all()
