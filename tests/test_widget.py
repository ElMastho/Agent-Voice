"""The banner's gestures, driven with real Qt mouse events (offscreen, no window shown)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtCore = pytest.importorskip("PySide6.QtCore")
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from agent_voice import widget  # noqa: E402

ITEM = {"id": 1, "text": "All the tests pass.", "title": "Notch redesign", "color": "#F59E0B", "session": "a"}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def own_position_file(tmp_path, monkeypatch):
    # Never the real ~/.agent-voice/widget.json: what the user left folded or hidden mustn't change the tests.
    monkeypatch.setattr(widget, "POS_FILE", tmp_path / "widget.json")


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


# Every test widget lives until the run ends: torn down by the garbage collector halfway (with its side panels,
# timers and Qt children in whatever order) it can crash Python. The app has one, until it quits.
_KEEP: list = []


def _widget(st):
    w = widget.VoiceWidget()
    _KEEP.append(w)
    w.poller.stop()
    w.poller.status.disconnect()
    # One app has one widget; the tests make many: a copy must not reach the ones Qt is tearing down.
    QApplication.clipboard().dataChanged.disconnect(w._clip_changed)
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
    w._on_status({**w.st, "phase": "idle"})  # the news has passed: back up to the line
    w._place_notch()
    assert w._tucked is not None and not w._retuck


def test_closing_the_banner_tucks_the_notch_again(app, monkeypatch):
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: None)
    w = _widget({"phase": "idle", "threads": []})
    w.expanded = False
    w.tuck()
    w._on_status({**w.st, "banner": {**ITEM, "id": 5, "age": 0}})  # a notification brings it down
    w._place_notch()
    assert w._tucked is None and w._retuck
    QTest.mouseClick(w.banner, Qt.LeftButton, Qt.NoModifier, w.banner._close_rect().center().toPoint())
    assert w._tucked is not None and w._reveal_until == 0.0  # straight back up to the line


def test_banner_close_button(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append(path))
    b = widget.Banner()
    b.present(ITEM, 420)
    QTest.mouseClick(b, Qt.LeftButton, Qt.NoModifier, b._close_rect().center().toPoint())
    assert b.until == 0.0 and posted == []  # gone, nothing opened


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
        w._last_tick -= widget.FRAME  # animations run on real time: each tick is one 30 fps frame here
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


def test_usage_card_rows_refresh_and_turning_claude_on(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    far = 4102444800
    w = _widget({"phase": "idle", "threads": [], "agents": {"claude": "on"}, "usage_line": "off",
                 "limits": {"codex": {"five_hour": {"pct": 48, "resets": far}}}})
    assert [s for s, _ in w._limit_rows()] == ["codex", "claude"]  # Claude: a row to turn it on
    assert not any(k.startswith("limits:") for k in w._rects())  # only in the Companion's Usage card now
    side = w.companion
    side._build(None)
    side.activate("uon")
    side.activate("urefresh")
    assert posted == [("/usage_line", {"enable": True}), ("/limits_refresh", None)]
    side._build(None)
    assert "uon" not in side.hits  # on its way: no button
    side.grab()  # paints the card without errors
    assert widget._limit_color(95).name() == "#f87171"


def test_opening_the_notch_resizes_its_window_rarely(app, monkeypatch):
    monkeypatch.setattr(widget, "SCREEN_W", 1920)  # a laptop: open, it takes a third of the screen
    w = _widget({"phase": "idle", "threads": []})
    w.expanded = False
    w._apply_size()
    for _ in range(30):
        w._last_tick -= widget.FRAME
        w._tick()
    sizes, widths = [], []
    w.toggle_expanded()
    for _ in range(90):
        w._last_tick -= widget.FRAME
        w._tick()
        sizes.append((w.width(), w.height()))
        widths.append(round(w._vw))
    assert len(set(widths)) > 10  # the island glides open...
    assert len(set(sizes)) <= 3  # ...inside a canvas sized once (and trimmed at the end), not every frame
    assert len({x for x, _ in sizes}) == 1  # never wider or narrower: it doesn't jolt sideways at the end
    assert abs(w._vw - widget.PANEL_MAX_W) < 1 and w.height() == round(w._vis_h())



def test_the_panel_holds_threads_and_settings_hold_the_switches(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    monkeypatch.setattr(widget, "SCREEN_W", 1920)
    st = {"phase": "idle", "threads": [], "phone": {"enabled": True, "url": "https://ntfy.sh/agent-voice-abc"},
          "agents": {"claude": "on", "codex": "off"}, "summary": True, "wake": False}
    w = _widget(st)
    rects = w._rects()
    # The panel: what needs you, the threads, the actions that aren't about one thread, and the agents' line.
    assert {"hold", "stop", "read", "newthread", "history", "agents", "settings"} <= set(rects)
    assert not {"phone:test", "summary:on", "agent:claude", "reply", "replay", "copyreply"} & set(rects)
    assert w._agent_counts() == (1, 1)
    w._activate("agents")  # its line opens Settings, where the agents are
    rects = w._rects()
    assert w.settings and {"phone:test", "phone:new", "phone:fold", "phone:copy", "agent:claude", "guide",
                           "setdone"} <= set(rects)
    for name, (_label, options) in widget.SETTING_ROWS.items():
        assert {f"{name}:{value}" for value, _text in options} <= set(rects), name
    assert w._agents_y() > w._phone_y() + widget.PHONE_H  # the QR code under its switch, the agents last
    w._activate("phone:test")
    w._activate("summary:off")
    w._activate("wake:on")
    w._activate("speed:1.25")
    assert posted == [("/phone", {"action": "test"}), ("/summary", {"mode": "off"}), ("/wake", {"mode": "on"}),
                      ("/speed", {"value": 1.25})]
    assert w._setting_value("summary") == "off" and w._setting_value("wake") == "on"  # the switch moves at once
    w._apply_size()  # the panel has glided to the sheet's height
    w.grab()  # the sheet, the QR code and the agents paint without errors
    assert widget._QR  # the code was made once, and kept
    w._activate("setdone")
    assert not w.settings and "phone:test" not in w._rects()
    w._activate("settings")
    w.toggle_expanded()
    assert not w.settings  # closing the panel: it opens on the threads next time


def test_reply_box_schedules_for_later(app, monkeypatch):
    from PySide6.QtCore import QRectF

    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    box = widget.ReplyBox()
    box.open(ITEM, QRectF(0, 0, 440, 44), 422)
    QTest.keyClicks(box.edit, "Run the tests")
    box.when.setText("never o'clock")
    box.send()
    assert posted == [("/listen_cancel", None)] and box.isVisible()  # not a time: nothing sent, the field says so
    box.when.setText("in 30 minutes")
    box.send()
    assert posted[-1][0] == "/schedule" and posted[-1][1]["session"] == "a" and posted[-1][1]["prompt"] == "Run the tests"
    assert not box.isVisible() and box.when.text() == ""



def test_banner_follows_the_notch_width(app, monkeypatch):
    from PySide6.QtCore import QRectF

    monkeypatch.setattr(widget, "SCREEN_W", 1920)
    w = _widget({"phase": "idle", "threads": []})
    w.expanded = False
    w._reveal_until = 1e12  # down, as it is while you look at it
    w.banner.present(ITEM, 462)
    w.toggle_expanded()  # opening: the banner widens with it, frame by frame
    widths = []
    for _ in range(60):
        w._last_tick -= widget.FRAME
        w._tick()
        widths.append(w.banner.width())
    assert widths[-1] == int(w.visual_rect().width() - 2 * widget.NOTCH_EAR) and len(set(widths)) > 5
    assert w.banner.y() >= w.visual_rect().bottom()  # right under the notch
    w.banner.step(QRectF(0, 0, 500, 44), widget.FRAME, 400)
    assert w.banner.width() == 400


def test_new_thread_folder_from_recent_or_browse(app, monkeypatch):
    from PySide6.QtCore import QRectF

    box = widget.ReplyBox()
    box.open_new(["claude"], {"target": "claude", "cwd": "C:/a", "recent": ["C:/a", "C:/b"]},
                 QRectF(0, 0, 440, 44), 422)
    assert box.folder_row.isVisibleTo(box) and box.recent == ["C:/a", "C:/b"] and box.folder.completer() is not None
    box._use_folder("C:/b")
    assert box.folder.text() == "C:/b"
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a: "C:/picked"))
    box._browse()
    assert box.folder.text().replace("\\", "/") == "C:/picked" and not box._picking
    box.hide()


def test_phone_card_folds_to_one_line(app, monkeypatch):
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: None)
    monkeypatch.setattr(widget, "SCREEN_W", 1920)
    monkeypatch.setattr(widget.VoiceWidget, "_save_position", lambda self: None)
    w = _widget({"phase": "idle", "threads": [], "phone": {"enabled": True, "url": "https://ntfy.sh/agent-voice-ab"}})
    w.settings = True
    w._activate("phone:fold")
    assert w.phone_folded and w._phone_h() == widget.PHONE_FOLDED_H + 10 and "phone:new" not in w._rects()
    w.grab()
    w._activate("phone:fold")
    assert not w.phone_folded and "phone:new" in w._rects()


def test_button_tooltips_explain_once(app):
    w = _widget({"phase": "idle", "threads": []})
    assert w._tooltip("wake:on").startswith("Hey Claude: while idle")  # a settings row explains itself
    hold = w._tooltip("hold")
    assert hold.count("Hold to talk") == 1  # the name once, with its shortcut


def test_take_back_banner_sends_keeps_or_drops(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    b = widget.Banner()
    b.present({"id": 3, "kind": "pending", "pending": 9, "until": widget.time.time() + 5, "title": "Codex",
               "session": "c", "text": "Deploy to production"}, 420)
    rects = b._buttons()
    assert set(rects) == {"send", "edit", "cancel"} and b.until > widget.time.time() + 5
    QTest.mouseClick(b, Qt.LeftButton, Qt.NoModifier, rects["edit"].center().toPoint())
    assert posted == [("/pending", {"id": 9, "action": "hold"})] and b.until > 0  # the next banner replaces it


def test_do_not_disturb_has_a_way_back_on_the_notch(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    w = _widget({"phase": "muted", "output": "silent", "dnd_from": "chime", "threads": [],
                 "activity": {"session": "a", "title": "Codex", "color": "#60A5FA", "label": "Run", "steps": 1,
                              "elapsed": 3}})
    assert w._primary()[0] == "undnd" and "Chimes" in w._primary()[2]
    w.expanded, w._reveal_until = False, 0.0
    w._place_notch()
    assert w._reveal_until == 0.0  # an agent at work doesn't bring it down
    w._activate("primary")
    assert posted == [("/output", {"mode": "chime"})] and w._output() == "chime"


def test_voice_engine_switch(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    w = _widget({"phase": "idle", "output": "voice", "engine": "piper", "threads": []})
    assert "engine:kokoro" not in w._rects()  # in Settings
    w.toggle_settings(True)
    assert {"engine:piper", "engine:kokoro"} <= set(w._rects())
    w._activate("engine:kokoro")
    assert posted == [("/engine", {"engine": "kokoro"})] and "120 MB" in w._tooltip("engine:kokoro")
    w._on_status({"phase": "idle", "output": "chime", "threads": []})
    assert "engine:kokoro" in w._rects() and "read" not in w._tiles()  # Settings stay put; no voice to read with


def test_history_box_lists_and_shows_entries(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    asked = []
    box = widget.HistoryBox()
    monkeypatch.setattr(box, "_get", lambda path, tag: asked.append(path))
    now = widget.time.time()
    box._fill([{"id": 4, "at": now, "session": "a", "agent": "claude", "title": "Login", "kind": "reply",
                "snippet": "Fixed the [login] bug"},
               {"id": 2, "at": now - 60, "session": "a", "agent": "claude", "title": "Login", "kind": "you",
                "snippet": "fix the login"}])
    assert box.results.count() == 2 and "Fixed the login bug" in box.results.item(0).text()
    assert asked == ["/history?id=4"]  # the first one opens on the right
    box._arrived((("entry", 4), {"id": 4, "at": now, "session": "a", "agent": "claude", "title": "Login",
                                 "kind": "reply", "text": "**Fixed** the login bug."}))
    assert "Fixed the login bug." in box.body.toPlainText() and box.open_thread.isEnabled()
    box._open_thread()
    assert posted == [("/open_thread", {"session": "a"})]
    box._asked = "zzz"
    box._fill([])
    assert "Nothing found" in box.body.toPlainText()
    w = _widget({"phase": "idle", "threads": []})
    assert "history" in w._rects() and "every conversation" in w._tooltip("history")


COMPANION = {"timers": [{"id": 1, "label": "Tea", "total": 600, "end": 0, "left": 240.0}],
             "stopwatch": {"start": 0.0, "elapsed": 0.0},
             "todos": [{"id": 2, "text": "Review the PR", "done": False, "source": "claude", "at": 0},
                       {"id": 3, "text": "Release 0.6.0", "done": True, "source": "you", "at": 0}],
             "reminders": [{"id": 4, "text": "Stand-up", "at": 2_000_000_000}]}


def test_companion_opens_beside_the_panel(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    w = _widget({"phase": "idle", "threads": [], "companion": COMPANION})
    w._reveal = 1.0
    assert "tab:utils" in w._rects() and "Utils" in w._tooltip("tab:utils")
    w._activate("tab:utils")
    w.show()
    assert w.side_open['utils'] and w._side_wanted('utils')
    w.utils.step(w.visual_rect(), True)
    side, beside = w.utils.geometry(), w.visual_rect()
    assert w.utils.isVisible() and (side.left() > beside.right() or side.right() < beside.left())  # apart
    hits = w.utils.hits
    assert {"preset:5", "tpause:1", "tcancel:1", "swtoggle", "todo:2", "tclear", "todo_add"} <= set(hits)
    assert "rdel:4" not in hits  # reminders are in the Companion's agenda
    for key in ("preset:5", "tpause:1", "todo:2", "tclear", "swtoggle"):
        w.utils.activate(key)
    assert posted == [("/timer", {"action": "start", "seconds": 300}), ("/timer", {"action": "resume", "id": 1}),
                      ("/todo", {"action": "toggle", "id": 2}), ("/todo", {"action": "clear_done"}),
                      ("/stopwatch", {"action": "toggle"})]
    w.set_side_widget("utils", "stopwatch", False)
    assert "swtoggle" not in w.utils.hits and w.side_widgets["utils"] == ["pomodoro", "timer", "todo", "notes", "clipboard"]
    w.toggle_expanded()  # the panel closes: Utils goes with it, and is back when it opens
    assert not w._side_wanted('utils') and w.side_open['utils']
    w.hide()


def test_notch_shows_a_running_timer_on_a_second_line(app, monkeypatch):
    now = widget.time.time()
    running = {**COMPANION, "timers": [{"id": 1, "label": "Tea", "total": 600, "end": now + 125, "left": 0.0}]}
    w = _widget({"phase": "idle", "threads": [], "companion": running})
    w.expanded = False
    assert w._target_size()[1] == widget.NOTCH_H + widget.STRIP_H and "strip" in w._rects()
    w._reveal = 1.0
    assert w._strip_key() in (("2:05",), ("2:04",))
    w._on_status({"phase": "idle", "threads": [], "companion": running, "output": "silent"})
    assert w._strip_h() == 0  # Do not disturb: the notch stays up
    w._on_status({"phase": "idle", "threads": [], "companion": COMPANION})
    assert w._strip_items()[0][2] == "4:00"  # paused: what was left


def test_notch_second_line_wraps_instead_of_losing_the_song_buttons(app, monkeypatch):
    now = widget.time.time()
    busy = {**COMPANION, "timers": [], "pomodoro": {"phase": "focus", "end": now + 1440, "left": 0.0, "length": 1500}}
    agenda = {"events": [{"title": "Weekly sync with the platform team", "start": now + 300, "end": now + 2100,
                          "all_day": False}]}
    media = {"app": "Spotify", "playing": True, "title": "A rather long song title", "artist": "Somebody"}
    w = _widget({"phase": "idle", "threads": [], "companion": busy, "agenda": agenda, "media": media})
    w.expanded = False
    spans, buttons = w._strip_layout()
    assert len(spans) == 3 and len({y for _x, y, _w in spans}) == 2  # two rows
    assert w._strip_h() == 2 * widget.STRIP_H and w._target_size()[1] == widget.NOTCH_H + 2 * widget.STRIP_H
    assert buttons["snext"].right() <= w.nw - widget.NOTCH_EAR  # still on the island, clickable
    assert w._rects()["strip"].height() == 2 * widget.STRIP_H


def test_timer_banner_snoozes_or_ends(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    b = widget.Banner()
    b.present({"id": 3, "text": "Tea", "kind": "timer", "tag": "Timer done", "at": 0,
               "snooze": {"kind": "timer", "seconds": 300, "label": "Tea"}}, 420)
    assert set(b._buttons()) == {"snooze", "ok"} and b.until > widget.time.time() + 50
    QTest.mouseClick(b, Qt.LeftButton, Qt.NoModifier, b._buttons()["snooze"].center().toPoint())
    assert posted == [("/timer", {"action": "start", "seconds": 300, "label": "Tea"})] and b.until == 0.0


def test_quick_input_parses_before_sending(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    box = widget.QuickInput()
    box.mode = "reminder"
    box.edit.setText("someday stretch")
    box.submit()
    assert posted == [] and "248,113,113" in box.edit.styleSheet()  # not a time: said, kept
    box.edit.setText("in 20 minutes stretch")
    box.submit()
    assert posted[0][0] == "/reminder" and posted[0][1]["text"] == "Stretch"
    box.mode = "timer"
    box.edit.setText("12 min tea")
    box.submit()
    assert posted[1] == ("/timer", {"action": "start", "seconds": 720, "label": "Tea"})


def test_every_companion_control_answers_a_real_click(app, monkeypatch):
    posted, opened = [], []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    running = {**COMPANION, "stopwatch": {"start": widget.time.time() - 5, "elapsed": 0.0}}
    w = _widget({"phase": "idle", "threads": [], "companion": running})
    w._reveal, w.side_open['utils'] = 1.0, True
    w.show()
    side = w.utils
    side.step(w.visual_rect(), True)
    side.repaint()  # what is drawn is what is clickable
    monkeypatch.setattr(side.input, "open", lambda mode, at: opened.append(mode))
    monkeypatch.setattr(side, "choose_widgets", lambda: opened.append("menu"))
    expected = {"mic": ("/companion_listen", None), "preset:25": ("/timer", {"action": "start", "seconds": 1500}),
                "tpause:1": ("/timer", {"action": "resume", "id": 1}), "tcancel:1": ("/timer", {"action": "cancel", "id": 1}),
                "swtoggle": ("/stopwatch", {"action": "toggle"}), "swreset": ("/stopwatch", {"action": "reset"}),
                "todo:2": ("/todo", {"action": "toggle", "id": 2}), "tdel:2": ("/todo", {"action": "remove", "id": 2}),
                "tclear": ("/todo", {"action": "clear_done"})}
    for key, call in expected.items():
        posted.clear()
        QTest.mouseClick(side, Qt.LeftButton, Qt.NoModifier, side.hits[key].center().toPoint())
        assert posted == [call], key
    for key, mode in (("tcustom", "timer"), ("todo_add", "todo"), ("menu", "menu")):
        QTest.mouseClick(side, Qt.LeftButton, Qt.NoModifier, side.hits[key].center().toPoint())
        assert opened[-1] == mode, key
    w.hide()


def test_pomodoro_card_and_second_line(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    now = widget.time.time()
    pomo = {"phase": "focus", "end": now + 600, "left": 0.0, "length": 1500, "round": 1, "today": 1,
            "day": widget._today(now), "every": 4, "focus_length": 1500}
    w = _widget({"phase": "idle", "threads": [], "companion": {**COMPANION, "timers": [], "pomodoro": pomo}})
    w._reveal, w.side_open['utils'] = 1.0, True
    w.show()
    w.utils.step(w.visual_rect(), True)
    w.utils.repaint()
    for key, action in (("ptoggle", "toggle"), ("pskip", "skip"), ("pstop", "stop")):
        QTest.mouseClick(w.utils, Qt.LeftButton, Qt.NoModifier, w.utils.hits[key].center().toPoint())
        assert posted[-1] == ("/pomodoro", {"action": action})
    w.expanded = False
    assert w._strip_items()[0][1] == "Focus" and w._strip_items()[0][2] in ("10:00", "9:59")
    w.hide()


def test_companion_folds_before_the_panel_and_opens_after_it(app):
    w = _widget({"phase": "idle", "threads": [], "companion": COMPANION})
    w._reveal, w.side_open['utils'] = 1.0, True
    w.show()

    def run(frames, until):
        for _ in range(frames):
            w._last_tick -= widget.FRAME
            w._tick()
            if until():
                return True
        return False

    assert run(120, lambda: w.utils.isVisible())
    w.toggle_expanded()  # close: the side panels first, the panel still open meanwhile
    assert w.expanded and not w._side_wanted('utils')
    assert run(200, lambda: not w.expanded) and not w.utils.isVisible()
    assert run(200, lambda: abs(w._h - w._target_size()[1]) < 0.5)  # the panel has closed
    w.toggle_expanded()  # open: the panel grows first, the side panels wait for it
    w._tick()
    assert w.expanded and not w.utils.isVisible()
    assert run(200, lambda: w.utils.isVisible())
    w.hide()


def test_companion_is_never_taller_than_the_panel(app, monkeypatch):
    todos = [{"id": 100 + i, "text": f"Item {i}", "done": False, "source": "you", "at": 0} for i in range(8)]
    notes = [{"id": 200 + i, "text": f"N {i}", "at": 0} for i in range(4)]
    timers = [{"id": 300 + i, "label": "", "total": 600, "end": 0, "left": 60.0} for i in range(3)]
    w = _widget({"phase": "idle", "threads": [], "companion": {**COMPANION, "todos": todos, "notes": notes,
                                                                  "timers": timers}})
    w._reveal, w.side_open['utils'] = 1.0, True
    monkeypatch.setattr(w, "_side_max_h", lambda: 400)
    w.show()
    side = w.utils
    side.step(w.visual_rect(), True)
    side.grab()  # paints it: what is drawn is what is clickable
    assert side.height() == 400 and side._content_h > 400
    assert "ndel:203" not in side.hits  # past the bottom: not clickable until scrolled to
    side.scroll = side._content_h - side.height()
    side.grab()  # paints it: what is drawn is what is clickable
    assert "ndel:203" in side.hits and "preset:5" not in side.hits
    w.hide()


def test_companion_on_the_left_plays_music_and_shows_the_day(app, monkeypatch):
    posted, opened = [], []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    now = widget.time.time()
    st = {"phase": "idle", "threads": [], "companion": COMPANION,
          "media": {"app": "Spotify", "title": "Get Lucky", "artist": "Daft Punk", "playing": True},
          "agenda": {"events": [{"title": "Stand-up", "location": "", "start": now + 300, "end": now + 1200,
                                 "all_day": False}], "sources": 1, "error": ""},
          "schedules": [{"id": 3, "at": now + 600, "title": "Codex", "prompt": "Run the tests", "repeat": ""}],
          "day": {"turns": 23, "threads": 4, "tokens": 1_800_000, "hours": [0] * 9 + [3, 5, 2] + [0] * 12}}
    w = _widget(st)
    w._reveal = 1.0
    assert {"tab:utils", "tab:companion"} <= set(w._rects())
    w._activate("tab:companion")
    w.show()
    side = w.companion
    side.step(w.visual_rect(), True)
    side.grab()
    beside = w.visual_rect()
    room = beside.left() - widget.TAB_W - 2 - side.W >= side.screen().availableGeometry().left() + 8
    assert side.isVisible() and (side.geometry().right() < beside.left() if room  # on the left (else the right)
                                 else side.geometry().left() > beside.right())
    assert "mic" not in side.hits  # the tools' mic is in Utils
    monkeypatch.setattr(side.input, "open", lambda mode, at: opened.append(mode))
    # The agenda: the meeting, then the scheduled prompt, then the reminder (far off), in time order.
    rows = sorted((side.hits[k].y(), k) for k in ("prow:3", "rrow:4"))
    assert [k for _y, k in rows] == ["prow:3", "rrow:4"]
    for key, call in (("mtoggle", ("/media", {"action": "toggle"})), ("mnext", ("/media", {"action": "next"})),
                      ("calunlink", ("/calendar", {"action": "clear"})),
                      ("rdel:4", ("/reminder", {"action": "remove", "id": 4})),
                      ("pdel:3", ("/schedule_cancel", {"id": 3}))):
        side.grab()
        QTest.mouseClick(side, Qt.LeftButton, Qt.NoModifier, side.hits[key].center().toPoint())
        assert posted[-1] == call, key
    assert "pdel:3" not in side.hits  # gone from the list at once
    for key in ("cal_add", "rem_add"):
        QTest.mouseClick(side, Qt.LeftButton, Qt.NoModifier, side.hits[key].center().toPoint())
    assert opened == ["calendar", "reminder"]
    assert w.side_widgets["companion"] == ["activity", "media", "agenda", "usage"]
    w.expanded = False
    assert any(item[1] == "Stand-up" and item[2] == "in 5 min" for item in w._strip_items())  # on the second line
    assert any(item[2] == "Get Lucky · Daft Punk" for item in w._strip_items())  # the song too, while it plays
    w.st["media"] = {**w.st["media"], "playing": False}
    assert any(item[0] == widget.ICONS["music"] for item in w._strip_items())  # paused: its controls stay
    w.st["media"] = None
    assert not any(item[0] == widget.ICONS["music"] for item in w._strip_items())  # no player open: gone
    w.hide()


def test_a_song_lights_the_notch_and_skips_from_it(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    st = {"phase": "idle", "threads": [], "companion": {**COMPANION, "timers": []},
          "media": {"app": "Spotify", "title": "Get Lucky", "artist": "Daft Punk", "playing": True}}
    w = _widget(st)
    w._reveal, w.expanded = 1.0, False
    w.show()
    assert w._music_glow() and w._glow_moving() and w._edge_live() == 0.7  # nothing else on: the song's light
    rects = w._rects()
    assert {"sprev", "splay", "snext"} <= set(rects)
    assert rects["sprev"].right() <= rects["splay"].left() and rects["splay"].right() <= rects["snext"].left()
    for key, action in (("sprev", "previous"), ("snext", "next")):
        w._activate(key)
        assert posted[-1] == ("/media", {"action": action})
    assert w._tooltip("splay") == "Pause"
    w.grab()  # paints the second line with its buttons
    w.st["activity"] = {"text": "Editing", "color": "#8B7CFF"}
    assert not w._music_glow()  # an agent at work: its own light
    w.st.pop("activity")
    w.st["media"] = {**w.st["media"], "playing": False}
    assert not w._music_glow() and not w._glow_moving()  # paused: the light is still again...
    assert {"sprev", "splay", "snext"} <= set(w._rects()) and not w._strip_busy()  # ...the controls stay, not holding it
    w._activate("splay")
    assert posted[-1] == ("/media", {"action": "toggle"}) and w.st["media"]["playing"]  # shown playing at once
    w.hide()
    w.poller.stop()


def test_utils_clipboard_notes_and_agent_todos(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    todos = [{"id": 2, "text": "Run the migration on staging", "done": False, "source": "claude", "at": 0,
              "session": "a"}]
    notes = [{"id": 5, "text": "Two monitors only", "at": 0}]
    st = {"phase": "idle", "threads": [], "last_reply": {"session": "a"},
          "companion": {**COMPANION, "todos": todos, "notes": notes}}
    w = _widget(st)
    w._reveal, w.side_open["utils"] = 1.0, True
    QApplication.clipboard().dataChanged.connect(w._clip_changed)  # this one listens to copies
    QApplication.clipboard().setText("SELECT * FROM users;")
    app.processEvents()
    QApplication.clipboard().dataChanged.disconnect(w._clip_changed)
    assert w.clips[0] == "SELECT * FROM users;"  # kept in memory
    w.show()
    side = w.utils
    side.step(w.visual_rect(), True)
    side.scroll = 0
    side.grab()
    w.utils.activate("trow:2")
    w.utils.activate("ndel:5")
    w.utils.activate("clipsend:0")
    assert posted == [("/open_thread", {"session": "a"}), ("/note", {"action": "remove", "id": 5}),
                      ("/attach", {"paths": [], "text": "SELECT * FROM users;", "session": "a"})]
    w.hide()


def test_light_theme_is_saved_and_swaps_the_palette(app, monkeypatch):
    assert widget._saved_theme() == "dark"  # the default
    assert widget._qss("color:rgba(255,255,255,90);") == "color:rgba(255,255,255,90);"
    w = _widget({"phase": "idle", "threads": []})
    monkeypatch.setattr(w, "_relaunch", lambda: None)
    try:
        w.toggle_theme()
        assert widget.LIGHT and widget._saved_theme() == "light"  # a fresh widget starts light
        assert widget._ink().name() == "#111827" and widget._body().name() == "#f8f9fb"
        assert widget._qss("#box{background:#000;color:rgba(255, 255, 255,90);}") == (
            "#box{background:#F8F9FB;color:rgba(17,24,39,90);}")
        assert widget._tone("#FBBF24").value() < QColor("#FBBF24").value()  # accents darker, to read on white
        assert not w.glass.follow(w.geometry(), True)  # offscreen: never a grab of the screen
        w.grab()  # glass with nothing behind it: frosted
    finally:
        widget.set_theme("dark")
    w.hide()


TODOS = [{"id": 2, "text": "Review the PR", "done": False, "source": "claude", "at": 0, "category": "agent-voice"},
         {"id": 3, "text": "Call Paul", "done": False, "source": "you", "at": 0, "category": "Work"},
         {"id": 4, "text": "Buy milk", "done": False, "source": "you", "at": 0}]


def test_todo_card_separates_yours_and_your_agents_by_category(app, monkeypatch):
    posted, opened = [], []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    w = _widget({"phase": "idle", "threads": [], "companion": {**COMPANION, "todos": TODOS}})
    w._reveal, w.side_open["utils"] = 1.0, True
    w.show()
    side = w.utils
    side.step(w.visual_rect(), True)
    side._build(None)  # laid out: every card's rows, scrolled into view or not
    rows = {int(k.split(":")[1]): r.y() for k, r in side.hits.items() if k.startswith("trow:")}
    assert rows[4] < rows[3] < rows[2]  # yours (no category, then Work), then your agents'
    monkeypatch.setattr(side.input, "open", lambda mode, at: opened.append((mode, side.input.item_id)))
    side._ask_category(3)
    side.input.mode, side.input.item_id = "category", 3
    side.input.edit.setText("home")
    side.input.submit()
    assert opened == [("category", 3)] and posted[-1] == ("/todo", {"action": "category", "id": 3, "category": "home"})
    w.hide()


def test_a_util_goes_large_over_the_panel_and_back(app, monkeypatch):
    posted = []
    monkeypatch.setattr(widget, "_post", lambda path, payload=None: posted.append((path, payload)))
    monkeypatch.setattr(widget, "SCREEN_W", 1920)  # a laptop: the open panel takes a third of it
    many = TODOS + [{"id": 100 + i, "text": f"Item {i}", "done": False, "source": "you", "at": 0} for i in range(12)]
    w = _widget({"phase": "idle", "threads": [], "companion": {**COMPANION, "todos": many}})
    w._reveal, w.side_open["utils"] = 1.0, True
    w.show()
    for _ in range(60):  # the panel opens to its width
        w._last_tick -= widget.FRAME
        w._tick()
    w.utils.step(w.visual_rect(), True)
    w.utils._build(None)
    assert {f"full:{card}" for card in widget.SIDE_CARDS["utils"]} <= set(w.utils.hits)
    w.utils.activate("full:todo")
    full = w.full
    try:
        island = w.visual_rect()
        assert full.isVisible() and full.card == "todo" and not w._folding  # over the panel, which stays open
        assert island.left() < full.x() and full.geometry().right() < island.right()  # as wide as the panel
        assert full.y() >= island.y() + widget.NOTCH_H  # the notch's row in sight
        full.grab()
        assert {"trow:2", "full:todo", "fclose"} <= set(full.hits)
        mine, agents = full.hits["trow:4"], full.hits["trow:2"]  # the first of yours, the first of your agents'
        assert agents.x() > mine.right() and abs(agents.y() - mine.y()) < 30  # two columns
        point = full.hits["todo:2"].center()
        QTest.mouseClick(full, Qt.LeftButton, Qt.NoModifier, (point * full.s).toPoint())  # clicks are scaled too
        assert posted[-1] == ("/todo", {"action": "toggle", "id": 2})
        QTest.mouseClick(full, Qt.LeftButton, Qt.NoModifier, QPoint(5, full.height() - 5))  # beside the card
        assert not full.isVisible() and not w._folding and w.expanded  # the panel stays open
    finally:
        full.close_card()
        w.hide()


def test_tucked_notch_glows_while_something_runs(app):
    act = {"session": "a", "title": "Notch", "color": "#F59E0B", "label": "Edit", "steps": 2, "elapsed": 5}
    w = _widget({"phase": "idle", "threads": [], "activity": act})
    w.expanded, w._reveal = False, 0.0  # tucked: only its last pixels show
    w.show()
    assert w._peek_color() is not None and w._glow_moving()  # an agent works: the line glows, gently moving
    w.grab()  # paints it without errors
    w._on_status({"phase": "idle", "threads": [], "activity": act, "output": "silent"})
    assert w._peek_color() is None  # Do not disturb: quiet
    now = widget.time.time()
    w._on_status({"phase": "idle", "threads": [], "companion": {**COMPANION, "timers": [
        {"id": 1, "label": "", "total": 60, "end": now + 30, "left": 0.0}]}})
    assert w._peek_color() is not None  # a timer runs
    w._on_status({"phase": "idle", "threads": []})
    assert w._peek_color() is None and not w._glow_moving()  # nothing runs: no glow, no redraws
    w.hide()


def test_an_open_notch_never_goes_up_by_itself(app):
    w = _widget({"phase": "idle", "threads": []})
    w.expanded = False
    w.tuck()  # tucked by hand earlier...
    w._hovering, w._tuck_hover = True, False  # ...brought down by the mouse, and opened
    w._place_notch()
    w.toggle_expanded()
    w._hovering = False  # then off to Utils, the Companion or a large card
    clock = widget.time.time
    try:
        widget.time.time = lambda: clock() + 60  # a minute later
        w._place_notch()
        assert w.expanded and w._tucked is None and w._reveal_until > widget.time.time()  # still down
        w._banner_closed()
        assert w._tucked is None  # closing a notification doesn't send an open notch up either
    finally:
        widget.time.time = clock



def test_saved_cards_follow_the_agenda(app):
    import json

    # Saved before the agenda: Today's meetings becomes the agenda, Reminders goes (it's in the agenda), and a card
    # that didn't exist then (Activity) shows. A card you hid stays hidden.
    widget.POS_FILE.write_text(json.dumps({"x": 10, "y": 10, "sides": {
        "companion": {"open": True, "widgets": ["calendar", "usage"]},
        "utils": {"open": False, "widgets": ["timer", "reminders", "notes"]}}}), encoding="utf-8")
    w = _widget({"phase": "idle", "threads": []})
    w._restore_position()
    assert w.side_widgets["companion"] == ["activity", "agenda", "usage"]  # Now playing: hidden before, still hidden
    assert w.side_widgets["utils"] == ["timer", "notes"]
    w.set_side_widget("companion", "activity", False)  # saved with the cards it knows: hidden stays hidden
    w._restore_position()
    assert w.side_widgets["companion"] == ["agenda", "usage"]
