"""Claude's usage from its local transcripts, for limits when nothing reports them (the desktop app has no status line).

Every Claude Code conversation, the desktop app's included, writes its requests to `~/.claude/projects/**/*.jsonl`
with their token counts. Weighted by each model's relative price, they add up to how much of a 5-hour or weekly
window has gone. Each real reading (the status line, Claude's own usage cache, a limit hit = 100%) tells how much
one percent is worth; in between, the percentage is estimated. Read-only, nothing leaves the machine.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

SPANS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
# Relative API prices per million tokens (input, output, cache write, cache read): only the ratios matter, the
# calibration absorbs the scale.
PRICES = {"opus": (5.0, 25.0, 6.25, 0.5), "sonnet": (3.0, 15.0, 3.75, 0.3), "haiku": (1.0, 5.0, 1.25, 0.1)}
KEEP = 9 * 86400  # a weekly window and a bit


def cost(model: str, usage: dict) -> float:
    """What a request would cost on the API, in dollars: the weight it has against the plan's limits."""
    name = (model or "").lower()
    prices = next((p for family, p in PRICES.items() if family in name), PRICES["opus"])
    counts = [int(usage.get(k) or 0) for k in
              ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    return sum(n * p for n, p in zip(counts, prices)) / 1e6


def _stamp(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class Ledger:
    """Every Claude request of the last nine days: (time, cost, new tokens). Files are read once, then only their end."""

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".claude" / "projects"
        self.items: list[tuple[float, float, int]] = []
        self._offsets: dict[Path, int] = {}
        self._seen: set[str] = set()

    def refresh(self, now: float | None = None) -> int:
        """Read what was added since last time; returns how many requests were new."""
        now = time.time() if now is None else now
        if not self.root.is_dir():
            return 0
        added = 0
        for path in self.root.rglob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime > KEEP or stat.st_size <= self._offsets.get(path, 0):
                continue
            added += self._read(path, self._offsets.get(path, 0), now)
            self._offsets[path] = stat.st_size
        if added:
            self.items.sort()
            cut = now - KEEP
            self.items = [i for i in self.items if i[0] >= cut]
        return added

    def _read(self, path: Path, offset: int, now: float) -> int:
        added = 0
        with path.open("rb") as f:
            f.seek(offset)
            for raw in f:
                if b'"usage"' not in raw or b'"assistant"' not in raw:
                    continue  # most lines are tool results and prompts: skip them unparsed
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                usage = message.get("usage") if isinstance(message, dict) else None
                if entry.get("type") != "assistant" or not isinstance(usage, dict):
                    continue
                key = str(message.get("id") or entry.get("requestId") or entry.get("uuid") or "")
                if not key or key in self._seen:
                    continue  # one request is written as several entries (one per content block)
                at = _stamp(entry.get("timestamp"))
                if not at or now - at > KEEP:
                    continue
                self._seen.add(key)
                tokens = sum(int(usage.get(k) or 0) for k in
                             ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
                self.items.append((at, cost(str(message.get("model") or ""), usage), tokens))
                added += 1
        return added

    def window(self, start: float, end: float) -> tuple[float, int]:
        """(cost, new tokens) of the requests in [start, end)."""
        c, t = 0.0, 0
        for at, x, n in self.items:
            if start <= at < end:
                c, t = c + x, t + n
        return c, t

    def first_after(self, at: float) -> float:
        return next((i[0] for i in self.items if i[0] >= at), 0.0)

    def covers(self, start: float) -> bool:
        """Do we have requests from that far back (otherwise a window's total would come out too low)?"""
        return bool(self.items) and self.items[0][0] <= start + 3600


def current_window(window: str, now: float, anchor: float, ledger: Ledger) -> tuple[float, float]:
    """(start, reset) of the window running now, from a reset time seen before (`anchor`, 0 if none).

    Weekly windows follow each other on a fixed schedule. A 5-hour one starts with the first request after the
    previous one ended, to the minute.
    """
    span = SPANS[window]
    if window == "seven_day":
        if not anchor:
            return now - span, 0.0  # no schedule known: the last seven days, no reset time
        k = max(0, int((now - anchor) // span) + 1) if anchor <= now else 0
        end = anchor + k * span
        return end - span, end
    if anchor and anchor > now:
        return anchor - span, anchor
    after = anchor or now - span
    start = ledger.first_after(after)
    while start:  # windows chain: find the one that contains `now`
        start -= start % 60
        end = start + span
        if end > now:
            return start, end
        start = ledger.first_after(end)
    return 0.0, 0.0  # nothing since the last reset: no window running


class Estimator:
    """Real readings in, estimated percentages out, for one account's Claude usage."""

    def __init__(self, ledger: Ledger, state: dict | None = None):
        self.ledger = ledger
        state = state or {}
        self.rate: dict[str, float] = dict(state.get("rate") or {})  # dollars of usage per percent, per window
        self.real: dict[str, dict] = dict(state.get("real") or {})  # the last reading: pct, resets, at

    def state(self) -> dict:
        return {"rate": self.rate, "real": self.real}

    def reading(self, window: str, pct: float, resets: float, at: float) -> None:
        """A real percentage (status line, Claude's cache, a limit hit): remember it, and learn the rate from it."""
        if window not in SPANS or not resets:
            return
        last = self.real.get(window) or {}
        if at < float(last.get("at") or 0):
            return  # older than what we have
        self.real[window] = {"pct": float(pct), "resets": float(resets), "at": at}
        start = resets - SPANS[window]
        if pct < 3 or not self.ledger.covers(start):
            return  # too little to divide by, or we can't see the whole window
        spent, _ = self.ledger.window(start, at)
        if spent > 0:
            rate = spent / pct
            old = self.rate.get(window)
            self.rate[window] = rate if not old else 0.6 * old + 0.4 * rate

    def estimate(self, window: str, now: float) -> dict | None:
        """{'pct' (None until calibrated), 'resets', 'tokens', 'est': True} for the window running now."""
        real = self.real.get(window) or {}
        start, end = current_window(window, now, float(real.get("resets") or 0), self.ledger)
        if not start and not end:
            return {"pct": 0, "resets": 0, "tokens": 0, "est": True} if real else None
        spent, tokens = self.ledger.window(start, now)
        rate = self.rate.get(window)
        pct = None
        if real and real.get("resets") == end:  # the same window as the last reading: start from it
            later, _ = self.ledger.window(float(real["at"]), now)
            pct = float(real["pct"]) + (later / rate if rate else 0.0)
        elif rate:
            pct = spent / rate
        return {"pct": None if pct is None else max(0, min(100, round(pct))), "resets": int(end), "tokens": tokens,
                "est": True}
