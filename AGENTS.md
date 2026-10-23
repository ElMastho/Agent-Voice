# AGENTS.md

Guidance for AI coding agents (and humans) working on **agent-voice**: local text-to-speech and dictation for AI agents (coding agents, MCP apps, chat sites, terminal agents), with a notch-style status widget that follows all of them. Read this before changing code.

## What this project is

A Windows-first Python app (macOS and Linux in beta) with three cooperating processes, plus short-lived bridges for the agents that have no hooks:

| Process | Entry point | Role |
|---|---|---|
| **Daemon** | `agent-voice serve` → `agent_voice/server.py` + `agent_voice/daemon/` | Owns the models, audio, global hotkeys and a local HTTP API on `127.0.0.1:59125` |
| **Hook** (short-lived, per event) | `agent-voice hook <agent>` → `agent_voice/hooks.py` | Called by Claude Code / Codex / Gemini CLI / Cursor; extracts the reply text and POSTs it to the daemon |
| **Widget + tray** | `agent-voice widget` → `agent_voice/widget.py` | PySide6 UI (the notch by default, or a floating bar); polls `/status` while animating, long-polls it at rest, and calls daemon endpoints |
| **MCP server** (per app session) | `agent-voice mcp` → `agent_voice/mcp.py` | Started by MCP apps (Antigravity, Claude Desktop, Windsurf, VS Code, and Codex/Gemini/Cursor alongside their hooks): `speak`, `notify`, `ask_user`, `set_status` over stdio |
| **Browser bridge** (per browser) | `agent-voice native-host` → `agent_voice/native_host.py` | Started by Chrome/Edge for the extension in `agent_voice/browser/` (native messaging) |
| **Terminal wrapper** | `agent-voice watch <cmd>` → `agent_voice/watch.py` | Runs a terminal agent in a pseudo-terminal (pywinpty / pty) and reads its screen (pyte) |

The daemon launches the widget on start (`config.widget.enabled`). A Windows Startup `.vbs` (`agent-voice autostart on`) launches the daemon at sign-in.

## Layout

```
agent_voice/
  cli.py        argparse entry point (all subcommands)
  config.py     defaults + ~/.agent-voice/{config,state}.json
  server.py     the HTTP API (`_allowed`, routes), the single-instance lock, serve()
  daemon/       the Daemon, one mixin per part, all sharing one state (set up in core.Daemon.__init__):
                core (worker, /status) · settings (switches, modes, _say) · speech (replies) · needs (approvals,
                questions) · listening (reply mic, barge-in, take-back) · threads (windows, _paste) · agents
                (connect, new thread) · handoff · limits · phone · schedules · tools (Utils) · day (music,
                meetings, brief, recap) · presence (away, calls) · sound (ducking) · desktop_apps · updates ·
                wake · hotkeys; plus phrases (what you say, parsed), sounds (cues, chimes), common
  tts.py        Speaker: Piper (or Kokoro) synth thread -> playback thread, sentence by sentence
  history.py    every prompt and reply in a local SQLite FTS5 file, searched from the notch's History box
  companion.py  Utils' state: Pomodoro, timers, stopwatch, to-do, reminders, notes (state.json); their voice phrases
  media.py      what's playing (players' window titles + audio sessions), media keys; playerctl on Linux
  agenda.py     today's meetings: a small iCal reader (repeats, exceptions) and Outlook classic (only while open)
  kokoro.py     the natural voice (opt-in): Kokoro via kokoro-onnx, its verified one-time download
  stt.py        Recorder, energy-based voice-activity listener, faster-whisper Transcriber
  lang.py       FR/EN detection (lingua + word-hint heuristic), sentence segmentation
  textclean.py  markdown -> speakable text, sentence splitting, truncation
  i18n.py       tr() / plural() / action(): the app's words in English or French; i18n_fr.py the French
  hooks.py      per-agent payload adapters, Claude transcript parsing, spoken-text dedupe
  install.py    writes/removes hooks in each agent's config; Windows autostart
  client.py     stdlib-only HTTP client used by hooks/CLI/widget; can start the daemon detached
  winfocus.py   foreground window get/restore, idle time, an app's window (Win32 ctypes; System Events on macOS)
  keys.py       keystrokes and global hotkeys (`keyboard`; AppleScript + pynput on macOS)
  calls.py      is another app on the mic (a call)? read from Windows' privacy records
  mcp.py        MCP server (stdio JSON-RPC): speak / notify / ask_user / set_status
  native_host.py  browser extension bridge (Chrome native messaging: 4-byte length + JSON)
  browser/      the extension (MV3): sites.js (per-site selectors + pure reply detection), content.js, background.js
  watch.py      `agent-voice watch`: pseudo-terminal + pyte screen; Tracker turns screen changes into turns
  quota.py      agents' rate limits (5-hour, weekly): Codex's session logs, Claude's usage cache
  usage.py      Claude's usage from its transcripts (Ledger) and the estimate between real readings (Estimator)
  ntfy.py       phone notifications (opt-in): ntfy messages, batching, sending
  outputs.py    headphones or speakers: the default playback device's form factor (pycaw)
  pulse.py      Linux sound through pactl (PulseAudio / PipeWire): streams, recording apps, the output's kind, players
  ducking.py    other apps' volume under the voice / while you reply, media pause (Windows mixer, pycaw)
  schedule.py   scheduled prompts: "14:00", "in 20 minutes", "demain à 9h", repeats
  apps.py       desktop AI apps' Windows notifications (wpndatabase.db, read-only, filtered in SQL)
  updater.py    release check helpers + one-click update of the Windows build (verified download, file swap)
  widget.py     notch / floating bar, banner (read along, chimes), tray icon, animations
scripts/render_docs.py   regenerates docs/images/* from the real widget code
tests/test_core.py       pytest suite (no audio/GUI needed)
tests/test_widget.py     widget gestures with real Qt events (offscreen)
tests/browser_sites.test.js  the extension's reply detection, run in Node by test_core
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

CI: `.github/workflows/tests-macos.yml` runs the suite on macOS on every push; `tests-linux.yml` on Ubuntu, then `scripts/linux_smoke.py` against a real PulseAudio server and the notch under Xvfb.
Releases: `.github/workflows/release.yml` builds the exe on every push and publishes `agent-voice-windows-x64.zip` when a `v*` tag is pushed. The exe calls itself through `client.self_command()` (never `python -m agent_voice` directly): `agent-voice.exe` for hooks/CLI, `agent-voice-w.exe` (no console) for the daemon, widget and autostart. Keep the zip small (lingua is excluded; voices and Whisper are downloaded at first run).

The daemon and widget run as `pythonw` in the background. **Code changes need a restart**: `agent-voice restart` (or the tray's *Restart agent-voice*). The widget relaunches itself when it sees the service's `boot` change, so it picks up new code too. Hooks are re-executed per event, so `hooks.py` changes apply immediately.

## Invariants: do not break these

1. **Hooks never break the agent.** `hooks.py`, `client.py`, `textclean.py` (and `schedule.py`, which it uses), `mcp.py` and `native_host.py` stay stdlib-only and fast, catch every exception (logged to `~/.agent-voice/logs/hook-errors.log`) and exit 0. Gemini and Cursor parse stdout as JSON, so print exactly `{}` for them and nothing else; Claude's `UserPromptSubmit` prints only its JSON `hookSpecificOutput`. The MCP server's and the native host's stdout carry their protocol and nothing else (no prints, no logging there).
2. **Spoken once, in order.** One daemon worker (`Daemon.jobs`) processes `/speak` in arrival order. Dedupe of Claude texts happens in `hooks._unspoken` under a cross-process lock file (parallel tool calls fire concurrent hooks). The `Stop` payload's `last_assistant_message` is only a fallback (`_covered`, whitespace-insensitive).
3. **Summaries are decided in the hook.** `hooks.summary_enabled()` reads `state.json` (runtime toggle) then config. Claude's `UserPromptSubmit` hook prints `additionalContext` asking for a `Voice summary:` line; `hooks.summarize()` prefers that line and falls back to the opening sentences. Full-text mode strips the line (`strip_summary`) so it is not read twice.
4. **`final=True` arms auto-listen.** Only end-of-turn events (Claude `Stop`, Codex/Gemini/Cursor payloads) set it. Progress (`PreToolUse`) must not open the mic.
5. **Local API stays locked down.** `_allowed()` in `server.py` rejects any `Origin` header, a `Host` other than `127.0.0.1:<port>`/`localhost:<port>`, non-JSON POSTs, and bodies over 1 MB. Every new endpoint goes through it; never bind to anything but `127.0.0.1` (`serve()` ignores any other `host` in the config: `config.LOOPBACK`). The browser extension never calls the API: it goes through the native host, which only our extension id may start (`allowed_origins`, pinned by the manifest's `key`) and which only accepts the sites in `native_host.SITES`.
6. **The widget never steals focus.** It uses `WS_EX_NOACTIVATE` + `Qt.WindowDoesNotAcceptFocus`; dictation pastes into the user's agent window and depends on this.
7. **The widget is a view.** State lives in the daemon (`/status`). The widget only keeps its own position/visibility (`~/.agent-voice/widget.json`; not `state.json`, which the daemon rewrites wholesale).
8. **Nothing leaves the machine.** No network calls except model downloads (Piper voices, Whisper weights (then loaded with `local_files_only`: no revision check at each start), the wake-word model, Kokoro's files checked against pinned SHA-256 sums in `kokoro.FILES`) on first use, the optional release check (`update_check`, daily), calendar links the user adds (`agenda.fetch`, every 15 min, only links in `state.json`'s `calendars`), phone notifications once the user turns them on (`ntfy.py`: only through `Daemon._push`, gated by `_ntfy_settings()` returning None while off; `ntfy.content` decides how much text), and the release zip when the user clicks *Update* (`updater.py`: the exact asset, size/checksum/CRC verified before anything is swapped; the swap rolls back on failure). `apps.py` reads only the listed apps' notifications (the filter is in the SQL query) and is off by default.
9. **Battery first.** ONNX Runtime sessions we create never busy-wait (Piper's and Kokoro's both go through `tts._quiet_session`: `allow_spinning=0`, 2 threads; the default spinning cost ~20x the CPU per sentence). Feed models whole chunks, not 30 ms blocks. The wake word runs a volume gate first and the keyword spotter only on finished utterances.
10. **The widget doesn't redraw at rest.** `_tick` drops to 4 Hz and skips `update()` once `_is_still()` (the one exception: the working light glides at 15 fps while the notch is down, `_glow_moving()`; the ready rim is still); status polling slows to 0.6 s. Any new animation must be covered by `_is_still()`, or the widget redraws at 30 fps forever. At rest the widget long-polls `/status?wait=<tag>` (one request per change, or every 20 s): keep `server.status_tag` blind to values that change without anything visible changing (mic level, seconds).

## Adding an agent integration

1. In `hooks.py`, make sure `extract_text()` finds the reply (add the field name to `TEXT_KEYS` if needed) and that the payload is read from the right place (stdin vs argv). Call `client.speak(text, source=agent, final=True)`.
2. In `install.py`, add an idempotent installer that strips entries containing `MARKER` before adding its own, backs up the file with `_backup()`, and supports `remove=True`. Register it in `INSTALLERS` and `AGENTS`.
3. Add a test with a sample payload in `tests/test_core.py`; update the README and `docs/guide.md` tables.

`hook_payload` saves the last raw payload per agent to `~/.agent-voice/logs/payload-<agent>.json`: use it to learn a new format.

## Gotchas (learned the hard way)

- **Windows Terminal turns on "win32-input-mode"** for ConPTY children: in `watch`, keys arrive as `ESC [ Vk;Sc;Uc;Kd;Cs;Rc _`; decode them (`WIN32_KEY`) before reading what was typed. ConPTY also waits for the terminal's answer to its `ESC [c` query before drawing anything (a test harness must answer it).
- **Claude Desktop from the Microsoft Store** keeps its settings in `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`; only processes launched by the app see them at `%APPDATA%\Claude` (`install._claude_desktop_dir`).
- **A custom Claude Code status line hides some of its footer hints**: `agent-voice usage on` is opt-in for that reason; context % also comes from the transcript in the `Stop` hook.
- **Heredocs mangle backslashes** in this repo's agent shells (`\n` in a Python string written through `cat <<EOF` came out as a real newline): write code with an editor or a script file.

- **Linux**: windows are X11 window ids (`xdotool`); under Wayland the widget forces `QT_QPA_PLATFORM=xcb` (XWayland), since Wayland won't let a window place itself or stay on top. Keystrokes and hotkeys go through pynput (`keyboard` needs root there). Every Linux tool (`pactl`, `xdotool`, `xclip`, `playerctl`, `import`) is optional: missing, its feature is off, nothing breaks.
- **macOS**: `Qt.Tool` windows hide when another app is active unless `WA_MacAlwaysShowToolWindow` is set; fonts are counted at 72 dpi (`widget._font` scales them); Segoe Fluent Icons doesn't exist (plain symbols instead).
- **PySide6 `QPen` rejects keyword args** (`cap=`, `join=`): use `widget._round_pen()`.
- **ctypes handles must be typed.** Set `restype`/`argtypes` to `wintypes.HWND`/`HRGN` etc., or 64-bit handles are truncated.
- **No acrylic**: the notch, the floating island and the compact orb draw their own black shape (`_body_path`) with DWM rounding off and the system border hidden. If acrylic ever comes back: it only clips to DWM's native rounding (8 px); `SetWindowRgn` does not clip the acrylic accent.
- **The light theme is our own glass** (`Glass`): a grab of the screen behind, shrunk and smoothed (the blur), under a frosted tint. In that theme our windows are excluded from screen captures (`WDA_EXCLUDEFROMCAPTURE`), so they don't appear in their own grabs, nor in screenshots or screen shares. Grabs happen only when an area isn't covered yet or every `Glass.REFRESH` s while still, never per frame (~25 ms each). Never while tucked, never offscreen; `render_docs.py` turns them off.
- **`sounddevice` can hang on a broken audio driver**: playback polls with a deadline instead of `sd.wait()`. Closing the mic stream can take ~1 s, so `cancel_listen()` updates the phase immediately.
- **Claude `async` hooks may be killed on `Stop`**: only `PreToolUse` is async; `Stop` stays synchronous.
- **Whisper hallucinates on silence** ("Thank you." and its French equivalent): filtered by `stt.is_hallucination`.
- **Python 3.14 lacks some wheels** (onnxruntime/ctranslate2 at the time of writing): use 3.13.
- **Global hotkeys (`keyboard`) can't see apps running as administrator** unless the daemon is elevated too.
- **`render_docs.py` replaces `widget.time`** with a fake clock; keep animation code reading time through the module-level `time` import. It and the widget tests point `widget.POS_FILE` at a temporary file: your own `widget.json` (a folded card, a hidden notch) must never change a test or a screenshot.
- **Animations run on real time**: easing factors are tuned per 30 fps frame and go through `_k()` / `_spring()` with the frame's measured `dt`, so a late frame doesn't slow them down. Tests that tick the widget must step its clock (`w._last_tick -= widget.FRAME` before each `_tick()`).
- **Never resize the notch every frame**: resizing a see-through window re-uploads all of it (the jerky opening). The notch's window is a canvas sized once for where an animation is going and trimmed when it settles; the island is drawn at `_vw` x `_vis_h()` inside it, and `visual_rect()` is where it is on screen.
- **Sounds never go through `sounddevice.play()` outside `tts.py`**: it shares one global stream with the voice, so a cue there would cut the speech off. Cues and chimes use `_play_apart` (a stream of their own), built by `tone_audio` (no harmonics above the note, eased in and out). Never `winsound.Beep`.
- **Ducking restores what it lowered**: `Ducker` saves the original volumes to `state.json` (`ducked`) before fading, and the next start puts them back.
- **The daemon is one object in parts** (`agent_voice/daemon/`): a new method goes in the part it belongs to, and any state it needs is set up in `core.Daemon.__init__`. Each part imports its own names, so a test patches `save_state`, `_beep`, `HOME`... with the `patch_daemon` fixture (every part at once), never on one module.
- **Tests never touch `~/.agent-voice`**: `tests/conftest.py` points `AGENT_VOICE_HOME` at a temporary folder before anything is imported. Keep it first.
- **Replies carry `full`** (hooks -> `/speak`): the whole reply when what's read out is only its summary. The preview and the history use it; never speak it.
- **Barge-in listens only in headphones** (`_barge_ok`): through speakers the mic hears the voice and stops it. It needs 300 ms of sound (`start_blocks=10`) to cut in, and hands what you said to `_handle_heard`, the same path as the reply mic.
- **Dictated sends go through `Daemon._send_spoken`** (the take-back: a few seconds under the notch, the mic open for "wait" / "cancel that"). Scheduled prompts, resumes and typed replies call `_paste` directly: never hold those.
- **The side panels are views** (`SidePanel`, "utils" on the right, "companion" on the left; their cards in `SIDE_CARDS`): the state is the daemon's (`companion.Companion` in `state.json`, `media`, `agenda`), changed through `/timer`, `/stopwatch`, `/pomodoro`, `/todo`, `/reminder`, `/note`, `/media`, `/calendar`. Only the clipboard list lives in the widget, in memory, never saved. A test widget must `poller.stop()`: a poller emitting into a deleted widget crashes Python. Times are epoch seconds and the widget counts down itself: `status_tag` never changes per second, and the notch's second line and the panel repaint once a second only while a clock runs (`_strip_key`, `CompanionPanel.clock_key`). Due items wake a thread that sleeps until the next one (`_companion_loop`), never a poll.
- **One home per thing.** The open panel holds what needs you, the threads (each with its own talk, type, replay and copy), the actions that aren't about one thread, and a line for the agents. Every switch lives in the Settings sheet (⚙, `SETTINGS` / `SETTING_ROWS`), your day in the Companion (agenda: meetings, reminders and scheduled prompts in one list; usage; activity; music), your tools in Utils. A new feature goes in one of them, not as a second button somewhere else; the tray, hotkeys and voice are shortcuts.
- **Claude Code's to-do tools come through `claude mcp add`** (`install.claude_mcp`, with `--todo-only`): Claude rewrites `~/.claude.json` itself, so we never edit it. Tests stub `claude_mcp` (`conftest.py`): never run the real `claude` from a test.
- **Window pictures copy the screen** (`winfocus.window_png`, BitBlt with the thread made DPI-aware for the capture): `PrintWindow` gives black for browsers and GPU-drawn apps.
- **Claude's limits come from `claude -p /usage`**: a local Claude Code command (no model request) that refreshes `cachedUsageUtilization` in `~/.claude.json`. Never pass it through Git Bash (it rewrites `/usage` into a path and Claude gets a real prompt); the daemon runs it without a shell. Never read Claude's credentials ourselves.
- **Claude Code's `StopFailure` hook** (matcher `rate_limit`) is how an out-of-quota turn reaches `/limit_hit`; the resume schedule lives in `state.json` (`limited`) so a restart doesn't lose it.

## Style

- Python 3.10+, type hints, `from __future__ import annotations`, f-strings, `pathlib`.
- Comments explain *why*, not what. Keep functions small; match the surrounding code.
- User-facing text is written in English and goes through `i18n.tr()` (`plural()` for counts): add its French to `i18n_fr.FR` (a test fails on any string without it, or with other placeholders). What's shown follows *Interface* (`i18n.LANG`); what the voice says itself follows *Voice & dictation*: `tr(text, in_lang=daemon.language)`, or `Daemon._confirm` / `_say`. Module-level constants are translated where they're shown. Spoken confirmations are short.
- Line length ~120.

## Commits

- Run `pytest` first; for widget changes, re-render and eyeball `docs/images/`.
- Never commit `.venv/`, models (`*.onnx`), audio (`*.wav`), logs, or anything from `~/.agent-voice/` (all in `.gitignore`).
- The repo is GPL-3.0-or-later (Piper is GPL). New dependencies must be GPL-compatible; add them to `pyproject.toml` and to the README credits table.
