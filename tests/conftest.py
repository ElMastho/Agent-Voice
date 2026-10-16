"""Every test runs against a throwaway ~/.agent-voice: your settings, history, logs and notch never change."""

import os
import tempfile

os.environ["AGENT_VOICE_HOME"] = tempfile.mkdtemp(prefix="agent-voice-tests-")
# Linux: no test reaches a real sound server, and pactl never starts one (it would outlive the tests).
os.environ["PULSE_SERVER"] = "unix:/nonexistent/agent-voice-tests"


def pytest_unconfigure(config):
    # Something holding the exit (a thread, an audio driver): print where every thread is, and leave anyway.
    import faulthandler
    import sys

    faulthandler.dump_traceback_later(30, exit=True, file=sys.stderr)
