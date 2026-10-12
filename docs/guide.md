# agent-voice: user guide

A practical walkthrough: installing, everyday use, tuning, and fixing things when they go wrong.
For a feature overview see the [README](../README.md).

- [1. Install](#1-install)
- [2. Your first conversation](#2-your-first-conversation)
- [3. Everyday use](#3-everyday-use)
- [4. The widget and the tray icon](#4-the-widget-and-the-tray-icon)
- [5. Languages and voices](#5-languages-and-voices)
- [6. Connecting each agent](#6-connecting-each-agent)
- [7. Configuration reference](#7-configuration-reference)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Uninstall](#9-uninstall)

---

## 1. Install

**Requirements:** Windows 11, Python 3.13 (3.10+ works if wheels exist for your version), a microphone, about 1 GB of disk for the models.

```bash
git clone https://github.com/ElMastho/agent-voice.git
cd agent-voice
py -3.13 -m venv .venv
.venv/Scripts/pip install -e .
.venv/Scripts/agent-voice setup
```

`setup` writes `~/.agent-voice/config.json`, downloads the two default voices (~60 MB each) and the Whisper `small` speech-recognition model (~500 MB, first time only).

Connect your agent(s) and start everything:

```bash
.venv/Scripts/agent-voice install claude      # see section 6 for the others
.venv/Scripts/agent-voice autostart on        # launch at Windows sign-in
.venv/Scripts/agent-voice start
```

You should hear nothing yet, but you'll see the **glass widget** near the top-right corner and a small **orb icon** in the notification area (bottom right, next to the clock. Windows may tuck new icons behind the `^` arrow; drag it onto the taskbar to keep it visible).

> **Tip:** `agent-voice say "Hello, this is a sound check."` is the quickest test. Mixed French and English text is read with both voices.

## 2. Your first conversation

1. Open a **new** Claude Code session (hooks are loaded when a session starts).
2. Ask something. While Claude works you'll hear its short progress notes; when it finishes, the full answer.
3. When the voice stops, a **short beep** plays and the widget turns green: **Listening**.
4. Talk. The widget switches to **Hearing you** and follows your voice. Pause for 1.5 s to finish.
5. A second beep, **Transcribing**, and your words appear in Claude's prompt box.
6. Press **Enter** to send, or turn on **auto-send** so it's sent for you.

If you say nothing for 15 s, a low beep closes the mic. Nothing is typed.

## 3. Everyday use

### Answer without waiting: *reply now*
Press **`Ctrl+Alt+A`** (or the ⏭️ button on the widget while it speaks). The voice stops immediately and the mic opens.

### Hands-free: *auto-send*
**`Ctrl+Alt+H`** toggles it; the voice confirms "Hands-free on/off". When on, everything you dictate (auto-listen *and* push-to-talk) is followed by **Enter**. The widget shows an `AUTO` badge while listening, and the *Auto-send* tile is green.

> Leave it off when you want to proofread before sending.

### Push-to-talk in any app
Hold **`Ctrl+Alt+Space`**, talk, release. The text is pasted into whatever window has focus (an editor, a chat, a terminal…). You can also press and hold the **Hold to talk** tile on the widget.

### Stop / mute
- **`Ctrl+Alt+S`**: stop the current speech (and cancel listening).
- **`Ctrl+Alt+M`**: mute. Nothing is spoken and the mic doesn't open after answers. A high beep means on, a low beep means muted.
- In Claude Code, sending a new prompt yourself also stops the voice instantly.

### Summaries instead of long answers
By default, final answers are spoken as **one short sentence**, so a long recap doesn't keep talking for minutes:

- **Claude Code** is asked (via the `UserPromptSubmit` hook) to end each final reply with a line `Voice summary: …`. That's the sentence you hear; the full answer stays on screen.
- **Other agents**, or a reply without that line: the opening sentences of the answer are spoken (up to `summary.max_chars`). To get real summaries from Codex, Gemini CLI or Cursor too, add the same instruction to their project rules (`AGENTS.md`, `GEMINI.md`, `.cursor/rules`): *"End your final reply with a line `Voice summary: <one short sentence>`."*
- Progress messages during the work are not affected.

Toggle it from the tray (*Summarize answers*) or with `agent-voice summary on|off`. The voice confirms "Summaries on" or "Full answers".

### Read anything aloud
Select text in any application and press **`Ctrl+Alt+R`**: it is read in the right language. With nothing selected, the clipboard is read. This is the way to use agent-voice with **Antigravity** or any tool without hooks.

## 4. The widget and the tray icon

| You want to… | Do this |
|---|---|
| See what's happening | Glance at the orb colour: slate *ready*, violet *speaking*, green *listening / hearing you*, amber *transcribing*, red *muted*, grey *offline* |
| See every action | Click the chevron ⌄ |
| Move it | Drag it; it snaps to screen edges and remembers its position |
| Hide / show it | `Ctrl+Alt+W`, click the tray icon, or right-click the widget → *Hide widget* |
| Reach actions without the widget | Right-click the tray icon |
| Start/stop the service | Tray menu, or the ⏻ button when the widget says *Offline* |
| Turn start-with-Windows on/off | Tray menu → *Start with Windows* |
| Get it back after *Quit widget* | `agent-voice widget` (or sign out/in) |

The widget **never takes keyboard focus**: clicking it keeps your agent window active, so dictated text still lands in the right place.

## 5. Languages and voices

- **What you hear**: each sentence is classified French or English and read by that language's voice. Short phrases fall back on common-word hints, then on the reply's dominant language.
- **What you say**: Whisper detects French or English from your voice each time, restricted to those two to avoid mix-ups.

**Change a voice:** browse the [Piper voice catalog](https://huggingface.co/rhasspy/piper-voices), then set it in `~/.agent-voice/config.json`:

```json
{ "voices": { "en": "en_US-ryan-high", "fr": "fr_FR-upmc-medium:pierre" } }
```

`name:speaker` picks a speaker inside multi-speaker models. Missing voices download automatically. Try one before switching:

```bash
agent-voice say --voice en_US-ryan-high "Hello, I am Ryan."
```

**Speed:** `"speed": 0.67` (about 1.5× faster than normal). `1.0` is the natural pace; lower is faster.

## 6. Connecting each agent

| Agent | Command | What it changes |
|---|---|---|
| Claude Code | `agent-voice install claude` | Adds `Stop`, `PreToolUse` (async) and `UserPromptSubmit` hooks to `~/.claude/settings.json` |
| Codex CLI | `agent-voice install codex` | Adds a top-level `notify = [...]` to `~/.codex/config.toml` (skipped if you already have your own `notify`) |
| Gemini CLI | `agent-voice install gemini` | Adds an `AfterAgent` hook to `~/.gemini/settings.json` |
| Cursor | `agent-voice install cursor` | Adds `afterAgentResponse` to `~/.cursor/hooks.json` |
| Cline | `agent-voice install cline` | Writes `UserPromptSubmit`, `PreToolUse` and `Notification` hooks to `~/Documents/Cline/Hooks` (`.ps1` on Windows; existing hooks of your own are left alone). Turn on *Settings → Features → Enable Hooks* in Cline |
| Antigravity | — | Use `Ctrl+Alt+R` on the reply |

Every installer keeps a backup (`*.bak-agent-voice`), can be re-run safely, and is reverted with `agent-voice uninstall <agent>`.
Hooks start the service automatically if it isn't running.

**Claude Code specifics:** progress messages are spoken between tool calls (`"speak_progress": false` to only hear final answers); the window you prompted from is remembered so replies are typed back there even if you clicked elsewhere meanwhile.

## 7. Configuration reference

File: `~/.agent-voice/config.json`. Only put the keys you want to change; they're merged over the defaults. Restart afterwards: `agent-voice shutdown && agent-voice start`.

| Key | Default | Notes |
|---|---|---|
| `voices` | `{"en": "en_US-hfc_male-medium", "fr": "fr_FR-siwis-medium"}` | See section 5 |
| `default_lang` | `"en"` | Used when detection is unsure |
| `speed` | `0.67` | Lower is faster |
| `volume` | `1.0` | |
| `max_chars` | `1500` | Longer replies are cut at a sentence |
| `speak_progress` | `true` | Claude's in-between messages |
| `summary.enabled` | `true` | Speak a one-sentence summary of final answers (runtime toggle overrides) |
| `summary.max_chars` | `280` | Length of the fallback summary |
| `hotkeys` | `stop`, `reply`, `hands_free`, `mute`, `read_selection`, `widget`, `push_to_talk` | Empty string disables one |
| `stt.model` | `"small"` | `tiny` / `base` / `small` / `medium` |
| `stt.languages` | `["fr", "en"]` | Languages Whisper may pick |
| `stt.auto_submit` | `false` | Always press Enter after push-to-talk, even without auto-send |
| `auto_listen.enabled` | `true` | Open the mic after final answers |
| `auto_listen.window_seconds` | `15` | Time to start talking |
| `auto_listen.silence_seconds` | `1.5` | Pause that ends your reply |
| `auto_listen.max_seconds` | `90` | Hard limit |
| `widget.enabled` | `true` | Launch the widget with the service |
| `widget.blur` | `"acrylic"` | `"blur"` or `"none"` if acrylic misbehaves |

Runtime state (mute, auto-send, summary) lives in `~/.agent-voice/state.json`, and the widget position in `~/.agent-voice/widget.json`.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| **No sound at all** | Test with `agent-voice say "test"`. If the volume meter moves but the speakers stay silent, it's the Windows audio device/driver: pick the output device again, restart the *Windows Audio* service, or reboot. |
| **Nothing spoken in Claude** | Open a *new* session (hooks load at start). Check `agent-voice status`. Look in `~/.agent-voice/logs/hook-errors.log`. |
| **Codex / Gemini / Cursor silent** | Check `~/.agent-voice/logs/payload-<agent>.json`: it holds the last payload received, which shows if the agent changed its format. |
| **Hotkeys do nothing in one app** | Windows blocks global hotkeys over apps running *as administrator* unless agent-voice also runs elevated. |
| **Dictation lands in the wrong window** | Replies go to the window you last *prompted from*. Push-to-talk goes to the focused window. |
| **Mic opens but hears nothing / cuts too early** | Raise `auto_listen.silence_seconds`; check the default recording device in Windows sound settings. |
| **Transcription is slow** | `"stt": {"model": "base"}` (faster, slightly worse French). |
| **Widget missing** | `Ctrl+Alt+W`, the tray icon, or `agent-voice widget`. |
| **Widget looks wrong / black** | Set `"widget": {"blur": "blur"}` or `"none"`. |
| **Anything else** | `agent-voice serve` runs in the foreground with logs; the full log is `~/.agent-voice/logs/server.log`. |

## 9. Uninstall

```bash
agent-voice uninstall all
agent-voice autostart off
agent-voice shutdown
```

Then delete the project folder and `~/.agent-voice/` (config, voices, logs). The Whisper model cache is in `~/.cache/huggingface/`.
