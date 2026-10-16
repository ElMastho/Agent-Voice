"""Linux smoke test (CI): the real pactl, xdotool and the notch on an X display, not mocks.

Run under a PulseAudio server and an X server (xvfb-run). Exits non-zero on the first thing that doesn't work.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from agent_voice import ducking, outputs, pulse, winfocus


def check(what: str, ok: bool, detail: object = "") -> None:
    print(f"{'ok ' if ok else 'FAIL'} {what} {detail}")
    if not ok:
        sys.exit(1)


def main() -> None:
    check("pactl is there", pulse.available())
    sinks = pulse.items("sinks")
    check("pactl lists sinks as JSON", bool(sinks), [s.get("name") for s in sinks])
    kind, name = pulse.output_kind()
    check("the default output is read", bool(name), (kind, name))
    check("outputs.OutputWatch polls it", outputs.OutputWatch().poll() == kind)
    check("ducking is available", ducking.available())
    check("other apps' streams are listed", isinstance(pulse.playing(), list), pulse.playing())

    # The notch on the X display: it starts, shows, and xdotool finds and reads its window.
    env = {**os.environ, "QT_QPA_PLATFORM": "xcb", "AGENT_VOICE_HOME": os.environ.get("AGENT_VOICE_HOME", "/tmp/av")}
    widget = subprocess.Popen([sys.executable, "-m", "agent_voice", "widget", "--demo", "idle"], env=env)
    try:
        found = 0
        for _ in range(40):
            time.sleep(0.25)
            check("the notch is still running", widget.poll() is None)
            found = winfocus.find_window("agent-voice")
            if found:
                break
        check("xdotool finds the notch's window", bool(found), found)
        check("its title reads", winfocus.title(found) == "agent-voice", winfocus.title(found))
        check("is_window", winfocus.is_window(found))
        check("idle time reads (0 without xprintidle)", winfocus.idle_seconds() >= 0)
    finally:
        widget.terminate()
        widget.wait(10)
    print("linux smoke test passed")


if __name__ == "__main__":
    main()
