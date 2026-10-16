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
    now = widget.time.time()
    assert now + 4.5 <= b.until <= now + 5.5 and not b.holding  # 5 s more to finish reading it


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


def test_tuck_hides_until_something_new(app):
    w = _widget({"phase": "idle", "threads": [],
                 "activity": {"session": "a", "title": "Notch", "color": "#F59E0B", "label": "Run pytest",
                              "steps": 3, "elapsed": 9, "others": 0}})
    w._hovering = True
    w._activate("tuck")
    w._place_notch()
    assert w._reveal_until == 0.0  # working, mouse still on it: it goes up anyway
    w.leaveEvent(None)
    w._on_status({**w.st, "phase": "idle"})
    w._place_notch()
    assert w._tucked is not None and w._reveal_until == 0.0  # the same activity: still tucked
    w._on_status({**w.st, "phase": "speaking"})  # news: it comes back down
    w._place_notch()
    assert w._tucked is None and w._reveal_until > 0


def test_banner_reply_and_talk_buttons(app, monkeypatch):
    posted, replies = [], []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    b = widget.Banner()
    b.on_reply = replies.append
    b.present(ITEM, 420)
    buttons = b._buttons()
    assert set(buttons) == {"type", "talk"}
    QTest.mouseClick(b, Qt.LeftButton, Qt.NoModifier, buttons["type"].center().toPoint())
    assert replies == [ITEM] and posted == []
    b.present(ITEM, 420)
    QTest.mouseClick(b, Qt.LeftButton, Qt.NoModifier, b._buttons()["talk"].center().toPoint())
    assert posted == [("/reply", {"session": "a"})]
    b.present({**ITEM, "kind": "you"}, 420)
    assert b._buttons() == {}  # your own words: nothing to answer


def test_reply_box_sends_to_its_conversation(app, monkeypatch):
    from PySide6.QtCore import QRectF

    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    box = widget.ReplyBox()
    box.open(ITEM, QRectF(0, 0, 440, 44), 422)
    QTest.keyClicks(box.edit, "Ship it")
    QTest.keyClick(box.edit, Qt.Key_Return, Qt.ShiftModifier)  # a new line, not sent
    QTest.keyClicks(box.edit, "and tag it")
    QTest.keyClick(box.edit, Qt.Key_Return)
    assert posted == [("/listen_cancel", None), ("/send", {"session": "a", "text": "Ship it\nand tag it"})]
    assert not box.isVisible() and box.edit.toPlainText() == ""


def test_thread_buttons_talk_and_type(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    threads = [{"session": "a", "title": "Notch", "color": "#F59E0B", "state": "", "ago": 5, "reply": "Done."},
               {"session": "b", "title": "API", "color": "#34D399", "state": "working", "ago": 5}]
    w = _widget({"phase": "idle", "threads": threads})
    rects = w._rects()
    assert {"thrtalk:0", "thrtype:0", "thrplay:0", "thrcopy:0", "thrtalk:1", "thrtype:1"} <= set(rects)
    assert "thrplay:1" not in rects  # nothing to replay yet
    opened = []
    monkeypatch.setattr(w, "open_reply", opened.append)
    w._activate("thrtalk:1")
    w._activate("thrtype:0")
    assert posted == [("/reply", {"session": "b"})] and opened == [threads[0]]


def test_agents_always_answer_a_click(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    w = _widget({"phase": "idle", "threads": [], "agents": {"claude": "on", "codex": "missing", "browser": "setup"}})
    w._activate("agent:claude")
    assert "is connected" in w.banner.item["text"]
    w._activate("agent:codex")
    assert "isn't installed" in w.banner.item["text"]
    w._activate("agent:browser")  # bridge ready, extension not loaded: the steps again
    assert posted == [("/install", {"agent": "browser"})]


def test_new_thread_box_types_or_dictates(app, monkeypatch):
    from PySide6.QtCore import QRectF

    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    box = widget.ReplyBox()
    box.open_new(["claude-app", "claude"], {"target": "claude", "cwd": "C:/work"}, QRectF(0, 0, 440, 44), 422)
    assert box.target == "claude" and not box.chips["codex"].isVisibleTo(box)
    box.chips["claude-app"].click()
    QTest.keyClicks(box.edit, "Add a health check")
    QTest.keyClick(box.edit, Qt.Key_Return)
    assert posted[-1] == ("/new_thread", {"text": "Add a health check", "target": "claude-app", "cwd": "C:/work"})
    box.open_new(["claude-app"], {"target": "claude-app", "cwd": "C:/work"}, QRectF(0, 0, 440, 44), 422)
    box.talk.click()
    assert posted[-1] == ("/new_thread", {"listen": True, "target": "claude-app", "cwd": "C:/work"})


def test_notifications_drop_below_the_reply_box(app):
    from PySide6.QtCore import QRectF

    w = _widget({"phase": "idle", "threads": []})
    w.reply_box.open(ITEM, QRectF(w.geometry()), int(w.pw))
    w.banner.present(ITEM, int(w.pw))
    for _ in range(40):
        w._tick()
    # Below the box, or above it on a short screen (no room below): never over it.
    assert not w.banner.geometry().intersects(w.reply_box.geometry())
    w.reply_box.hide()


def test_update_chip_installs_and_the_widget_quits(app, monkeypatch):
    posted, quits = [], []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append(path))
    monkeypatch.setattr(widget.QTimer, "singleShot", lambda ms, fn: quits.append(fn))
    w = _widget({"phase": "idle", "threads": [], "update": {"version": "0.6.0", "url": "u", "how": "auto", "state": ""}})
    assert "update" in w._rects() and "restarts" in w._tooltip("update")
    w._activate("update")
    assert posted == ["/update"] and not quits
    w._on_status({**w.st, "update": {**w.st["update"], "state": "installing"}})
    assert w._update_label() == "Restarting…" and quits == [widget.QApplication.quit]


def test_limits_rows_and_turning_claude_on(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    far = 4102444800
    w = _widget({"phase": "idle", "threads": [], "agents": {"claude": "on"}, "usage_line": "off",
                 "limits": {"codex": {"five_hour": {"pct": 48, "resets": far}}}})
    assert [s for s, _ in w._limit_rows()] == ["codex", "claude"]  # Claude: a row to turn it on
    assert w._agents_y() == w._limits_y() + 16 + 2 * widget.LIMIT_H + 8
    w._activate("limits:on")
    assert posted == [("/usage_line", {"enable": True})] and "limits:on" not in w._rects()
    w.grab()  # paints the section without errors
    assert widget._limit_color(95).name() == "#f87171"
