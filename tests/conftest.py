"""Every test runs against a throwaway ~/.agent-voice: your settings, history, logs and notch never change."""

import os
import tempfile

os.environ["AGENT_VOICE_HOME"] = tempfile.mkdtemp(prefix="agent-voice-tests-")
# Linux: no test reaches a real sound server, and pactl never starts one (it would outlive the tests).
os.environ["PULSE_SERVER"] = "unix:/nonexistent/agent-voice-tests"



def pytest_sessionfinish(session, exitstatus):
    session.config.agent_voice_status = int(exitstatus)


def pytest_unconfigure(config):
    # Linux: Qt's offscreen platform can deadlock tearing itself down at exit, after every test has run and the
    # results are printed. Leave now, with pytest's own exit code.
    import sys

    if sys.platform.startswith("linux"):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(getattr(config, "agent_voice_status", 0))
