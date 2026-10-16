# agent-voice: to-do

Everything listed so far is built and committed on `main`, not pushed or released yet. What's left:

## To check live (built and tested, not yet seen working for real)
- **Resume after a limit**: Claude Code's `StopFailure` hook firing on a real exhausted limit; "Continue." reaching the desktop-app conversation.
- **Phone (ntfy)**: scan the QR code with the ntfy app, press *Test*, then step away for 2 minutes and check that a reply, an approval and the recap arrive.
- **Scheduled prompts**: by voice ("at 2 pm tell Codex to run the tests"), from the reply box's *When* field, and with `agent-voice schedule`; a new thread started at its time.
- **Claude's limits**: the 10-minute `claude -p /usage` refresh while you work, the ↻ button, and the ≈ estimate between readings.
- **Smoothness**: the notch opening and closing with no jolt at the end; the banner following its width and position.
- **The new sounds**: the mic cues and chimes.
- **Reply preview, focus in full screen, the 18:00 recap, voice handoff, saved prompts, approvals by number.**

## Known limits
- Gemini CLI and Antigravity quotas: not shown (no local source; decided).
- Between two `/usage` readings, Claude's bars are estimates from this PC's transcripts: claude.ai and other devices count against the same limits but aren't visible until the next reading.
- Older Claude Code versions may not know the `StopFailure` hook event.
- Tokens per thread: Claude Code only (Codex logs its own and could be added).
- macOS: full-screen focus detection is Windows-only; the clipboard screenshot, the wider panel and the previews are untested on a Mac.
- The floating island (not docked) keeps the narrow single-column layout.
- Possibly raise `catch_up.away_minutes`, or only catch up after calls (undecided).

## Release
- Version 0.5.2: bump `pyproject.toml`, `agent_voice/__init__.py`, `agent_voice/browser/manifest.json`; run the tests; push; tag `v0.5.2` (CI builds the Windows zip, now with `qrcode`).
- The first release the 0.5.1 one-click updater installs: its first real test.
