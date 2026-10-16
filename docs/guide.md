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

**Requirements:** Windows 11 (or macOS, in beta, from source), a microphone, about 1 GB of disk for the models.

**The easy way:** download `agent-voice-windows-x64.zip` from the [latest release](https://github.com/ElMastho/agent-voice/releases/latest), extract it (e.g. `C:\Tools\agent-voice`) and double-click `agent-voice.exe`. It downloads the models, connects the agents it finds and starts. Skip to section 2.

**From source** (Python 3.13; 3.10+ works if wheels exist for your version):

```bash
git clone https://github.com/ElMastho/agent-voice.git
cd agent-voice
py -3.13 -m venv .venv
.venv/Scripts/pip install -e .
.venv/Scripts/agent-voice setup
```

On **macOS**, use `python3.13 -m venv .venv` and `.venv/bin/...`, and allow your terminal in *System Settings → Privacy & Security → Accessibility* (global shortcuts, and typing your replies into other apps). The notch hangs from the top of the screen, around the camera notch on MacBooks.

`setup` writes `~/.agent-voice/config.json`, downloads the two default voices (~60 MB each) and the Whisper `small` speech-recognition model (~500 MB, first time only).

Connect your agent(s) and start everything:

```bash
.venv/Scripts/agent-voice install claude      # see section 6 for the others
.venv/Scripts/agent-voice autostart on        # launch at Windows sign-in
.venv/Scripts/agent-voice start
```

You should hear nothing yet. At the top centre of the screen there's a thin black line: that's the **notch**, tucked away. Touch the top edge with the mouse and it drops down. There's also a small **orb icon** in the notification area (bottom right, next to the clock; Windows may tuck new icons behind the `^` arrow, drag it onto the taskbar to keep it visible).

> **Tip:** `agent-voice say "Hello, this is a sound check."` is the quickest test. Mixed French and English text is read with both voices.

### Updating

agent-voice checks GitHub for a new release when it starts and once a day after that (`update_check: false` turns it off). A new one shows as an **Update** chip in the notch and an item at the top of the tray menu.

- **Windows download**: click the chip or the tray item. It downloads the release's zip, verifies it (exact asset name and link, size, checksum, every file's CRC), extracts it to a temp folder and starts the new version's updater from there. That updater stops the service and the notch, replaces the files in your agent-voice folder and starts it again; a notification under the notch says *Updated to version X* or why it didn't. Nothing in `~/.agent-voice` (settings, voices, logs) is touched, and the agents stay connected because the folder doesn't move. A failed download changes nothing; a failed swap puts the old files back. Details are in `~/.agent-voice/logs/update.log`.
- **From source**: the chip tells you to `git pull`, `.venv/Scripts/pip install -e .`, then `agent-voice shutdown` and `agent-voice start`.
- **By hand**: `agent-voice.exe shutdown`, tray → *Quit widget*, then extract the new zip over the folder, double-click `agent-voice.exe`.

### Starting, restarting, quitting
agent-voice is in the **Start menu** (it adds itself the first time it runs; `agent-voice shortcut on` if you removed it): search for *agent-voice* and it opens, starting the service if needed. The tray icon's menu has **Restart agent-voice** (after changing the config, or if anything looks stuck: the notch comes back with it) and **Quit agent-voice**. Same in a terminal: `agent-voice restart`, `agent-voice shutdown`, `agent-voice open`.

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
- **Mode** (in the panel): **Voice**, **Chimes** (`Ctrl+Alt+C`: a soft sound when the agent is done or needs you) or **Silent** (`Ctrl+Alt+M`: no sound at all). In Chimes and Silent nothing talks, the mic doesn't open after answers, and each finished reply drops down under the notch as a **banner** for about 10 seconds, longer for long replies (hover to keep it, swipe it up to dismiss it, click it to open the conversation).
- **Calls**: while another app holds the microphone (Teams, Zoom, Meet in a browser...), agent-voice switches to chimes on its own and the notch says *In a call*; it goes back to the voice when the call ends. Pick *Voice* during the call to keep the voice.

### Approvals and questions
- When Claude Code asks permission to run a command or edit a file, it's read out and shown in the notch with *Allow once* / *Deny*. Say *"yes"* / *"no"* (*"oui"* / *"non"*) or click. No answer within a minute: Claude shows its own dialog as usual. Turn it off with the *Approvals* tile.
- When an agent asks you to choose (Claude Code's questions, Cline's follow-ups), the options are read out and listed: click one or say *"the second one"*, *"option 3"* or the option's name.

### Everything that needs you, in one place
Approvals, questions and agents waiting for input, from every agent, are counted in the notch (*2 need you*) and listed at the top of the panel:

- **Approvals** (Claude Code; and any agent that asks through agent-voice): *Allow* or *Deny* with a click. When several agents ask at once, the first one is read out and the others wait in the list; answer any of them by voice by naming it: *"approve Codex"*, *"deny the notch one"*, *"yes"*. With several waiting they're numbered: *"approve two"*, *"deny the first one"*, *"approve all"* (*"tout"*, *"les deux"*). With the reply mic closed, *"Hey Claude, approve Codex"* works too (wake word on).
- **Out of quota**: *API refactor: Out of quota · resumes 15:20*. See [Out of quota](#out-of-quota-it-picks-up-again-on-its-own).
- **Waiting for you** (Gemini CLI asking to confirm a tool, Claude Code idle on your input, a terminal agent's *(y/n)*, an MCP app's `notify`): *Open* brings its window to the front.

### Out of quota: it picks up again on its own
When a thread hits its 5-hour or weekly limit, it joins *Needs you* with the time it resets. A minute after that, agent-voice sends it **"Continue."** (`resume.message`) in its own window, so the work goes on while you're away. That's on by default (`resume.auto`); click *✓ Resume* to cancel it for that thread. With `resume.auto: false` it asks instead: click *Resume*, or say *"resume when it resets"* (*"reprends quand la limite est levée"*).

How it knows: Claude Code's `StopFailure` hook (a turn that ends on `rate_limit`; added by `agent-voice install claude`), the limit message itself in any agent's reply (*"5-hour limit reached ∙ resets 3pm"*, *"Try again at 3:45 PM"*), or a window at 100% in **Limits** (Codex). When nothing says when it resets, it tries every 30 minutes until the thread answers. The schedule survives a restart.

### Focus: calls, presentations, full screen
In a call (another app on the mic), while presenting, or with an app full screen (a video, a game, a slideshow: the same signals Windows uses for its own notifications), agent-voice **holds replies**: no voice, no banner, no chime. Approvals, questions and agents waiting for input still come through. When it ends, the *While you were away* banner tells you what finished. The notch shows *Focus* (or *In a call*), and the panel's **Today** header says why. Choose Voice in the panel to hear replies anyway until it ends. `focus.full_screen: false` ignores full screen; `focus.calls: "chimes"` keeps chimes during calls instead of holding.

### Today, at a glance
Open the panel: on a wide enough screen the notch takes a third of it, with a second column. **Today** counts the turns, the threads and the tokens since midnight (Claude Code's, from its transcripts: input, output and newly cached; not the context it re-reads each turn), with the turns by hour. Below: **Limits**, what you said, and your agents.

**Rest the mouse on a thread** for half a second: its last reply opens beside the panel in full, formatted (code blocks, lists, tables), with what the turn changed, its tokens today and its context. Scroll it with the wheel; it goes when the mouse leaves.

### The daily recap
At 18:00 (`recap.at`), once a day and only if something happened, a violet *Today* banner sums up the day: turns and tokens, each thread with its last summary, what's still waiting on you, and where your limits are; in Voice mode it's also said in a sentence or two. If you're away or in focus at that time, it waits for you. Ask for it any time: *"Hey Claude, daily recap"*, *"what did I do today?"*, *"résumé de la journée"*, the tray's *Today's recap*, or `agent-voice recap` in a terminal.

### Your video turns down while it talks
On Windows, while the voice speaks, every other app (a YouTube video, Spotify, a game) fades to 30% of its own volume and comes back when the voice is done. While you reply, they go quiet entirely and a video or song that's playing is **paused** (the media key, which browsers and players obey), then played again when you're done. Only what agent-voice paused is resumed; if you change an app's volume in the meantime, it's yours and stays as you set it. If agent-voice ever stops mid-way, the next start puts the volumes back. Settings: `ducking.voice` (0 to 1), `ducking.listen`, `ducking.pause_media`, or `ducking.enabled: false`.

### Your phone (ntfy)
Off until you turn it on. Click the **Phone** tile (or `agent-voice phone on`): a card with a QR code appears in the panel. Install the free [ntfy](https://ntfy.sh) app (iPhone or Android; the web works too), then in the app tap **+** (*Subscribe to topic*) and scan the QR code or paste the link (click the link under it in the card to copy it). Scanning with the phone's camera instead opens the channel in the browser, which doesn't subscribe the app. Press **Test** to check. **⌃** folds the card to one line once you're set (**⌄** shows the QR code again); the *Phone* button turns notifications off.

What reaches your phone, by default only while you're away from the PC (2 minutes without input, in focus, or away; `ntfy.when: "always"` for always): what needs you (approvals at high priority, questions, agents waiting), finished replies with their one-line summary, limits hit and resumed, scheduled prompts sent, and the daily recap. Events close together come as one notification. Choose with `ntfy.events`, and how much text with `ntfy.content`: `"titles"` (just *"Codex finished."*), `"summary"` (default), or `"full"`.

**Privacy**: this is the one feature that sends something over the internet. It goes over HTTPS to the ntfy server (`ntfy.sh`, or yours with `ntfy.server` and `ntfy.token`), then to your phone. The QR code holds a long random channel name: anyone with it can read your notifications, so keep it private; **New link** makes a new one and the old one stops getting anything. On ntfy.sh, channels aren't protected by a password and messages stay on the server for a few hours; run your own ntfy server for full control. Your audio, prompts, files and transcripts are never sent.

### Scheduled prompts
Send a prompt later, to a thread or as a new thread:

- **By voice**: *"Hey Claude, at 2 pm tell Codex to run the tests"*, *"in 20 minutes, new thread: add a health check"*, *"tomorrow at 9, review this"* (the current thread), *"à 14h dis à Codex de lancer les tests"*.
- **From the reply box**: type the prompt, put a time in **When** (*14:00*, *2pm*, *in 30 min*, *tomorrow 9am*, *demain 9h*), press Enter. In the new-thread box, the new thread starts then.
- **From a terminal**: `agent-voice schedule "Run the tests" --at 14:00 --thread Codex`, `--new --agent claude-app --cwd C:\work`, `--repeat daily` or `weekdays`; `--list`, `--cancel ID`.

They wait in the panel's **Upcoming** list (× cancels one) and survive restarts. At that time the prompt is typed into the thread's own window and sent (saved prompts expand), and your phone hears about it if that's on. If the PC was off and it's more than 6 hours late, you're told and nothing is sent.

### Hand your selection or a screenshot to a thread
Select some text (code, an error, a paragraph) in any app, or copy a screenshot (`Win+Shift+S`), then say *"Hey Claude, send this to Codex"*. It goes to that thread's message box, without being sent, so you can add your question; say *"... and ask it to explain it"* to send it at once. Screenshots are saved to `~/.agent-voice/shots/` (the last 20 are kept) and passed by path. Also *"give the screenshot to the notch redesign"*, *"envoie ça à Codex"*.

### Saved prompts
Prompts you use all the time get a short name: *"Hey Claude, run the tests"* sends *"Run the test suite and fix anything that fails."* to the current thread; *"tell Codex to commit this"* to Codex; *"review this"* said in the reply window, or typed in the reply box, works too. Edit or add them in `shortcuts` (`{"the name you say": "the prompt sent"}`); the defaults are *run the tests*, *commit this*, *review this*, *explain that*, *lance les tests* and *fais un commit*.

### Start a new thread
Press **`Ctrl+Alt+N`** (or *+ New* in the panel's Threads header, or the tray menu): a box opens under the notch.

- **Where it runs**: *Claude app* starts a new Claude Code session in the desktop app (in the group of the folder below, like its "New session in …" button; the app doesn't come forward), *Claude Code*, *Codex* or *Gemini CLI* open a new Windows Terminal tab running that agent in the folder. Only what's installed is offered; the default is where your last conversation was.
- **Folder**: the one you last prompted from. **Folders** next to it lists the ones you worked in lately, and *Browse…* opens the folder picker; typing a path completes it as you go.
- **The first prompt**: type it and press Enter, or press **Talk** and say it.

By voice, with the wake word on: *"Hey Claude, new thread: add a health check"*, *"start a new conversation with Codex to …"*, *"nouvelle session : …"*. The new thread joins the Threads list as soon as it starts.

### Messages to any conversation, by name
*"Hey Claude, tell Codex to run the tests"*, or in the reply window *"tell the notch redesign to commit"* (*"dis à Codex de lancer les tests"*), types *"Run the tests"* into that conversation's own window, sends it, and brings you back to what you were doing. The name is the conversation's title or its agent (*Codex*, *Gemini*, *Aider*...). agent-voice needs to know the window first: for Claude Code that's the one you prompted from; terminal agents (Codex, Gemini CLI, `agent-voice watch`) are found from their terminal.

### While you were away
After 5 minutes without touching the keyboard or mouse (`catch_up.away_minutes`), or once a call ends, agent-voice notes what happens; when you're back, a violet banner sums it up (*"Notch redesign and API refactor finished; Codex needs you."*) and, in Voice mode, says it.

### What a turn changed, and how much context is left
When a Claude Code conversation finishes, the notch says *Done · 3 files changed · tests passed* (the files it edited, and its last test run: pytest, npm test, cargo test, go test...). Click it to open the conversation. Each thread in the panel shows its last result and how much of its context is used; the notch warns with *Context 86%* past 80%.

The panel's **Limits** section shows each agent's **5-hour** and **weekly** limits (Pro, Max and ChatGPT plans) as bars, with when they reset; a note drops down once when one passes 90%. They're kept across restarts.

- **Claude** (in a terminal and in the desktop app alike): every 10 minutes while you're using Claude, and once at start, agent-voice runs **`claude -p /usage`**, Claude Code's own usage command. It's local: no model request, nothing typed anywhere; Claude Code fetches your usage with your login, as its Usage page does, and saves it, and agent-voice reads that. The numbers are the ones on Claude's Usage page (which also counts claude.ai). Between two readings, the bars follow your requests from the token counts in your local transcripts (`~/.claude/projects`), weighted by model; those are marked *≈*. A terminal session's status line (`agent-voice usage on`, or *Turn on* in the row) reports exact numbers too. `limits.claude_refresh_minutes` sets how often (`0`: never).
- **Codex** writes them in its session logs (`~/.codex/sessions`): they show after its first turn, nothing to set up.
- **Any other agent or script** can report its own: write `~/.agent-voice/usage/<agent>.json` with `{"agent": "Name", "five_hour": {"pct": 42, "resets": 1792188000}, "seven_day": {"pct": 69, "resets": 1792605600}}` (resets in epoch seconds; files older than 12 hours are ignored). It shows up within a minute.
- **Gemini CLI and Antigravity** don't expose their quotas, so they aren't shown.

Click **↻** next to *Limits* to refresh them all at once (it turns while Claude Code answers).

### Drop files on the notch
Drag a screenshot, a log or any file onto the notch (it drops down as you get near the top edge): its path is typed into the last conversation's prompt, not sent, so you can add your question. Drop it on a thread in the open panel to pick the conversation. An image dragged from a browser is saved to `~/.agent-voice/drops/` first.

### Several conversations at once
Each conversation gets a colour, its own voice pitch (the first keeps the natural voice, the next ones are a little lower or higher) and remembers the window it was prompted from, so replies never land in the wrong terminal. When one finishes while another is talking or waiting for your reply, it waits its turn and is announced by name. The panel's **Threads** list shows what each one is doing and the start of its last reply, with replay and copy buttons; click a thread to open it. With two or more live, the notch shows **a dot per conversation** on its right: filled while it works or talks, ringed in amber when it waits on you, dimmed once done; click a dot to open it.

### "Hey Claude"
Turn on the *Hey Claude* tile (or `agent-voice wake on`). While idle, *"Hey Claude, run the tests"* is sent to your last conversation as a new prompt; *"Hey Claude"* alone opens the mic. It's off by default; on battery it costs next to nothing (a volume gate, then a tiny keyword model only while someone speaks).

### Summaries instead of long answers
By default, final answers are spoken as **one short sentence**, so a long recap doesn't keep talking for minutes:

- **Claude Code** is asked (via the `UserPromptSubmit` hook) to end each final reply with a line `Voice summary: …`. That's the sentence you hear; the full answer stays on screen.
- **Other agents**, or a reply without that line: the opening sentences of the answer are spoken (up to `summary.max_chars`). To get real summaries from Codex, Gemini CLI or Cursor too, add the same instruction to their project rules (`AGENTS.md`, `GEMINI.md`, `.cursor/rules`): *"End your final reply with a line `Voice summary: <one short sentence>`."*
- Progress messages during the work are not affected.

Toggle it with the *Summaries* tile, the tray (*Summarize answers*) or `agent-voice summary on|off`. The voice confirms "Summaries on" or "Full answers". It also decides what the Chimes/Silent banner shows.

### Read anything aloud
Select text in any application and press **`Ctrl+Alt+R`**: it is read in the right language. With nothing selected, the clipboard is read.

## 4. The notch and the tray icon

| You want to… | Do this |
|---|---|
| See what's happening | The notch drops down by itself: *Working* (with the step and a timer), *Speaking* (violet edge light), *Listening* / *Hearing you* (green), *Transcribing* (amber) |
| Bring it down at rest | Touch the top edge of the screen with the mouse |
| Get it out of the way while an agent works | The **—** button: tucked away until there's news (a voice, a question, something that needs you, a new reply) |
| Answer a conversation later, by voice or typing | Its thread row in the panel: 🎙️ talks to it (sent when you pause), ⌨️ opens a box to type to it |
| Answer a notification by typing | **Reply** on the banner: type, Enter sends it to that conversation (Shift+Enter for a new line, Esc to close); **Talk** opens the mic for it |
| See every action | Click the chevron ⌄: what needs you, actions, speed, mode, threads, what you said, agents |
| See who's working | A dot per live conversation on the right; hover for its name, click to open it |
| Answer an approval | *Allow* / *Deny* at the top of the panel, or say *"approve Codex"* |
| Send files to a conversation | Drop them on the notch, or on a thread in the panel |
| Read what's being said | It hangs under the notch while it's spoken; swipe it up to hide it (`read_along: false` turns it off) |
| Connect another agent | Expand the notch and click it in the **Agents** row: a green dot means connected, a hollow one installed but not connected (click to connect), an amber one (the browser) means the bridge waits for the extension to be loaded; a message under the notch says what happened and what to do next. **?** opens this guide |
| Have it floating instead | Right-click → *Float the widget again*: the same island, draggable (it snaps to screen edges and remembers its position); *Compact* shrinks it to a black orb |
| Hide / show it | `Ctrl+Alt+W`, click the tray icon, or right-click → *Hide widget* |
| Reach actions without the widget | Right-click the tray icon |
| Start/stop the service | Tray menu, or the ⏻ button when the widget says *Offline* |
| Turn start-with-Windows on/off | Tray menu → *Start with Windows* (*Start with macOS* on a Mac) |
| Hear ChatGPT / Copilot desktop apps | Tray menu → *Read ChatGPT / Copilot notifications* |
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
| Codex CLI | `agent-voice install codex` | Adds a top-level `notify = [...]` (skipped if you already have your own) and an `[mcp_servers.agent-voice]` table to `~/.codex/config.toml` |
| Gemini CLI | `agent-voice install gemini` | Adds `AfterAgent` and `Notification` hooks and the MCP server to `~/.gemini/settings.json` |
| Cursor | `agent-voice install cursor` | Adds `afterAgentResponse` to `~/.cursor/hooks.json` and the MCP server to `~/.cursor/mcp.json` |
| Cline | `agent-voice install cline` | Writes `UserPromptSubmit`, `PreToolUse` and `Notification` hooks to `~/Documents/Cline/Hooks` (`.ps1` on Windows; existing hooks of your own are left alone). Turn on *Settings → Features → Enable Hooks* in Cline |
| Antigravity | `agent-voice install antigravity` | Adds the MCP server to its `mcp_config.json` (`~/.gemini/antigravity/` and/or `~/.gemini/config/`) |
| Claude Desktop | `agent-voice install claude-desktop` | Adds the MCP server to `claude_desktop_config.json` (the Microsoft Store version keeps it in its package folder: found automatically). Restart the app |
| Windsurf | `agent-voice install windsurf` | Adds the MCP server to `~/.codeium/windsurf/mcp_config.json` |
| VS Code | `agent-voice install vscode` | Adds the MCP server to your user `mcp.json` (Copilot's agent mode can then use it) |
| Browser | `agent-voice install browser` | Registers the native messaging bridge for Chrome, Edge, Brave and Chromium; then load the extension (below) |
| Terminal agents | `agent-voice watch <command>` | Nothing: run the agent through it |
| ChatGPT / Copilot / Perplexity apps | `agent-voice apps on` | Nothing: reads their notifications |

Or expand the notch and click the agent in the **Agents** row. Every installer keeps a backup (`*.bak-agent-voice`), keeps your other settings and MCP servers, can be re-run safely, and is reverted with `agent-voice uninstall <agent>`.
Hooks and the MCP server start the service automatically if it isn't running.

**Claude Code specifics:** progress messages are spoken between tool calls (`"speak_progress": false` to only hear final answers); each tool call updates the notch's live activity; the window and conversation you prompted from are remembered, so replies are typed back there even if you clicked elsewhere meanwhile.

### MCP apps (Antigravity, Claude Desktop, Windsurf, VS Code...)
`agent-voice mcp` gives the agent four tools, and asks it to use them: `speak` (a one-sentence summary at the end of each reply), `set_status` (*Working · Running the tests* in the notch), `ask_user` (a question, with options or free text, answered by voice or a click) and `notify` (a banner; `needs_you` puts it in *Needs you*). Agents follow these instructions more or less faithfully: if yours forgets to speak, add to its rules: *"At the end of every reply, call the agent-voice `speak` tool with one short sentence."*
For an MCP app not listed: command `agent-voice` (or the full path to `agent-voice.exe`), arguments `mcp --client <its name>`.

### ChatGPT, Claude, Gemini... in the browser
1. `agent-voice install browser` (or click *Browser* in the panel's Agents row): it registers the bridge and opens the extension's folder.
2. In Chrome, Edge or Brave, open `chrome://extensions` (`edge://extensions`), turn on **Developer mode**, click **Load unpacked** and pick that folder.
3. Ask something on ChatGPT, Claude, Gemini, AI Studio, Perplexity, Copilot, Mistral or DeepSeek: the notch shows the tab working, then the reply is read out (its summary, or in full). Your spoken reply is typed back into the page.

The extension only reads the conversation on these sites, and only talks to agent-voice through the bridge (web pages still can't reach the local API).

### Terminal agents: `agent-voice watch`
`agent-voice watch aider` (or `opencode`, `goose`, `gh copilot suggest`...) runs the agent in a pseudo-terminal inside your terminal: you use it exactly as before. agent-voice reads its screen: Enter starts *Working*; when the screen stays quiet for 2.5 s, the new text is the reply (in summary mode, its last lines); a last line asking something (*Apply these edits? (y/n)*, a `?`) goes to *Needs you* instead.

### Desktop apps without hooks
`agent-voice apps on` (or the tray menu) reads the notifications of ChatGPT, Copilot and Perplexity's desktop apps (when they say a reply is ready, they show its start). Only those apps' notifications are read, from Windows' local notification database. Change the list with `desktop_apps.apps` in the config. Off by default.

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
| `catch_up.enabled` / `away_minutes` | `true` / `5` | *While you were away* when you come back |
| `resume.auto` / `message` | `true` / `"Continue."` | Out of quota: send this once the limit resets (`false`: only when you ask) |
| `focus.enabled` / `full_screen` / `calls` | `true` / `true` / `"hold"` | Hold replies in focus; `calls: "chimes"` for chimes during calls |
| `recap.enabled` / `at` | `true` / `"18:00"` | The daily recap |
| `shortcuts` | 6 saved prompts | `{"name": "prompt"}` |
| `ducking.enabled` / `voice` / `listen` / `pause_media` | `true` / `0.3` / `0.0` / `true` | Other apps' volume under the voice and while you reply |
| `limits.claude_refresh_minutes` | `10` | Run `claude -p /usage` this often while Claude is in use (`0`: never) |
| `ntfy.server` / `token` | `"https://ntfy.sh"` / `""` | Your own ntfy server and its access token |
| `ntfy.content` / `when` / `away_minutes` | `"summary"` / `"away"` / `2` | How much text; only while away, or `"always"` |
| `ntfy.events` | `["needs", "done", "limit", "recap", "scheduled"]` | What reaches your phone |
| `desktop_apps.enabled` / `apps` | `false` / `{chatgpt, copilot, perplexity}` | Read these desktop apps' notifications (app id fragment → name) |
| `permissions.enabled` / `wait_seconds` | `true` / `60` | Approvals by voice and in the notch, and how long Claude waits |
| `wake_word.phrases` | `["hey claude", ...]` | Phrases Whisper accepts as the wake word |
| `background_send` | `true` | Claude desktop app: send replies without bringing it forward |
| `update_check` | `true` | Look for a newer release at start, then once a day |
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

Runtime state (mode, auto-send, summary, speed, wake word, approvals, desktop apps, today's numbers, threads waiting to resume) lives in `~/.agent-voice/state.json`, and the notch/bar choice and position in `~/.agent-voice/widget.json`.

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
| **"I don't know where Codex is yet"** | A message by name needs the conversation's window: send it one message from there first (or, for terminal agents, let it finish one turn). |
| **Browser replies not read** | Check the extension is loaded and enabled, and `agent-voice install browser` was run *after* moving the app. Sites change their pages: the extension falls back to "the text stopped changing for 4 s", so a reply may come a few seconds late. |
| **MCP app silent** | Restart the app after installing (they read their MCP config at start), and check agent-voice appears in its MCP server list. |
| **`watch` doesn't announce replies** | It waits for 2.5 s of quiet after you press Enter; agents that keep animating a spinner never go quiet. |
| **The update says a file couldn't be replaced** | An app keeps a part of agent-voice open (an MCP app, the browser): close it and click *Update* again. Nothing was changed meanwhile. |
| **Anything else** | `agent-voice serve` runs in the foreground with logs; the full log is `~/.agent-voice/logs/server.log`. |

## 9. Uninstall

```bash
agent-voice uninstall all
agent-voice autostart off
agent-voice shutdown
```

Remove the browser extension from the browser yourself (`chrome://extensions`).

Then delete the project folder and `~/.agent-voice/` (config, voices, logs). The Whisper model cache is in `~/.cache/huggingface/`.
