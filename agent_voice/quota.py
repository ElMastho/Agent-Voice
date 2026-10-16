"""Rate limits each agent reports locally (5-hour and weekly windows), for the notch's Limits section.

Claude Code hands them to its status line (`hooks.statusline`); Codex writes them in its session logs
(`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, a `token_count` event per turn). Read-only, nothing leaves the machine.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

WINDOWS = ("five_hour", "seven_day")


def window_name(minutes: float) -> str:
    """'five_hour' or 'seven_day' for a window's length; '' for anything else."""
    if 60 <= minutes <= 6 * 60:
        return "five_hour"
    if 6 * 24 * 60 <= minutes <= 8 * 24 * 60:
        return "seven_day"
    return ""


def _stamp(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def codex_limits_from(entry: dict) -> dict:
    """The windows in one Codex log line (a token_count event): {'five_hour': {'pct', 'resets'}, ...}."""
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
    limits = payload.get("rate_limits") or (payload.get("info") or {}).get("rate_limits")
    if not isinstance(limits, dict):
        return {}
    at = _stamp(entry.get("timestamp")) or 0.0
    out: dict = {}
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
            continue
        name = window_name(float(w.get("window_minutes") or (300 if key == "primary" else 10080)))
        if not name:
            continue
        resets = w.get("resets_at")
        if not isinstance(resets, (int, float)) and isinstance(w.get("resets_in_seconds"), (int, float)) and at:
            resets = at + w["resets_in_seconds"]
        out[name] = {"pct": round(float(w["used_percent"])), "resets": int(resets or 0)}
    return out


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def latest_codex_log(home: Path | None = None) -> Path | None:
    """The newest session log, looking only in the latest few day folders (not the whole history)."""
    root = (home or codex_home()) / "sessions"
    if not root.is_dir():
        return None
    days = []
    for year in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)[:2]:
        for month in sorted((d for d in year.iterdir() if d.is_dir()), reverse=True)[:2]:
            days += sorted((d for d in month.iterdir() if d.is_dir()), reverse=True)[:3]
    files = [f for d in sorted(days, reverse=True)[:3] for f in d.glob("rollout-*.jsonl")]
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


def codex_limits(path: Path, tail: int = 512 * 1024) -> dict:
    """The last rate limits in a Codex session log (only its end is read)."""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - tail))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            found = codex_limits_from(json.loads(line))
        except (ValueError, AttributeError):
            continue
        if found:
            return found
    return {}
