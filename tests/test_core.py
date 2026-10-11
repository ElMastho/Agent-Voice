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
