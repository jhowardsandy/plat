"""The thinking ladder. Escalation used to have exactly one rung: sonnet -> opus,
three times the price in a single step."""
from pathlib import Path
import pytest
import yaml

from plat import roles as R
from plat.adapters import claude, codex

CFG = yaml.safe_load((Path(R.__file__).parent / "roles.default.yaml").read_text())


def _role(name, attempt):
    return R.bind(CFG, name, attempt)


def test_the_ladder_has_three_distinct_rungs():
    a1, a2, a3 = (_role("coder", n) for n in (1, 2, 3))
    assert (a1.model, a1.effort) == ("sonnet", "medium")
    assert (a2.model, a2.effort) == ("sonnet", "xhigh"), "attempt 2 thinks harder, same model"
    assert (a3.model, a3.effort) == ("opus", "high"), "attempt 3 changes model"
    assert len({(r.model, r.effort) for r in (a1, a2, a3)}) == 3


def test_claude_passes_effort_through(tmp_path):
    pf = tmp_path / "p.md"; pf.write_text("hi")
    cmd = claude.build(_role("coder", 2), pf, tmp_path)
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "xhigh"


def test_codex_maps_down_to_its_coarser_scale(tmp_path):
    pf = tmp_path / "p.md"; pf.write_text("hi")
    r = _role("reviewer.correctness", 1)
    r.effort = "xhigh"
    cmd = codex.build(r, pf, tmp_path)
    assert any('model_reasoning_effort="high"' in c for c in cmd), \
        "xhigh must map to high, not be dropped"


def test_no_effort_means_no_flag(tmp_path):
    pf = tmp_path / "p.md"; pf.write_text("hi")
    r = _role("coder", 1); r.effort = None
    assert "--effort" not in claude.build(r, pf, tmp_path)
    assert not any("model_reasoning_effort" in c for c in codex.build(r, pf, tmp_path))


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_every_level_is_accepted_by_both_adapters(tmp_path, level):
    """claude --effort takes exactly these five; codex must not emit an unknown value."""
    pf = tmp_path / "p.md"; pf.write_text("hi")
    r = _role("coder", 1); r.effort = level
    cmd = claude.build(r, pf, tmp_path)
    assert cmd[cmd.index("--effort") + 1] == level
    emitted = [c for c in codex.build(r, pf, tmp_path) if "model_reasoning_effort" in c]
    assert emitted and emitted[0].split('"')[1] in ("low", "medium", "high")


def test_the_variadic_flag_is_last(tmp_path):
    """--allowedTools swallows everything after it that does not look like a flag."""
    pf = tmp_path / "p.md"; pf.write_text("hi")
    r = _role("coder", 2)
    r.session_id = "abc"
    cmd = claude.build(r, pf, tmp_path)
    assert cmd.index("--allowedTools") > cmd.index("--resume")
    assert cmd.index("--allowedTools") > cmd.index("--effort")
