"""Adapters that turn each agent's hook payload into text to speak.

Every adapter must exit quickly and never fail loudly: a broken voice must not break the agent.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from agent_voice import client, textclean
from agent_voice.config import HOME, LOG_DIR, load_config, load_state

# Field names agents use for "the assistant's reply", most specific first.
TEXT_KEYS = (
    "last_assistant_message",
    "last-assistant-message",
    "prompt_response",
    "response",
    "text",
    "message",
    "content",
    "output",
)

SESSIONS_DIR = HOME / "sessions"

SUMMARY_MARKER = "Voice summary:"
SUMMARY_INSTRUCTION = (
    "[agent-voice] The user hears your replies through text-to-speech. End the final reply of this turn "
    "with one last line formatted exactly as `Voice summary: <one short sentence, at most 25 words, in the "
    "language of your reply, saying what happened and anything the user must do next>`. "
    "Do not add it to intermediate messages."
)
# Tools that stop and wait for the user: worth a chime even when nothing is spoken.
ATTENTION_TOOLS = {"ExitPlanMode"}

_SUMMARY_LINE = re.compile(r"^[\s>*_`#-]*voice summary\s*:[\s*_`]*(.+?)[\s*_`]*$", re.I | re.M)


def summary_enabled() -> bool:
    """Runtime toggle (state.json, written by the daemon) wins over the config default."""
    state = load_state()
    if "summary" in state:
        return bool(state["summary"])
    return bool(load_config().get("summary", {}).get("enabled", True))


def summarize(text: str, max_chars: int = 280) -> str:
    """The agent's own `Voice summary:` line if present, else the opening sentences of the reply."""
    found = _SUMMARY_LINE.findall(text)
    if found:
        return found[-1].strip()
    out = ""
    for para in textclean.paragraphs(textclean.clean(text)):
        for sentence in textclean.sentences(para):
            if out and len(out) + len(sentence) + 1 > max_chars:
                return out
            out = f"{out} {sentence}".strip()
        if out:  # the first paragraph is where agents state the outcome
            return out
    return out


def strip_summary(text: str) -> str:
    """Full-text mode: don't read the summary line on top of the answer it summarizes."""
    return _SUMMARY_LINE.sub("", text)


def _log_payload(agent: str, raw: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"payload-{agent}.json").write_text(raw[:200_000], encoding="utf-8")
    except OSError:
        pass


def extract_text(payload) -> str:
    """Find the reply text in an arbitrary hook payload."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return "\n".join(t for t in (extract_text(p) for p in payload) if t)
    if not isinstance(payload, dict):
        return ""
    for key in TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (dict, list)):
            found = extract_text(value)
            if found:
                return found
    # Anthropic-style content blocks: [{"type": "text", "text": ...}]
    if payload.get("type") == "text" and isinstance(payload.get("text"), str):
        return payload["text"]
    return ""


# -- Claude Code -------------------------------------------------------------------
def _text_blocks(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return []


def _is_real_user_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def claude_turn_texts(transcript: Path) -> list[str]:
    """Assistant text blocks of the current turn (since the last real user prompt), main thread only."""
    entries = []
    with transcript.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    start = 0
    for i, entry in enumerate(entries):
        if _is_real_user_prompt(entry):
            start = i + 1
    texts = []
    for entry in entries[start:]:
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            texts.extend(t for t in _text_blocks(entry.get("message") or {}) if t.strip())
    return texts


def _digest(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


def _norm(text: str) -> str:
    return " ".join(text.split())


def _covered(text: str, texts: list[str]) -> bool:
    """True if `text` is already (part of) `texts`, ignoring whitespace differences."""
    target = _norm(text)
    joined = _norm("\n".join(texts))
    return target in joined or any(_norm(t) and _norm(t) in target for t in texts)


@contextlib.contextmanager
def _session_lock(path: Path, timeout: float = 5.0):
    """Cross-process lock: parallel tool calls fire several hooks at once."""
    lock = path.with_suffix(".lock")
    end = time.time() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):  # Windows: PermissionError while another hook deletes it
            try:
                if time.time() - lock.stat().st_mtime > 10:  # left behind by a killed hook
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() > end:
                break
            time.sleep(0.02)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            lock.unlink(missing_ok=True)


def _unspoken(session_id: str, texts: list[str]) -> list[str]:
    """Filter out texts already spoken in this session, and remember the new ones."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_") or "default"
    path = SESSIONS_DIR / f"{safe_id}.json"
    with _session_lock(path):
        return _unspoken_locked(path, texts)


def _unspoken_locked(path: Path, texts: list[str]) -> list[str]:
    try:
        spoken = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        spoken = []
    seen = set(spoken)
    fresh = []
    for text in texts:
        digest = _digest(text)
        if digest not in seen:
            seen.add(digest)
            spoken.append(digest)
            fresh.append(text)
    try:
        path.write_text(json.dumps(spoken[-300:]), encoding="utf-8")
    except OSError:
        pass
    return fresh


def describe_tool(tool: str, args: dict) -> str:
    """What a tool call does, as a short phrase that reads well after "Claude wants to"."""
    args = args if isinstance(args, dict) else {}
    if tool == "Bash":
        command = " ".join(str(args.get("command", "")).split())
        return f"run {command[:90] + '…' if len(command) > 90 else command}"
    path = str(args.get("file_path") or args.get("notebook_path") or "")
    name = Path(path).name if path else ""
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"edit {name}" if name else "edit a file"
    if tool == "Read":
        return f"read {name}" if name else "read a file"
    if tool in ("WebFetch", "WebSearch"):
        return f"look up {args.get('url') or args.get('query') or 'the web'}"
    if tool.startswith("mcp__"):
        return f"use {tool.split('__')[-1].replace('_', ' ')}"
    return f"use {tool}"


def permission_decision(data: dict) -> dict:
    """Ask the daemon, then wait (a while) for your answer by voice or from the widget.

    No answer in time, or no daemon: no decision, so Claude Code shows its own dialog.
    """
    cfg = load_config().get("permissions", {})
    if not cfg.get("enabled", True) or not load_state().get("approvals", True):
        return {}
    tool = str(data.get("tool_name", ""))
    reply = client.request("/permission", {"session": str(data.get("session_id", "")), "tool": tool,
                                           "summary": describe_tool(tool, data.get("tool_input") or {})},
                           timeout=2.0)
    if not reply or not reply.get("id"):
        return {}
    perm_id = reply["id"]
    deadline = time.time() + float(cfg.get("wait_seconds", 60))
    while time.time() < deadline:
        state = client.request("/permission_state", {"id": perm_id}, timeout=2.0)
        if state is None:
            return {}
        if state.get("decision"):
            decision = {"behavior": state["decision"]}
            if state["decision"] == "deny":
                decision["message"] = "The user said no (agent-voice)."
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}
        time.sleep(0.3)
    client.request("/permission_state", {"id": perm_id, "cancel": True}, timeout=2.0)
    return {}


def hook_claude(raw: str) -> None:
    try:
        data = json.loads(raw)
    except ValueError:
        return
    event = data.get("hook_event_name", "")
    if event == "PermissionRequest":
        out = permission_decision(data)
        if out:
            sys.stdout.write(json.dumps(out))
        return
    if event == "UserPromptSubmit":
        # The window you just typed in is where spoken replies get pasted back.
        from agent_voice import winfocus

        client.request("/prompt", {"hwnd": winfocus.foreground(), "session": str(data.get("session_id", "")),
                                   "source": "claude"}, timeout=1.0)
        if summary_enabled():
            # Claude Code adds this to the model's context for the turn.
            out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": SUMMARY_INSTRUCTION}}
            sys.stdout.write(json.dumps(out))
        return
    if event == "Notification":
        # Permission prompts and "waiting for your input"; the daemon only rings in chime mode.
        if "permission" in str(data.get("message", "")).lower():
            client.request("/notify", {"kind": "attention"}, timeout=1.0)
        return
    if event == "PreToolUse" and data.get("tool_name") != "AskUserQuestion":
        # Live activity in the notch: what it's doing now (this hook is async: the tool doesn't wait).
        label = describe_tool(str(data.get("tool_name", "")), data.get("tool_input") or {})
        client.request("/activity", {"session": str(data.get("session_id", "")), "source": "claude",
                                     "label": label[:1].upper() + label[1:]}, timeout=1.0)
    if event == "PreToolUse" and data.get("tool_name") in ATTENTION_TOOLS:
        client.request("/notify", {"kind": "attention"}, timeout=1.0)
    asking = event == "PreToolUse" and data.get("tool_name") == "AskUserQuestion"
    if event == "PreToolUse" and not asking and not load_config().get("speak_progress", True):
        return
    texts: list[str] = []
    transcript = data.get("transcript_path")
    if transcript and Path(transcript).exists():
        texts = claude_turn_texts(Path(transcript))
    last = data.get("last_assistant_message") or ""
    if event == "Stop" and last.strip() and not _covered(last, texts):
        # Only a fallback for when the transcript isn't flushed yet: its formatting can differ
        # slightly from the transcript blocks, so an exact-match dedupe would speak it twice.
        texts.append(last)
    fresh = _unspoken(str(data.get("session_id", "")), texts)
    final = event == "Stop"
    text = "\n\n".join(fresh)
    if asking:
        questions = (data.get("tool_input") or {}).get("questions") or []
        if questions and isinstance(questions[0], dict):
            q = questions[0]
            notify_question(
                session=str(data.get("session_id", "")),
                agent="claude",
                question=str(q.get("question", "")),
                header=str(q.get("header", "")),
                options=[str(o.get("label", "")) for o in q.get("options") or [] if isinstance(o, dict)],
                more=len(questions) - 1,
                preface=strip_summary(text) if load_config().get("speak_progress", True) else "",
            )
            return
    if final and text.strip() and summary_enabled():
        text = summarize(text, int(load_config().get("summary", {}).get("max_chars", 280)))
    else:
        text = strip_summary(text)
    # On Stop, call even with nothing new to say: it marks the end of the turn (arms auto-listen).
    if text.strip() or final:
        # The session id lets the daemon type your spoken reply into this conversation (desktop app).
        client.speak(text, source="claude", final=final, session=str(data.get("session_id", "")))


def notify_question(session: str, agent: str, question: str, options: list[str], header: str = "",
                    more: int = 0, preface: str = "") -> None:
    """The agent is waiting on a choice: the daemon reads it out and the widget shows the options."""
    client.request("/notify", {"kind": "question", "session": session, "agent": agent, "question": question,
                               "header": header, "options": options, "more": more, "preface": preface},
                   timeout=2.0)


# -- Cline: PowerShell (Windows) / executable (macOS, Linux) hook files ----------------
# Tools whose arguments are what Cline says to you.
CLINE_SPOKEN = {"attempt_completion": "result", "plan_mode_respond": "response"}


def hook_cline(raw: str) -> dict:
    """Cline sends {hookName, taskId, <hookName in camelCase>: {...}}; returns the hook's JSON reply."""
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    name = data.get("hookName", "")
    task = str(data.get("taskId", ""))
    if name == "UserPromptSubmit":
        from agent_voice import winfocus

        client.request("/prompt", {"hwnd": winfocus.foreground(), "session": task, "source": "cline"}, timeout=1.0)
        return {"contextModification": SUMMARY_INSTRUCTION} if summary_enabled() else {}
    if name == "Notification":
        note = data.get("notification") or {}
        if note.get("requiresUserAction") or note.get("waitingForUserInput"):
            client.request("/notify", {"kind": "attention"}, timeout=1.0)
        return {}
    if name != "PreToolUse":
        return {}
    pre = data.get("preToolUse") or {}
    tool, params = pre.get("toolName", ""), pre.get("parameters") or {}
    if tool == "ask_followup_question":
        try:
            options = [str(o) for o in json.loads(params.get("options") or "[]")]
        except (ValueError, TypeError):
            options = []
        question = str(params.get("question", ""))
        if options:
            notify_question(session=task, agent="cline", question=question, options=options)
        elif question.strip():
            client.speak(question, source="cline", final=True, session=task)
    elif tool in CLINE_SPOKEN:
        text = str(params.get(CLINE_SPOKEN[tool], ""))
        if text.strip() and summary_enabled():
            text = summarize(text, int(load_config().get("summary", {}).get("max_chars", 280)))
        client.speak(strip_summary(text), source="cline", final=True, session=task)
    return {}


# -- Generic JSON payloads (Codex, Gemini CLI, Cursor, anything else) -----------------
def hook_payload(agent: str, raw: str) -> None:
    _log_payload(agent, raw)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    if isinstance(payload, dict):
        kind = payload.get("type")
        if agent == "codex" and kind and kind != "agent-turn-complete":
            return
    text = extract_text(payload)
    if text.strip():
        if summary_enabled():
            text = summarize(text, int(load_config().get("summary", {}).get("max_chars", 280)))
        else:
            text = strip_summary(text)
        client.speak(text, source=agent, final=True)


def run(agent: str, argv: list[str]) -> int:
    reply: dict = {}
    try:
        if agent == "codex" and argv:
            raw = argv[-1]  # Codex passes the JSON payload as the last argument
        else:
            # Hook payloads are UTF-8 JSON whatever the console code page.
            raw = sys.stdin.buffer.read().decode("utf-8", "replace").lstrip("﻿") if not sys.stdin.isatty() else ""
        if agent == "claude":
            hook_claude(raw)
        elif agent == "cline":
            reply = hook_cline(raw)
        else:
            hook_payload(agent, raw)
    except Exception as exc:  # a voice problem must never break the agent
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (LOG_DIR / "hook-errors.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{agent}: {exc!r}\n")
        except OSError:
            pass
    if agent in ("gemini", "cursor"):
        # These agents parse the hook's stdout as JSON.
        sys.stdout.write("{}")
    if agent == "cline":
        sys.stdout.write(json.dumps(reply))
    return 0
