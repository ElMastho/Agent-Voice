# agent-voice: user guide

A practical walkthrough: installing, everyday use, tuning, and fixing things when they go wrong.
For a feature overview see the [README](../README.md).

- [1. Install](#1-install)
- [2. Your first conversation](#2-your-first-conversation)
- [3. Everyday use](#3-everyday-use)
- [4. The notch and the tray icon](#4-the-notch-and-the-tray-icon)
- [5. Languages and voices](#5-languages-and-voices)
- [6. Connecting each agent](#6-connecting-each-agent)
- [7. Configuration reference](#7-configuration-reference)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Uninstall](#9-uninstall)

---

## 1. Install

**Requirements:** Windows 11, a microphone, about 1 GB of disk for the models.

**The easy way:** download `agent-voice-windows-x64.zip` from the [latest release](https://github.com/ElMastho/agent-voice/releases/latest), extract it (e.g. `C:\Tools\agent-voice`) and double-click `agent-voice.exe`. It downloads the models, connects the agents it finds and starts. Skip to section 2.

**From source** (Python 3.13; 3.10+ works if wheels exist for your version):

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

You should hear nothing yet. At the top centre of the screen there's a thin black line: that's the **notch**, tucked away. Touch the top edge with the mouse and it drops down. There's also a small **orb icon** in the notification area (bottom right, next to the clock; Windows may tuck new icons behind the `^` arrow, drag it onto the taskbar to keep it visible).

> **Tip:** `agent-voice say "Hello, this is a sound check."` is the quickest test. Mixed French and English text is read with both voices.

## 2. Your first conversation

1. Open a **new** Claude Code session (hooks are loaded when a session starts).
2. Ask something. The notch drops down: **Working**, the current step (*Edit widget.py*), how many steps and a timer. You'll also hear Claude's short progress notes.
3. When it finishes, you hear the answer (a one-sentence summary by default) and read it under the notch as it's spoken.
4. When the voice stops, a **short beep** plays and the notch turns green: **Listening**.
5. Talk. It switches to **Hearing you** and follows your voice. Pause for 1.5 s to finish.
6. A second beep, **Transcribing**, and your words appear in that conversation's prompt box.
7. What you said also drops down under the notch, framed in green, with where it went (*sent* or *typed*).
8. Press **Enter** to send, or turn on **auto-send** so it's sent for you.

If you say nothing for 15 s, a low beep closes the mic. Nothing is typed.

## 3. Everyday use

### Answer without waiting: *reply now*
Press **`Ctrl+Alt+A`** (or the ⏭️ button on the widget while it speaks). The voice stops immediately and the mic opens.

### Hands-free: *auto-send*
**`Ctrl+Alt+H`** toggles it; the voice confirms "Auto-send on/off". When on, everything you dictate (auto-listen *and* push-to-talk) is followed by **Enter**. The notch shows an *Auto* chip, and the *Auto-send* tile is green.

> Leave it off when you want to proofread before sending.

### Push-to-talk in any app
Hold **`Ctrl+Alt+Space`**, talk, release. The text is pasted into whatever window has focus (an editor, a chat, a terminal…). You can also press and hold the **Hold to talk** tile in the notch's panel.

### Voice commands
In the reply window, say on its own *"repeat that"*, *"slower"*, *"faster"* or *"never mind"* (*"répète"*, *"plus lentement"*, *"plus vite"*, *"annule"*): agent-voice acts on it and nothing is sent to the agent.

### Stop, and the three modes
- **`Ctrl+Alt+S`**: stop the current speech (and cancel listening).
- In Claude Code, sending a new prompt yourself also stops the voice instantly.
- **Mode** (in the panel): **Voice**, **Chimes** (`Ctrl+Alt+C`: a soft sound when the agent is done or needs you) or **Silent** (`Ctrl+Alt+M`: no sound at all). In Chimes and Silent nothing talks, the mic doesn't open after answers, and each finished reply drops down under the notch as a **banner** for about 5 seconds (hover to keep it, swipe it up to dismiss it, click it to open the conversation).
- **Calls**: while another app holds the microphone (Teams, Zoom, Meet in a browser...), agent-voice switches to chimes on its own and the notch says *In a call*; it goes back to the voice when the call ends. Pick *Voice* during the call to keep the voice.

### Approvals and questions
- When Claude Code asks permission to run a command or edit a file, it's read out and shown in the notch with *Allow once* / *Deny*. Say *"yes"* / *"no"* (*"oui"* / *"non"*) or click. No answer within a minute: Claude shows its own dialog as usual. Turn it off with the *Approvals* tile.
- When an agent asks you to choose (Claude Code's questions, Cline's follow-ups), the options are read out and listed: click one or say *"the second one"*, *"option 3"* or the option's name.

### Several conversations at once
Each conversation gets a colour, its own voice pitch (the first keeps the natural voice, the next ones are a little lower or higher) and remembers the window it was prompted from, so replies never land in the wrong terminal. When one finishes while another is talking or waiting for your reply, it waits its turn and is announced by name. The panel's **Threads** list shows what each one is doing and the start of its last reply, with replay and copy buttons; click a thread to open it.

### "Hey Claude"
Turn on the *Hey Claude* tile (or `agent-voice wake on`). While idle, *"Hey Claude, run the tests"* is sent to your last conversation as a new prompt; *"Hey Claude"* alone opens the mic. It's off by default; on battery it costs next to nothing (a volume gate, then a tiny keyword model only while someone speaks).

### Summaries instead of long answers
By default, final answers are spoken as **one short sentence**, so a long recap doesn't keep talking for minutes:

- **Claude Code** is asked (via the `UserPromptSubmit` hook) to end each final reply with a line `Voice summary: …`. That's the sentence you hear; the full answer stays on screen.
- **Other agents**, or a reply without that line: the opening sentences of the answer are spoken (up to `summary.max_chars`). To get real summaries from Codex, Gemini CLI or Cursor too, add the same instruction to their project rules (`AGENTS.md`, `GEMINI.md`, `.cursor/rules`): *"End your final reply with a line `Voice summary: <one short sentence>`."*
- Progress messages during the work are not affected.

Toggle it with the *Summaries* tile, the tray (*Summarize answers*) or `agent-voice summary on|off`. The voice confirms "Summaries on" or "Full answers". It also decides what the Chimes/Silent banner shows.

### Read anything aloud
Select text in any application and press **`Ctrl+Alt+R`**: it is read in the right language. With nothing selected, the clipboard is read. This is the way to use agent-voice with **Antigravity** or any tool without hooks.

## 4. The notch and the tray icon

| You want to… | Do this |
|---|---|
| See what's happening | The notch drops down by itself: *Working* (with the step and a timer), *Speaking* (violet edge light), *Listening* / *Hearing you* (green), *Transcribing* (amber) |
| Bring it down at rest | Touch the top edge of the screen with the mouse |
| See every action | Click the chevron ⌄: actions, speed, mode, threads, what you said, agents |
| Read what's being said | It hangs under the notch while it's spoken; swipe it up to hide it (`read_along: false` turns it off) |
| Connect another agent | Expand the notch and click it in the **Agents** row (a green dot means connected); **?** opens this guide |
| Have it floating instead | Right-click → *Float the widget again*: the same island, draggable (it snaps to screen edges and remembers its position); *Compact* shrinks it to a black orb |
| Hide / show it | `Ctrl+Alt+W`, click the tray icon, or right-click → *Hide widget* |
| Reach actions without the widget | Right-click the tray icon |
| Start/stop the service | Tray menu, or the ⏻ button when the widget says *Offline* |
| Turn start-with-Windows on/off | Tray menu → *Start with Windows* |
| Get it back after *Quit widget* | `agent-voice widget` (or sign out/in) |

The notch **never takes keyboard focus**: clicking it keeps your agent window active, so dictated text still lands in the right place. Tooltips open just below it.

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
| Claude Code | `agent-voice install claude` | Adds `Stop`, `PreToolUse` (async), `UserPromptSubmit`, `Notification` (async) and `PermissionRequest` hooks to `~/.claude/settings.json` |
| Codex CLI | `agent-voice install codex` | Adds a top-level `notify = [...]` to `~/.codex/config.toml` (skipped if you already have your own `notify`) |
| Gemini CLI | `agent-voice install gemini` | Adds an `AfterAgent` hook to `~/.gemini/settings.json` |
| Cursor | `agent-voice install cursor` | Adds `afterAgentResponse` to `~/.cursor/hooks.json` |
| Cline | `agent-voice install cline` | Writes `UserPromptSubmit`, `PreToolUse` and `Notification` hooks to `~/Documents/Cline/Hooks` (`.ps1` on Windows; existing hooks of your own are left alone). Turn on *Settings → Features → Enable Hooks* in Cline |
| Antigravity | — | Use `Ctrl+Alt+R` on the reply |

Or expand the notch and click the agent in the **Agents** row. Every installer keeps a backup (`*.bak-agent-voice`), can be re-run safely, and is reverted with `agent-voice uninstall <agent>`.
Hooks start the service automatically if it isn't running.

**Claude Code specifics:** progress messages are spoken between tool calls (`"speak_progress": false` to only hear final answers); each tool call updates the notch's live activity; the window and conversation you prompted from are remembered, so replies are typed back there even if you clicked elsewhere meanwhile.

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
| `read_along` | `true` | Show the text being spoken under the notch |
| `thread_voices` | `true` | One voice pitch per conversation |
| `auto_chime_in_calls` | `true` | Chimes while another app uses the microphone |
| `permissions.enabled` / `wait_seconds` | `true` / `60` | Approvals by voice and in the notch, and how long Claude waits |
| `wake_word.phrases` | `["hey claude", ...]` | Phrases Whisper accepts as the wake word |
| `background_send` | `true` | Claude desktop app: send replies without bringing it forward |
| `update_check` | `true` | Look for a newer release at start |
| `chime.volume` | `0.35` | Chime loudness |
| `hotkeys` | `stop`, `reply`, `hands_free`, `mute`, `chime`, `read_selection`, `widget`, `push_to_talk` | Empty string disables one |
| `stt.model` | `"small"` | `tiny` / `base` / `small` / `medium` |
| `stt.languages` | `["fr", "en"]` | Languages Whisper may pick |
| `stt.auto_submit` | `false` | Always press Enter after push-to-talk, even without auto-send |
| `auto_listen.enabled` | `true` | Open the mic after final answers |
| `auto_listen.window_seconds` | `15` | Time to start talking |
| `auto_listen.silence_seconds` | `1.5` | Pause that ends your reply |
| `auto_listen.max_seconds` | `600` | Hard limit |
| `widget.enabled` | `true` | Launch the widget with the service |

Runtime state (mode, auto-send, summary, speed, wake word, approvals) lives in `~/.agent-voice/state.json`, and the notch/bar choice and position in `~/.agent-voice/widget.json`.

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
| **Notch missing** | Touch the top edge of the screen; else `Ctrl+Alt+W`, the tray icon, or `agent-voice widget`. |
| **Notch stays down / says "Working"** | It shows live activity until the agent's turn ends (or 15 minutes without a step). |
| **It went quiet during a meeting** | That's call detection: another app is using the mic. The notch says *In a call*; pick *Voice* to override, or set `auto_chime_in_calls` to `false`. |
| **Anything else** | `agent-voice serve` runs in the foreground with logs; the full log is `~/.agent-voice/logs/server.log`. |

## 9. Uninstall

```bash
agent-voice uninstall all
agent-voice autostart off
agent-voice shutdown
```

Then delete the project folder and `~/.agent-voice/` (config, voices, logs). The Whisper model cache is in `~/.cache/huggingface/`.
