"""The banner's gestures, driven with real Qt mouse events (offscreen, no window shown)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtCore = pytest.importorskip("PySide6.QtCore")
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from agent_voice import widget  # noqa: E402

ITEM = {"id": 1, "text": "All the tests pass.", "title": "Notch redesign", "color": "#F59E0B", "session": "a"}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_swipe_up_dismisses_the_banner(app, monkeypatch):
    opened = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: opened.append(path))
    b = widget.Banner()
    b.present(ITEM, 420)
    assert b.until > 0
    start = QPoint(200, 40)
    QTest.mousePress(b, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(b, QPoint(200, 10))
    QTest.mouseRelease(b, Qt.LeftButton, Qt.NoModifier, QPoint(200, 10))
    assert b.until == 0.0 and opened == []  # gone, and nothing opened


def test_click_opens_the_conversation(app, monkeypatch):
    opened = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: opened.append((path, payload)))
    b = widget.Banner()
    b.present(ITEM, 420)
    QTest.mouseClick(b, Qt.LeftButton, Qt.NoModifier, QPoint(200, 40))
    assert opened == [("/open_thread", {"session": "a"})] and b.until == 0.0


def test_read_along_leaves_with_the_voice(app):
    b = widget.Banner()
    b.present(ITEM, 420, hold=True)
    assert b.until >= 1e12  # held for as long as the voice plays
    b.release()
    assert b.until <= widget.time.time() + 0.5 and not b.holding
