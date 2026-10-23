"""Phone notifications through ntfy (opt-in)."""

from __future__ import annotations

import logging

from agent_voice import ntfy, winfocus
from agent_voice.config import save_state

log = logging.getLogger("agent_voice.server")


class PhoneMixin:
    """Phone notifications through ntfy (opt-in)."""

    def _ntfy_settings(self) -> dict | None:
        """Where notifications go, or None while they're off (nothing is ever sent then)."""
        phone = self.state.get("ntfy") or {}
        if not phone.get("enabled") or not phone.get("topic"):
            return None
        cfg = self.config.get("ntfy", {})
        return {"server": cfg.get("server") or ntfy.DEFAULT_SERVER, "token": cfg.get("token") or "",
                "topic": phone["topic"], "content": cfg.get("content", "summary")}

    def _push(self, kind: str, title: str, text: str = "", full: str = "", force: bool = False) -> None:
        """Send it to the phone if that's on, this kind is wanted, and (by default) you're away from the PC."""
        if not self._ntfy_settings():
            return
        cfg = self.config.get("ntfy", {})
        group = {"permission": "needs", "resumed": "limit"}.get(kind, kind)
        if not force and group not in (cfg.get("events") or ["needs", "done", "limit", "recap", "scheduled"]):
            return
        if not force and cfg.get("when", "away") == "away" and not self._away_for_phone():
            return
        content = cfg.get("content", "summary")
        self.notifier.push(kind, title, full if content == "full" and full else text)

    def _away_for_phone(self) -> bool:
        idle = winfocus.idle_seconds()
        return bool(self._away_since or self._holding() or self.muted) or idle >= 60 * float(
            self.config.get("ntfy", {}).get("away_minutes", 2))

    def set_phone(self, on: bool) -> dict:
        """Turn phone notifications on (a private topic is made the first time) or off."""
        phone = dict(self.state.get("ntfy") or {})
        phone["enabled"] = on
        if on and not phone.get("topic"):
            phone["topic"] = ntfy.new_topic()
        self.state["ntfy"] = phone
        save_state(self.state)
        log.info("phone notifications %s", "on" if on else "off")
        return self._phone_status()

    def new_phone_topic(self) -> dict:
        """A new private topic: the old QR code stops receiving anything (scan the new one)."""
        self.state["ntfy"] = {**(self.state.get("ntfy") or {}), "topic": ntfy.new_topic()}
        save_state(self.state)
        return self._phone_status()

    def phone_test(self) -> bool:
        cfg = self._ntfy_settings()
        return bool(cfg) and self.notifier.send(ntfy.message("test", "agent-voice", "", "titles"), cfg)

    def _phone_status(self) -> dict:
        cfg = self._ntfy_settings()
        if not cfg:
            return {"enabled": False}
        return {"enabled": True, "url": ntfy.subscribe_url(cfg["server"], cfg["topic"]), "sent": self.notifier.sent,
                "error": self.notifier.last_error, "when": self.config.get("ntfy", {}).get("when", "away"),
                "content": cfg["content"]}
