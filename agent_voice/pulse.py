"""Linux sound, through `pactl`: PulseAudio, or PipeWire's pipewire-pulse (what current desktops run).

Which apps play (to lower them under the voice), which record (a call), and whether the sound goes to headphones.
Needs `pactl` 16 or newer (JSON output: `pulseaudio-utils`, `pipewire-pulse`); without it these features are off.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

# Names our own processes go by: dictation and the wake word open the mic too, and the voice plays.
OURSELVES = ("python", "agent-voice", "agent_voice")


def available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("pactl") is not None


def _pactl(*args: str) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def items(kind: str) -> list[dict]:
    """`pactl -f json list <kind>` (sinks, sink-inputs, source-outputs); [] when pactl is missing or too old."""
    try:
        found = json.loads(_pactl("-f", "json", "list", kind) or "[]")
    except ValueError:
        return []
    return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []


def volume(item: dict) -> float:
    """A stream's volume, 0..1 (the average of its channels)."""
    levels = []
    for channel in (item.get("volume") or {}).values():
        try:
            levels.append(float(str(channel.get("value_percent", "100%")).rstrip("%")) / 100)
        except (AttributeError, ValueError):
            continue
    return sum(levels) / len(levels) if levels else 1.0


def set_volume(index: int, level: float) -> None:
    _pactl("set-sink-input-volume", str(int(index)), f"{round(max(0.0, min(1.5, level)) * 100)}%")


def _app(item: dict) -> tuple[str, int]:
    props = item.get("properties") or {}
    name = str(props.get("application.process.binary") or props.get("application.name") or "")
    try:
        pid = int(props.get("application.process.id") or 0)
    except ValueError:
        pid = 0
    return name, pid


def _ours(name: str, pid: int) -> bool:
    return pid == os.getpid() or any(us in name.lower() for us in OURSELVES)


def playing(streams: list[dict] | None = None) -> list[dict]:
    """Every other app's playback stream: [{'index', 'name', 'pid', 'volume', 'running'}]."""
    out = []
    for item in items("sink-inputs") if streams is None else streams:
        name, pid = _app(item)
        if not _ours(name, pid) and "index" in item:
            out.append({"index": int(item["index"]), "name": name, "pid": pid, "volume": volume(item),
                        "running": not item.get("corked", False)})
    return out


def recording_apps(streams: list[dict] | None = None) -> list[str]:
    """Other apps recording right now (a Zoom, Teams or Meet call): their names."""
    names = []
    for item in items("source-outputs") if streams is None else streams:
        name, pid = _app(item)
        props = item.get("properties") or {}
        if str(props.get("media.name", "")).lower().startswith("peak detect"):
            continue  # a volume meter (pavucontrol), not a call
        if name and not _ours(name, pid) and not item.get("corked", False):
            names.append(name)
    return names


def output_kind(sinks: list[dict] | None = None, default: str | None = None) -> tuple[str, str]:
    """('headphones' | 'speakers' | '', the device's description) for the default output."""
    default = _pactl("get-default-sink").strip() if default is None else default
    for sink in items("sinks") if sinks is None else sinks:
        if sink.get("name") != default:
            continue
        props = sink.get("properties") or {}
        form = str(props.get("device.form_factor") or "").lower()
        port = next((p for p in sink.get("ports") or [] if p.get("name") == sink.get("active_port")), {})
        said = f"{port.get('type', '')} {port.get('name', '')} {port.get('description', '')}".lower()
        label = str(sink.get("description") or default)
        if form in ("headphone", "headset", "hands-free", "handsfree") or "headphone" in said or "headset" in said:
            return "headphones", label
        if form in ("speaker", "internal", "tv", "computer") or "speaker" in said or "hdmi" in said:
            return "speakers", label
        return "", label
    return "", ""


# -- media players (MPRIS, through playerctl): what's playing pauses while you reply -----------------
_paused: list[str] = []


def pause_players() -> bool:
    """Pause every player that's playing; True if one was."""
    if not shutil.which("playerctl"):
        return False
    try:
        players = subprocess.run(["playerctl", "-l"], capture_output=True, text=True, timeout=3).stdout.split()
        for player in players:
            status = subprocess.run(["playerctl", "-p", player, "status"], capture_output=True, text=True,
                                    timeout=3).stdout.strip()
            if status == "Playing":
                subprocess.run(["playerctl", "-p", player, "pause"], capture_output=True, timeout=3)
                _paused.append(player)
    except (OSError, subprocess.SubprocessError):
        pass
    return bool(_paused)


def resume_players() -> None:
    """Play again what pause_players() paused (and nothing else)."""
    while _paused:
        player = _paused.pop()
        try:
            subprocess.run(["playerctl", "-p", player, "play"], capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass
