"""L1 - the rules, exhaustively. This is where a bug costs money rather than an error.

The FSM is pure, so the whole reachable space can be walked. Four properties:
every path terminates, no path exceeds MAX_ATTEMPTS, oscillation always reaches a
human, and a failed gate never advances.
"""
import itertools
import pytest
from plat.fsm import (decide, Ctx, LotState, MAX_ATTEMPTS, advance, gate_passed,
                      criteria_met, oscillating)

GOOD_GATE = {"tests_failed": 0, "diff_files": 3}
BAD_GATE = {"tests_failed": 2, "diff_files": 3}
EMPTY_GATE = {"tests_failed": 0, "diff_files": 0}     # "complete" with no diff
PASS = {"verdict": "pass", "criteria_check": [{"id": "AC1", "met": True}], "findings": []}
UNMET = {"verdict": "pass", "criteria_check": [{"id": "AC1", "met": False}], "findings": []}
CR = {"verdict": "changes_requested", "criteria_check": [{"id": "AC1", "met": False}],
      "findings": [{"fingerprint": "fp1"}]}
BLOCKED = {"verdict": "blocked", "criteria_check": [], "findings": []}
TERMINAL = {LotState.DONE, LotState.BLOCKED, LotState.ABORTED}


def test_gate_requires_both_conditions():
    assert gate_passed(GOOD_GATE)
    assert not gate_passed(BAD_GATE)
    assert not gate_passed(EMPTY_GATE), "an empty diff must never pass the gate"
    assert not gate_passed(None)


def test_advance_requires_all_four():
    assert advance(PASS, GOOD_GATE)
    assert not advance(PASS, BAD_GATE)
    assert not advance(UNMET, GOOD_GATE), "unmet criteria must not advance"
    assert not advance(CR, GOOD_GATE)
    assert not advance(None, GOOD_GATE)


def test_empty_criteria_is_not_met():
    """A verdict that declares no criteria has proven nothing."""
    assert not criteria_met({"criteria_check": []})


@pytest.mark.parametrize("attempt", range(1, MAX_ATTEMPTS + 2))
def test_gate_failure_never_advances(attempt):
    n = decide(LotState.GATE, attempt, Ctx(deps_met=True, gate=BAD_GATE))
    assert n.state != LotState.REVIEWING
    assert n.state in (LotState.CODING, LotState.BLOCKED)


@pytest.mark.parametrize("attempt", range(1, MAX_ATTEMPTS + 2))
def test_oscillation_always_reaches_a_human(attempt):
    n = decide(LotState.REVIEWING, attempt,
               Ctx(deps_met=True, verdict=CR, gate=GOOD_GATE,
                   prev_fingerprints=frozenset({"fp1"})))
    assert n.kind == "human" and n.state == LotState.BLOCKED
    assert "oscillation" in n.reason


def test_never_exceeds_max_attempts():
    for state in (LotState.GATE, LotState.REVIEWING, LotState.QUALITY):
        n = decide(state, MAX_ATTEMPTS, Ctx(deps_met=True, verdict=CR, gate=BAD_GATE))
        assert n.kind == "human", f"{state} at max attempts must stop, got {n.kind}"
        assert n.attempt is None or n.attempt <= MAX_ATTEMPTS


def test_attempt_two_resumes_attempt_three_is_cold():
    a2 = decide(LotState.GATE, 1, Ctx(deps_met=True, gate=BAD_GATE))
    assert a2.attempt == 2 and a2.resume_session is True
    a3 = decide(LotState.GATE, 2, Ctx(deps_met=True, gate=BAD_GATE))
    assert a3.attempt == 3 and a3.resume_session is False, \
        "by attempt 3 the session carries a wrong mental model; start cold"


def test_reviewer_is_never_resumed():
    n = decide(LotState.GATE, 1, Ctx(deps_met=True, gate=GOOD_GATE))
    assert n.state == LotState.REVIEWING and n.resume_session is False


def test_pause_and_budget_halt_everything():
    for state in LotState:
        assert decide(state, 1, Ctx(deps_met=True, paused=True)).kind == "noop"
    n = decide(LotState.PENDING, 1, Ctx(deps_met=True, budget_exhausted=True))
    assert n.kind == "human"


def test_every_reachable_path_terminates():
    """Walk the whole space. Any cycle that cannot reach a terminal state is a bug
    that would burn budget unattended."""
    verdicts = [None, PASS, UNMET, CR, BLOCKED]
    gates = [None, GOOD_GATE, BAD_GATE, EMPTY_GATE]
    for v, g, prev in itertools.product(verdicts, gates, [frozenset(), frozenset({"fp1"})]):
        state, attempt, seen = LotState.PENDING, 0, 0
        while state not in TERMINAL:
            seen += 1
            assert seen < 40, f"no termination from v={v} g={g} prev={prev}"
            n = decide(state, attempt, Ctx(deps_met=True, verdict=v, gate=g,
                                           prev_fingerprints=prev))
            if n.kind in ("noop", "wait"):
                break
            assert n.attempt is None or n.attempt <= MAX_ATTEMPTS
            state = n.state or state
            attempt = n.attempt or attempt


def test_disabled_phases_are_unreachable():
    """v0 ships code+review. A phase with no provider must never be dispatched."""
    v0 = frozenset({"code", "review"})
    n = decide(LotState.REVIEWING, 1,
               Ctx(deps_met=True, verdict=PASS, gate=GOOD_GATE, phases=v0))
    assert n.kind == "close" and n.state == LotState.DONE

    full = frozenset({"code", "review", "docs", "quality"})
    n = decide(LotState.REVIEWING, 1,
               Ctx(deps_met=True, verdict=PASS, gate=GOOD_GATE, phases=full))
    assert n.state == LotState.DOCS
    assert decide(LotState.DOCS, 1, Ctx(deps_met=True, phases=full)).state == LotState.QUALITY
    assert decide(LotState.DOCS, 1,
                  Ctx(deps_met=True, phases=frozenset({"docs"}))).kind == "close"


def test_pause_is_reachable_from_plat_status():
    """Ctx.paused was honoured by the FSM and never populated by build_ctx, so
    pausing did nothing at all. Guard the wiring, not just the rule."""
    import inspect
    from plat import runner
    src = inspect.getsource(runner.build_ctx)
    assert "paused=" in src and 'status == "paused"' in src
    assert "db.refresh(plat)" in inspect.getsource(runner.run_lot), \
        "pause set by another process is invisible without a refresh"


def test_branch_name_does_not_double_the_ticket_prefix():
    """DEV-3911 produced dev-dev-3911-<slug>. The convention is dev-<n>-<slug>."""
    import re
    src = __import__("plat.worktree", fromlist=["x"])
    assert 'f"dev-{m.group(2)}-{slug}"' in __import__("inspect").getsource(src.ensure)


def test_anchor_column_fits_more_than_a_ticket_key():
    """A review anchor is review/<repo>@<branch> and blew a varchar(32)."""
    from plat.models import Plat, Finding
    assert Plat.__table__.c.anchor.type.length >= 160
    assert Finding.__table__.c.anchor.type.length >= 160
