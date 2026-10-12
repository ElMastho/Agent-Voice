import json

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
    time.sleep(0.2)
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
    assert sent == [("/notify", "attention"), ("/notify", "question")]
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
