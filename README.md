# agent-voice

Local, CPU-only voice for AI coding agents. It reads the agent's replies aloud in **French or English** (detected sentence by sentence) and lets you **answer by voice** with a push-to-talk key. Nothing leaves your machine.

- **Text-to-speech:** [Piper](https://github.com/OHF-Voice/piper1-gpl), about 10× faster than real time on CPU, ~60 MB per voice
- **Speech-to-text:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`small`, int8), French/English auto-detected
- **Language detection:** [lingua](https://github.com/pemistahl/lingua-py), restricted to FR/EN

## Hotkeys (global)

| Keys | Action |
|---|---|
| `Ctrl+Alt+A` | Reply now: cut the voice off and start listening for your answer immediately |
| `Ctrl+Alt+Space` (hold) | Push-to-talk: speak, release, and the text is pasted into the focused window |
| `Ctrl+Alt+S` | Stop the current speech (and cancel listening) |
| `Ctrl+Alt+H` | Hands-free mode on/off: everything you say is sent automatically. Off by default; the voice confirms the switch |
| `Ctrl+Alt+M` | Mute / unmute (high beep = on, low beep = muted) |
| `Ctrl+Alt+R` | Read the selected text aloud (or the clipboard). Works in any app, e.g. Antigravity |

Speech stops automatically when you start push-to-talk.

**Auto-listen:** after an agent's final answer has been spoken, a beep plays and the mic stays open for 15 s. Talk and your reply (French or English, detected from your voice) is typed into the window you last prompted from. A pause of 1.5 s ends it. In hands-free mode it is also sent; otherwise you review it and press Enter. A low beep means nothing was heard.

## Status widget

A small glass panel (Windows acrylic blur), always on top. It starts with the service, sits at the top right by default, and can be dragged anywhere (it snaps to screen edges and remembers its position).

- **Live state:** an animated orb, an aurora and a waveform show what's happening: *Ready*, *Speaking* (with an FR/EN badge), *Listening*, *Hearing you* (follows your mic level), *Transcribing*, *Muted*, *Offline*
- **Main button:** changes with the state: skip and answer while it speaks, cancel while it listens, talk when idle, start the service when offline
- **Chevron:** opens every action with its shortcut: reply now, hold to talk, stop, auto-send, mute, read selection. The last transcription is shown at the bottom
- **Never steals focus:** clicking it keeps your agent window active, so dictated text still lands there
- **Discreet:** it fades when idle; right-click it to move or hide it, or to stop the service

Run `agent-voice widget` to reopen it. Set `"widget": {"enabled": false}` to stop it from launching, or `"blur": "blur"`/`"none"` if acrylic misbehaves on your machine.

## Supported agents

| Agent | How | Install |
|---|---|---|
| Claude Code (CLI, desktop, IDE) | `Stop` + `PreToolUse` hooks. Speaks progress messages between tool calls and the final answer, never twice | `agent-voice install claude` |
| Codex CLI | top-level `notify` in `~/.codex/config.toml` | `agent-voice install codex` |
| Gemini CLI | `AfterAgent` hook in `~/.gemini/settings.json` | `agent-voice install gemini` |
| Cursor | `afterAgentResponse` in `~/.cursor/hooks.json` | `agent-voice install cursor` |
| Antigravity / anything else | select the reply and press `Ctrl+Alt+R`, or pipe text: `echo hi \| agent-voice say` | none needed |

Each installer backs up the file it changes (`*.bak-agent-voice`) and is idempotent; `agent-voice uninstall <agent>` reverts it.
The payload the Codex, Gemini and Cursor hooks last received is saved to `~/.agent-voice/logs/payload-<agent>.json`, which helps if one of them changes its format.

## Setup

```bash
py -3.13 -m venv .venv
.venv/Scripts/pip install -e .
.venv/Scripts/agent-voice setup              # config, voices, whisper model
.venv/Scripts/agent-voice install claude     # or: codex gemini cursor / all
.venv/Scripts/agent-voice autostart on       # start hidden at Windows login
.venv/Scripts/agent-voice start
```

Python 3.13 is used because some audio/ML wheels lag behind the newest Python.

## Commands

`start`, `shutdown`, `status`, `widget`, `say [text] [--lang fr|en]`, `stop`, `mute [on|off|toggle]`, `unmute`, `handsfree [on|off|toggle]`, `install`, `uninstall`, `autostart on|off`, `setup`, `serve` (foreground, for debugging).

## Configuration: `~/.agent-voice/config.json`

- `voices`: Piper voice per language ([catalog](https://huggingface.co/rhasspy/piper-voices)); missing voices are downloaded automatically
- `speed`: `1.0` is normal; lower is faster
- `max_chars`: long replies are cut at a sentence boundary
- `speak_progress`: also speak Claude's messages between tool calls
- `hotkeys`: any [keyboard](https://github.com/boppreh/keyboard) combo
- `stt.model`: `base` is faster, `small` is better in French
- `auto_listen`: `enabled`, `window_seconds`, `silence_seconds`, `max_seconds`

Restart the daemon after editing (`agent-voice shutdown && agent-voice start`). Logs: `~/.agent-voice/logs/server.log`.

## How it works

```
agent hook ──POST /speak──▶ daemon (127.0.0.1:59125)
                             ├─ clean markdown (code blocks, links, paths, tables)
                             ├─ split into sentences, detect FR/EN
                             ├─ Piper synth thread ─▶ playback thread (next sentence synthesized while one plays)
                             └─ global hotkeys, push-to-talk ─▶ Whisper ─▶ paste
```

Hooks are stdlib-only, fail silently, and start the daemon if it isn't running.
