"""Build-time setup for the release exe: comtypes UI Automation wrappers and the app icon."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# comtypes writes Python wrappers for COM type libraries on first use; bundle them instead.
import comtypes.client  # noqa: E402

comtypes.client.GetModule("UIAutomationCore.dll")

# The widget's orb, as the exe icon.
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from agent_voice import widget  # noqa: E402

app = QApplication([])
icon = widget.orb_icon(QColor(widget.PHASES["speaking"][0]), widget.ICONS["volume"])
icon.pixmap(256, 256).save(str(HERE / "agent-voice.ico"))
print("prepared")
