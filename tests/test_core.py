import json

import pytest

from agent_voice import hooks, lang, textclean


def test_clean_strips_markdown_noise():
    md = (
        "## Title\n"
        "See [server.py](src/agent_voice/server.py:42) and `config.json`.\n"
        "```python\nprint('x')\n```\n"
        "- item **bold** -> next\n"
        "Docs: https://example.com/x\n"
    )
    out = textclean.clean(md)
    assert "```" not in out and "print" not in out
    assert "http" not in out and "src/" not in out
    assert "server.py" in out and "config.json" in out
    assert "**" not in out and "#" not in out
    assert "item bold, next" in out


def test_table_rows_become_sentences():
    out = textclean.clean("| Agent | Hook |\n|---|---|\n| Claude | Stop |")
    assert "Claude, Stop." in out and "---" not in out


def test_truncate_cuts_at_sentence():
    assert textclean.truncate("One. Two. Three.", 12) == "One. Two."


def test_language_segmentation_mixed():
    units = ["Bonjour, j'ai terminé la modification du fichier.", "The tests are all passing now, good news."]
    assert [l for l, _ in lang.segment(units)] == ["fr", "en"]


def test_extract_text_known_payloads():
    assert hooks.extract_text({"last-assistant-message": "codex"}) == "codex"
    assert hooks.extract_text({"prompt_response": "gemini"}) == "gemini"
    assert hooks.extract_text({"text": "cursor"}) == "cursor"
    assert hooks.extract_text({"content": [{"type": "text", "text": "blocks"}]}) == "blocks"


def test_turn_texts(tmp_path):
    rows = [
        {"type": "user", "message": {"content": "old"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "old answer"}]}},
        {"type": "user", "message": {"content": "new"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}, {"type": "tool_use"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "sub"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert hooks.turn_texts(hooks._read_entries(path)) == ["working", "done"]


def test_unspoken_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "SESSIONS_DIR", tmp_path)
    assert hooks._unspoken("s", ["a", "b"]) == ["a", "b"]
    assert hooks._unspoken("s", ["a", "b", "c"]) == ["c"]


def test_stop_message_not_spoken_twice_when_formatting_differs():
    texts = ["Done. All tests pass.", "Next step:\n\n- push"]
    assert hooks._covered("Done.  All tests pass.\nNext step:\n- push", texts)
    assert not hooks._covered("Something new.", texts)


def test_parallel_hooks_speak_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(hooks, "SESSIONS_DIR", tmp_path)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: hooks._unspoken("p", ["same text"]), range(8)))
    assert sum(len(r) for r in results) == 1


def test_summary_prefers_agent_line():
    reply = "## Done\nLots of details here.\n\n- item\n\n**Voice summary:** Deployed to staging, all 12 tests pass."
    assert hooks.summarize(reply) == "Deployed to staging, all 12 tests pass."


def test_summary_falls_back_to_opening_sentences():
    reply = "Fixed the login bug. The token was not refreshed. More detail.\n\nSecond paragraph with lots more."
    assert hooks.summarize(reply, max_chars=60) == "Fixed the login bug. The token was not refreshed."


def test_full_mode_drops_summary_line():
    assert "Voice summary" not in hooks.strip_summary("Answer.\nVoice summary: short.")


def test_speech_waits_while_mic_is_held(monkeypatch):
    import time

    from agent_voice import tts

    played = []
    monkeypatch.setattr(tts.sd, "play", lambda audio, rate, device=None: played.append(time.time()))
    monkeypatch.setattr(tts.sd, "stop", lambda: None)
    monkeypatch.setattr(tts.sd, "get_stream", lambda: type("S", (), {"active": False})())
    sp = tts.Speaker({"voices": {"en": "x"}, "default_lang": "en", "speed": 1.0, "volume": 1.0, "max_chars": 0})
    sp.hold()  # auto-listen
    sp.hold()  # push-to-talk on top
    sp._audio_q.put((sp._gen, tts.np.zeros(10, dtype="float32"), 16000, "x", "A sentence."))
    time.sleep(0.2)
    sp.release()  # auto-listen ends, push-to-talk still recording
    time.sleep(0.2)
    assert played == []
    sp.release()
    deadline = time.time() + 3  # the player thread wakes up on its own schedule: wait for it, don't guess
    while not played and time.time() < deadline:
        time.sleep(0.02)
    assert len(played) == 1


def test_chime_is_soft_and_short():
    from agent_voice.server import chime_audio

    for kind in ("done", "attention"):
        audio, rate = chime_audio(kind, volume=0.35)
        assert 0.3 < abs(audio).max() <= 0.35 + 1e-6
        assert len(audio) / rate < 2.0


def test_claude_hooks_ring_when_attention_is_needed(monkeypatch, tmp_path):
    import json

    monkeypatch.setattr(hooks, "SESSIONS_DIR", tmp_path)
    sent, spoken = [], []
    monkeypatch.setattr(hooks.client, "request",
                        lambda path, payload=None, timeout=2.0: sent.append((path, (payload or {}).get("kind"))))
    monkeypatch.setattr(hooks.client, "speak", lambda *a, **k: spoken.append(a))
    hooks.hook_claude(json.dumps({"hook_event_name": "Notification", "message": "Claude needs your permission to use Bash"}))
    hooks.hook_claude(json.dumps({"hook_event_name": "Notification", "message": "Claude is waiting for your input"}))
    ask = {"questions": [{"question": "Which one?", "header": "Pick", "multiSelect": False,
                          "options": [{"label": "A", "description": "first"}, {"label": "B", "description": "second"}]}]}
    hooks.hook_claude(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                                  "session_id": "t", "tool_input": ask}))
    hooks.hook_claude(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "t"}))
    assert sent == [("/notify", "attention"), ("/notify", "input"), ("/notify", "question"), ("/activity", None)]
    assert spoken == []  # the question itself is read out by the daemon, with its options


def test_cline_hooks(monkeypatch):
    import json

    sent, spoken = [], []
    monkeypatch.setattr(hooks.client, "request", lambda path, payload=None, timeout=2.0: sent.append((path, payload)))
    monkeypatch.setattr(hooks.client, "speak", lambda text, **k: spoken.append((text, k.get("final"))))
    monkeypatch.setattr(hooks, "summary_enabled", lambda: False)

    def pre(tool, **params):
        return json.dumps({"hookName": "PreToolUse", "taskId": "T1", "preToolUse": {"toolName": tool, "parameters": params}})

    assert hooks.hook_cline(pre("read_file", path="x")) == {}
    hooks.hook_cline(pre("attempt_completion", result="All tests pass."))
    hooks.hook_cline(pre("ask_followup_question", question="Which DB?", options='["Postgres", "SQLite"]'))
    assert spoken == [("All tests pass.", True)]
    assert sent[-1][0] == "/notify" and sent[-1][1]["options"] == ["Postgres", "SQLite"]


def test_spoken_answers_pick_an_option():
    from agent_voice.server import match_option

    options = ["Thin strip (Recommended)", "Small pill", "Fully hidden"]
    assert match_option("the second one", options) == 1
    assert match_option("small pill please", options) == 1
    assert match_option("option 3", options) == 2
    assert match_option("la première", options) == 0
    assert match_option("let's do something else entirely, one more idea", options) is None


def test_only_one_daemon_can_hold_the_instance_lock(tmp_path):
    from agent_voice.server import InstanceLock

    first, second = InstanceLock(tmp_path / "daemon.lock"), InstanceLock(tmp_path / "daemon.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_daemon_port_cannot_be_bound_twice():
    import pytest

    from agent_voice.server import _Server

    first = _Server(("127.0.0.1", 0), None)
    try:
        with pytest.raises(OSError):
            _Server(first.server_address, None)
    finally:
        first.server_close()


def test_parallel_autostarts_spawn_once(tmp_path, monkeypatch):
    import threading

    from agent_voice import client

    monkeypatch.setattr(client, "STARTING_PATH", tmp_path / "starting")
    spawned, up = [], threading.Event()
    monkeypatch.setattr(client, "is_running", up.is_set)

    def fake_spawn():
        spawned.append(1)
        threading.Timer(0.5, up.set).start()

    monkeypatch.setattr(client, "_spawn", fake_spawn)
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(client.start_detached(wait=5))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert spawned == [1] and results == [True] * 5
    assert not (tmp_path / "starting").exists()


def test_desktop_session_title_and_sidebar_entry(tmp_path):
    import json

    from agent_voice import claudeapp

    d = tmp_path / "acct" / "org"
    d.mkdir(parents=True)
    (d / "local_1.json").write_text(json.dumps({"cliSessionId": "abc", "title": "Fix the build"}), encoding="utf-8")
    (d / "local_2.json").write_text(json.dumps({"cliSessionId": "zzz", "title": "Other"}), encoding="utf-8")
    assert claudeapp.session_title("abc", tmp_path) == "Fix the build"
    assert claudeapp.session_title("nope", tmp_path) == ""

    names = ["Fix the build, rename session", "Idle Fix the build (fork)", "Running Fix the build"]
    assert claudeapp.pick_entry(names, "Fix the build") == 2
    assert claudeapp.pick_entry(["Idle Fix the build Started by Claude from Main."], "Fix the build") == 0
    assert claudeapp.pick_entry(["Fix the build, rename session"], "Fix the build") == -1
    assert claudeapp.is_current("Fix the build - Claude Code", "Fix the build")
    assert not claudeapp.is_current("Fix the build (fork) - Claude Code", "Fix the build")


def test_claude_code_session_title_from_its_transcript(tmp_path):
    import json

    from agent_voice import claudeapp

    d = tmp_path / "c--work-app"
    d.mkdir()
    log = d / "s1.jsonl"
    quoted = {"type": "user", "message": {"content": '{"type":"ai-title","aiTitle":"Quoted"}'}}  # a tool result
    log.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n" + json.dumps(quoted) + "\n",
                   encoding="utf-8")
    titles = claudeapp.TranscriptTitles(tmp_path)
    assert titles.title("s1") == ""  # no title yet
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "ai-title", "aiTitle": "Music on the notch", "sessionId": "s1"}) + "\n")
        f.write('{"type":"ai-title","aiTitle":"Half wr')  # still being written: not read yet
    assert titles.title("s1") == "Music on the notch"
    with log.open("a", encoding="utf-8") as f:
        f.write('itten"}\n' + json.dumps({"type": "custom-title", "customTitle": "Renamed"}) + "\n")
        f.write(json.dumps({"type": "ai-title", "aiTitle": "Later"}) + "\n")
    assert titles.title("s1") == "Renamed"  # /rename wins over Claude's own
    assert titles.title("missing") == "" and titles.title("../s1") == ""


def test_permission_answers_and_wake_phrases():
    from agent_voice.server import match_yes_no, strip_wake

    assert match_yes_no("yes go ahead") is True
    assert match_yes_no("oui vas-y") is True
    assert match_yes_no("no, don't") is False
    assert match_yes_no("hmm let me think") is None
    phrases = ["hey claude", "ok claude"]
    assert strip_wake("Hey Claude, run the tests please.", phrases) == "run the tests please."
    assert strip_wake("hey claude", phrases) == ""
    assert strip_wake("I told him hey claude", phrases) is None
    # Accents, as Whisper spells them
    assert strip_wake("Hey Klöte!", phrases) == "!" or strip_wake("Hey Klöte!", phrases) == ""
    assert strip_wake("Hé Clod, lance les tests.", phrases) == "lance les tests."
    assert strip_wake("Hey Chloe, pass the salt.", phrases) is None
    assert strip_wake("Hey, close the door.", phrases) is None


def test_permission_hook_waits_for_the_daemon(monkeypatch):
    calls = []

    def request(path, payload=None, timeout=2.0):
        calls.append(path)
        if path == "/permission":
            return {"id": 7}
        return {"decision": "allow" if calls.count("/permission_state") > 1 else ""}

    monkeypatch.setattr(hooks.client, "request", request)
    monkeypatch.setattr(hooks.time, "sleep", lambda s: None)
    out = hooks.permission_decision({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "npm test"}})
    assert out["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert hooks.describe_tool("Bash", {"command": "npm   test"}) == "run npm test"


def test_agent_states(tmp_path, monkeypatch):
    import json as _json

    from agent_voice import install

    monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(_json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"command": '"C:/x/agent-voice.exe" hook claude'}]}]}}), encoding="utf-8")
    (tmp_path / ".gemini").mkdir()
    states = install.agent_states()
    assert states["claude"] == "on"
    assert states["gemini"] == "off"
    assert states["cursor"] == "missing"


# -- the daemon, without audio or threads ----------------------------------------------------
class FakeSpeaker:
    def __init__(self, config):
        self.said = []
        self.busy = False
        self.current_spec = self.current_text = self.current_lang = ""

    def speak(self, text, lang=None, voice=None, pitch=1.0):
        self.said.append((text, pitch))
        return 1

    def stop(self):
        pass

    hold = release = stop


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    from agent_voice import config, server

    monkeypatch.setattr(server, "Speaker", FakeSpeaker)
    monkeypatch.setattr(server.history, "PATH", tmp_path / "history.db")  # never your real history
    monkeypatch.setattr(server, "load_state", lambda: {})
    monkeypatch.setattr(server, "save_state", lambda state: None)
    monkeypatch.setattr(server.install, "agent_states", lambda: {})
    monkeypatch.setattr(server.install, "usage_line_state", lambda: "off")
    monkeypatch.setattr(server.claudeapp, "session_title", lambda session: "")
    monkeypatch.setattr(server.winfocus, "is_window", lambda hwnd: True)
    d = server.Daemon(config.load_config(), start_threads=False)
    d.config["take_back"]["seconds"] = 0  # sent at once, unless a test is about the take-back
    d.chimes = []
    monkeypatch.setattr(d, "play_chime", lambda kind: d.muted or d.chimes.append(kind))  # silent, like the real one
    return d


def test_chime_and_silent_show_the_reply_as_a_banner(daemon):
    daemon.state["chime"] = True
    daemon.speak("Working on it.", source="claude", session="a")
    daemon.speak("Voice summary: all tests pass.", source="claude", final=True, session="a")
    assert daemon.speaker.said == []  # no voice in chime mode
    assert daemon.chimes == ["done"]
    assert daemon.banner["text"].endswith("all tests pass.")
    daemon.set_output("silent")  # Do not disturb: no sound, no banner, the notch stays up
    shown = daemon.banner["id"]
    daemon._check_away(0, __import__("time").time() - 1)  # counts as away
    daemon.speak("Second answer.", source="claude", final=True, session="a")
    assert daemon.banner["id"] == shown and daemon.output == "silent" and daemon.chimes == ["done"]
    assert daemon.speaker.said == [] and daemon.status()["dnd_from"] == "chime"
    daemon.set_output(daemon.dnd_from)  # the notch's button: back to where you were
    daemon._check_away(0, __import__("time").time())
    assert daemon.output == "chime" and daemon.banner["kind"] == "catchup"  # what happened meanwhile


def test_settings_stay_quiet_without_a_voice(daemon):
    daemon.state["chime"] = True
    daemon.set_hands_free(True)
    daemon.set_summary(False)
    assert daemon.speaker.said == []
    daemon.state["chime"] = False
    daemon.set_hands_free(False)
    assert daemon.speaker.said[-1][0] == "Auto-send off."


def test_replay_and_copy_use_the_whole_turn(daemon, monkeypatch):
    import pyperclip

    copied = []
    monkeypatch.setattr(pyperclip, "copy", copied.append)
    daemon.speak("First part.", source="claude", session="a")
    daemon.speak("Last part.", source="claude", final=True, session="a")
    daemon.copy_reply()
    assert copied == ["First part.\n\nLast part."]
    daemon.replay("a")
    assert daemon.speaker.said[-1][0] == "First part.\n\nLast part."
    daemon.prompt_submitted(0, "a")
    daemon.speak("New turn.", source="claude", session="a")
    assert daemon._turn_text("a") == "New turn."


def test_each_conversation_has_its_window_and_voice(daemon):
    daemon.prompt_submitted(111, "a")
    daemon.prompt_submitted(222, "b")
    assert daemon._hwnd_for("a") == 111 and daemon._hwnd_for("b") == 222
    daemon.speak("From a.", session="a")
    daemon.speaker.busy = False
    daemon.speak("From b.", session="b", interrupt=True)
    assert [pitch for _, pitch in daemon.speaker.said] == [1.0, 0.92]


def test_live_activity(daemon):
    daemon.prompt_submitted(0, "a", "claude")
    daemon.tool_used("a", "claude", "Edit widget.py")
    daemon.tool_used("a", "claude", "Run pytest")
    act = daemon.status()["activity"]
    assert act["label"] == "Run pytest" and act["steps"] == 2 and act["others"] == 0
    daemon.speak("Done.", source="claude", final=True, session="a")
    assert daemon.status()["activity"] is None


def test_calls_switch_to_chimes_until_you_ask_for_the_voice(daemon):
    daemon.in_call = "Zoom"
    assert daemon.chime and daemon.output == "chime"
    daemon.set_chime(False)
    assert not daemon.chime  # you chose the voice during the call
    daemon.in_call, daemon._call_override = "", False
    assert not daemon.chime


def test_voice_commands():
    from agent_voice.server import match_command

    assert match_command("Repeat that.") == "repeat"
    assert match_command("Répète.") == "repeat"
    assert match_command("Slower, please") == "slower"
    assert match_command("never mind") == "cancel"
    assert match_command("Repeat the tests on the server please") is None


def test_status_tag_ignores_what_the_widget_does_not_show():
    from agent_voice.server import status_tag

    a = {"phase": "idle", "level": 0.1, "threads": [{"ago": 61}], "banner": {"id": 1, "age": 2}}
    b = {"phase": "idle", "level": 0.3, "threads": [{"ago": 100}], "banner": {"id": 1, "age": 9}}
    assert status_tag(a) == status_tag(b)
    assert status_tag(a) != status_tag({**a, "phase": "speaking"})


def test_read_along_follows_the_voice(daemon):
    daemon.speaker.busy = True
    daemon.speak("First sentence. Second one.", source="claude", session="a")
    assert daemon.status()["reading"]["text"] == "First sentence. Second one."
    daemon.speaker.busy = False
    assert daemon.status()["reading"] is None  # the voice stopped: the text goes with it
    daemon.state["chime"] = True
    daemon.speaker.busy = True
    assert daemon.status()["reading"] is None  # no voice in chime mode: the end-of-turn banner instead


def test_what_you_said_shows_as_yours(daemon):
    daemon.threads["a"] = {"source": "claude", "last": 0}
    daemon._heard("Yes, ship it.", "en", "a", tag="sent")
    assert daemon.banner["kind"] == "you" and daemon.banner["tag"] == "sent"
    assert daemon.banner["text"] == "Yes, ship it." and daemon.history[0]["text"] == "Yes, ship it."


def test_call_watch_switches_to_chimes_and_back(daemon, monkeypatch):
    from agent_voice import server

    apps = [["C:#Program Files#Zoom#bin#Zoom.exe"], ["C:#Program Files#Zoom#bin#Zoom.exe"], [], None]

    def fake_apps():
        nxt = apps.pop(0)
        if nxt is None:
            raise SystemExit  # end the watch loop
        return nxt

    seen = []
    monkeypatch.setattr(server.calls, "apps_using_mic", fake_apps)
    monkeypatch.setattr(daemon, "_check_output", lambda: None)  # not about headphones (and no real pactl)
    monkeypatch.setattr(server.winfocus, "busy_state", lambda: "")  # not about your screen (an app full screen)
    monkeypatch.setattr(server.media, "now_playing", lambda: None)  # no player open (never the real media keys)
    monkeypatch.setattr(server.time, "sleep", lambda s: seen.append((daemon.in_call, daemon.chime)))
    with pytest.raises(SystemExit):
        daemon._call_watch()
    # (in_call, chime) seen before each check: idle, in the Zoom call (chimes), still in it, back to voice
    assert seen == [("", False), ("Zoom", True), ("Zoom", True), ("", False)]


def test_a_call_pauses_the_music_and_plays_it_again_after(daemon, monkeypatch):
    from agent_voice import server

    song = {"app": "Spotify", "title": "Get Lucky", "artist": "Daft Punk", "playing": True}
    pressed = []

    def toggle(action):
        pressed.append(action)
        song["playing"] = not song["playing"]
        return True

    monkeypatch.setattr(server.media, "now_playing", lambda: dict(song))
    monkeypatch.setattr(server.media, "control", toggle)
    monkeypatch.setattr(daemon, "media_control", toggle)  # not the 0.6 s refresh timer
    daemon._pause_for_call()
    assert pressed == ["toggle"] and not song["playing"]
    daemon._resume_after_call()
    assert pressed == ["toggle", "toggle"] and song["playing"]

    pressed.clear()
    song["playing"] = False
    daemon._pause_for_call()  # nothing playing: the toggle would start it, so no key
    daemon._resume_after_call()
    assert pressed == []

    song["playing"] = True
    daemon._pause_for_call()
    song["playing"] = True  # you played it again yourself during the call
    daemon._resume_after_call()
    assert pressed == ["toggle"] and song["playing"]


def test_several_approvals_wait_and_are_answered_by_name(daemon):
    daemon.threads["c"] = {"source": "codex", "last": 1}
    first = daemon.request_permission("a", "Bash", "run npm test")
    second = daemon.request_permission("c", "Bash", "run cargo build", source="codex")
    assert daemon.permission["id"] == first  # the first is asked, the second waits in "needs you"
    assert [n["kind"] for n in daemon.status()["needs"]] == ["permission", "permission"]
    assert daemon._voice_approval("approve codex")  # named: that one, not the one being asked
    assert daemon.permission_state(second) == "allow" and daemon.permission["id"] == first
    assert daemon._voice_approval("no")
    assert daemon.permission_state(first) == "deny" and daemon.permission is None
    assert daemon.status()["needs"] == []


def test_waiting_for_input_joins_needs_until_the_agent_moves_on(daemon):
    daemon.need_input("g", "gemini", "Allow shell command?", announce=False)
    needs = daemon.status()["needs"]
    assert needs[0]["kind"] == "input" and needs[0]["title"] == "Gemini"
    assert daemon.status()["threads"][0]["state"] == "input"
    daemon.speak("Done.", source="gemini", final=True, session="g")
    assert daemon.status()["needs"] == []


def test_messages_go_to_the_conversation_named(daemon, monkeypatch):
    from agent_voice import server

    assert server.parse_route("Tell Codex to run the tests.") == ("Codex", "Run the tests.")
    assert server.parse_route("dis à codex de lancer les tests") == ("codex", "Lancer les tests")
    assert server.parse_route("I told him to stop") is None
    daemon.threads.update({"c": {"source": "codex", "last": 2}, "a": {"source": "claude", "last": 1}})
    daemon._session_hwnd["c"] = 42
    pasted = []
    monkeypatch.setattr(daemon, "_paste", lambda text, hwnd=0, submit=False, session="": pasted.append((text, hwnd, submit)))
    daemon._send_to(*daemon._routed("tell codex to run the tests"), "en")
    assert pasted == [("Run the test suite and fix anything that fails.", 42, True)]  # a saved prompt, by its name
    daemon._send_to(*daemon._routed("tell codex to fix the login bug"), "en")
    assert pasted[-1] == ("Fix the login bug", 42, True)
    assert daemon._routed("tell gemini to stop") is None  # no such conversation: not a message


def test_mcp_questions_collect_the_answer(daemon):
    ask_id = daemon.ask("m", "antigravity", "Deploy to staging or prod?", ["Staging", "Prod"])
    assert daemon.ask_state(ask_id) == {"answer": None, "pending": True}
    assert daemon.answer(text="prod please")
    assert daemon.ask_state(ask_id) == {"answer": "Prod"}
    free = daemon.ask("m", "antigravity", "What should the release be called?", [])
    daemon.answer(text="Aurora")
    assert daemon.ask_state(free) == {"answer": "Aurora"}


def test_catch_up_after_being_away(daemon):
    daemon.config["catch_up"] = {"enabled": True, "away_minutes": 5}
    daemon._check_away(idle=400, now=1000)  # away since t=600
    daemon._events = [{"kind": "done", "session": "a", "source": "claude", "text": "All tests pass.", "at": 700}]
    daemon.need_input("g", "gemini", "Allow?", announce=False)
    daemon._events[-1]["at"] = 800
    daemon._check_away(idle=1, now=1500)
    assert daemon.banner["kind"] == "catchup"
    assert daemon.banner["text"] == "Claude Code finished: All tests pass. Gemini needs you."
    assert daemon.speaker.said[-1][0] == "While you were away: Claude Code finished; Gemini needs you."


def test_turn_result_and_context_from_the_transcript():
    rows = [
        {"type": "user", "message": {"content": "fix it"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "1", "name": "Edit", "input": {"file_path": "a.py"}},
            {"type": "tool_use", "id": "2", "name": "Write", "input": {"file_path": "b.py"}},
            {"type": "tool_use", "id": "3", "name": "Bash", "input": {"command": "python -m pytest -q"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "3", "content": "1 failed"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "4", "name": "Bash", "input": {"command": "python -m pytest -q"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "4", "content": "9 passed"}]}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 49990},
                                          "content": [{"type": "text", "text": "Done."}]}},
    ]
    assert hooks.claude_turn_result(rows) == {"files": 2, "tests": "passed"}
    assert hooks.claude_context(rows) == 25.0


def test_turn_result_shows_in_the_notch(daemon):
    from agent_voice.server import result_label

    daemon.speak("Fixed.", source="claude", final=True, session="a", result={"files": 2, "tests": "passed"})
    st = daemon.status()
    assert st["result"]["files"] == 2 and st["threads"][0]["result"]["tests"] == "passed"
    assert result_label(st["result"]) == "2 files changed · tests passed"
    daemon.set_usage({"session": "a", "source": "claude", "context": 81.4,
                      "five_hour": {"pct": 40, "resets": 4102444800}})
    st = daemon.status()
    assert st["threads"][0]["context"] == 81 and st["limits"]["claude"]["five_hour"]["pct"] == 40


def test_codex_limits_come_from_its_session_log(tmp_path):
    import json

    from agent_voice import quota

    day = tmp_path / "sessions" / "2026" / "10" / "16"
    day.mkdir(parents=True)
    old = {"timestamp": "2026-10-16T10:00:00Z", "type": "event_msg",
           "payload": {"type": "token_count", "rate_limits": {
               "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_in_seconds": 600},
               "secondary": {"used_percent": 30.0, "window_minutes": 10080, "resets_in_seconds": 86400}}}}
    new = {"timestamp": "2026-10-16T11:00:00Z", "type": "event_msg",
           "payload": {"type": "token_count", "info": None, "rate_limits": {
               "primary": {"used_percent": 47.5, "window_minutes": 300, "resets_at": 1792150000},
               "secondary": {"used_percent": 31.0, "window_minutes": 10080, "resets_at": 1792600000}}}}
    log = day / "rollout-2026-10-16T10-00-00-abc.jsonl"
    log.write_text("\n".join(json.dumps(x) for x in (old, {"type": "response_item", "payload": {}}, new)) + "\n",
                   encoding="utf-8")
    assert quota.latest_codex_log(tmp_path) == log
    assert quota.codex_limits(log) == {"five_hour": {"pct": 48, "resets": 1792150000},
                                       "seven_day": {"pct": 31, "resets": 1792600000}}
    at = quota._stamp(old["timestamp"])
    assert quota.codex_limits_from(old)["five_hour"] == {"pct": 12, "resets": int(at + 600)}
    assert quota.latest_codex_log(tmp_path / "nothing") is None


def test_claude_limits_from_its_cache(tmp_path, daemon):
    import json

    from agent_voice import quota

    cache = tmp_path / ".claude.json"
    cache.write_text(json.dumps({"cachedUsageUtilization": {"fetchedAtMs": 1792000000000, "utilization": {
        "five_hour": {"utilization": 68, "resets_at": "2026-10-09T20:59:59.687531+00:00"},
        "seven_day": {"utilization": 36, "resets_at": "2026-10-14T17:59:59+00:00"}, "seven_day_opus": None}}}))
    fetched, found = quota.claude_cached(cache)
    assert fetched == 1792000000 and found["five_hour"]["pct"] == 68 and found["seven_day"]["resets"] == int(quota._stamp("2026-10-14T17:59:59+00:00"))
    assert quota.claude_cached(tmp_path / "missing.json") == (0.0, {})
    far = 4102444800
    daemon.set_usage({"source": "claude", "five_hour": {"pct": 12, "resets": far}})  # the status line, just now
    daemon._set_limits("claude", {"five_hour": {"pct": 68, "resets": far}}, at=fetched)  # an older cache
    assert daemon.limits["claude"]["five_hour"]["pct"] == 12


def test_limits_are_kept_and_warn_once_near_full(daemon):
    far = 4102444800
    daemon._set_limits("codex", {"five_hour": {"pct": 91, "resets": far}, "seven_day": {"pct": 20, "resets": far}})
    assert daemon.banner["text"].startswith("Codex has used 91% of its 5-hour limit")
    assert daemon.state["limits"]["codex"]["seven_day"]["pct"] == 20  # survives a restart
    daemon.banner = None
    daemon._set_limits("codex", {"five_hour": {"pct": 95, "resets": far}, "seven_day": {"pct": 21, "resets": far}})
    assert daemon.banner is None  # the same window: warned already
    daemon._set_limits("claude", {"five_hour": {"pct": 10, "resets": 1}})  # already reset
    st = daemon.status()
    assert set(st["limits"]) == {"codex"} and st["usage_line"] == "off"


def test_mcp_installers_are_idempotent_and_reversible(tmp_path, monkeypatch):
    from agent_voice import install

    monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(install, "_app_data", lambda: tmp_path / "AppData")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    vs = tmp_path / "AppData" / "Code" / "User" / "mcp.json"
    vs.parent.mkdir(parents=True)
    vs.write_text(json.dumps({"servers": {"github": {"command": "gh-mcp"}}}), encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "o4"\n\n[profiles.x]\nmodel = "o3"\n', encoding="utf-8")
    for _ in range(2):
        install.vscode()
        install.antigravity()
        install.codex()
    servers = json.loads(vs.read_text(encoding="utf-8"))["servers"]
    assert set(servers) == {"github", "agent-voice"} and servers["agent-voice"]["type"] == "stdio"
    assert "mcp" in servers["agent-voice"]["args"] and install.is_installed("vscode")
    assert install.is_installed("antigravity")
    toml = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert toml.count("[mcp_servers.agent-voice]") == 1 and toml.startswith("notify = ")
    assert toml.index("[profiles.x]") < toml.index("[mcp_servers.agent-voice]")
    install.vscode(remove=True)
    install.codex(remove=True)
    assert json.loads(vs.read_text(encoding="utf-8")) == {"servers": {"github": {"command": "gh-mcp"}}}
    assert (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8") == 'model = "o4"\n\n[profiles.x]\nmodel = "o3"\n'
    assert not install.is_installed("vscode")


def test_mcp_server_speaks_and_asks(monkeypatch):
    from agent_voice import mcp

    sent = []

    def fake_request(path, payload=None, timeout=2.0):
        sent.append((path, payload))
        if path == "/ask":
            return {"id": 7}
        if path == "/ask_state":
            return {"answer": "Staging"}
        return {"ok": True}

    monkeypatch.setattr(mcp.client, "request", fake_request)
    monkeypatch.setattr(mcp.client, "speak", lambda text, **k: sent.append(("/speak", {"text": text, **k})) or {})
    monkeypatch.setattr(mcp.Server, "_window", lambda self: 0)
    server = mcp.Server()
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "Antigravity"}}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and server.source == "antigravity"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert names == ["speak", "notify", "ask_user", "set_status", "todo_list", "todo_add", "todo_update"]
    out = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "speak", "arguments": {"text": "Deployed."}}})
    assert out["result"]["content"][0]["text"] == "Said to the user."
    assert sent[-1][1]["final"] is True and sent[-1][1]["session"].startswith("mcp-antigravity-")
    out = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "ask_user", "arguments": {"question": "Where?", "options": ["Staging", "Prod"]}}})
    assert out["result"]["content"][0]["text"] == "The user answered: Staging"
    assert server.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})["error"]["code"] == -32601
    hooked = mcp.Server("codex", hooked=True)
    assert "speak" not in [t["name"] for t in hooked.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]


def test_generic_hooks_pass_the_session_and_the_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.client, "speak", lambda text, **k: calls.append(("speak", text, k)))
    monkeypatch.setattr(hooks.client, "request", lambda path, payload=None, timeout=2.0: calls.append((path, payload)))
    monkeypatch.setattr(hooks, "_log_payload", lambda agent, raw: None)
    monkeypatch.setattr(hooks, "summary_enabled", lambda: False)
    from agent_voice import winfocus

    monkeypatch.setattr(winfocus, "console_window", lambda: 99)
    hooks.hook_payload("codex", json.dumps({"type": "agent-turn-complete", "thread-id": "t1",
                                            "last-assistant-message": "Done."}))
    assert calls[-1][2]["session"] == "codex-t1" and calls[-1][2]["hwnd"] == 99
    hooks.hook_payload("gemini", json.dumps({"hook_event_name": "Notification", "session_id": "g",
                                             "message": "Allow rm -rf build?"}))
    assert calls[-1] == ("/notify", {"kind": "input", "session": "gemini-g", "agent": "gemini",
                                     "text": "Allow rm -rf build?"})


def test_statusline_reports_usage(monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.client, "request", lambda path, payload=None, timeout=2.0: sent.append(payload))
    line = hooks.statusline(json.dumps({"session_id": "s", "model": {"display_name": "Opus"},
                                        "context_window": {"used_percentage": 42.4},
                                        "rate_limits": {"five_hour": {"used_percentage": 63, "resets_at": 99}}}))
    assert line == "Opus · context 42% · 5h 63%"
    assert sent == [{"session": "s", "source": "claude", "context": 42.4, "five_hour": {"pct": 63, "resets": 99}}]


def test_watch_tracks_a_terminal_agent():
    from agent_voice import watch

    clock = [0.0]
    posts = []
    t = watch.Tracker("aider", lambda path, payload: posts.append((path, payload)), rows=10, cols=60,
                      now=lambda: clock[0])
    t.output("aider v1\r\n> ")
    t.typed_input("fix the bug")
    t.output("fix the bug")
    t.typed_input("\r")
    assert posts == [("/prompt", {"hwnd": 0, "session": t.session, "source": "aider"})]
    t.output("\r\nEditing server.py\r\nFixed the off-by-one in server.py. All tests pass.\r\n> ")
    clock[0] = 1.0
    t.tick()  # still talking: a live label
    assert posts[-1][1]["label"] == "Fixed the off-by-one in server.py. All tests pass."
    clock[0] = 5.0
    t.tick()  # quiet: that was the reply
    path, payload = posts[-1]
    assert path == "/speak" and payload["final"] and "off-by-one" in payload["text"]
    assert "fix the bug" not in payload["text"]
    t.typed_input("go\r")
    t.output("go\r\nApply these edits? (y/n) ")
    clock[0] = 10.0
    t.tick()
    assert posts[-2] == ("/notify", {"kind": "input", "session": t.session, "agent": "aider",
                                     "text": "Apply these edits? (y/n)"})


def test_desktop_app_notifications_only_from_listed_apps(tmp_path):
    import sqlite3

    from agent_voice import apps

    db = tmp_path / "wpndatabase.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE NotificationHandler (RecordId INTEGER PRIMARY KEY, PrimaryId TEXT);
        CREATE TABLE Notification (Id INTEGER PRIMARY KEY, HandlerId INTEGER, Type TEXT, Payload BLOB);
        INSERT INTO NotificationHandler VALUES (1, 'OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT'), (2, 'Microsoft.Outlook');
        INSERT INTO Notification VALUES (1, 1, 'toast', '<toast><visual><binding><text>old</text></binding></visual></toast>');
    """)
    con.commit()
    w = apps.Watcher(db=db)
    assert w.poll() == []  # what was already there isn't news
    toast = ("<toast><visual><binding template='ToastGeneric'><text>ChatGPT</text>"
             "<text>Here is the plan: three steps.</text></binding></visual></toast>")
    con.execute("INSERT INTO Notification VALUES (2, 2, 'toast', ?)", ("<toast><text>Meeting</text><text>secret</text></toast>",))
    con.execute("INSERT INTO Notification VALUES (3, 1, 'toast', ?)", (toast.encode(),))
    con.commit()
    con.close()
    assert w.poll() == [{"app": "chatgpt", "title": "ChatGPT", "text": "Here is the plan: three steps."}]
    assert w.poll() == []  # nothing changed: not even a query


def test_browser_extension_reply_detection():
    """The extension's reply detection (agent_voice/browser/sites.js), run in Node when it's installed."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([node, str(root / "tests" / "browser_sites.test.js"),
                          str(root / "agent_voice" / "browser" / "sites.js")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    manifest = json.loads((root / "agent_voice" / "browser" / "manifest.json").read_text(encoding="utf-8"))
    from agent_voice import install
    import base64
    import hashlib

    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == install.EXTENSION_ID  # the id the key pins


def test_native_host_relays_the_page(monkeypatch):
    import io

    from agent_voice import native_host

    sent = []
    monkeypatch.setattr(native_host.client, "request", lambda path, payload=None, timeout=2.0: sent.append((path, payload)))
    monkeypatch.setattr(native_host.client, "speak", lambda text, **k: sent.append(("/speak", {"text": text, **k})))
    monkeypatch.setattr(native_host, "summary_enabled", lambda: False)
    monkeypatch.setattr(native_host.winfocus, "foreground", lambda: 5)
    stream = io.BytesIO()
    for msg in ({"type": "start", "site": "chatgpt", "conversation": "/c/1", "title": "Fix the parser"},
                {"type": "reply", "site": "chatgpt", "conversation": "/c/1", "text": "Here is the fix."},
                {"type": "reply", "site": "evil", "text": "nope"}):
        native_host.write_message(stream, msg)
    stream.seek(0)
    out = io.BytesIO()
    monkeypatch.setattr(native_host.sys, "stdin", type("S", (), {"buffer": stream})())
    monkeypatch.setattr(native_host.sys, "stdout", type("S", (), {"buffer": out})())
    assert native_host.run() == 0
    assert sent[0][0] == "/prompt" and sent[0][1]["hwnd"] == 5 and sent[0][1]["title"] == "Fix the parser"
    assert sent[1] == ("/speak", {"text": "Here is the fix.", "source": "chatgpt", "final": True,
                                  "session": sent[0][1]["session"], "autostart": False})
    assert len(sent) == 2  # an unknown site is ignored


def test_cli_parses_every_command(capsys):
    from agent_voice import cli

    with pytest.raises(SystemExit):
        cli.main(["--help"])
    help_text = capsys.readouterr().out
    for command in ("mcp", "watch", "apps", "usage", "install", "say", "recap", "schedule", "phone"):
        assert command in help_text


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="registry")
def test_browser_bridge_registration(tmp_path, monkeypatch):
    import winreg

    from agent_voice import config, install

    monkeypatch.setattr(config, "HOME", tmp_path)
    keys = {}

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(winreg, "CreateKey", lambda root, path: Key(path))
    monkeypatch.setattr(winreg, "SetValueEx", lambda k, name, _r, _t, value: keys.__setitem__(k.path, value))
    monkeypatch.setattr(winreg, "DeleteKey", lambda root, path: keys.pop(path))
    assert "Load unpacked" in install.browser()
    manifest = json.loads((tmp_path / "native-host" / "com.agentvoice.bridge.json").read_text(encoding="utf-8"))
    assert manifest["allowed_origins"] == ["chrome-extension://bmkeeojeabngjnlndcnopnddhlimlkak/"]
    assert "native-host" in (tmp_path / "native-host" / "agent-voice-host.cmd").read_text(encoding="utf-8")
    assert set(keys) == {rf"{k}\com.agentvoice.bridge" for k in install._HOST_KEYS}
    assert install.is_installed("browser")
    install.browser(remove=True)
    assert keys == {} and not install.is_installed("browser")
    assert (install.extension_dir() / "manifest.json").exists()


def test_hotkeys_in_pynput_notation():
    from agent_voice import keys

    assert keys.pynput_combo("ctrl+alt+space") == "<ctrl>+<alt>+<space>"
    assert keys.pynput_combo("Ctrl+Alt+S") == "<ctrl>+<alt>+s"


def test_typed_reply_and_talking_to_a_thread(daemon, monkeypatch):
    pasted = []
    monkeypatch.setattr(daemon, "_paste", lambda text, hwnd=0, submit=False, session="": pasted.append((text, hwnd, submit, session)))
    daemon._session_hwnd["a"] = 42
    daemon.threads["a"] = {"source": "claude", "last": 1}
    daemon.send_text("a", "  Ship it.  ")
    assert pasted == [("Ship it.", 42, True, "a")]
    assert daemon.banner["kind"] == "you" and daemon.banner["tag"] == "sent"
    started = []
    monkeypatch.setattr(daemon, "transcriber", object())
    monkeypatch.setattr(daemon, "_auto_listen", lambda: started.append((daemon._reply_session, daemon._force_submit)))
    daemon.reply_now("b")
    import time as _t

    _t.sleep(0.1)
    assert started == [("b", True)]  # the thread you picked, and sent when you pause


def test_browser_is_on_only_once_the_extension_said_hello(daemon, monkeypatch):
    from agent_voice import server

    monkeypatch.setattr(server.install, "agent_states", lambda: {"browser": "on"})
    daemon._agents_at = 0
    assert daemon.agent_states()["browser"] == "setup"
    daemon.bridge_seen()
    assert daemon.agent_states()["browser"] == "on" and daemon.banner["kind"] == "note"


def test_connecting_an_agent_says_how_it_went(daemon, monkeypatch):
    from agent_voice import server

    monkeypatch.setitem(server.install.INSTALLERS, "codex", lambda: "installed in config.toml")
    daemon.install_agent("codex")
    assert daemon.banner["text"] == "Codex connected. Restart Codex to load it."
    monkeypatch.setitem(server.install.INSTALLERS, "codex", lambda: "skipped: you have your own notify")
    daemon.install_agent("codex")
    assert daemon.banner["text"] == "Codex: skipped: you have your own notify"

    def broken():
        raise ValueError("bad JSON")

    monkeypatch.setitem(server.install.INSTALLERS, "cursor", broken)
    daemon.install_agent("cursor")
    assert daemon.banner["text"] == "Couldn't connect Cursor: bad JSON"


def test_new_thread_phrases():
    from agent_voice.server import parse_new_thread

    assert parse_new_thread("New thread: fix the login bug") == ("", "Fix the login bug")
    assert parse_new_thread("start a new conversation with Codex to add a health check") == ("codex", "Add a health check")
    assert parse_new_thread("nouvelle session : ajoute des tests") == ("", "Ajoute des tests")
    assert parse_new_thread("new thread") is None and parse_new_thread("I need a new thread soon") is None


def test_new_session_button_in_the_desktop_app():
    from agent_voice.claudeapp import pick_new_session

    names = ["New", "New session with no folder selected, added to group CV",
             "New session in agent-voice, added to group Agent Voice", "New session"]
    assert pick_new_session(names, "agent-voice") == 2  # in that folder's group
    assert pick_new_session(names, "elsewhere") == 3 and pick_new_session(names) == 3
    assert pick_new_session(["Nouvelle session dans agent-voice, ajoutée au groupe Agent Voice"], "agent-voice") == 0
    assert pick_new_session(["Settings"]) == -1


def test_new_thread_goes_to_the_app_or_a_terminal(daemon, monkeypatch, tmp_path):
    from agent_voice import launch, server

    started = []
    monkeypatch.setattr(server.claudeapp, "new_session", lambda hwnd, text, folder: started.append(("app", hwnd, text, folder)) or True)
    monkeypatch.setattr(server.claudeapp, "is_desktop_app", lambda hwnd: hwnd == 7)
    monkeypatch.setattr(launch, "open_terminal", lambda agent, prompt, cwd: started.append(("tab", agent, prompt, cwd)))
    monkeypatch.setattr(launch, "targets", lambda app: (["claude-app"] if app else []) + ["claude"])
    daemon._session_hwnd["a"] = 7
    daemon._last_session = "a"
    daemon.state["last_cwd"] = str(tmp_path)
    assert daemon._default_target() == "claude-app"  # your last thread was in the app
    assert daemon.new_thread("Add a health check")
    assert started[-1] == ("app", 7, "Add a health check", tmp_path.name)
    assert daemon.banner["kind"] == "you" and daemon.banner["tag"] == "new · Claude app"
    daemon.prompt_submitted(0, "fresh", "claude")  # the new session's first prompt: it lives in the app's window
    assert daemon._session_hwnd["fresh"] == 7
    daemon.new_thread("Fix the build", "claude", str(tmp_path))
    assert started[-1] == ("tab", "claude", "Fix the build", str(tmp_path))


def test_launch_trampoline_reads_and_removes_the_prompt(tmp_path, monkeypatch):
    from agent_voice import launch

    ran = []
    monkeypatch.setattr(launch, "find_cli", lambda agent: "C:/bin/gemini.exe")
    monkeypatch.setattr(launch.subprocess, "call", lambda cmd: ran.append(cmd) or 0)
    prompt = tmp_path / "p.txt"
    prompt.write_text('Fix "quotes" & pipes | ; too', encoding="utf-8")
    assert launch.run("gemini", str(prompt)) == 0
    assert ran == [["C:/bin/gemini.exe", "-i", 'Fix "quotes" & pipes | ; too']] and not prompt.exists()


def test_limit_messages_and_their_reset_time():
    from datetime import datetime

    from agent_voice.textclean import limit_reset

    now = datetime(2026, 10, 16, 14, 0).timestamp()
    assert limit_reset("5-hour limit reached ∙ resets 3pm", now) == datetime(2026, 10, 16, 15, 0).timestamp()
    assert limit_reset("Claude AI usage limit reached|1792180000", now) == 1792180000
    assert limit_reset("You've hit your usage limit. Try again in 2 hours.", now) == now + 7200
    assert limit_reset("Usage limit reached · resets 9:30", now) == datetime(2026, 10, 17, 9, 30).timestamp()
    assert limit_reset("Rate limit reached", now) == 0.0  # a limit, no time given
    assert limit_reset("All tests pass; I also added a rate limit to the API.", now) is None
    assert limit_reset("The usage limit reached its max " + "x " * 200, now) is None  # long: a reply, not the error


def test_out_of_quota_resumes_when_the_limit_resets(daemon, monkeypatch):
    sent = []
    monkeypatch.setattr(daemon, "_send_to", lambda session, message, lang: sent.append((session, message)))
    daemon.speak("Working.", source="claude", session="a")
    daemon.speak("5-hour limit reached ∙ resets 3pm", source="claude", final=True, session="a")
    info = daemon._limited["a"]
    assert info["resume"] and not info["guess"]  # on by default
    assert daemon.banner["text"].startswith("Claude Code is out of quota until") and "continue" in daemon.banner["text"]
    assert daemon.state["limited"]["a"]["resets"] == info["resets"]  # survives a restart
    assert [n["kind"] for n in daemon.status()["needs"]] == ["limit"]
    daemon._check_resumes(info["resets"] + 30)
    assert daemon.jobs.empty()  # not yet: a minute after the reset
    daemon._check_resumes(info["resets"] + 61)
    daemon.jobs.get()()
    assert sent == [("a", "Continue.")]
    daemon._check_resumes(info["resets"] + 120)
    assert daemon.jobs.empty()  # once
    daemon.limit_hit("a", "claude", "Rate limit reached")  # still out: our "continue" came too early
    assert daemon._limited["a"]["resume"] and not daemon._limited["a"].get("sent")
    daemon.speak("Picking up where I left off.", source="claude", final=True, session="a")
    assert "a" not in daemon._limited  # it talks again


def test_resume_can_be_asked_for_when_auto_is_off(daemon):
    from agent_voice import server

    daemon.config["resume"] = {"auto": False, "message": "Continue."}
    daemon.limit_hit("c", "codex", "You've hit your usage limit. Try again at 3:45 PM.")
    assert not daemon._limited["c"]["resume"] and "Resume" in daemon.banner["text"]
    assert server.match_resume("resume when it resets") and server.match_resume("reprends quand la limite est levée")
    assert not server.match_resume("resume the work on the login page")
    assert daemon.set_resume() and daemon._limited["c"]["resume"]  # the latest one out of quota


def test_claude_stop_failure_reports_the_limit(monkeypatch):
    import json

    sent = []
    monkeypatch.setattr(hooks.client, "request", lambda path, payload=None, timeout=2.0: sent.append((path, payload)))
    hooks.hook_claude(json.dumps({"hook_event_name": "StopFailure", "session_id": "s", "error": "rate_limit",
                                  "last_assistant_message": "5-hour limit reached ∙ resets 3pm"}))
    hooks.hook_claude(json.dumps({"hook_event_name": "StopFailure", "session_id": "s", "error": "server_error"}))
    assert sent == [("/limit_hit", {"session": "s", "source": "claude", "text": "5-hour limit reached ∙ resets 3pm"})]


def test_claude_installer_listens_for_stop_failures(tmp_path, monkeypatch):
    import json

    from agent_voice import install

    monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
    install.claude()
    hooks_ = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
    assert hooks_["StopFailure"][0]["matcher"] == "rate_limit"
    install.claude(remove=True)
    assert "StopFailure" not in json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8")).get(
        "hooks", {})


def test_approvals_by_number_and_all(daemon):
    for s in ("a", "b", "c"):
        daemon.request_permission(s, "Bash", f"run step {s}")
    answers = []
    daemon.answer_permission = lambda perm_id, allow: (answers.append((perm_id, allow)), daemon._perms.pop(perm_id))
    assert daemon._voice_approval("deny two")
    assert answers == [(2, False)]
    assert daemon._voice_approval("approve all")
    assert sorted(answers[1:]) == [(1, True), (3, True)] and not daemon._perms


def test_focus_holds_replies_and_catches_up(daemon):
    daemon._fullscreen = "presenting"
    assert daemon.focus == "presenting" and daemon.chime and daemon.status()["focus"] == "presenting"
    daemon.speak("All done.", source="claude", final=True, session="a")
    assert daemon.banner is None and daemon.chimes == [] and daemon.speaker.said == []  # held
    daemon.request_permission("a", "Bash", "run the migration")  # approvals still come through
    assert daemon.permission is not None
    daemon._check_away(0, 100.0)
    daemon._fullscreen = ""
    daemon._perms.clear()
    daemon.permission = None
    daemon._check_away(0, 200.0)
    assert daemon.banner["kind"] == "catchup" and "finished" in daemon.banner["text"]


def test_daily_recap(daemon, monkeypatch):
    import time as t

    from agent_voice import server

    assert server.match_recap("Daily recap.") and server.match_recap("what did I do today?")
    assert not server.match_recap("recap the changes to the login page")
    assert daemon.recap_text()[0] == "Nothing yet today."
    daemon.speak("Voice summary: the tests pass.", source="claude", final=True, session="a")
    daemon.speak("Fixed the parser.", source="codex", final=True, session="c")
    daemon.set_usage({"session": "a", "source": "claude", "tokens": 120000})
    daemon.request_permission("c", "Bash", "deploy", source="codex")
    shown, said = daemon.recap_text()
    assert shown.startswith("2 turns in 2 threads · 120k tokens.") and "Still waiting on you: Codex." in shown
    assert "the tests pass" in shown and "Codex still needs you." in said
    daemon.config["recap"] = {"enabled": True, "at": "00:00"}
    daemon.banner = None
    daemon._check_recap(t.time())
    assert daemon.banner["kind"] == "recap"
    daemon.banner = None
    daemon._check_recap(t.time())
    assert daemon.banner is None  # once a day
    assert daemon.status()["day"]["turns"] == 2 and sum(daemon.status()["day"]["hours"]) == 2


def test_shortcuts_and_handoff(daemon, monkeypatch):
    from agent_voice import server

    pasted = []
    monkeypatch.setattr(daemon, "_paste", lambda text, hwnd=0, submit=False, session="": pasted.append((text, submit)))
    daemon.threads.update({"c": {"source": "codex", "last": 2}})
    daemon._session_hwnd["c"] = 42
    daemon.send_text("c", "commit this")
    assert pasted[-1] == ("Commit the current changes with a clear, short commit message.", True)
    assert server.parse_handoff("Send this to Codex and ask it to explain it.") == ("Codex", "Explain it.")
    assert server.parse_handoff("give the screenshot to the notch redesign") == ("the notch redesign", "")
    assert server.parse_handoff("envoie ça à Codex") == ("Codex", "")
    assert server.parse_handoff("send the tests to Codex") is None
    monkeypatch.setattr(daemon, "_copy_selection", lambda: ("def broken(): pass", ""))
    daemon.handoff("codex", "", "en")
    assert pasted[-1] == ("def broken(): pass ", False)  # in its box, waiting for your question
    daemon.handoff("codex", "review this", "en")
    assert pasted[-1][1] and pasted[-1][0].startswith("Review the changes") and pasted[-1][0].endswith("pass")
    monkeypatch.setattr(daemon, "_copy_selection", lambda: ("", ""))
    monkeypatch.setattr(server.winfocus, "clipboard_png", lambda: b"\x89PNG fake")
    monkeypatch.setattr(server, "HOME", daemon_home := __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    daemon.handoff("codex", "what's wrong here", "en")
    shot = pasted[-1][0].split("\n\n")[1]
    assert shot.endswith(".png") and (daemon_home / "shots").exists()
    daemon.handoff("gemini", "", "en")
    assert "don't know a thread called gemini" in daemon.banner["text"]


def test_thread_detail_and_tokens_today(daemon):
    from agent_voice import hooks as h

    daemon.speak("## Done\n\n```py\nprint(1)\n```\n\nVoice summary: printed it.", source="claude", final=True,
                 session="a", result={"files": 1, "tests": "passed"})
    detail = daemon.thread_detail("a")
    assert "```py" in detail["text"] and "Voice summary" not in detail["text"] and detail["turns"] == 1
    assert daemon.thread_detail("nope") == {}
    today = "2026-10-16"
    entries = [
        {"type": "assistant", "timestamp": "2026-10-16T09:00:00Z",
         "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 90, "cache_read_input_tokens": 9999,
                                           "cache_creation_input_tokens": 400}}},
        {"type": "assistant", "timestamp": "2026-10-16T09:00:01Z",
         "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 90}}},  # the same request again
        {"type": "assistant", "timestamp": "2026-10-15T09:00:00Z", "message": {"id": "m0", "usage": {"input_tokens": 5}}},
    ]
    assert h.claude_tokens_today(entries, today) == 500


def test_clipboard_bitmaps_become_png():
    import io

    PIL = pytest.importorskip("PIL.Image")
    from agent_voice import winfocus

    img = PIL.new("RGB", (3, 2), (255, 0, 0))
    img.putpixel((2, 1), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "BMP")
    png = winfocus.dib_to_png(buf.getvalue()[14:])  # the clipboard's CF_DIB: a BMP without its file header
    out = PIL.open(io.BytesIO(png))
    assert out.size == (3, 2) and out.getpixel((0, 0)) == (255, 0, 0) and out.getpixel((2, 1)) == (0, 0, 255)



def _transcript(path, rows):
    import json
    from datetime import datetime, timezone

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for i, (at, model, out) in enumerate(rows):
            stamp = datetime.fromtimestamp(at, timezone.utc).isoformat()
            f.write(json.dumps({"type": "assistant", "timestamp": stamp, "message": {
                "id": f"{path.stem}-{at}-{i}", "model": model,
                "usage": {"input_tokens": 1000, "output_tokens": out, "cache_read_input_tokens": 50000}}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": "x" * 50}}) + "\n")


def test_claude_usage_from_transcripts(tmp_path):
    from agent_voice import usage

    now = float(int(__import__("time").time()))  # files are dated now: the ledger skips week-old ones
    log = tmp_path / "proj" / "a.jsonl"
    _transcript(log, [(now - 3 * 3600 + 60 * i, "claude-opus-5-5", 2000) for i in range(10)])
    ledger = usage.Ledger(tmp_path)
    assert ledger.refresh(now) == 10 and ledger.refresh(now) == 0  # read once, then only what's new
    spent, tokens = ledger.window(now - 5 * 3600, now)
    one = usage.cost("opus", {"input_tokens": 1000, "output_tokens": 2000, "cache_read_input_tokens": 50000})
    assert tokens == 30000 and abs(spent - 10 * one) < 1e-9
    assert usage.cost("claude-sonnet-5", {"output_tokens": 1_000_000}) == 15.0
    start, end = usage.current_window("five_hour", now, 0.0, ledger)
    first = now - 3 * 3600
    assert start == first - first % 60 and end == start + 5 * 3600  # from its first request, to the minute
    est = usage.Estimator(ledger)
    assert est.estimate("five_hour", now)["pct"] is None  # tokens, but no reading yet to scale them
    est.reading("five_hour", 20, end, now)  # Claude says 20% of this window
    _transcript(log, [(now + 60 + i, "claude-opus-5-5", 2000) for i in range(5)])
    ledger.refresh(now + 120)
    e = est.estimate("five_hour", now + 120)
    assert e["est"] and e["resets"] == int(end) and e["pct"] == 30  # 10 requests were 20%: 5 more, 10% more
    week = usage.Estimator(ledger, {"real": {"seven_day": {"pct": 50, "resets": now - 3600, "at": now - 7200}}})
    assert week.estimate("seven_day", now)["resets"] == int(now - 3600 + 7 * 86400)  # weekly: the same schedule


def test_limits_from_the_usage_folder_and_claude_refresh(daemon, monkeypatch, tmp_path):
    import json
    import subprocess

    from agent_voice import launch, server

    monkeypatch.setattr(server, "HOME", tmp_path)
    (tmp_path / "usage").mkdir()
    (tmp_path / "usage" / "mytool.json").write_text(json.dumps(
        {"agent": "mytool", "five_hour": {"pct": 42, "resets": 4102444800}}), encoding="utf-8")
    daemon._read_usage_drops()
    assert daemon._limits_status()["mytool"]["five_hour"]["pct"] == 42
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a[0]))
    monkeypatch.setattr(launch, "find_cli", lambda agent: "claude.exe")
    t0 = 1_000_000_000.0
    assert daemon._ask_claude_usage(t0) and ran == [["claude.exe", "-p", "/usage"]]
    assert not daemon._ask_claude_usage(t0 + 100)  # every 10 minutes at most
    assert not daemon._ask_claude_usage(t0 + 700)  # and only while Claude is in use (no requests here)
    daemon.config["limits"] = {"claude_refresh_minutes": 0}
    daemon._claude_asked = 0
    assert not daemon._ask_claude_usage(5000.0)


def test_ntfy_messages_and_batching():
    from agent_voice import ntfy

    topic = ntfy.new_topic()
    assert topic.startswith("agent-voice-") and len(topic) > 30 and all(c.isalnum() or c == "-" for c in topic)
    long = "word " * 100
    assert ntfy.message("done", "Codex", long, "titles")["message"] == "Codex finished."
    summary = ntfy.message("done", "Codex", long, "summary")
    assert len(summary["message"]) <= 240 and summary["priority"] == 3
    assert ntfy.message("permission", "Codex", "Wants to deploy.")["priority"] == 5
    both = ntfy.batch([ntfy.message("done", "A", "ok"), ntfy.message("permission", "B", "Wants to deploy.")])
    assert both["title"] == "agent-voice: 2 updates" and both["priority"] == 5
    assert "• B: Wants to deploy." in both["message"]
    assert ntfy.subscribe_url("https://ntfy.example.com/", "t") == "https://ntfy.example.com/t"


def test_phone_notifications_only_when_on_and_away(daemon, monkeypatch):
    from agent_voice import server

    pushed = []
    monkeypatch.setattr(daemon.notifier, "push", lambda kind, title, text="": pushed.append((kind, title, text)))
    monkeypatch.setattr(server.winfocus, "idle_seconds", lambda: 0.0)
    daemon.speak("Voice summary: shipped.", source="claude", final=True, session="a")
    assert pushed == [] and daemon.status()["phone"] == {"enabled": False}  # off: nothing, ever
    status = daemon.set_phone(True)
    assert status["enabled"] and status["url"].startswith("https://ntfy.sh/agent-voice-")
    daemon.speak("Voice summary: shipped again.", source="claude", final=True, session="a")
    assert pushed == []  # you're at the PC
    monkeypatch.setattr(server.winfocus, "idle_seconds", lambda: 600.0)
    daemon.speak("Voice summary: shipped once more.", source="claude", final=True, session="a")
    daemon.request_permission("c", "Bash", "deploy", source="codex")
    assert pushed == [("done", "Claude Code", "shipped once more."), ("permission", "Codex", "Wants to deploy.")]
    old = status["url"]
    assert daemon.new_phone_topic()["url"] != old
    daemon.set_phone(False)
    assert daemon._ntfy_settings() is None


def test_scheduled_prompts(daemon, monkeypatch):
    import time as t

    from agent_voice import server

    sent, started = [], []
    monkeypatch.setattr(daemon, "_send_to", lambda session, message, lang: sent.append((session, message)))
    monkeypatch.setattr(daemon, "new_thread",
                        lambda prompt, target="", cwd="", lang="": started.append((prompt, target)))
    daemon.threads.update({"c": {"source": "codex", "last": 2}})
    now = t.time()
    one = daemon.add_schedule("Run the tests", now + 60, session="c")
    daemon.add_schedule("Add a health check", now + 120, new={"target": "codex", "cwd": "C:/w"})
    daily = daemon.add_schedule("Standup notes", now + 30, session="c", repeat="daily")
    assert [s["title"] for s in daemon.status()["schedules"]] == ["Codex", "Codex", "New thread (Codex)"]
    assert daemon.state["schedules"][0]["prompt"] == "Run the tests"  # survives a restart
    daemon._check_schedules(now + 200)
    while not daemon.jobs.empty():
        daemon.jobs.get()()
    assert sorted(sent) == [("c", "Run the tests"), ("c", "Standup notes")]
    assert started == [("Add a health check", "codex")]
    assert [s["id"] for s in daemon._schedules] == [daily["id"]] and daemon._schedules[0]["at"] > now + 80000
    assert daemon.cancel_schedule(daily["id"]) and daemon._schedules == []
    daemon.add_schedule("Too late", now - 7 * 3600, session="c")
    daemon._check_schedules(now)
    assert daemon.jobs.empty() and "Missed" in daemon.banner["text"]  # the PC was off: said, not sent late
    assert daemon.schedule_said("At 2 pm tell Codex to commit this", "en", "")
    assert daemon._schedules[-1]["session"] == "c" and daemon._schedules[-1]["prompt"] == "Commit this"
    assert daemon.schedule_said("in 20 minutes, new thread with codex: add logging", "en", "")
    assert daemon._schedules[-1]["new"] == {"target": "codex", "cwd": ""}
    assert not daemon.schedule_said("run the tests", "en", "")
    assert one["id"] == 1 and server.launch.LABELS["claude-app"] == "Claude app"


def test_schedule_times():
    from datetime import datetime

    from agent_voice.schedule import next_run, parse_when, split_when

    now = datetime(2026, 10, 16, 10, 0).timestamp()

    def at(h, m=0, d=16):
        return datetime(2026, 10, d, h, m).timestamp()

    assert parse_when("14:00", now) == at(14) and parse_when("2pm", now) == at(14)
    assert parse_when("2:30 pm", now) == at(14, 30) and parse_when("at 9", now) == at(9, d=17)
    assert parse_when("in 20 minutes", now) == now + 1200 and parse_when("demain à 9h", now) == at(9, d=17)
    assert parse_when("à 14h30", now) == at(14, 30) and parse_when("noon", now) == at(12)
    assert parse_when("hello", now) is None and parse_when("25:00", now) is None
    assert split_when("At 2 pm tell Codex to run the tests", now) == (at(14), "Tell Codex to run the tests")
    assert split_when("at the end it works", now) is None
    assert next_run(at(9), "weekdays", at(9, 1)) == at(9, d=19)  # Friday -> Monday



def test_cues_and_chimes_are_soft():
    import numpy as np

    from agent_voice import server

    for kind in ("done", "attention"):
        audio, rate = server.chime_audio(kind, 0.35)
        assert abs(float(np.abs(audio).max()) - 0.35) < 1e-3 and abs(float(audio[-1])) < 1e-3  # no click at the end
        spectrum = np.abs(np.fft.rfft(audio))
        freqs = np.fft.rfftfreq(len(audio), 1 / rate)
        assert spectrum[freqs > 2500].sum() < 0.02 * spectrum.sum()  # nothing shrill
    cue, _ = server.tone_audio(((700.0, 0.0),), 0.16, decay=14.0, tail=0.18)
    assert len(cue) < 0.25 * 44100 and float(np.abs(cue).max()) <= 0.161


def test_limits_refresh_now(daemon, monkeypatch):
    asked = []
    monkeypatch.setattr(daemon, "_ask_claude_usage", lambda now=None, force=False: asked.append(force) or False)
    monkeypatch.setattr(daemon, "_estimate_claude", lambda now=None: None)
    daemon.refresh_limits()
    assert asked == [True] and daemon.status()["limits_refreshing"] is False


def test_a_single_window_reading_keeps_the_other_one(daemon):
    far = 4102444800
    daemon._set_limits("claude", {"five_hour": {"pct": 40, "resets": far}, "seven_day": {"pct": 70, "resets": far}},
                       at=100.0)
    daemon._set_limits("claude", {"five_hour": {"pct": 100, "resets": far}}, at=200.0)  # a limit hit: one window
    assert daemon.limits["claude"] == {"five_hour": {"pct": 100, "resets": far}, "seven_day": {"pct": 70, "resets": far}}


def test_the_ledger_waits_for_a_line_to_be_complete(tmp_path):
    import time

    from agent_voice import usage

    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
    line = json.dumps({"type": "assistant", "timestamp": stamp, "message": {
        "id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 5}}})
    log = tmp_path / "p" / "a.jsonl"
    log.parent.mkdir()
    log.write_text(line[:40], encoding="utf-8")  # Claude Code is still writing it
    ledger = usage.Ledger(tmp_path)
    assert ledger.refresh(now) == 0
    log.write_text(line + "\n", encoding="utf-8")
    assert ledger.refresh(now) == 1 and ledger.items[0][2] == 15


def test_state_is_saved_despite_a_reader_holding_the_file(tmp_path, monkeypatch):
    from agent_voice import config

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "state.json")
    real, calls = config.os.replace, []

    def busy_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("a hook is reading it")
        real(src, dst)

    monkeypatch.setattr(config.os, "replace", busy_once)
    config.save_state({"muted": True})
    assert len(calls) == 2 and config.load_state() == {"muted": True}


def test_config_copies_never_share_the_defaults():
    from agent_voice import config

    a = config.load_config()
    a["auto_listen"]["enabled"] = "changed"
    assert config.load_config()["auto_listen"]["enabled"] != "changed"


def test_toggles_answer_with_their_new_value(daemon):
    from agent_voice import server

    assert server.TOGGLES["/mute"] == "muted" and all(hasattr(daemon, f"set_{n}") for n in server.TOGGLES.values())


def test_screenshot_conversion_handles_24_and_32_bits():
    import struct

    from agent_voice import winfocus

    for bits in (24, 32):
        step = bits // 8
        row = bytes([10, 20, 30, 0][:step] * 3)  # three pixels, blue-green-red(-alpha)
        stride = (3 * step + 3) & ~3
        pixels = (row + b"\0" * (stride - len(row))) * 2
        dib = struct.pack("<IiiHHIIiiII", 40, 3, 2, 1, bits, 0, len(pixels), 0, 0, 0, 0) + pixels
        png = winfocus.dib_to_png(dib)
        assert png.startswith(b"\x89PNG")
        import zlib

        idat = png[png.index(b"IDAT") + 4:]
        raw = zlib.decompress(idat[:struct.unpack(">I", png[png.index(b"IDAT") - 4:png.index(b"IDAT")])[0]])
        assert raw == (b"\0" + bytes([30, 20, 10]) * 3) * 2  # RGB, row by row


def test_take_back_phrases_and_look_at_this():
    from agent_voice import server

    assert server.match_take_back("Wait!") == "hold" and server.match_take_back("No, cancel that.") == "cancel"
    assert server.match_take_back("Send it now") == "send" and server.match_take_back("annule") == "cancel"
    assert server.match_take_back("wait until the tests pass") is None  # a sentence is more of the message
    assert server.parse_look("Look at this and tell me why it's red.") == "Tell me why it's red."
    assert server.parse_look("look at this") == "" and server.parse_look("Regarde ça, c'est cassé") == "C'est cassé"
    assert server.parse_look("look at the tests") is None


def _taking_back(daemon, monkeypatch, seconds=0.3):
    import threading

    pasted = []
    monkeypatch.setattr(daemon, "_paste", lambda text, hwnd=0, submit=False, session="": pasted.append((text, submit)))
    daemon.config["take_back"]["seconds"] = seconds
    thread = threading.Thread(target=daemon._send_spoken, args=("Deploy to production", 7, "a", "en"))
    thread.start()
    deadline = __import__("time").time() + 2
    while daemon._pending is None and __import__("time").time() < deadline:
        __import__("time").sleep(0.01)
    return pasted, thread


def test_take_back_sends_after_a_moment_unless_you_stop_it(daemon, monkeypatch):
    pasted, thread = _taking_back(daemon, monkeypatch)
    assert daemon.status()["pending"] and daemon.banner["kind"] == "pending" and not pasted  # waiting
    thread.join(2)
    assert pasted == [("Deploy to production", True)] and daemon._pending is None
    pasted, thread = _taking_back(daemon, monkeypatch, seconds=5)
    assert daemon.pending_action(daemon._pending["id"], "hold")
    thread.join(2)
    assert pasted == [("Deploy to production", False)]  # typed in, not sent: fix it there
    pasted, thread = _taking_back(daemon, monkeypatch, seconds=5)
    daemon.pending_action(daemon._pending["id"], "cancel")
    thread.join(2)
    assert pasted == [] and daemon.banner["tag"] == "cancelled"
    copied = []
    monkeypatch.setattr("pyperclip.copy", copied.append)
    pasted, thread = _taking_back(daemon, monkeypatch, seconds=5)
    daemon.stop()  # interrupted: not sent, kept
    thread.join(2)
    assert pasted == [] and copied == ["Deploy to production"]


def test_voice_only_in_headphones(daemon):
    daemon.state["headphones_only"] = True
    daemon._output.kind = "headphones"
    assert not daemon.chime
    daemon._output.kind = "speakers"
    assert daemon.chime and daemon.status()["speakers"]
    daemon.speak("Done.", source="claude", final=True, session="a")
    assert daemon.banner["text"] == "Done."  # a banner, not out loud
    daemon.set_chime(False)  # you picked Voice anyway
    assert not daemon.chime
    daemon._output.poll = lambda: "headphones"
    daemon._check_output()
    daemon._output.kind, daemon._output.poll = "speakers", lambda: "speakers"
    assert daemon.chime  # a device change: back to chimes on speakers
    from agent_voice import outputs

    assert outputs.kind_of(3) == "headphones" and outputs.kind_of(1) == "speakers" and outputs.kind_of(8) == ""


def test_look_at_this_sends_a_picture_of_the_window(daemon, monkeypatch, tmp_path):
    from agent_voice import server

    pasted = []
    monkeypatch.setattr(daemon, "_paste", lambda text, hwnd=0, submit=False, session="": pasted.append((text, submit)))
    monkeypatch.setattr(server, "HOME", tmp_path)
    monkeypatch.setattr(server.winfocus, "foreground", lambda: 5)
    monkeypatch.setattr(server.winfocus, "title", lambda hwnd: "Chrome")
    monkeypatch.setattr(server.winfocus, "window_png", lambda hwnd: b"\x89PNG fake")
    daemon.threads["a"] = {"source": "claude", "last": 1}
    daemon.look_at("a", "Why is the button grey?", "en")
    text, sent = pasted[-1]
    assert sent and text.startswith("Why is the button grey?\n\n") and text.endswith(".png")
    daemon.look_at("a", "", "en")
    assert pasted[-1][1] is False and pasted[-1][0].endswith(".png ")  # waits in its box for your question
    monkeypatch.setattr(server.winfocus, "window_png", lambda hwnd: b"")
    daemon.look_at("a", "", "en")
    assert "Couldn't take a picture" in daemon.banner["text"]


def test_the_natural_voice_kokoro(daemon, monkeypatch, tmp_path):
    import io

    from agent_voice import kokoro, tts

    sp = tts.Speaker.__new__(tts.Speaker)
    sp.config = {"speed": 0.5, "volume": 0.5}
    monkeypatch.setattr(kokoro, "synthesize", lambda text, voice, speed=1.0: (tts.np.ones(4), 24000) if speed == 2.0
                        else (tts.np.zeros(0), 0))
    audio, rate = sp._synthesize("kokoro:af_heart", "Hello.")
    assert rate == 24000 and float(audio[0]) == 0.5  # its speed is a rate; the volume applies
    sp.current_spec = "kokoro:ff_siwis"
    assert sp.current_lang == "fr"
    monkeypatch.setattr(kokoro, "available", lambda: True)
    monkeypatch.setattr(kokoro, "ready", lambda: True)
    daemon.set_engine("kokoro")
    assert daemon.config["voices"]["en"] == "kokoro:af_heart" and daemon.status()["engine"] == "kokoro"
    daemon.set_engine("piper")
    assert not daemon.config["voices"]["en"].startswith("kokoro:")
    # A download that doesn't match its checksum is never kept.
    monkeypatch.setattr(kokoro, "FOLDER", tmp_path)
    monkeypatch.setattr(kokoro, "FILES", {"model.onnx": (5, "0" * 64)})

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(kokoro.urllib.request, "urlopen", lambda url, timeout=0: Reply(b"12345"))
    with pytest.raises(ValueError):
        kokoro.download()
    assert not list(tmp_path.iterdir())


def test_talking_over_the_voice_stops_it(daemon, monkeypatch):
    from agent_voice import server, stt

    assert server.match_interrupt("No, wait!") and server.match_interrupt("arrête") and not server.match_interrupt(
        "no I meant the other file")
    daemon.transcriber = type("T", (), {"transcribe": lambda self, audio: (said[0], "en")})()
    daemon._output.kind = "speakers"
    assert not daemon._barge_ok()  # it would hear itself
    daemon._output.kind = "headphones"
    assert daemon._barge_ok()
    stopped, replies, handled = [], [], []
    monkeypatch.setattr(daemon.speaker, "stop", lambda: stopped.append(1))
    monkeypatch.setattr(daemon, "reply_now", lambda session=None: replies.append(session))
    monkeypatch.setattr(daemon, "_handle_heard", lambda text, *a: handled.append(text))

    def you_talk(*args, on_start=None, **kwargs):
        on_start()
        return stt.np.zeros(16000, dtype="float32")

    monkeypatch.setattr(stt, "listen_for_speech", you_talk)
    daemon._voice_session = "a"
    said = ["No, wait."]
    daemon._barge_once()
    assert stopped and replies == ["a"] and not handled  # cut off, then the mic opens for the correction
    said[0] = "Use the staging database instead."
    daemon._barge_once()
    assert handled == ["Use the staging database instead."] and not daemon._listening
    monkeypatch.setattr(stt, "listen_for_speech", lambda *a, **k: None)  # the voice finished first
    daemon._barge_once()
    assert len(handled) == 1 and len(replies) == 1


def test_history_keeps_and_finds_every_conversation(daemon, tmp_path):
    from agent_voice import history

    h = history.History(tmp_path / "h.db", days=1)
    h.add("you", "Corrige le bug de la journée de connexion", "a", "claude", "Login", at=1000.0)
    h.add("reply", "Fixed the login bug: the token wasn't refreshed.", "a", "claude", "Login", at=2000.0)
    h.add("reply", "Deployed the API.", "b", "codex", "API", at=3000.0)
    found = h.search("login tok")  # every word, as a prefix
    assert [r["kind"] for r in found] == ["reply"] and "[token]" in found[0]["snippet"]
    assert h.search("journee")[0]["kind"] == "you"  # accents don't matter
    assert [r["title"] for r in h.search("")] == ["API", "Login", "Login"]  # the latest first
    assert h.get(found[0]["id"])["text"].startswith("Fixed the login bug")
    h.add("reply", "Much later.", at=200000.0)  # a day later: the old ones go
    assert len(h.search("")) == 1
    daemon.prompt_submitted(0, "s", "claude", prompt="Run the tests please")
    daemon.speak("Voice summary: tests pass.", source="claude", final=True, session="s",
                 full="All 42 tests pass after the fix.\n\nVoice summary: tests pass.")
    kinds = {r["kind"]: r for r in daemon.history_db.search("")}
    assert kinds["you"]["snippet"] == "Run the tests please"
    assert daemon.history_db.get(kinds["reply"]["id"])["text"] == "All 42 tests pass after the fix."  # all of it
    assert daemon.thread_detail("s")["text"] == "All 42 tests pass after the fix."


def test_linux_sound_through_pactl(monkeypatch):
    """Linux: what pactl (PulseAudio / PipeWire) reports, read the way the notch needs it."""
    import os

    from agent_voice import pulse

    streams = [
        {"index": 42, "corked": False, "volume": {"front-left": {"value_percent": "80%"},
                                                  "front-right": {"value_percent": "60%"}},
         "properties": {"application.process.binary": "firefox", "application.process.id": "1234"}},
        {"index": 43, "corked": False, "volume": {}, "properties": {"application.process.binary": "python3",
                                                                    "application.process.id": str(os.getpid())}},
    ]
    assert pulse.playing(streams) == [{"index": 42, "name": "firefox", "pid": 1234, "volume": 0.7, "running": True}]
    recording = [{"properties": {"application.process.binary": "zoom", "application.process.id": "77"}},
                 {"properties": {"application.name": "pavucontrol", "media.name": "Peak detect"}},
                 {"corked": True, "properties": {"application.process.binary": "teams"}}]
    assert pulse.recording_apps(recording) == ["zoom"]  # not a volume meter, not a paused stream
    sinks = [{"name": "alsa_output.analog", "description": "Built-in Audio", "active_port": "analog-output-headphones",
              "ports": [{"name": "analog-output-speaker", "type": "Speaker"},
                        {"name": "analog-output-headphones", "type": "Headphones", "description": "Headphones"}]},
             {"name": "bluez_output.AA", "description": "WH-1000XM4", "properties": {"device.form_factor": "headset"}}]
    assert pulse.output_kind(sinks, "alsa_output.analog") == ("headphones", "Built-in Audio")
    assert pulse.output_kind(sinks, "bluez_output.AA")[0] == "headphones"
    sinks[0]["active_port"] = "analog-output-speaker"
    assert pulse.output_kind(sinks, "alsa_output.analog")[0] == "speakers"


def test_linux_desktop_entries(tmp_path):
    from agent_voice import install

    path = tmp_path / "agent-voice.desktop"
    install._desktop_entry(path, ["/home/me/my venv/bin/python", "-m", "agent_voice", "open"], True)
    text = path.read_text(encoding="utf-8")
    assert 'Exec="/home/me/my venv/bin/python" -m agent_voice open' in text and "Type=Application" in text
    install._desktop_entry(path, [], False)
    assert not path.exists()


def test_companion_phrases():
    from datetime import datetime

    from agent_voice.companion import parse_command, parse_duration, parse_reminder

    now = datetime(2026, 10, 16, 10, 0).timestamp()
    assert parse_duration("10 minutes") == 600 and parse_duration("1h30") == 5400
    assert parse_duration("1 hour 30") == 5400 and parse_duration("half an hour") == 1800
    assert parse_duration("une demi-heure") == 1800 and parse_duration("2 minutes and a half") == 150
    assert parse_duration("tea") is None and parse_duration("30 hours") is None
    assert parse_command("Timer 10 minutes.", now) == {"kind": "timer", "seconds": 600, "label": ""}
    assert parse_command("Set a timer for 25 minutes for the tea", now)["label"] == "The tea"
    assert parse_command("a 5 minute timer", now)["seconds"] == 300
    assert parse_command("Minuteur de 3 minutes pour les pâtes", now)["seconds"] == 180
    assert parse_command("Lance le chrono", now) == {"kind": "stopwatch", "action": "start"}
    assert parse_command("Cancel the timer", now) == {"kind": "timer_cancel"}
    assert parse_command("Add buy milk to my list", now) == {"kind": "todo", "text": "Buy milk"}
    assert parse_command("Ajoute à ma liste : appeler Paul", now)["text"] == "Appeler Paul"
    reminder = parse_command("Remind me at 4 pm to call Paul", now)
    assert reminder == {"kind": "reminder", "at": datetime(2026, 10, 16, 16, 0).timestamp(), "text": "Call Paul"}
    assert parse_command("remind me to stretch in 20 minutes", now)["at"] == now + 1200
    assert parse_command("Rappelle-moi dans 10 minutes de sortir le linge", now)["text"] == "Sortir le linge"
    assert parse_command("In 20 minutes remind me to drink water", now)["text"] == "Drink water"
    assert parse_command("Tell Codex to run the tests", now) is None
    assert parse_reminder("16:00 tomatoes", now) == (datetime(2026, 10, 16, 16, 0).timestamp(), "Tomatoes")


def test_companion_keeps_timers_todos_and_reminders():
    from agent_voice.companion import Companion

    saved = []
    box = Companion(None, saved.append)
    tea = box.timer("start", seconds=300, label="tea", now=1000)
    assert tea["end"] == 1300 and tea["label"] == "Tea" and box.next_due() == 1300 and box.wake.is_set()
    box.timer("pause", tea["id"], now=1100)
    assert tea["end"] == 0 and tea["left"] == 200 and box.next_due() is None
    box.timer("resume", tea["id"], now=2000)
    assert tea["end"] == 2200
    box.stopwatch("start", now=10)
    box.stopwatch("pause", now=100)
    assert box.status()["stopwatch"] == {"start": 0.0, "elapsed": 90.0}
    milk = box.todo("add", text="  buy   milk ", source="claude")
    box.todo("toggle", milk["id"])
    assert box.status()["todos"][0] == {**milk, "text": "buy milk", "done": True, "source": "claude"}
    assert box.todo("clear_done") == 1 and box.status()["todos"] == []
    box.reminder("add", text="Call Paul", at=2100)
    assert [kind for kind, _ in box.due(now=2300)] == ["reminder", "timer"]  # oldest first, then gone
    assert box.status()["timers"] == [] and box.status()["reminders"] == [] and saved[-1] is box.data
    for bad in (lambda: box.timer("start", seconds=0), lambda: box.todo("add", text=" "),
                lambda: box.reminder("add", text="x")):
        with pytest.raises(ValueError):
            bad()


def test_todo_categories_and_sections():
    from agent_voice.companion import Companion, split_category, todo_categories, todo_sections

    assert split_category("Call Paul #work") == ("Call Paul", "work")
    assert split_category("Ship it #release.") == ("Ship it.", "release")
    assert split_category("Fix issue #42") == ("Fix issue #42", "")  # a number, not a category
    assert split_category("#home") == ("#home", "")  # nothing left to do: kept as written
    box = Companion(None, lambda data: None)
    box.data["todos"].append({"id": 99, "text": "Saved before categories", "done": False, "source": "you"})
    assert box.todo("add", text="Water the plants")["category"] == ""  # old items have no "category" key
    box.data["todos"].clear()
    paul = box.todo("add", text="Call Paul #Work")
    bank = box.todo("add", text="Call the bank #work")
    assert (paul["category"], bank["category"]) == ("Work", "Work")  # one category, as first written
    milk = box.todo("add", text="Buy milk")
    pr = box.todo("add", text="Review the PR", source="claude", category="agent-voice")
    box.todo("toggle", paul["id"])
    assert box.todo("category", milk["id"], category="#home")["category"] == "home"
    assert todo_categories(box.status()["todos"]) == ["agent-voice", "home", "Work"]
    sections = todo_sections(box.status()["todos"])
    assert [(title, [(name, [t["text"] for t in items]) for name, items in groups]) for title, groups in sections] == [
        ("Yours", [("home", ["Buy milk"]), ("Work", ["Call the bank", "Call Paul"])]),  # what's left first
        ("From your agents", [("agent-voice", ["Review the PR"])])]
    box.todo("category", pr["id"], category="")
    assert todo_sections(box.status()["todos"])[1][1] == [("", [box._find("todos", pr["id"])])]


def test_companion_said_and_due(daemon, monkeypatch):
    monkeypatch.setattr(server_module(), "_beep", lambda freq: None)
    assert daemon.companion_said("Timer 10 minutes for the pasta", "en")
    assert daemon.companion_said("Add call the bank to my list", "en")
    assert not daemon.companion_said("Run the tests again", "en")
    st = daemon.status()["companion"]
    assert st["timers"][0]["label"] == "The pasta" and st["todos"][0]["text"] == "Call the bank"
    timer = st["timers"][0]
    daemon._companion_due("timer", timer, now=timer["end"])
    assert daemon.banner["kind"] == "timer" and daemon.banner["snooze"]["label"] == "The pasta"
    assert daemon.chimes == ["attention"] and daemon.speaker.said[-1][0] == "Time's up: The pasta."
    daemon.set_muted(True)
    daemon._companion_due("reminder", {"id": 9, "text": "Stand-up", "at": 100}, now=100)
    assert daemon.banner["kind"] == "reminder" and daemon.chimes == ["attention"]  # shown, silently
    # The companion's mic: not a command, so it joins the to-do list
    daemon._handle_heard("Water the plants", "en", server_module().COMPANION, 0, None, None, False)
    assert daemon.status()["companion"]["todos"][-1]["text"] == "Water the plants"


def server_module():
    from agent_voice import server

    return server


def test_companion_endpoints_and_mcp_todo(daemon, monkeypatch):
    import http.client
    import threading as th

    from agent_voice import client, mcp, server

    httpd = server._Server(("127.0.0.1", 0), server._handler(daemon))
    th.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def post(path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")

    try:
        assert post("/timer", {"action": "start", "duration": "25 min", "label": "focus"})[0] == 200
        assert post("/timer", {"action": "start", "seconds": 0})[0] == 400
        status, out = post("/reminder", {"action": "add", "text": "in 20 minutes stretch"})
        assert status == 200 and out["item"]["text"] == "Stretch"
        assert post("/reminder", {"action": "add", "text": "stretch someday"})[0] == 400
        assert post("/stopwatch", {"action": "toggle"})[1]["item"]["start"] > 0
        assert post("/todo", {"action": "toggle", "id": 999})[0] == 404
        monkeypatch.setattr(client, "request", lambda path, payload=None, timeout=2.0: post(path, payload)[1])
        agent = mcp.Server("claude", hooked=True, todo_only=True)  # Claude Code: the list only, hooks do the rest
        listed = agent.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        assert [t["name"] for t in listed] == ["todo_list", "todo_add", "todo_update"]
        assert agent.call("todo_list", {}) == ("The to-do list is empty.", False)
        text, error = agent.call("todo_add", {"text": "Review the migration"})
        item_id = int(text.split()[-1].rstrip("."))
        assert not error and "added by claude" in agent.call("todo_list", {})[0]
        assert agent.call("todo_update", {"id": item_id, "done": True}) == ("Updated.", False)
        assert "[x] Review the migration" in agent.call("todo_list", {})[0]
        assert agent.call("todo_update", {"id": item_id, "remove": True}) == ("Removed.", False)
        assert daemon.status()["companion"]["todos"] == []
        text, _error = agent.call("todo_add", {"text": "Tag the release", "category": "agent-voice"})
        item_id = int(text.split()[-1].rstrip("."))
        assert "Tag the release #agent-voice (added by claude)" in agent.call("todo_list", {})[0]
        assert agent.call("todo_update", {"id": item_id, "category": ""}) == ("Updated.", False)  # not ticked off
        todo = daemon.status()["companion"]["todos"][0]
        assert todo["category"] == "" and not todo["done"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_pomodoro_cycles_focus_and_breaks(daemon, monkeypatch):
    from agent_voice import server
    from agent_voice.companion import Companion, parse_command

    box = Companion(None, lambda data: None, {"focus": 25, "short": 5, "long": 15, "every": 2})
    box.pomodoro("start", now=0)
    assert box.data["pomodoro"]["phase"] == "focus" and box.next_due() == 1500
    box.pomodoro("pause", now=600)
    assert box.next_due() is None and box.data["pomodoro"]["left"] == 900
    box.pomodoro("start", now=1000)  # on from where it was
    phases = []
    for _ in range(4):
        now = box.next_due()
        phases += [(e["phase"], e["next"]["phase"]) for _, e in box.due(now)]
    assert phases == [("focus", "short"), ("short", "focus"), ("focus", "long"), ("long", "focus")]
    assert box.data["pomodoro"]["today"] == 2 and box.data["pomodoro"]["round"] == 0
    box.pomodoro("skip", now=box.next_due() - 60)  # focus skipped: a break, but not counted
    assert box.data["pomodoro"]["phase"] == "short" and box.data["pomodoro"]["today"] == 2
    box.pomodoro("stop")
    assert box.data["pomodoro"]["phase"] == "" and box.data["pomodoro"]["today"] == 2
    assert parse_command("Start a pomodoro", 0) == {"kind": "pomodoro", "action": "start"}
    assert parse_command("Arrête le pomodoro", 0) == {"kind": "pomodoro", "action": "stop"}
    # The daemon: the phase that ended, said; the next one has started
    monkeypatch.setattr(server, "_beep", lambda freq: None)
    assert daemon.companion_said("start the pomodoro", "en")
    pomo = daemon.status()["companion"]["pomodoro"]
    assert pomo["phase"] == "focus" and pomo["focus_length"] == 1500
    for kind, item in daemon.companion.due(pomo["end"]):
        daemon._companion_due(kind, item, now=pomo["end"])
    assert daemon.banner["kind"] == "pomodoro" and "Focus done (1 today)" in daemon.banner["text"]
    assert daemon.speaker.said[-1][0] == "Time for a break: 5 min."


def test_now_playing_from_window_titles():
    from agent_voice.media import parse_title

    assert parse_title("Spotify.exe", "Daft Punk - Get Lucky") == {"app": "Spotify", "title": "Get Lucky",
                                                                   "artist": "Daft Punk"}
    assert parse_title("Spotify.exe", "Spotify Premium")["title"] == ""  # paused
    assert parse_title("chrome.exe", "(3) Lo-fi beats - YouTube - Google Chrome") == {
        "app": "YouTube", "title": "Lo-fi beats", "artist": ""}
    assert parse_title("msedge.exe", "Inbox - Outlook - Microsoft Edge") is None
    assert parse_title("notepad.exe", "a - YouTube") is None
    # The YouTube Music web app: its name first, the page after (a no-break space in it); its name isn't a song.
    assert parse_title("chrome.exe", "YouTube Music - YouTube\xa0Music")["title"] == ""
    assert parse_title("chrome.exe", "YouTube Music - Home | YouTube\xa0Music") == {
        "app": "YouTube Music", "title": "Home", "artist": ""}


def test_now_playing_prefers_windows_media_sessions(monkeypatch):
    from agent_voice import media

    assert media.app_name("Chrome._crx_cinhimbnkkghhklpknlkffjgod") == "YouTube Music"
    assert media.app_name("Spotify.exe") == "Spotify" and media.app_name("MSEdge") == "Edge"
    assert media.app_name("308046B0AF4A39CB") == "Firefox" and media.app_name("C:\\Apps\\vlc.exe") == "vlc"
    song = {"app": "YouTube Music", "title": "Midnight Light Flow", "artist": "NOYA", "playing": True}
    monkeypatch.setattr(media, "_session_playing", lambda: dict(song))
    monkeypatch.setattr(media, "_windows_playing", lambda: {"app": "YouTube Music", "title": "", "artist": "",
                                                            "playing": False})
    assert media._windows_now() == song  # the session knows the song; the window title doesn't
    monkeypatch.setattr(media, "_session_playing", lambda: {**song, "app": "Chrome"})
    monkeypatch.setattr(media, "_windows_playing", lambda: {"app": "YouTube", "title": "x", "artist": "",
                                                            "playing": True})
    assert media._windows_now()["app"] == "YouTube"  # a browser tab: named after its site
    monkeypatch.setattr(media, "_session_playing", lambda: None)  # no winrt, or nothing reports
    assert media._windows_now()["app"] == "YouTube"  # the window titles, as before


def test_agenda_reads_ical_repeats_and_exceptions():
    from datetime import date, datetime

    from agent_voice import agenda

    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:a", "SUMMARY:Stand-up", "DTSTART;TZID=Europe/Paris:20261001T093000",
        "DTEND;TZID=Europe/Paris:20261001T094500", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        "EXDATE;TZID=Europe/Paris:20261020T093000", "END:VEVENT",
        "BEGIN:VEVENT", "UID:b", "SUMMARY:Lunch with", "  Paul", "DTSTART:20261019T110000", "DURATION:PT1H",
        "LOCATION:Café\\, rue X", "END:VEVENT",
        "BEGIN:VEVENT", "UID:c", "SUMMARY:Off", "DTSTART;VALUE=DATE:20261019", "DTEND;VALUE=DATE:20261020",
        "END:VEVENT",
        "BEGIN:VEVENT", "UID:a", "RECURRENCE-ID;TZID=Europe/Paris:20261021T093000", "SUMMARY:Stand-up (moved)",
        "DTSTART;TZID=Europe/Paris:20261021T100000", "DTEND;TZID=Europe/Paris:20261021T101500", "END:VEVENT",
        "BEGIN:VEVENT", "UID:d", "SUMMARY:Review", "DTSTART:20261001T140000", "DTEND:20261001T150000",
        "RRULE:FREQ=MONTHLY;BYDAY=3MO", "END:VEVENT",
        "BEGIN:VEVENT", "UID:e", "SUMMARY:Gone", "DTSTART:20261019T160000", "STATUS:CANCELLED", "END:VEVENT",
        "END:VCALENDAR"])
    events = agenda.parse(ics)

    def day(d):
        return [(e["title"], datetime.fromtimestamp(e["start"]).strftime("%H:%M"), e["all_day"])
                for e in agenda.on_day(events, date(2026, 10, d))]

    assert day(19) == [("Off", "00:00", True), ("Stand-up", "09:30", False), ("Lunch with Paul", "11:00", False),
                       ("Review", "14:00", False)]  # the third Monday
    assert day(20) == []  # the stand-up was cancelled that day
    assert day(21) == [("Stand-up (moved)", "10:00", False)]
    assert day(24) == []  # Saturday
    assert agenda.on_day(events, date(2026, 10, 19))[2]["location"] == "Café, rue X"


def test_agenda_reads_outlook_items_without_indexing():
    from datetime import date, datetime
    from types import SimpleNamespace

    from agent_voice import agenda

    class Found:  # Outlook's Restrict result: 1-based, so comtypes' iteration (item 0) fails
        def __init__(self, items):
            self.items, self.at = items, -1

        def __getitem__(self, index):
            raise OSError("Array index out of bounds.")

        def GetFirst(self):
            self.at = -1
            return self.GetNext()

        def GetNext(self):
            self.at += 1
            return self.items[self.at] if self.at < len(self.items) else None

    items = [SimpleNamespace(Subject="Stand-up", Location=None, AllDayEvent=False,
                             Start=46314.375, End=46314.390625),  # late-bound: OLE days
             SimpleNamespace(Subject="", Location="Room 2", AllDayEvent=True,
                             Start=datetime(2026, 10, 19), End=datetime(2026, 10, 20))]
    got = agenda._outlook_items(Found(items))
    assert [(e["title"], datetime.fromtimestamp(e["start"]).strftime("%d/%m %H:%M"),
             datetime.fromtimestamp(e["end"]).strftime("%H:%M"), e["location"], e["all_day"]) for e in got] == [
        ("Stand-up", "19/10 09:00", "09:22", "", False), ("(no title)", "19/10 00:00", "00:00", "Room 2", True)]
    assert len(agenda._outlook_items(Found(items * 20), limit=30)) == 30

    query = agenda._outlook_filter(date(2026, 10, 5))  # never '10/05/2026', which a French Outlook reads as 10 May
    assert query.startswith('@SQL="urn:schemas:calendar:dtstart" < \'2026-10-0')
    assert "/" not in query


def test_agent_todos_focus_sessions_and_notes(daemon, monkeypatch):
    from agent_voice import server

    monkeypatch.setattr(server, "_beep", lambda freq: None)
    daemon.speak("All migrated.\n\nTo-do for you: run the migration on staging\nVoice summary: done.",
                 source="claude", final=True, session="a")
    daemon.speak("**To-do for you:** run the migration on staging", source="claude", final=True, session="a")
    todos = daemon.status()["companion"]["todos"]
    assert [(t["text"], t["source"], t["session"]) for t in todos] == [
        ("run the migration on staging", "claude", "a")]  # once, with the thread it came from
    # Focus: during a Pomodoro focus session, replies wait (and are told at the break)
    daemon.companion.pomodoro("start")
    assert daemon.focus == "focus session" and daemon._holding()
    daemon.config["pomodoro"]["hold"] = False
    assert daemon.focus == ""
    daemon.companion.pomodoro("stop")
    # A meeting holds them too, when asked
    now = server.time.time()
    daemon.agenda = [{"title": "Stand-up", "location": "", "start": now - 60, "end": now + 600, "all_day": False}]
    assert daemon.focus == ""
    daemon.config["calendar"]["quiet_in_meetings"] = True
    assert daemon.focus == "meeting"
    # Notes: "note: ..." by voice, kept, and in the history
    assert daemon.companion_said("Note: the bug only shows with two monitors", "en")
    assert daemon.status()["companion"]["notes"][0]["text"] == "The bug only shows with two monitors"
    assert daemon.history_db.search("monitors")[0]["kind"] == "note"
    with pytest.raises(ValueError):
        daemon.set_calendar("add", "not a link")
    assert daemon.set_calendar("add", "https://calendar.example/secret.ics") == ["https://calendar.example/secret.ics"]
    assert daemon.set_calendar("clear") == []


def test_transcript_titles_read_in_chunks_and_refuse_patterns(tmp_path, monkeypatch):
    import json as js

    from agent_voice.claudeapp import TranscriptTitles

    folder = tmp_path / "C--work"
    folder.mkdir()
    path = folder / "abc.jsonl"
    filler = js.dumps({"type": "user", "message": "x" * 300}) + "\n"
    path.write_text(filler * 40 + js.dumps({"type": "ai-title", "aiTitle": "Fix the login"}) + "\n", encoding="utf-8")
    titles = TranscriptTitles(tmp_path)
    monkeypatch.setattr(TranscriptTitles, "CHUNK", 4096)
    seen = [titles.title("abc") for _ in range(6)]
    assert seen[0] == "" and seen[-1] == "Fix the login"  # caught up over a few polls, never all at once
    assert titles.title("*") == "" and titles.title("../abc") == ""


def test_agents_add_few_todos_and_the_music_stays_down_while_they_work(daemon):
    for i in range(4):  # a chatty thread: only 2 wait on you at once, one per turn
        daemon.speak(f"Done.\nTo-do for you: step {i}\nTo-do for you: extra {i}", source="claude", final=True,
                     session="a")
    assert [t["text"] for t in daemon.status()["companion"]["todos"]] == ["step 0", "step 1"]
    for i in range(10):
        daemon.speak(f"To-do for you: other {i}", source="codex", final=True, session=f"s{i}")
    assert sum(1 for t in daemon.companion.data["todos"] if t["source"] != "you") == 8  # 8 open at most
    daemon.companion.todo("add", text="Mine")
    assert daemon.companion.todo("clear_agents") == 8 and [t["text"] for t in daemon.companion.data["todos"]] == ["Mine"]
    # Ducking: between two progress notes of a thread still at work, the music isn't brought back up
    daemon._ducker = object()
    daemon._duck_state, daemon._voice_session = "voice", "w"
    daemon.activity["w"] = {"source": "claude", "started": 0, "steps": 1, "last": server_time()}
    daemon._duck_tick(100.0)
    daemon._duck_tick(105.0)
    assert daemon._duck_state == "voice" and daemon._duck_jobs.empty()
    daemon.activity.clear()  # the turn is over: back up almost at once
    daemon._duck_tick(105.5)
    assert daemon._duck_state == "" and daemon._duck_jobs.get() == ("restore",)
    # The mic open for your reply: in headphones the video is back at once; through speakers it stays at the
    # voice's level (the mic would take it for you); only once you speak is it silenced (and paused).
    daemon._duck_state, daemon._mic_phase, daemon._output.kind = "voice", "waiting", "headphones"
    daemon._duck_tick(200.0)
    daemon._duck_tick(200.5)
    assert daemon._duck_state == "" and daemon._duck_jobs.get() == ("restore",)
    daemon._output.kind = "speakers"
    daemon._duck_tick(201.0)
    assert daemon._duck_state == "voice" and daemon._duck_jobs.get() == ("duck", 0.3, False)
    daemon._mic_phase = "recording"
    daemon._duck_tick(202.0)
    assert daemon._duck_state == "listen" and daemon._duck_jobs.get() == ("duck", 0.0, True)
    daemon._mic_phase = ""


def server_time():
    from agent_voice import server

    return server.time.time()


def test_one_reply_is_said_once_even_through_two_channels(daemon, monkeypatch):
    from agent_voice import mcp

    # Claude Code (the desktop app's Code tab starts the app's MCP servers): no speak tool beside its hooks
    monkeypatch.setattr(mcp, "_claude_hooked", lambda: True)
    server = mcp.Server("claude-desktop")
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-code"}}})
    tools = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert "speak" not in tools and tools == ["todo_list", "todo_add", "todo_update"]
    desktop = mcp.Server("claude-desktop")  # the desktop app's own chat keeps its voice
    desktop.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"clientInfo": {"name": "claude-ai"}}})
    assert "speak" in [t["name"] for t in desktop.handle({"jsonrpc": "2.0", "id": 2,
                                                          "method": "tools/list"})["result"]["tools"]]
    # The daemon's net: the same words from another conversation a moment later aren't said again
    daemon.speak("All tests pass.", source="claude", final=True, session="hook-a")
    daemon.speak("All tests pass!", source="claude-desktop", final=True, session="mcp-x")
    assert [t for t, _p in daemon.speaker.said] == ["All tests pass."]
    daemon.speak("All tests pass.", source="claude", final=True, session="hook-a")  # the same thread: its own call
    assert len(daemon.speaker.said) == 2


def test_music_is_found_playing_through_a_quiet_moment(monkeypatch):
    from agent_voice import ducking

    class Meter:
        def __init__(self, peaks):
            self.peaks = list(peaks)

        def GetPeakValue(self):
            return self.peaks.pop(0) if self.peaks else 0.0

    monkeypatch.setattr(ducking.time, "sleep", lambda s: None)
    quiet_then_loud = [("spotify:1", "Spotify.exe", None, Meter([0.0, 0.0, 0.3])), ("x:2", "x.exe", None, Meter([]))]
    assert ducking._sounding(quiet_then_loud) == ["Spotify.exe"]
    assert ducking._sounding([("x:2", "x.exe", None, Meter([]))]) == []


def test_your_language_wins_when_detection_is_unsure(daemon, monkeypatch):
    from types import SimpleNamespace

    from agent_voice import lang, stt

    daemon.set_language("fr")
    assert daemon.status()["language"] == "fr" and daemon.state["language"] == "fr"
    assert daemon.config["default_lang"] == "fr" and daemon.config["stt"]["prefer"] == "fr"
    assert lang.detect("Docker") == "fr"  # nothing to tell it by: yours
    assert lang.detect("Could you restart the server please?", "fr") == "en"  # clear: as it is

    calls = []

    class Model:
        def transcribe(self, audio, beam_size=1, vad_filter=True, language=None):
            calls.append(language)
            probs = [("en", self.en), ("fr", 1 - self.en)]
            return iter([SimpleNamespace(text=" Oui.")]), SimpleNamespace(
                language=language or "en", language_probability=self.en, all_language_probs=probs)

    t = stt.Transcriber({"stt": {"languages": ["fr", "en"], "prefer": "fr"}})
    t._model = Model()
    audio = stt.np.zeros(stt.RATE, dtype="float32")
    t._model.en = 0.6  # unsure: French
    assert t.transcribe(audio) == ("Oui.", "fr") and calls == [None, "fr"]
    calls.clear()
    t._model.en = 0.95  # clearly English: English
    assert t.transcribe(audio)[1] == "en" and calls == [None]
    daemon.set_language("en")
    assert lang.detect("Docker") == "en"


def _translated_strings():
    """Every literal passed to tr() / plural() (and the voice's _confirm / _say) in agent_voice."""
    import ast
    from pathlib import Path

    def literals(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.IfExp):
            return literals(node.body) + literals(node.orelse)
        return []

    found = {}
    for path in sorted((Path(__file__).parent.parent / "agent_voice").glob("*.py")):
        if path.name in ("i18n.py", "i18n_fr.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                args = node.args[:1] if name in ("tr", "_confirm", "_say") else node.args[1:3] if name == "plural" else []
                for a in args:
                    for v in literals(a):
                        found.setdefault(v, f"{path.name}:{node.lineno}")
    return found


def test_every_string_has_its_french():
    import ast
    import string
    from pathlib import Path

    from agent_voice import companion, i18n, i18n_fr, ntfy, server, widget

    def fields(text):
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    # Shown where they're displayed (tr(variable)): the constants behind them.
    shown = [title for _c, title, sub in widget.PHASES.values()] + [sub for _c, _t, sub in widget.PHASES.values() if sub]
    shown += [label for label, options in widget.SETTING_ROWS.values()]
    shown += [text for _l, options in widget.SETTING_ROWS.values() for _v, text in options if text and not text[0].isdigit()]
    shown += [section for section, _rows in widget.SETTINGS] + list(widget.SidePanel.LABELS.values())
    shown += list(widget.SidePanel.TITLES.values()) + [h for _c, h in widget.BANNER_KINDS.values() if h]
    shown += list(companion.PHASES.values()) + list(widget.QuickInput.HINTS.values())
    shown += [label for _icon, label in widget.BANNER_BUTTONS.values()]
    shown += [said for _p, _t, said in ntfy.KINDS.values()] + list(server.Daemon._NEXT_STEP.values())
    shown += ["Claude app", "Browser", "the Claude desktop app", "Yours", "From your agents", "presenting",
              "full screen", "focus session", "meeting", "daily", "weekdays", "passed", "failed"]
    missing = {k: w for k, w in _translated_strings().items() if k not in i18n_fr.FR}
    missing.update({k: "constant" for k in shown if k not in i18n_fr.FR})
    assert not missing, missing
    for en, fr in i18n_fr.FR.items():
        assert fields(en) == fields(fr), (en, fr)  # the same placeholders, or format() fails in French
    keys = [k.value for k in ast.parse(Path(i18n_fr.__file__).read_text(encoding="utf-8")).body[-1].value.keys]
    assert len(keys) == len(set(keys)), [k for k in keys if keys.count(k) > 1]  # no key given twice

    i18n.set_language("fr")
    try:
        assert i18n.tr("Settings") == "Réglages" and i18n.plural(2, "{n} needs you", "{n} need you") == "2 vous attendent"
        assert i18n.plural(0, "{n} turn", "{n} turns") == "0 tour"  # French: 0 is singular
        assert i18n.action("Run pytest") == "Exécuter pytest" and i18n.action("edit widget.py") == "modifier widget.py"
        assert i18n.tr("Speed {x}.", in_lang="en", x="1.5") == "Speed 1.5."  # the voice keeps its own language
    finally:
        i18n.set_language("en")
    assert i18n.tr("Settings") == "Settings" and i18n.plural(0, "{n} turn", "{n} turns") == "0 turns"


def test_devices_match_portaudio_names_cut_at_31():
    from agent_voice import devices

    found = [{"name": "Microphone (UGREEN Camera 2K)", "max_input_channels": 1, "hostapi": 0},
               {"name": "Haut-parleurs (HyperX Cloud Al", "max_input_channels": 0, "max_output_channels": 2,
                "hostapi": 0},
               {"name": "Microphone (HyperX Cloud Alpha ", "max_input_channels": 1, "hostapi": 0},
               {"name": "Microphone (HyperX Cloud Alpha S Chat)", "max_input_channels": 1, "hostapi": 1}]
    assert devices.match_index("Microphone (HyperX Cloud Alpha S Chat)", found, 0) == 2
    assert devices.match_index("Microphone (HyperX Cloud Alpha S Chat)", found, 1) == 3
    assert devices.match_index("Microphone (Other)", found, 0) is None
    assert devices.match_index("Haut-parleurs (HyperX Cloud Alpha S Game)", found, 0, "output") == 1
    assert devices.match_index("Haut-parleurs (HyperX Cloud Alpha S Game)", found, 0) is None


def test_a_mic_that_sends_nothing_is_skipped(monkeypatch):
    import sys
    import types

    import numpy as np

    from agent_voice import devices

    opened, closed, silent = [], [], []

    class Stream:
        def __init__(self, device, callback, **_kw):
            self.device, self.callback = device, callback
            opened.append(device)

        def start(self):
            # The headset (7) sends only zeros, the default real sound.
            block = np.zeros((480, 1), np.float32) if self.device == 7 else np.full((480, 1), 0.01, np.float32)
            self.callback(block, 480, None, None)

        def abort(self):
            closed.append(self.device)

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=Stream))
    monkeypatch.setattr(devices, "candidates", lambda: [7, None])
    monkeypatch.setattr(devices, "on_silent", lambda: silent.append(1))
    got = []
    stream = devices.open_input(lambda indata, *_a: got.append(indata), 16000, probe=0.05)
    assert stream.device is None and opened == [7, None] and len(got) == 1 and not silent

    monkeypatch.setattr(devices, "candidates", lambda: [7])
    assert devices.open_input(lambda *_a: None, 16000, probe=0.05) is None and silent == [1]



def test_device_rows_show_the_device_in_brackets():
    from agent_voice import widget

    assert widget._device_label("Microphone (HyperX Cloud Alpha S Chat)") == "HyperX Cloud Alpha S Chat"
    assert widget._device_label("Odyssey G65B") == "Odyssey G65B"
    assert widget._device_label("") == widget.tr("System default")
    assert widget._device_label("Odyssey G65B (2- HD Audio Driver for Display Audio)") == "Odyssey G65B"
    shown = widget._device_label("Speakers (Cirrus Logic XU (with APO Extensions))")
    assert shown.startswith("Cirrus Logic XU (") and shown.endswith("…") and len(shown) == 30


def test_meeting_join_links():
    from agent_voice import agenda

    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:Design review\nDTSTART:20261020T100000\n"
           "DTEND:20261020T103000\nLOCATION:Microsoft Teams Meeting\nDESCRIPTION:Join: <https://teams.microsoft.com/l/"
           "meetup-join/19%3ameeting_x%40thread.v2/0?context=%7b%7d>\\nhttps://example.com/other\nEND:VEVENT\n"
           "BEGIN:VEVENT\nUID:2\nSUMMARY:Standup\nDTSTART:20261020T090000\nDTEND:20261020T091500\n"
           "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij\nDESCRIPTION:https://zoom.us/j/1\nEND:VEVENT\n"
           "BEGIN:VEVENT\nUID:3\nSUMMARY:Lunch\nDTSTART:20261020T120000\nDTEND:20261020T130000\n"
           "LOCATION:https://zoom.us.evil.example/j/1 https://evil.example/?u=https://zoom.us/j/1\nEND:VEVENT\n"
           "END:VCALENDAR\n")
    from datetime import date

    day = {e["title"]: e for e in agenda.on_day(agenda.parse(ics), date(2026, 10, 20))}
    assert day["Design review"]["join"].startswith("https://teams.microsoft.com/l/meetup-join/")
    assert day["Standup"]["join"] == "https://meet.google.com/abc-defg-hij"  # Meet's own field before the text
    assert day["Lunch"]["join"] == ""  # only the meeting services' own hosts
    assert agenda.join_link("Zoom: https://us02web.zoom.us/j/123?pwd=ab.") == "https://us02web.zoom.us/j/123?pwd=ab"
    assert "_texts" not in agenda.parse(ics)[0]  # the invitation's text isn't kept


def test_join_a_meeting(daemon, monkeypatch):
    import time as t
    import webbrowser

    from agent_voice import server

    assert server.match_join("Join my meeting.") and server.match_join("rejoins la réunion")
    assert not server.match_join("join the two lists in utils.py")
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    now = t.time()
    daemon.agenda = [
        {"title": "Later", "location": "", "join": "https://zoom.us/j/2", "start": now + 3 * 3600,
         "end": now + 4 * 3600, "all_day": False},
        {"title": "Standup", "location": "", "join": "https://meet.google.com/abc", "start": now + 300,
         "end": now + 1200, "all_day": False},
        {"title": "Forged", "location": "", "join": "https://evil.example/x", "start": now + 5 * 3600,
         "end": now + 6 * 3600, "all_day": False}]
    daemon.agenda.sort(key=lambda e: e["start"])
    assert daemon.join_meeting() == "Standup" and opened == ["https://meet.google.com/abc"]
    assert daemon.join_meeting(start=daemon.agenda[1]["start"]) == "Later"  # the agenda's Join: that one
    assert daemon.join_meeting(start=daemon.agenda[2]["start"]) == "" and len(opened) == 2  # never another host
    daemon.agenda = daemon.agenda[1:]
    assert daemon.join_meeting() == ""  # nothing within 15 minutes


def test_morning_brief(daemon, monkeypatch):
    import time as t

    from agent_voice import server

    assert server.match_brief("Morning brief.") and server.match_brief("What's my day?")
    assert server.match_brief("c'est quoi ma journée ?") and not server.match_brief("brief Codex on the bug")
    assert daemon.brief_text() == ("", "")  # nothing to tell: no banner by itself
    now = t.time()
    daemon.state["day"] = {"date": "2026-01-01", "hours": [0] * 24,
                           "threads": {"a": {"source": "claude", "turns": 7, "tokens": 0, "last": "", "title": "Notch"}}}
    daemon.agenda = [{"title": "Design review", "location": "", "join": "", "start": now + 600, "end": now + 2400,
                      "all_day": False}]
    daemon.companion.todo("add", text="call Paul")
    daemon.request_permission("c", "Bash", "deploy", source="codex")
    shown, said = daemon.brief_text()
    assert "1 meeting:" in shown and "Design review" in shown and "1 to-do: call Paul." in shown
    assert "Still waiting on you: Codex." in shown and "Last time: 7 turns in 1 thread, most of it in Notch." in shown
    assert "Design review at" in said and "Codex still needs you." in said
    daemon.config["brief"] = {"enabled": False}
    daemon.brief(asked=True)  # asked for outside the morning: the morning's still comes
    assert daemon.banner["kind"] == "brief" and "brief_day" not in daemon.state
    daemon.config["brief"] = {"enabled": True, "from": "00:00", "until": "24:00"}
    daemon.banner = None
    daemon._check_brief(now, idle=2)
    assert daemon.banner["kind"] == "brief"
    daemon.banner = None
    daemon._check_brief(now, idle=2)
    assert daemon.banner is None  # once a day


def test_usage_forecast(daemon):
    import time as t

    now = t.time()
    daemon._estimator.rate["five_hour"] = 1.0  # a dollar of requests is one percent
    daemon._ledger.items = [(now - 1800 + i * 60, 0.5, 100) for i in range(30)]  # 15 % in the last half hour
    w = {"pct": 40, "resets": now + 4.5 * 3600, "est": True}  # the window started half an hour ago
    full = daemon._forecast("claude", "five_hour", w, now)
    assert full and abs(full - (now + 60 / 15 * 1800)) < 400  # 60 % left at 15 % per half hour: two hours
    assert not daemon._forecast("claude", "five_hour", {**w, "resets": now + 3600}, now)  # resets first
    daemon._ledger.items = [(now - 1800, 15.0, 100)]
    assert not daemon._forecast("claude", "five_hour", w, now)  # nothing in the last 15 minutes
    # Another agent: from how its readings rose.
    daemon._pace[("codex", "five_hour")] = [(now - 3000, 20.0, int((now + 3 * 3600) // 600)),
                                            (now - 1200, 30.0, int((now + 3 * 3600) // 600))]
    full = daemon._forecast("codex", "five_hour", {"pct": 40, "resets": now + 3 * 3600}, now)
    assert full and abs(full - (now + 60 / 20 * 3000)) < 400
