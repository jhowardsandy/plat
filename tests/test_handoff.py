"""Dependent lots: ordering that is real, and content that crosses the boundary.

`depends_on` used to buy neither. The FSM had a `deps_met` branch, the guide
described the guarantee, and `build_ctx` passed `deps_met=True` unconditionally --
so the ordering held only because `plat start` iterated in insertion order, which
is a coincidence and not a guarantee. And even in the right order, lot B was given
its own description plus the plat map AS WRITTEN BEFORE A RAN, so a contract A
settled at runtime was invisible to the agent that had to consume it.
"""
import inspect
import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session as DbSession

from plat import prompts as P
from plat import runner
from plat.fsm import Ctx, LotState, decide
from plat.models import Attempt, Lot, Plat, Verdict

# --- ordering -------------------------------------------------------------

class FakeLot:
    def __init__(self, key, depends_on=()):
        self.key, self.depends_on = key, list(depends_on)
        self.repo, self.attempt = f"core/{key}", 1

    def __repr__(self):
        return self.key


def keys(lots):
    return [l.key for l in lots]


def test_a_dependency_runs_before_its_dependent():
    lots = [FakeLot("web", ["api"]), FakeLot("api")]
    assert keys(runner.ordered_lots(lots)) == ["api", "web"]


def test_a_diamond_puts_both_middles_before_the_join():
    lots = [FakeLot("join", ["left", "right"]), FakeLot("left", ["base"]),
            FakeLot("right", ["base"]), FakeLot("base")]
    out = keys(runner.ordered_lots(lots))
    assert out[0] == "base" and out[-1] == "join"
    assert set(out[1:3]) == {"left", "right"}


def test_independent_lots_keep_their_insertion_order():
    """Reordering work nobody asked to reorder makes runs harder to compare."""
    lots = [FakeLot(k) for k in ("c", "a", "b")]
    assert keys(runner.ordered_lots(lots)) == ["c", "a", "b"]


def test_a_lot_in_a_cycle_is_never_dropped_from_the_run():
    """A lot that silently vanishes from a run is worse than one that waits and
    says why. `plat draft` rejects cycles; a hand-edited spec can still carry one."""
    lots = [FakeLot("a", ["b"]), FakeLot("b", ["a"]), FakeLot("c")]
    out = keys(runner.ordered_lots(lots))
    assert set(out) == {"a", "b", "c"}
    assert out[0] == "c"          # what can run, runs first


def test_an_edge_to_a_lot_outside_this_plat_does_not_stall_the_sort():
    lots = [FakeLot("a", ["not-in-this-plat"])]
    assert keys(runner.ordered_lots(lots)) == ["a"]


def test_start_runs_lots_in_dependency_order():
    """The ordering guarantee has to be applied by the caller that dispatches."""
    from plat import cli
    src = inspect.getsource(cli.start)
    assert "runner.ordered_lots(" in src


# --- the FSM names what it is waiting for ---------------------------------

def test_an_unmet_dependency_names_the_lot_it_is_waiting_on():
    nxt = decide(LotState.PENDING, 1,
                 Ctx(deps_met=False, deps_pending=(("api", "CODING"),)))
    assert nxt.kind == "wait"
    assert "api" in nxt.reason and "CODING" in nxt.reason


def test_a_dependency_that_will_never_finish_reads_differently():
    """Waiting on a lot that is progressing is patience. Waiting on a BLOCKED one
    is a deadlock, and the person reading the log is the only one who can break it."""
    nxt = decide(LotState.PENDING, 1,
                 Ctx(deps_met=False, deps_pending=(("api", "BLOCKED"),)))
    assert "will not finish" in nxt.reason
    assert "plat reopen" in nxt.reason


def test_with_no_detail_the_old_wording_still_appears():
    nxt = decide(LotState.PENDING, 1, Ctx(deps_met=False))
    assert nxt.kind == "wait" and nxt.reason


def test_the_fsm_stays_pure():
    """deps_pending is data assembled by the caller, like everything else in Ctx."""
    src = inspect.getsource(runner.deps_pending)
    assert "select(" in src                      # the query lives in the runner
    fsm_src = Path("src/plat/fsm.py").read_text()
    assert "import sqlalchemy" not in fsm_src and "from .models" not in fsm_src


# --- the handoff block ----------------------------------------------------

ROWS = [{"lot": "api", "repo": "core/api", "kind": "endpoint",
         "name": "GET /api/health/ping",
         "detail": 'returns {"version": "<semver>"}'},
        {"lot": "api", "repo": "core/api", "kind": "env",
         "name": "PING_ENABLED", "detail": "must be true or the route 404s"}]


def test_the_block_carries_every_reported_fact():
    out = P.handoff_block(ROWS)
    for r in ROWS:
        assert r["name"] in out and r["detail"] in out and r["kind"] in out
    assert "api" in out and "core/api" in out


def test_the_block_groups_by_producing_lot():
    out = P.handoff_block(ROWS + [{"lot": "db", "repo": "core/db", "kind": "type",
                                   "name": "Ping", "detail": "new table"}])
    assert out.count("### `api`") == 1
    assert "### `db`" in out


def test_no_upstream_means_no_section_at_all():
    """An empty heading tells the agent there is a thing it did not get."""
    assert P.handoff_block([]) == ""
    assert P.handoff_block(None) == ""


def test_the_block_forbids_resolving_a_contradiction_silently():
    """The one failure mode nobody downstream can see: B quietly picks between
    what the plan said and what A built, and nothing records that they differed."""
    out = P.handoff_block(ROWS).lower()
    assert "open_questions" in out
    assert "contradict" in out


def test_both_the_coder_and_the_reviewer_are_given_it():
    """review.md already asks for 'contract drift against other repos' — it was
    asking the reviewer to check drift against something it was never shown."""
    for name in ("code.md", "review.md"):
        assert "{upstream}" in (Path("src/plat/prompts") / name).read_text(), name
    for fn in (P.build_code, P.build_review):
        assert "upstream" in inspect.signature(fn).parameters, fn.__name__


GATE = {"cmd": "pytest", "exit_code": 0, "tests_passed": 1, "tests_failed": 0,
        "diff_files": 2}


@pytest.mark.parametrize("which", ["code", "review"])
def test_the_placeholder_is_always_substituted(which):
    """A literal {upstream} reaching an agent is a prompt bug that reads to the
    agent as an instruction about scope."""
    lot = FakeLot("web", ["api"])
    plat = type("P", (), {"anchor": "T-1"})()
    if which == "code":
        out = P.build_code(lot, plat, Path("/tmp"), "b", "HEAD", "map", "desc", [],
                           [], "true", Path("/tmp"))
    else:
        out = P.build_review(lot, plat, Path("/tmp"), "HEAD", "desc", [], GATE,
                             Path("/tmp"))
    import re
    # placeholders are {lower_snake}; the JSON samples in the prompt are not
    left = set(re.findall(r"\{[a-z_]+\}", out))
    assert not left, f"placeholders reached the agent verbatim: {sorted(left)}"


# --- the schema the agent writes it through ------------------------------

def test_handoff_is_part_of_the_code_contract():
    s = json.loads(Path("src/plat/schemas/code.schema.json").read_text())
    h = s["properties"]["handoff"]
    assert h["items"]["required"] == ["kind", "name", "detail"]
    assert "endpoint" in h["items"]["properties"]["kind"]["enum"]


def test_the_shape_shown_to_the_agent_includes_it():
    """The contract prompt inlines the concrete JSON. A field the schema accepts
    and the prompt never shows is a field no agent fills in."""
    assert "handoff" in P.CODE_SHAPE
    assert "handoff" in (Path("src/plat/prompts/code.md")).read_text()


def test_a_handoff_entry_missing_a_field_fails_ingest():
    from plat import ingest
    v = ingest._validator("code")
    bad = {"lot": "a", "phase": "code", "status": "complete", "summary": "s",
           "tests": {"cmd": "t", "ran": True}, "acceptance_criteria": [],
           "handoff": [{"kind": "endpoint", "name": "GET /x"}]}
    assert list(v.iter_errors(bad)), "a handoff with no detail is not a handoff"


# --- against the real database -------------------------------------------

@pytest.fixture
def db():
    """A real session, rolled back. These behaviours are queries; faking the
    query is testing the fake."""
    from plat.db import engine
    try:
        conn = engine().connect()
    except Exception as e:
        pytest.skip(f"no database: {e}")
    tx = conn.begin()
    s = DbSession(bind=conn)
    yield s
    s.close()
    tx.rollback()
    conn.close()


def _plat(db, lots):
    p = Plat(anchor="T-HANDOFF", kind="plat", status="running", budget_usd=1,
             origin_id="test")
    db.add(p); db.flush()
    made = {}
    for key, deps, state in lots:
        L = Lot(plat_id=p.id, key=key, repo=f"core/{key}", state=state, attempt=1,
                worktree_path="/tmp", depends_on=list(deps), config={})
        db.add(L); made[key] = L
    db.flush()
    return p, made


def test_a_lot_waits_until_its_dependency_is_done(db):
    p, lots = _plat(db, [("api", (), "CODING"), ("web", ("api",), "PENDING")])
    assert runner.deps_pending(db, p, lots["web"]) == (("api", "CODING"),)
    ctx = runner.build_ctx(db, p, lots["web"])
    assert ctx.deps_met is False
    assert decide(LotState.PENDING, 1, ctx).kind == "wait"

    lots["api"].state = "DONE"
    db.flush()
    assert runner.deps_pending(db, p, lots["web"]) == ()
    assert runner.build_ctx(db, p, lots["web"]).deps_met is True


def test_a_lot_with_no_edges_never_waits(db):
    p, lots = _plat(db, [("solo", (), "PENDING")])
    assert runner.build_ctx(db, p, lots["solo"]).deps_met is True


def test_a_dependency_naming_a_lot_that_does_not_exist_is_reported(db):
    """draft.validate catches this; a hand-edited plat can still reach the runner,
    and 'waiting on ghost [MISSING]' beats waiting forever with no reason."""
    p, lots = _plat(db, [("web", ("ghost",), "PENDING")])
    assert runner.deps_pending(db, p, lots["web"]) == (("ghost", "MISSING"),)


def test_the_contract_crosses_from_one_lot_to_the_next(db):
    """End to end: api reports what it built, web is handed it."""
    p, lots = _plat(db, [("api", (), "DONE"), ("web", ("api",), "PENDING")])
    a = Attempt(lot_id=lots["api"].id, phase="code", n=1,
                provider="claude", model="sonnet")
    db.add(a); db.flush()
    db.add(Verdict(attempt_id=a.id, status="complete", raw={
        "handoff": [{"kind": "endpoint", "name": "GET /api/health/ping",
                     "detail": 'returns {"version": "<semver>"}'}]}))
    db.flush()

    rows = runner.upstream_handoff(db, p, lots["web"])
    assert len(rows) == 1
    assert rows[0]["lot"] == "api" and rows[0]["repo"] == "core/api"
    assert "GET /api/health/ping" in P.handoff_block(rows)
    # and the other direction carries nothing
    assert runner.upstream_handoff(db, p, lots["api"]) == []


def test_the_latest_attempt_is_the_one_that_counts(db):
    """A lot that went round twice: the second pass is what is in the tree."""
    p, lots = _plat(db, [("api", (), "DONE"), ("web", ("api",), "PENDING")])
    for n, name in ((1, "GET /v1/ping"), (2, "GET /v2/ping")):
        a = Attempt(lot_id=lots["api"].id, phase="code", n=n,
                    provider="claude", model="sonnet")
        db.add(a); db.flush()
        db.add(Verdict(attempt_id=a.id, status="complete", raw={
            "handoff": [{"kind": "endpoint", "name": name, "detail": "d"}]}))
    db.flush()
    rows = runner.upstream_handoff(db, p, lots["web"])
    assert [r["name"] for r in rows] == ["GET /v2/ping"]


def test_a_verdict_written_before_handoff_existed_is_not_an_error(db):
    """Every code verdict in the database today predates this field."""
    p, lots = _plat(db, [("api", (), "DONE"), ("web", ("api",), "PENDING")])
    a = Attempt(lot_id=lots["api"].id, phase="code", n=1,
                provider="claude", model="sonnet")
    db.add(a); db.flush()
    db.add(Verdict(attempt_id=a.id, status="complete", raw={"summary": "old"}))
    db.flush()
    assert runner.upstream_handoff(db, p, lots["web"]) == []


def test_an_empty_handoff_on_a_lot_with_dependents_is_called_out():
    """Otherwise the feature can go unused forever and look like it is working."""
    src = inspect.getsource(runner._run_agent)
    assert 'not verdict.get("handoff")' in src
    assert "depend(s) on this lot" in src
