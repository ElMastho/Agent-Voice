"""Command-line entry point: `agent-voice <command>` or `python -m agent_voice <command>`."""

from __future__ import annotations

import argparse
import json
import sys


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
    p = sub.add_parser("chime", help="chime mode: no voice, a soft sound when the agent is done or needs you (default: toggle)")
    p.add_argument("mode", nargs="?", default="toggle", choices=["toggle", "on", "off"])

    p = sub.add_parser("hook", help="entry point used by agent hooks")
    p.add_argument("agent", help="claude | codex | gemini | cursor | generic")
    p.add_argument("rest", nargs=argparse.REMAINDER)

    p = sub.add_parser("install", help="register hooks for agents")
    p.add_argument("agents", nargs="*", default=["all"])
    p = sub.add_parser("uninstall", help="remove hooks for agents")
    p.add_argument("agents", nargs="*", default=["all"])

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
            print("muted (run `agent-voice unmute`)")
        return 0
    if args.cmd == "stop":
        client.request("/stop", {})
        return 0
    if args.cmd == "speed":
        result = client.request("/speed", {"value": args.value}) if args.value else client.request("/status")
        print("not running" if result is None else f"speed {result['speed_x']:g}x")
        return 0 if result else 1
    if args.cmd == "summary":
        result = client.request("/summary", {"mode": args.mode})
        print("not running" if result is None else f"summary {'on' if result['summary'] else 'off (full answers)'}")
        return 0 if result else 1
    if args.cmd == "handsfree":
        result = client.request("/handsfree", {"mode": args.mode})
        print("not running" if result is None else f"hands-free {'on' if result['hands_free'] else 'off'}")
        return 0 if result else 1
    if args.cmd == "chime":
        result = client.request("/chime", {"mode": args.mode})
        print("not running" if result is None else f"chime mode {'on' if result['chime'] else 'off (voice)'}")
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
        print("muted" if result["muted"] else "unmuted")
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


AGENT_DIRS = {"claude": ".claude", "codex": ".codex", "gemini": ".gemini", "cursor": ".cursor",
              "cline": "Documents/Cline"}


def quickstart() -> int:
    """agent-voice.exe double-clicked: download what's needed, hook into installed agents, start."""
    from pathlib import Path

    print("agent-voice: a local voice for your coding agents\n")
    try:
        main(["setup"])
        found = [name for name, folder in AGENT_DIRS.items() if (Path.home() / folder).is_dir()]
        print()
        if found:
            main(["install", *found])
        else:
            print("No agent found (Claude Code, Codex, Gemini CLI, Cursor, Cline). "
                  "Install one, then run `agent-voice install`.")
        print()
        ok = main(["start"]) == 0
        if ok:
            print("\nThe widget is at the top right of your screen; right-click it for options.")
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
