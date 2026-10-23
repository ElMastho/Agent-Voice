"""What waits on you: approvals, questions, agents waiting for input, answered by click or by voice."""

from __future__ import annotations

import logging
import time

from agent_voice import claudeapp, winfocus
from agent_voice.config import save_state
from agent_voice.daemon.common import _clock, thread_color
from agent_voice.daemon.phrases import _ALL, _plain, match_option, match_yes_no, spoken_number
from agent_voice.daemon.sounds import _beep
from agent_voice.i18n import action, tr

log = logging.getLogger("agent_voice.server")


class NeedsMixin:
    """What waits on you: approvals, questions, agents waiting for input, answered by click or by voice."""

    def answer(self, index: int | None = None, text: str = "") -> bool:
        """Answer the pending question: option `index`, or a spoken/typed `text`."""
        import pyperclip

        q = self.question
        if not q:
            return False
        options = q["options"]
        if index is None:
            index = match_option(text, options)
        label = options[index] if index is not None and 0 <= index < len(options) else ""
        if q.get("id"):  # asked through MCP: the asking agent collects the answer, nothing to type
            self._answers[q["id"]] = label or text
            self._answers = dict(list(self._answers.items())[-20:])
            self.question = None
            log.info("answer to ask #%d collected", q["id"])
            return True
        session = q["session"]
        hwnd = self._hwnd_for(session)
        title = self._title_for(hwnd, session)
        log.info("answer option=%r free_text=%s", label, not label)
        if title:
            ok = claudeapp.answer_question(hwnd, title, label=label, other="" if label else text)
        else:
            # Agents whose question UI we can't drive (Cline): the answer is simply the reply.
            self._paste(label or text, hwnd, submit=True, session=session)
            ok = True
        if ok:
            self.question = None
            if q["more"] and title:
                claudeapp.select_session(hwnd, title)  # more questions: show them
                winfocus.focus(hwnd)
        else:
            pyperclip.copy(label or text)
            _beep(300)
            if title:  # let you click it yourself
                winfocus.focus(hwnd)
                claudeapp.select_session(hwnd, title)
        return ok

    def ask(self, session: str, source: str, question: str, options: list[str]) -> int:
        """A question from an MCP client (ask_user): read out and shown; its answer is collected by ask_state()."""
        self._ask_seq += 1
        self.notify("question", {"session": session, "agent": source, "question": question, "options": options,
                                 "id": self._ask_seq})
        if session:
            self.threads[session] = {"source": source, "last": time.time()}
        return self._ask_seq

    def ask_state(self, ask_id: int, cancel: bool = False) -> dict:
        if ask_id in self._answers:
            return {"answer": self._answers.pop(ask_id)}
        pending = bool(self.question and self.question.get("id") == ask_id)
        if cancel and pending:
            self.question = None
            self.cancel_listen()
            pending = False
        return {"answer": None, "pending": pending}

    def need_input(self, session: str, source: str, text: str = "", announce: bool = True) -> None:
        """An agent waits on you for something we can't answer here: it joins the "needs you" list."""
        title = self._title(session, source)
        self.attention[session] = {"source": source, "text": text.strip()[:300], "at": time.time()}
        if session:
            self.threads[session] = {"source": source, "last": time.time()}
        self._event("needs", session, source, text)
        self._push("needs", title, text or tr("Waiting for you"))
        log.info("%s needs you", title)
        if not announce or self.muted:
            return
        self._show_banner(text or tr("Waiting for you"), session, source, kind="needs", tag="needs you")
        if self.chime:
            self.play_chime("attention")
        else:
            self._wake_cancel.set()
            self._say("{title} needs you.", title=title)
            if text and len(text) < 160:
                self.speaker.speak(text)

    def _settled(self, session: str) -> None:
        """The conversation moved on (it talks, works, or you wrote to it): it no longer waits on you."""
        self.attention.pop(session, None)

    def _needs_status(self) -> list[dict]:
        """Everything waiting on you, oldest first: approvals, a question, agents waiting for input."""
        items = []
        for p in sorted(self._perms.values(), key=lambda p: p["at"]):
            items.append({"kind": "permission", "id": p["id"], "session": p["session"], "source": p["source"],
                          "text": action(p["summary"]), "at": p["at"]})
        q = self.question
        if q:
            items.append({"kind": "question", "session": q["session"], "source": q["agent"], "text": q["question"],
                          "at": q["at"]})
        now = time.time()
        self.attention = {s: a for s, a in self.attention.items() if now - a["at"] < 3600}
        for session, a in self.attention.items():
            items.append({"kind": "input", "session": session, "source": a["source"],
                          "text": a["text"] or tr("Waiting for you"), "at": a["at"]})
        for session, info in self._limited.items():
            when = (tr("about ") if info.get("guess") else "") + _clock(info["resets"], "five_hour")
            items.append({"kind": "limit", "session": session, "source": info["source"], "resume": info["resume"],
                          "text": tr("Out of quota · resumes {when}" if info["resume"] else "Out of quota until {when}",
                                     when=when),
                          "at": info["at"]})
        for item in items:
            item["title"] = self._title(item["session"], item.pop("source"))
            item["color"] = thread_color(item["session"] or item["title"])
            item["at"] = round(item["at"])
        return items

    def _labels(self, session: str) -> list[str]:
        """What a conversation can be called: its title, or its agent's name."""
        source = self.threads.get(session, {}).get("source", "")
        return [self._title(session, source), source]

    def _voice_approval(self, text: str) -> bool:
        """'Yes' / 'approve Codex' / 'deny the notch one' while approvals wait: answer the one named, else the one shown."""
        if not self._perms:
            return False
        allow = match_yes_no(text)
        if allow is None:
            return False
        perms = sorted(self._perms.values(), key=lambda p: p["at"])  # the order of the "needs you" rows
        words = set(_plain(text).split())
        if words & _ALL or "les deux" in _plain(text):  # "approve all", "deny both"
            for p in perms:
                self.answer_permission(p["id"], allow)
            return True
        named = self._mentioned(text, {p["session"] for p in perms})
        number = None if named else spoken_number(words, len(perms))
        if named:
            perm = next((p for p in perms if p["session"] == named), None)
        elif number:  # "approve two", "deny the first one"
            perm = perms[number - 1]
        else:
            perm = self.permission
        if perm is None:
            return False
        self.answer_permission(perm["id"], allow)
        return True

    def request_permission(self, session: str, tool: str, summary: str, source: str = "claude") -> int:
        """An agent wants to use a tool: ask out loud and in the widget; the hook polls the answer.

        Several can wait at once (parallel agents): the first is asked, the others queue in "needs you".
        """
        self._perm_seq += 1
        perm = {"id": self._perm_seq, "session": session, "tool": tool, "summary": summary, "source": source,
                "at": time.time()}
        self._perms[perm["id"]] = perm
        if session:
            self.threads[session] = {"source": source, "last": time.time()}
        self._event("needs", session, source, summary)
        self._push("permission", self._title(session, source), tr("Wants to {action}.", action=action(summary)))
        log.info("permission #%d: %s (%d waiting)", self._perm_seq, summary, len(self._perms))
        if self.permission is None:
            self._present_permission(perm)
        elif self.chime:
            self.play_chime("attention")
        return self._perm_seq

    def _present_permission(self, perm: dict) -> None:
        self.permission = perm
        if self.muted:
            return
        if self.chime:
            self.play_chime("attention")
            return
        self.speaker.stop()
        self._wake_cancel.set()
        self._say("{title} wants to {action}. Allow?", title=self._title(perm['session'], perm['source']),
                  action=action(perm['summary'], in_lang=self.language))
        if self.transcriber is not None:
            self._reply_session = perm["session"]
            self._listen_armed = True

    def _drop_permission(self, perm_id: int) -> None:
        """Answered or given up: the next waiting request (if any) is asked."""
        self._perms.pop(perm_id, None)
        if self.permission and self.permission["id"] == perm_id:
            self.permission = None
            waiting = sorted(self._perms.values(), key=lambda p: p["at"])
            if waiting:
                self._present_permission(waiting[0])

    def answer_permission(self, perm_id: int, allow: bool) -> None:
        self._perm_decisions[perm_id] = "allow" if allow else "deny"
        log.info("permission #%d: %s", perm_id, "allowed" if allow else "denied")
        _beep(880 if allow else 440)
        self._drop_permission(perm_id)

    def permission_state(self, perm_id: int, cancel: bool = False) -> str:
        """The decision for a request ("" while pending). `cancel`: the hook gave up waiting."""
        if cancel and perm_id in self._perms:
            if self.permission and self.permission["id"] == perm_id:
                self.cancel_listen()
            self._drop_permission(perm_id)
        return self._perm_decisions.pop(perm_id, "")

    def _permission_status(self) -> dict | None:
        for perm_id, p in list(self._perms.items()):
            if time.time() - p["at"] > 900:  # the hook stopped asking long ago
                self._drop_permission(perm_id)
        p = self.permission
        return {k: p[k] for k in ("id", "tool", "summary")} if p else None

    @property
    def approvals(self) -> bool:
        return bool(self.state.get("approvals", True))

    def set_approvals(self, on: bool) -> None:
        # The PermissionRequest hook reads this from state.json.
        self.state["approvals"] = on
        save_state(self.state)
        log.info("approvals=%s", on)
        self._confirm("Approvals on." if on else "Approvals off.")

    def _question_status(self) -> dict | None:
        q = self.question
        if q and time.time() - q["at"] > 1800:  # stale: answered elsewhere, or abandoned
            self.question = q = None
        if not q:
            return None
        return {k: q[k] for k in ("question", "header", "options", "more", "agent")}
