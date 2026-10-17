"""Command-line entry point: `agent-voice <command>` or `python -m agent_voice <command>`."""

from __future__ import annotations

import argparse
import json
import sys


def _schedule(args, client) -> int:
    import time
    from datetime import datetime

    from agent_voice.schedule import parse_when

    if args.list or (not args.prompt and not args.cancel):
        items = (client.request("/status") or {}).get("schedules")
        if items is None:
            print("not running")
            return 1
        for item in items:
            when = datetime.fromtimestamp(item["at"]).strftime("%a %d %b %H:%M")
            print(f"#{item['id']}  {when}{' (' + item['repeat'] + ')' if item['repeat'] else ''}  {item['title']}: "
                  f"{item['prompt'][:80]}")
        print("" if items else "nothing scheduled")
        return 0
    if args.cancel:
        result = client.request("/schedule_cancel", {"id": args.cancel})
        print("cancelled" if result and result.get("ok") else "no such scheduled prompt")
        return 0 if result and result.get("ok") else 1
    at = parse_when(args.at, time.time()) if args.at else None
    if not at:
        print('when? --at "14:00", "2pm", "in 30 minutes", "tomorrow at 9"')
        return 2
    payload = {"prompt": " ".join(args.prompt), "at": at, "repeat": args.repeat}
    if args.new:
        payload["new"] = {"target": args.agent, "cwd": args.cwd}
    elif args.thread:
        payload["thread"] = args.thread
    result = client.request("/schedule", payload)
    if not result:
        print(f"not scheduled (no thread called {args.thread}?)" if args.thread else "not running")
        return 1
    print(f"#{result['id']} at {datetime.fromtimestamp(result['at']).strftime('%a %H:%M')}: {result['title']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-voice", description="Local voice for AI coding agents.")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("selfcheck", help=argparse.SUPPRESS)  # release build smoke test
    p = sub.add_parser("serve", help="run the daemon in this terminal")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("start", help="start the daemon in the background")
    sub.add_parser("shutdown", help="stop the daemon")
    sub.add_parser("status", help="show daemon status")
    p = sub.add_parser("widget", help="show the floating status widget")
    p.add_argument("--demo", help=argparse.SUPPRESS)

    p = sub.add_parser("say", help="speak text (argument or stdin)")
    p.add_argument("text", nargs="*")
    p.add_argument("--lang", choices=["en", "fr"])
    p.add_argument("--voice", help='Piper voice for this text only, e.g. "fr_FR-upmc-medium:pierre"')
    sub.add_parser("stop", help="stop the current speech")

    p = sub.add_parser("mute", help="mute / unmute (default: toggle)")
    p.add_argument("mode", nargs="?", default="toggle", choices=["toggle", "on", "off"])
    sub.add_parser("unmute", help="unmute")
    p = sub.add_parser("speed", help="voice speed as a multiplier, e.g. 1.5 (no value: show it)")
    p.add_argument("value", nargs="?", type=float)
    p = sub.add_parser("summary", help="final answers as a one-sentence summary, or in full (default: toggle)")
    p.add_argument("mode", nargs="?", default="toggle", choices=["toggle", "on", "off"])
    p = sub.add_parser("handsfree", help="hands-free mode: voice replies are sent automatically (default: toggle)")
    p.add_argument("mode", nargs="?", default="toggle", choices=["toggle", "on", "off"])
    p = sub.add_parser("wake", help='"Hey Claude, ..." while idle sends a new prompt (default: toggle)')
    p.add_argument("mode", nargs="?", default="toggle", choices=["toggle", "on", "off"])
    p = sub.add_parser("headphones", help="the voice only in headphones; chimes on speakers (default: toggle)")
    p.add_argument("mode", nargs="?", default="toggle", choices=["toggle", "on", "off"])
    p = sub.add_parser("apps", help="read out ChatGPT / Copilot / Perplexity desktop notifications (default: toggle)")
    p.add_argument("mode", nargs="?", default="toggle", choices=["toggle", "on", "off"])
    p = sub.add_parser("chime", help="chime mode: no voice, a soft sound when the agent is done or needs you (default: toggle)")
    p.add_argument("mode", nargs="?", default="toggle", choices=["toggle", "on", "off"])

    p = sub.add_parser("hook", help="entry point used by agent hooks")
    p.add_argument("agent", help="claude | codex | gemini | cursor | generic")
    p.add_argument("rest", nargs=argparse.REMAINDER)

    p = sub.add_parser("mcp", help="MCP server (stdio): speak, notify, ask_user and set_status for any MCP app")
    p.add_argument("--client", default="", help="the app's name, when it doesn't say it itself")
    p.add_argument("--hooks", action="store_true", help="the app already speaks through hooks (no speak tool)")
    p.add_argument("--todo-only", action="store_true", help="only the to-do list tools (Claude Code: its hooks do the rest)")

    p = sub.add_parser("watch", help="run a terminal agent (aider, opencode...) and announce when it waits for you")
    p.add_argument("command", nargs=argparse.REMAINDER)

    p = sub.add_parser("apply-update", help=argparse.SUPPRESS)  # the updater, run from the downloaded build
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--pid", type=int, default=0)
    p.add_argument("--version", default="")

    p = sub.add_parser("launch", help=argparse.SUPPRESS)  # a new thread's terminal tab: agent + prompt file
    p.add_argument("agent")
    p.add_argument("prompt_file")

    p = sub.add_parser("new", help="start a new thread: in the Claude desktop app, or an agent CLI in a new tab")
    p.add_argument("prompt", nargs="+")
    p.add_argument("--agent", default="", help="claude-app | claude | codex | gemini (default: like your last thread)")
    p.add_argument("--cwd", default="", help="its folder (default: the one you last worked in)")

    p = sub.add_parser("schedule", help="send a prompt later: to a thread, or as a new thread")
    p.add_argument("prompt", nargs="*")
    p.add_argument("--at", default="", help='when: "14:00", "2pm", "in 30 minutes", "tomorrow at 9"')
    p.add_argument("--thread", default="", help="the thread's name (default: your last one)")
    p.add_argument("--new", action="store_true", help="start a new thread with it")
    p.add_argument("--agent", default="", help="with --new: claude-app | claude | codex | gemini")
    p.add_argument("--cwd", default="", help="with --new: its folder")
    p.add_argument("--repeat", default="", choices=["", "daily", "weekdays"])
    p.add_argument("--list", action="store_true", help="what's scheduled")
    p.add_argument("--cancel", type=int, default=0, metavar="ID")

    sub.add_parser("restart", help="stop and start agent-voice again (the notch too)")
    sub.add_parser("open", help="show the notch's panel, starting agent-voice if it isn't running (the Start menu)")
    p = sub.add_parser("shortcut", help="agent-voice in the Windows Start menu")
    p.add_argument("mode", nargs="?", default="on", choices=["on", "off"])

    p = sub.add_parser("phone", help="phone notifications through ntfy (off until you turn them on)")
    p.add_argument("action", nargs="?", default="status", choices=["on", "off", "test", "new", "status"])

    p = sub.add_parser("native-host", help=argparse.SUPPRESS)  # browser extension bridge (Chrome native messaging)
    p.add_argument("origin", nargs="*")

    p = sub.add_parser("install", help="register hooks for agents")
    p.add_argument("agents", nargs="*", default=["all"])
    p = sub.add_parser("uninstall", help="remove hooks for agents")
    p.add_argument("agents", nargs="*", default=["all"])

    sub.add_parser("recap", help="today's recap: what each thread did, what's waiting, limits, tokens")
    p = sub.add_parser("history", help="search every conversation (what you asked, what they answered)")
    p.add_argument("words", nargs="*", help="what to look for (nothing: the latest)")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--clear", action="store_true", help="forget the whole history")
    p = sub.add_parser("usage", help="show Claude Code's rate limits in the notch (adds a status line)")
    p.add_argument("mode", nargs="?", default="on", choices=["on", "off"])

    p = sub.add_parser("autostart", help="start the daemon at Windows login")
    p.add_argument("mode", nargs="?", default="on", choices=["on", "off"])
    sub.add_parser("setup", help="write default config, download voices and the speech-recognition model")

    args = parser.parse_args(argv)

    if args.cmd is None:
        return quickstart() if getattr(sys, "frozen", False) else (parser.print_help() or 0)
    if args.cmd == "selfcheck":
        return selfcheck()
    if args.cmd == "hook":
        from agent_voice.hooks import run

        return run(args.agent, args.rest)

    if args.cmd == "mcp":
        from agent_voice.mcp import run

        return run(args.client, args.hooks, args.todo_only)
    if args.cmd == "watch":
        from agent_voice.watch import run

        return run(args.command)
    if args.cmd == "launch":
        from agent_voice.launch import run

        return run(args.agent, args.prompt_file)
    if args.cmd == "apply-update":
        from pathlib import Path

        from agent_voice.updater import apply

        return apply(Path(args.source), Path(args.target), args.pid, args.version)
    if args.cmd == "native-host":
        from agent_voice.native_host import run

        return run()

    if args.cmd == "widget":
        from agent_voice.widget import run

        return run(args.demo)

    if args.cmd == "serve":
        from agent_voice.server import serve

        return serve(foreground=not args.background)

    from agent_voice import client

    if args.cmd == "start":
        ok = client.start_detached(wait=60)  # the first start may download the voices
        print("agent-voice is running" if ok else "agent-voice did not start; see ~/.agent-voice/logs/server.log")
        return 0 if ok else 1
    if args.cmd == "shutdown":
        print("stopped" if client.request("/shutdown", {}) is not None else "not running")
        return 0
    if args.cmd == "status":
        status = client.request("/status")
        print(json.dumps(status, indent=2) if status else "not running")
        return 0 if status else 1
    if args.cmd == "say":
        text = " ".join(args.text) or sys.stdin.read()
        result = client.speak(text, source="cli", lang=args.lang, voice=args.voice)
        if result is None:
            print("agent-voice is not running", file=sys.stderr)
            return 1
        if result.get("muted"):
            print("do not disturb is on (agent-voice unmute)")
        return 0
    if args.cmd == "stop":
        client.request("/stop", {})
        return 0
    if args.cmd == "speed":
        result = client.request("/speed", {"value": args.value}) if args.value else client.request("/status")
        print("not running" if result is None else f"speed {result['speed_x']:g}x")
        return 0 if result else 1
    if args.cmd in SWITCHES:
        path, field, name, off = SWITCHES[args.cmd]
        result = client.request(path, {"mode": args.mode})
        print("not running" if result is None else f"{name} {'on' if result[field] else off}")
        return 0 if result else 1
    if args.cmd == "schedule":
        return _schedule(args, client)
    if args.cmd == "restart":
        ok = client.restart()
        print("agent-voice restarted" if ok else "agent-voice did not start; see ~/.agent-voice/logs/server.log")
        return 0 if ok else 1
    if args.cmd == "open":
        if client.is_running():
            client.request("/widget_open", {})
            return 0
        return 0 if client.start_detached(wait=60) else 1
    if args.cmd == "shortcut":
        from agent_voice.install import start_menu

        print(start_menu(args.mode == "on"))
        return 0
    if args.cmd == "phone":
        payload = {"on": args.action == "on"} if args.action in ("on", "off") else (
            {"action": args.action} if args.action in ("test", "new") else None)
        result = client.request("/phone", payload) if payload else (client.request("/status") or {}).get("phone")
        if result is None:
            print("not running")
            return 1
        if not result.get("enabled"):
            print("phone notifications off (agent-voice phone on)")
        else:
            print(f"phone notifications on: subscribe to {result['url']} in the ntfy app (or scan the QR code in the "
                  "notch); keep that link private")
            if args.action == "test":
                print("test sent" if result.get("ok") else f"test not sent ({result.get('error') or 'check the server'})")
        return 0
    if args.cmd == "new":
        result = client.request("/new_thread", {"text": " ".join(args.prompt), "target": args.agent, "cwd": args.cwd})
        print("not running" if result is None else "starting a new thread")
        return 0 if result else 1
    if args.cmd in ("mute", "unmute"):
        mode = "off" if args.cmd == "unmute" else args.mode
        result = client.request("/mute", {"mode": mode})
        if result is None:
            # Daemon down: flip the persisted flag directly.
            from agent_voice.config import load_state, save_state

            state = load_state()
            state["muted"] = (not state.get("muted")) if mode == "toggle" else mode == "on"
            save_state(state)
            result = {"muted": state["muted"]}
        print("do not disturb on" if result["muted"] else "do not disturb off")
        return 0

    if args.cmd in ("install", "uninstall"):
        from agent_voice.install import AGENTS, INSTALLERS

        names = AGENTS if "all" in args.agents else args.agents
        for name in names:
            if name not in INSTALLERS:
                print(f"{name}: unknown agent (choose from {', '.join(AGENTS)})")
                continue
            try:
                print(f"{name}: {INSTALLERS[name](remove=args.cmd == 'uninstall')}")
            except Exception as exc:
                print(f"{name}: failed: {exc}")
        return 0
    if args.cmd == "history":
        return _history(args)
    if args.cmd == "recap":
        reply = client.request("/recap", {"speak": False})
        print(reply["text"] if reply else "agent-voice isn't running (agent-voice start)")
        return 0 if reply else 1
    if args.cmd == "usage":
        from agent_voice.install import usage_line

        print(usage_line(args.mode == "on"))
        return 0
    if args.cmd == "autostart":
        from agent_voice.install import autostart

        print(autostart(args.mode == "on"))
        return 0
    if args.cmd == "setup":
        from agent_voice.config import load_config, write_default_config
        from agent_voice.tts import download_voice

        print(f"config: {write_default_config()}")
        cfg = load_config()
        for name in cfg["voices"].values():
            print(f"voice: {name}")
            download_voice(name.partition(":")[0])
        if cfg["stt"].get("enabled"):
            print(f"speech recognition model: whisper {cfg['stt']['model']} (first download takes a minute)")
            from agent_voice.stt import Transcriber

            Transcriber(cfg).warm_up()
        print("done")
        return 0
    return 1


def _history(args) -> int:
    """Read straight from the file: it works with the service stopped too."""
    import time

    from agent_voice.history import History

    h = History()
    if args.clear:
        h.clear()
        print("history cleared")
        return 0
    rows = h.search(" ".join(args.words), args.limit)
    if not rows:
        print("nothing found" if args.words else "nothing yet")
    for row in rows:
        who = "you" if row["kind"] == "you" else row.get("agent") or "agent"
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["at"]))
        print(f"{stamp}  {who:<8} {row.get('title') or '':<24.24} {row['snippet']}")
    return 0


# On/off commands: (endpoint, the field it answers with, what it's called, how "off" reads).
SWITCHES = {"summary": ("/summary", "summary", "summary", "off (full answers)"),
            "handsfree": ("/handsfree", "hands_free", "hands-free", "off"),
            "wake": ("/wake", "wake", "wake word", "off"),
            "headphones": ("/headphones", "headphones_only", "voice in headphones only", "off (any speaker)"),
            "apps": ("/apps", "desktop_apps", "desktop apps", "off"),
            "chime": ("/chime", "chime", "chime mode", "off (voice)")}


def quickstart() -> int:
    """agent-voice.exe double-clicked: download what's needed, hook into installed agents, start."""
    from agent_voice.install import agent_states

    print("agent-voice: a local voice for your coding agents\n")
    try:
        main(["setup"])
        # Every agent found on this PC; the browser extension needs a click in the browser, so it's offered later.
        found = [name for name, state in agent_states().items() if state != "missing" and name != "browser"]
        print()
        if found:
            main(["install", *found])
        else:
            print("No agent found (Claude Code, Codex, Gemini CLI, Cursor, Cline, Antigravity, Claude Desktop, "
                  "Windsurf, VS Code). Install one, then run `agent-voice install`.")
        print()
        print("ChatGPT, Claude or Gemini in the browser: `agent-voice install browser`.")
        print("Aider, OpenCode or any terminal agent: `agent-voice watch <command>`.")
        print()
        ok = main(["start"]) == 0
        if ok:
            print("\nThe notch is at the top of your screen: hover it to bring it down, right-click it for options.")
            print("Start with Windows: `agent-voice autostart on` (or tray icon > Start with Windows).")
    except Exception as exc:  # show it instead of a window that closes on its own
        print(f"\nSomething went wrong: {exc}")
        ok = False
    if sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close this window...")
    return 0 if ok else 1


def selfcheck() -> int:
    """Import every heavy dependency and run the offline parts once (used by the release build)."""
    import sounddevice  # noqa: F401  (PortAudio DLL)
    from faster_whisper.vad import get_vad_model
    import sherpa_onnx  # noqa: F401  (wake word)
    from piper import PiperVoice  # noqa: F401
    from PySide6.QtWidgets import QApplication  # noqa: F401

    from agent_voice import lang, widget  # noqa: F401

    assert lang.detect("Bonjour, est-ce que tu peux relancer le serveur ?") == "fr"
    assert lang.detect("Could you restart the server please?") == "en"
    get_vad_model()
    if sys.platform == "win32":
        from agent_voice import claudeapp

        claudeapp._uia()
    print("selfcheck ok")
    return 0
