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


def _widget(st):
    w = widget.VoiceWidget()
    w.poller.status.disconnect()
    w.notch, w.compact, w.expanded = True, False, True
    w._on_status(st)
    w._apply_size()
    return w


NEEDS = [{"kind": "permission", "id": 7, "session": "c", "title": "Codex", "color": "#60A5FA",
          "text": "run npm run migrate", "at": 0},
         {"kind": "input", "session": "g", "title": "Gemini", "color": "#A78BFA", "text": "Apply? (y/n)", "at": 0}]


def test_needs_you_allow_and_open(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    w = _widget({"phase": "idle", "needs": NEEDS, "threads": []})
    rects = w._rects()
    assert {"allow:0", "deny:0", "need:0", "need:1"} <= set(rects) and "allow:1" not in rects
    w._activate("allow:0")
    w._activate("need:0")  # what's left: Gemini, waiting in its terminal
    assert posted == [("/permission_answer", {"id": 7, "allow": True}), ("/open_thread", {"session": "g"})]


def test_pills_show_live_conversations(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    threads = [{"session": "a", "title": "Notch", "color": "#F59E0B", "state": "working", "ago": 5},
               {"session": "c", "title": "Codex", "color": "#60A5FA", "state": "input", "ago": 9},
               {"session": "old", "title": "Old", "color": "#34D399", "state": "", "ago": 4000}]
    w = _widget({"phase": "idle", "threads": threads})
    assert [t["session"] for t in w._pills()] == ["a", "c"]  # the old one isn't live
    assert w._tooltip("pill:1") == "Codex: waiting for you · click to open"
    w._activate("pill:1")
    assert posted == [("/open_thread", {"session": "c"})]
    assert widget.VoiceWidget._pills(_widget({"phase": "idle", "threads": threads[:1]})) == []  # one: no pills


def test_dropped_files_go_to_the_conversation(app, monkeypatch, tmp_path):
    from PySide6.QtCore import QMimeData, QPointF, QUrl

    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    w = _widget({"phase": "idle", "threads": [{"session": "a", "title": "Notch", "color": "#F59E0B", "state": "",
                                                "ago": 5}], "last_reply": {"session": "a"}})
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(shot))])

    class Drop:
        def mimeData(self):
            return mime

        def position(self):
            return QPointF(5, 5)

        def acceptProposedAction(self):
            pass

    w.dragEnterEvent(Drop())
    assert w._drop == {"session": "a", "title": "Notch"}
    w.dropEvent(Drop())
    assert posted == [("/attach", {"paths": [str(shot).replace("\\", "/")], "text": "", "session": "a"})]
    assert w._drop is None
