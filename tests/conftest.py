"""Every test runs against a throwaway ~/.agent-voice: your settings, history, logs and notch never change."""

import os
import tempfile

os.environ["AGENT_VOICE_HOME"] = tempfile.mkdtemp(prefix="agent-voice-tests-")
# Linux: no test reaches a real sound server, and pactl never starts one (it would outlive the tests).
os.environ["PULSE_SERVER"] = "unix:/nonexistent/agent-voice-tests"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def never_run_claude(monkeypatch):
    """Installing for Claude Code runs its `claude mcp` command: never against the real one in a test."""
    from agent_voice import install

    monkeypatch.setattr(install, "claude_mcp", lambda remove=False: "to-do list (test)")


@pytest.fixture
def patch_daemon(monkeypatch):
    """Patch a name the daemon's parts import (save_state, _beep, HOME...) in every part that has it: each module of
    agent_voice/daemon/ holds its own reference."""
    import importlib
    import pkgutil

    from agent_voice import daemon

    parts = [importlib.import_module(f"agent_voice.daemon.{m.name}") for m in pkgutil.iter_modules(daemon.__path__)]

    def patch(name, value):
        found = [part for part in parts if hasattr(part, name)]
        assert found, f"no part of the daemon has {name}"
        for part in found:
            monkeypatch.setattr(part, name, value)

    return patch


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
