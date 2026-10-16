"""Every test runs against a throwaway ~/.agent-voice: your settings, history, logs and notch never change."""

import os
import tempfile

os.environ["AGENT_VOICE_HOME"] = tempfile.mkdtemp(prefix="agent-voice-tests-")
