"""The daemon: the speech queue's worker, its state at start, and /status. Its parts are mixins."""

from __future__ import annotations

import logging
import queue
import threading
import time

from agent_voice import __version__, claudeapp, companion, devices, history, ntfy, outputs, usage, winfocus
from agent_voice.config import load_state
from agent_voice.daemon.agents import AgentsMixin
from agent_voice.daemon.day import DayMixin
from agent_voice.daemon.desktop_apps import DesktopAppsMixin
from agent_voice.daemon.handoff import HandoffMixin
from agent_voice.daemon.hotkeys import HotkeysMixin
from agent_voice.daemon.limits import LimitsMixin
from agent_voice.daemon.listening import ListeningMixin
from agent_voice.daemon.needs import NeedsMixin
from agent_voice.daemon.phone import PhoneMixin
from agent_voice.daemon.presence import PresenceMixin
from agent_voice.daemon.schedules import SchedulesMixin
from agent_voice.daemon.settings import SettingsMixin
from agent_voice.daemon.sound import SoundMixin
from agent_voice.daemon.speech import SpeechMixin
from agent_voice.daemon.threads import ThreadsMixin
from agent_voice.daemon.tools import ToolsMixin
from agent_voice.daemon.updates import UpdatesMixin
from agent_voice.daemon.wake import WakeMixin
from agent_voice.i18n import set_language as set_ui_language
from agent_voice.tts import Speaker

log = logging.getLogger("agent_voice.server")


class Daemon(SettingsMixin, SpeechMixin, NeedsMixin, ListeningMixin, ThreadsMixin, AgentsMixin, HandoffMixin,
             LimitsMixin, PhoneMixin, SchedulesMixin, ToolsMixin, DayMixin, PresenceMixin, SoundMixin, DesktopAppsMixin,
             UpdatesMixin, WakeMixin, HotkeysMixin):
    """The service. One object, so every part sees the same state; each part is a mixin in its own module (state is
    set up here, in __init__, and shared through `self`)."""

    def __init__(self, config: dict, start_threads: bool = True):
        self.config = config
        self.state = load_state()
        if self.state.get("speed_x"):
            # The speaker reads config["speed"] (Piper length_scale) for every sentence.
            config["speed"] = 1.0 / float(self.state["speed_x"])
        # The voice engine: Piper (light, the default) or Kokoro (natural); Piper's voices are kept to go back to.
        self._piper_voices = dict(config["voices"])
        self._engine_state = ""  # "downloading 40%" while the natural voice is fetched
        self._apply_engine()
        # Your language (Settings): an unsure detection falls back to it, for the voice and for what you say.
        self._apply_language(self.state.get("language") or config.get("default_lang", "en"))
        set_ui_language(self.state.get("ui_language") or config.get("ui_language") or "en")
        self.speaker = Speaker(config)
        # HTTP handlers, the job worker, the mic and wake threads all touch the thread queue.
        self._lock = threading.RLock()
        self.transcriber = None
        self._ptt_active = False
        # Requests are processed in order by one worker so replies are never spoken out of order.
        self.jobs: queue.Queue = queue.Queue()
        # Auto-listen: armed by an agent's final answer, starts once the speaker goes quiet.
        self._listen_armed = False
        self._listen_cancel = threading.Event()
        self._listening = False
        # Window the user last sent a prompt from (where replies are typed back).
        # Persisted so a daemon restart doesn't send the next reply to whatever app is in front.
        saved = int(self.state.get("target_hwnd") or 0)
        self._target_hwnd = saved if winfocus.is_window(saved) else 0
        # Agent session whose turn just ended: the conversation a spoken reply belongs to.
        self._reply_session = ""
        # A question the agent is waiting on (options to pick from the widget or by voice).
        self.question: dict | None = None
        # Session and agent behind the current speech, so the widget can say which thread talks.
        self._voice_session, self._voice_source = "", ""
        self._file_titles: dict[str, str] = {}
        self._transcript_titles = claudeapp.TranscriptTitles()
        self._recent_said: dict[str, tuple[float, str]] = {}  # what was just said (and by whom): never twice
        # Speech from other threads that arrived while one was talking (or waiting for your reply).
        self._queue: list[tuple] = []
        # Recent conversations, for the widget's thread list: session -> {source, last}.
        self.threads: dict[str, dict] = {}
        # A tool call waiting for your permission (Claude Code's PermissionRequest hook polls it).
        self.permission: dict | None = None
        self._perm_seq = 0
        self._perm_decisions: dict[int, str] = {}
        # Newer release on GitHub, if any (checked at start, then daily): version, url, how
        # ("auto": one-click install; "git": a source checkout; "page": download by hand) and state.
        self.update: dict | None = None
        self._release: dict = {}
        # Wake word: always listening for "Hey Claude" while idle (off unless you turn it on).
        self._wake_cancel = threading.Event()
        self._wake_transcriber = None  # Whisper tiny: checks short phrases the keyword spotter missed
        self._force_submit = False
        # Desktop app: CLI session id -> conversation title, learned when you send a prompt.
        self._titles: dict[str, str] = dict(self.state.get("desktop_titles") or {})
        # What the mic is doing, for the widget: "" | waiting | recording | transcribing
        self._mic_phase = ""
        self.level = 0.0
        self.last_heard = ""
        self.last_heard_lang = ""
        # Last few transcriptions, newest first, so a reply can be copied again from the widget.
        self.history: list[dict] = list(self.state.get("history") or [])[:5]
        # Each conversation's latest turn (everything said since your prompt): replay and copy use it.
        self._turns: dict[str, dict] = {}
        self._last_session = ""
        # The window each conversation was prompted from, so a reply goes back to the right terminal.
        self._session_hwnd: dict[str, int] = {k: int(v) for k, v in (self.state.get("session_hwnds") or {}).items()}
        # What each agent is doing right now (from its tool calls), for the notch's live activity.
        self.activity: dict[str, dict] = {}
        self._pitches: dict[str, float] = {}
        self._agents: dict[str, str] = {}
        self._agents_at = 0.0
        self.banner: dict | None = None  # chime/silent: the reply shown as a notification under the notch
        self._reading = ""  # voice mode: what is being read out, shown under the notch to read along
        # Another app holds the mic (a call): chimes instead of the voice until it ends.
        self.in_call = ""
        self._call_override = False
        self._call_paused = False  # we paused the music when the call started: play it again when it ends
        self._ptt_release = threading.Event()
        # Every permission request still waiting (several agents can ask at once); `permission` is the one shown.
        self._perms: dict[int, dict] = {}
        # Conversations waiting on you that we can't answer for you ("waiting for your input"): open them.
        self.attention: dict[str, dict] = {}
        # Answers to questions asked through the MCP server (ask_user), collected by its polling.
        self._answers: dict[int, str] = {}
        self._ask_seq = 0
        # Context used per conversation, and each agent's rate limits (Claude Code's status line, Codex's logs).
        # Limits are kept across restarts: Claude only reports them while its status line runs.
        self.usage: dict[str, dict] = {}
        self.limits: dict[str, dict] = {k: v for k, v in (self.state.get("limits") or {}).items() if isinstance(v, dict)}
        self._limit_warned: set[str] = set()
        # Threads out of quota: when they reset, and whether to send "continue" then. Kept across restarts.
        self._limited: dict[str, dict] = {k: v for k, v in (self.state.get("limited") or {}).items()
                                          if isinstance(v, dict)}
        self._fullscreen = ""  # "presenting" / "full screen" (focus)
        # Phone notifications (ntfy, opt-in) and scheduled prompts (kept across restarts).
        self.notifier = ntfy.Notifier(self._ntfy_settings)
        # Other apps' sound, lowered under the voice and while you reply (a worker: fades never hold the mic loop).
        self._ducker = None
        self._duck_state = ""
        self._duck_idle = 0.0
        self._duck_jobs: queue.Queue = queue.Queue()
        self._schedules: list[dict] = [s for s in self.state.get("schedules") or [] if isinstance(s, dict)]
        self._limits_at: dict[str, float] = {}  # when each agent's numbers were measured (newest wins)
        # Claude's usage from its transcripts: an estimate between real readings (the desktop app reports none).
        self._ledger = usage.Ledger()
        self._estimator = usage.Estimator(self._ledger, self.state.get("claude_usage"))
        self._claude_est: dict[str, dict] = {}
        self._pace: dict[tuple[str, str], list[tuple[float, float, int]]] = {}  # (at, pct, reset) per window: its pace
        self._claude_asked = 0.0  # the last time we ran `claude -p /usage`
        self._claude_seen = 0.0  # ~/.claude.json's mtime when last read
        self._codex_seen: tuple = (None, 0.0)  # Codex's latest log and its mtime when last read
        self._limits_lock = threading.Lock()
        self._limits_refreshing = False
        self._usage_line = ""  # Claude Code's status line: "on" (ours), "off" or "custom" (yours); cached with agents
        # The last finished turn's outcome (files changed, tests), shown for a while in the notch.
        self.last_result: dict | None = None
        # What happened recently (turns done, agents asking for you), told when you come back.
        self._events: list[dict] = []
        self._away_since = 0.0
        # New threads: where they can start (cached), the folder you last worked in, and the one being dictated.
        self._targets: list[str] = []
        self._targets_at = 0.0
        self._new_pending: dict = {}
        self._new_window: dict = {}  # a session started in the desktop app: its window, for the first prompt
        self.new_thread_opens = 0  # bumped by the hotkey: the widget opens its new-thread box
        # Bumped by the show/hide hotkey; the widget toggles when it sees the number change.
        self.widget_toggles = 0
        self.widget_opens = 0  # bumped by `agent-voice open` (the Start menu): the widget shows its panel
        self.boot = int(time.time() * 1000)
        # A dictated message waiting a few seconds before it's sent (the take-back), and what was said about it.
        self._pending: dict | None = None
        self._pending_seq = 0
        self._pending_event = threading.Event()
        # Headphones or speakers: on speakers (if you asked), chimes instead of the voice. You can still pick Voice.
        self._output = outputs.OutputWatch()
        self._speaker_override = False
        # Which mic and speaker (Settings), "" for the system's. A mic that sends nothing is skipped, and said.
        devices.mic, devices.speaker = self.mic, self.speaker_device
        self._output.preferred = self.speaker_device
        devices.on_silent = self._mic_silent
        devices.safe_to_refresh = lambda: not self.speaker.busy
        self._mic_silent_at = 0.0
        # Talking over the voice (barge-in): the mic listens while it speaks, in headphones (no echo to hear).
        # Every prompt and reply, searchable (a local SQLite file); None when `history.enabled` is off.
        hist = config.get("history", {})
        self.history_db = history.History(days=float(hist.get("days", 90))) if hist.get("enabled", True) else None
        self._barge_cancel = threading.Event()
        self._barging = False  # listening for you while it speaks
        self._barged = False  # ... and you spoke
        # The companion beside the panel: timers, the stopwatch, the to-do list, reminders (kept in state.json).
        self.companion = companion.Companion(self.state.get("companion"), self._save_companion,
                                             config.get("pomodoro"))
        self.media: dict | None = None  # what's playing (Spotify, YouTube...): the companion's player
        self.agenda: list[dict] = []  # today's meetings (calendar links you added, Outlook while it's open)
        self._agenda_error = ""
        self._agenda_wake = threading.Event()
        self._meeting_alerted: set[str] = set()  # the meetings already announced (title|start)
        self._meeting_snooze: dict[str, float] = {}  # snoozed: announced again at that time
        if start_threads:
            threading.Thread(target=self._job_loop, name="jobs", daemon=True).start()
            threading.Thread(target=self._listen_monitor, name="listen", daemon=True).start()
            threading.Thread(target=self._wake_loop, name="wake", daemon=True).start()
            threading.Thread(target=self._check_update, name="update", daemon=True).start()
            threading.Thread(target=self._call_watch, name="calls", daemon=True).start()
            threading.Thread(target=self._away_watch, name="away", daemon=True).start()
            threading.Thread(target=self._apps_watch, name="apps", daemon=True).start()
            threading.Thread(target=self._quota_watch, name="quota", daemon=True).start()
            threading.Thread(target=self._duck_loop, name="ducking", daemon=True).start()
            threading.Thread(target=self._barge_loop, name="barge-in", daemon=True).start()
            threading.Thread(target=self._ensure_start_menu, name="start-menu", daemon=True).start()
            threading.Thread(target=self._companion_loop, name="companion", daemon=True).start()
            threading.Thread(target=self._media_loop, name="media", daemon=True).start()
            threading.Thread(target=self._agenda_loop, name="agenda", daemon=True).start()

    def _job_loop(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception:
                log.exception("job failed")

    def phase(self) -> str:
        if self._mic_phase:
            return self._mic_phase
        if self.speaker.busy:
            return "speaking"
        return "muted" if self.muted else "idle"

    def status(self) -> dict:
        return {
            "version": __version__,
            "boot": self.boot,  # a new one: the service restarted, and the widget restarts with it
            "widget_opens": self.widget_opens,
            "phase": self.phase(),
            "level": round(self.level, 4),
            "speaking_lang": self.speaker.current_lang if self.speaker.busy else "",
            "engine": self.engine,
            "engine_state": self._engine_state,
            "caption": self.speaker.current_text if self.speaker.busy else "",
            "awaiting_reply": self._listen_armed,
            "widget_toggles": self.widget_toggles,
            "new_thread_opens": self.new_thread_opens,
            "new_targets": self.new_targets(),
            "new_default": {"target": self._default_target(), "cwd": self.state.get("last_cwd", ""),
                            "recent": self.state.get("recent_cwds") or []},
            "last_heard": self.last_heard[-160:],
            "last_heard_lang": self.last_heard_lang,
            "history": self.history,
            "agents": self.agent_states(),
            "activity": self._activity_status(),
            "reading": self._reading_status(),
            "output": self.output,
            "in_call": self.in_call,
            "focus": self.focus if self._holding() else "",
            "barge_in": self._barging,
            "headphones_only": self.headphones_only,
            "mic": self.mic,
            "speaker_device": self.speaker_device,
            "dnd_from": self.dnd_from,
            "speakers": self.on_speakers,
            "pending": {"id": self._pending["id"], "until": self._pending["until"]} if self._pending else None,
            "phone": self._phone_status(),
            "schedules": self._schedules_status(),
            "companion": self.companion.status(),
            "media": self.media,
            "agenda": self._agenda_status(),
            "day": self._day_status(),
            "banner": {**self.banner, "age": round(time.time() - self.banner["at"], 1)}
            if self.banner and time.time() - self.banner["at"] < 60 else None,
            "last_reply": {"session": self._last_session} if self._turn_text(self._last_session) else None,
            "question": self._question_status(),
            "needs": self._needs_status(),
            "limits": self._limits_status(),
            "usage_line": self._usage_line,
            "limits_refreshing": self._limits_refreshing,
            "result": self.last_result if self.last_result and time.time() - self.last_result["at"] < 120 else None,
            "thread": self._thread_status(),
            "threads": self._threads_status(),
            "queued": len(self._queue),
            "permission": self._permission_status(),
            "update": self.update,
            "wake": self.wake,
            "desktop_apps": self.desktop_apps,
            "approvals": self.approvals,
            "muted": self.muted,
            "hands_free": self.hands_free,
            "chime": self.chime,
            "summary": self.summary,
            "language": self.language,
            "ui_language": self.ui_language,
            "speed_x": self.speed_x,
            "speaking": self.speaker.busy,
            "listening": self._listening,
            "voices": self.config["voices"],
            "hotkeys": self.config["hotkeys"],
        }
