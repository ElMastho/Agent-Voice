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
    monkeypatch.setattr(tts.sd, "play", lambda audio, rate: played.append(time.time()))
    monkeypatch.setattr(tts.sd, "stop", lambda: None)
    monkeypatch.setattr(tts.sd, "get_stream", lambda: type("S", (), {"active": False})())
    sp = tts.Speaker({"voices": {"en": "x"}, "default_lang": "en", "speed": 1.0, "volume": 1.0, "max_chars": 0})
    sp.hold()  # auto-listen
    sp.hold()  # push-to-talk on top
    sp._audio_q.put((sp._gen, tts.np.zeros(10, dtype="float32"), 16000, "x"))
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
        self.current_spec = self.current_text = ""

    def speak(self, text, lang=None, voice=None, pitch=1.0):
        self.said.append((text, pitch))
        return 1

    def stop(self):
        pass

    hold = release = stop


@pytest.fixture
def daemon(monkeypatch):
    from agent_voice import config, server

    monkeypatch.setattr(server, "Speaker", FakeSpeaker)
    monkeypatch.setattr(server, "load_state", lambda: {})
    monkeypatch.setattr(server, "save_state", lambda state: None)
    monkeypatch.setattr(server.install, "agent_states", lambda: {})
    monkeypatch.setattr(server.install, "usage_line_state", lambda: "off")
    monkeypatch.setattr(server.claudeapp, "session_title", lambda session: "")
    monkeypatch.setattr(server.winfocus, "is_window", lambda hwnd: True)
    d = server.Daemon(config.load_config(), start_threads=False)
    d.chimes = []
    monkeypatch.setattr(d, "play_chime", lambda kind: d.chimes.append(kind))
    return d


def test_chime_and_silent_show_the_reply_as_a_banner(daemon):
    daemon.state["chime"] = True
    daemon.speak("Working on it.", source="claude", session="a")
    daemon.speak("Voice summary: all tests pass.", source="claude", final=True, session="a")
    assert daemon.speaker.said == []  # no voice in chime mode
    assert daemon.chimes == ["done"]
    assert daemon.banner["text"].endswith("all tests pass.")
    daemon.set_output("silent")
    daemon.speak("Second answer.", source="claude", final=True, session="a")
    assert daemon.banner["text"] == "Second answer." and daemon.output == "silent"
    assert daemon.speaker.said == []


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
    monkeypatch.setattr(server.time, "sleep", lambda s: seen.append((daemon.in_call, daemon.chime)))
    with pytest.raises(SystemExit):
        daemon._call_watch()
    # (in_call, chime) seen before each check: idle, in the Zoom call (chimes), still in it, back to voice
    assert seen == [("", False), ("Zoom", True), ("Zoom", True), ("", False)]


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
    assert names == ["speak", "notify", "ask_user", "set_status"]
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
    monkeypatch.setattr(daemon, "_grab_selection", lambda: "def broken(): pass")
    daemon.handoff("codex", "", "en")
    assert pasted[-1] == ("def broken(): pass ", False)  # in its box, waiting for your question
    daemon.handoff("codex", "review this", "en")
    assert pasted[-1][1] and pasted[-1][0].startswith("Review the changes") and pasted[-1][0].endswith("pass")
    monkeypatch.setattr(daemon, "_grab_selection", lambda: "")
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
    assert one["id"] == 1 and server.TARGET_NAMES["claude-app"] == "Claude app"


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
