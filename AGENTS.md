# AGENTS.md

Guidance for AI coding agents (and humans) working on **agent-voice**: local text-to-speech and dictation for AI coding agents, with a notch-style status widget. Read this before changing code.

## What this project is

A Windows-first Python app with three cooperating processes:

| Process | Entry point | Role |
|---|---|---|
| **Daemon** | `agent-voice serve` → `agent_voice/server.py` | Owns the models, audio, global hotkeys and a local HTTP API on `127.0.0.1:59125` |
| **Hook** (short-lived, per event) | `agent-voice hook <agent>` → `agent_voice/hooks.py` | Called by Claude Code / Codex / Gemini CLI / Cursor; extracts the reply text and POSTs it to the daemon |
| **Widget + tray** | `agent-voice widget` → `agent_voice/widget.py` | PySide6 UI (the notch by default, or a floating bar); polls `/status` while animating, long-polls it at rest, and calls daemon endpoints |

The daemon launches the widget on start (`config.widget.enabled`). A Windows Startup `.vbs` (`agent-voice autostart on`) launches the daemon at sign-in.

## Layout

```
agent_voice/
  cli.py        argparse entry point (all subcommands)
  config.py     defaults + ~/.agent-voice/{config,state}.json
  server.py     Daemon class (speak queue, auto-listen, push-to-talk, hotkeys) + HTTP handler
  tts.py        Speaker: Piper synth thread -> playback thread, sentence by sentence
  stt.py        Recorder, energy-based voice-activity listener, faster-whisper Transcriber
  lang.py       FR/EN detection (lingua + word-hint heuristic), sentence segmentation
  textclean.py  markdown -> speakable text, sentence splitting, truncation
  hooks.py      per-agent payload adapters, Claude transcript parsing, spoken-text dedupe
  install.py    writes/removes hooks in each agent's config; Windows autostart
  client.py     stdlib-only HTTP client used by hooks/CLI/widget; can start the daemon detached
  winfocus.py   Win32 foreground window get/restore (ctypes)
  calls.py      is another app on the mic (a call)? read from Windows' privacy records
  widget.py     notch / floating bar, banner (read along, chimes), tray icon, animations
scripts/render_docs.py   regenerates docs/images/* from the real widget code
tests/test_core.py       pytest suite (no audio/GUI needed)
```

## Commands

```bash
py -3.13 -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q          # must pass before committing
.venv/Scripts/agent-voice serve            # daemon in the foreground with console logs
.venv/Scripts/agent-voice shutdown && .venv/Scripts/agent-voice start   # reload after code changes
.venv/Scripts/python scripts/render_docs.py   # after any visual widget change
.venv/Scripts/python packaging/prepare.py && .venv/Scripts/python -m PyInstaller packaging/agent-voice.spec   # Windows exe
```

Releases: `.github/workflows/release.yml` builds the exe on every push and publishes `agent-voice-windows-x64.zip` when a `v*` tag is pushed. The exe calls itself through `client.self_command()` (never `python -m agent_voice` directly): `agent-voice.exe` for hooks/CLI, `agent-voice-w.exe` (no console) for the daemon, widget and autostart. Keep the zip small (lingua is excluded; voices and Whisper are downloaded at first run).

The daemon and widget run as `pythonw` in the background. **Code changes need a restart** (`shutdown` + `start`; kill a stray `pythonw3.13.exe` if the widget keeps old code). Hooks are re-executed per event, so `hooks.py` changes apply immediately.

## Invariants: do not break these

1. **Hooks never break the agent.** `hooks.py`, `client.py` and `textclean.py` stay stdlib-only and fast, catch every exception (logged to `~/.agent-voice/logs/hook-errors.log`) and exit 0. Gemini and Cursor parse stdout as JSON, so print exactly `{}` for them and nothing else; Claude's `UserPromptSubmit` prints only its JSON `hookSpecificOutput`.
2. **Spoken once, in order.** One daemon worker (`Daemon.jobs`) processes `/speak` in arrival order. Dedupe of Claude texts happens in `hooks._unspoken` under a cross-process lock file (parallel tool calls fire concurrent hooks). The `Stop` payload's `last_assistant_message` is only a fallback (`_covered`, whitespace-insensitive).
3. **Summaries are decided in the hook.** `hooks.summary_enabled()` reads `state.json` (runtime toggle) then config. Claude's `UserPromptSubmit` hook prints `additionalContext` asking for a `Voice summary:` line; `hooks.summarize()` prefers that line and falls back to the opening sentences. Full-text mode strips the line (`strip_summary`) so it is not read twice.
4. **`final=True` arms auto-listen.** Only end-of-turn events (Claude `Stop`, Codex/Gemini/Cursor payloads) set it. Progress (`PreToolUse`) must not open the mic.
5. **Local API stays locked down.** `_allowed()` in `server.py` rejects any `Origin` header, a `Host` other than `127.0.0.1:<port>`/`localhost:<port>`, non-JSON POSTs, and bodies over 1 MB. Every new endpoint goes through it; never bind to anything but `127.0.0.1`.
6. **The widget never steals focus.** It uses `WS_EX_NOACTIVATE` + `Qt.WindowDoesNotAcceptFocus`; dictation pastes into the user's agent window and depends on this.
7. **The widget is a view.** State lives in the daemon (`/status`). The widget only keeps its own position/visibility (`~/.agent-voice/widget.json`; not `state.json`, which the daemon rewrites wholesale).
8. **Nothing leaves the machine.** No network calls except model downloads (Piper voices, Whisper weights, the wake-word model) on first use, and the optional release check (`update_check`).
9. **Battery first.** ONNX Runtime sessions we create never busy-wait (`tts._quiet_session`: `allow_spinning=0`, 2 threads; the default spinning cost ~20x the CPU per sentence). Feed models whole chunks, not 30 ms blocks. The wake word runs a volume gate first and the keyword spotter only on finished utterances.
10. **The widget doesn't redraw at rest.** `_tick` drops to 4 Hz and skips `update()` once `_is_still()`; status polling slows to 0.6 s. Any new animation must be covered by `_is_still()`, or the widget redraws at 30 fps forever. At rest the widget long-polls `/status?wait=<tag>` (one request per change, or every 20 s): keep `server.status_tag` blind to values that change without anything visible changing (mic level, seconds).

## Adding an agent integration

1. In `hooks.py`, make sure `extract_text()` finds the reply (add the field name to `TEXT_KEYS` if needed) and that the payload is read from the right place (stdin vs argv). Call `client.speak(text, source=agent, final=True)`.
2. In `install.py`, add an idempotent installer that strips entries containing `MARKER` before adding its own, backs up the file with `_backup()`, and supports `remove=True`. Register it in `INSTALLERS` and `AGENTS`.
3. Add a test with a sample payload in `tests/test_core.py`; update the README and `docs/guide.md` tables.

`hook_payload` saves the last raw payload per agent to `~/.agent-voice/logs/payload-<agent>.json`: use it to learn a new format.

## Gotchas (learned the hard way)

- **PySide6 `QPen` rejects keyword args** (`cap=`, `join=`): use `widget._round_pen()`.
- **ctypes handles must be typed.** Set `restype`/`argtypes` to `wintypes.HWND`/`HRGN` etc., or 64-bit handles are truncated.
- **Acrylic only clips to DWM's native rounding.** `SetWindowRgn` does not clip the acrylic accent, so `RADIUS` must stay equal to Windows 11's `DWMWCP_ROUND` (8 px), with the system border hidden (`DWMWA_BORDER_COLOR = DWMWA_COLOR_NONE`).
- **`sounddevice` can hang on a broken audio driver**: playback polls with a deadline instead of `sd.wait()`. Closing the mic stream can take ~1 s, so `cancel_listen()` updates the phase immediately.
- **Claude `async` hooks may be killed on `Stop`**: only `PreToolUse` is async; `Stop` stays synchronous.
- **Whisper hallucinates on silence** ("Thank you." and its French equivalent): filtered by `stt.is_hallucination`.
- **Python 3.14 lacks some wheels** (onnxruntime/ctranslate2 at the time of writing): use 3.13.
- **Global hotkeys (`keyboard`) can't see apps running as administrator** unless the daemon is elevated too.
- **`render_docs.py` replaces `widget.time`** with a fake clock; keep animation code reading time through the module-level `time` import.

## Style

- Python 3.10+, type hints, `from __future__ import annotations`, f-strings, `pathlib`.
- Comments explain *why*, not what. Keep functions small; match the surrounding code.
- User-facing text is English; spoken confirmations are short.
- Line length ~120.

## Commits

- Run `pytest` first; for widget changes, re-render and eyeball `docs/images/`.
- Never commit `.venv/`, models (`*.onnx`), audio (`*.wav`), logs, or anything from `~/.agent-voice/` (all in `.gitignore`).
- The repo is GPL-3.0-or-later (Piper is GPL). New dependencies must be GPL-compatible; add them to `pyproject.toml` and to the README credits table.
