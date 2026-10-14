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


def test_claude_turn_texts(tmp_path):
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
    assert hooks.claude_turn_texts(path) == ["working", "done"]


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
    assert sent == [("/notify", "attention"), ("/notify", "question"), ("/activity", None)]
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
