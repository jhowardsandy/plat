"""Protocol is not narration. The live log stored raw stream-json: rate-limit
envelopes, tool-call JSON, and a 3KB result object nobody could read."""
from plat.adapters import narrate

# One object per line: that is what stream-json actually emits, and narrate parses
# line by line. A pretty-printed fixture would test something that never happens.
CLAUDE = (
    '{"type":"rate_limit_event","rate_limit_info":{}}\n'
    '{"type":"system","subtype":"init","tools":["Read"]}\n'
    '{"type":"assistant","message":{"content":['
    '{"type":"text","text":"Reading app/quota.py."},'
    '{"type":"tool_use","name":"Read","input":{"file_path":"app/quota.py"}}]}}\n'
    '{"type":"result","duration_ms":61503,"total_cost_usd":0.34}\n')

CODEX = """Reading additional input from stdin...
{"type":"thread.started","thread_id":"x"}
{"type":"item.completed","item":{"type":"agent_message","text":"Counter is local."}}
{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q"}}
{"type":"turn.completed","usage":{"output_tokens":412}}"""


def test_claude_narration_keeps_prose_and_drops_envelopes():
    out = narrate("claude", CLAUDE)
    joined = "\n".join(out)
    assert "Reading app/quota.py." in joined
    assert "Read app/quota.py" in joined
    assert "rate_limit" not in joined and "subtype" not in joined


def test_codex_narration_keeps_messages_and_commands():
    out = narrate("codex", CODEX)
    joined = "\n".join(out)
    assert "Counter is local." in joined and "pytest -q" in joined
    assert "thread.started" not in joined
    assert "Reading additional input" not in joined


def test_malformed_output_never_raises():
    assert narrate("claude", "not json at all\n{broken") == []
    assert narrate("codex", "") == []


def test_an_unknown_provider_falls_back_to_raw_lines():
    assert narrate("nope", "a\nb") == ["a", "b"]


def test_claude_streams_rather_than_buffering():
    """--output-format json emits ONE object at exit, so there is nothing to tail
    for the whole hour that matters."""
    import inspect

    from plat.adapters import claude
    src = inspect.getsource(claude.build)
    assert "stream-json" in src and '"json",' not in src


def test_a_line_split_across_chunks_is_not_lost():
    """spawn flushes every ~2s or ~4KB, so a chunk routinely ends mid-line. The
    runner carries the incomplete tail forward instead of dropping it."""
    import inspect

    from plat import runner
    src = inspect.getsource(runner._run_agent)
    assert "residue" in src
    assert 'buf.rfind("\\n")' in src or "rfind" in src

    # and the halves, rejoined, narrate as one line
    half_a, half_b = CLAUDE[:120], CLAUDE[120:]
    assert narrate("claude", half_a + half_b), "rejoined stream produced nothing"
