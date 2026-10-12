"""M0 behavior checks: false success must never become a delivery.

Providers are simulated; git, supervisor commands, transactions and CLI wiring
are real. Each database case uses its own schema, never the operator's records.
"""
import json
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from plat import adapters, cli, fsm, gates, runner, spec
from plat import db as DB
from plat.adapters.base import AgentResult, spawn
from plat.config import Config
from plat.models import Artifact, Attempt, Decision, Lot, Plat, Transcript, Verdict
from plat.models import Session as ProviderSession


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "plat-test@example.invalid")
    git(path, "config", "user.name", "Plat test")
    (path / "app.txt").write_text("base\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    git(path, "remote", "add", "origin", str(path))
    git(path, "fetch", "-q", "origin")
    git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return git(path, "rev-parse", "HEAD")


def valid_spec(**overrides):
    return {"anchor": "fix_m0-test", "phases": ["code", "review"],
            "lots": [{"key": "a", "repo": "services/a", "gate": {"setup": "true", "test": "true"}}],
            "criteria": [{"id": "AC1", "lot": "a", "statement": "app changes", "verify": "grep -q changed app.txt"}],
            **overrides}


@pytest.fixture
def project(tmp_path):
    repo(tmp_path / "services/a")
    return tmp_path


@pytest.mark.parametrize("overrides,match", [
    ({"anchor": "../escape"}, "anchor"),
    ({"anchor": "x;touch pwn"}, "anchor"),
    ({"slug": "../escape"}, "slug"),
    ({"phases": ["code", "unknown"]}, "unsupported"),
    ({"phases": ["code", "not-a-phase"]}, "unsupported"),
    ({"phases": ["review"]}, "include code"),
    ({"phases": "code"}, "list"),
    ({"budget_usd": float("nan")}, "finite"),
    ({"lots": [None]}, "mapping"),
    ({"criteria": [None]}, "mapping"),
    ({"criteria": [{"id": "AC1", "lot": "ghost", "statement": "x"}]}, "ghost"),
])
def test_rejects_bad_specs(project, overrides, match):
    assert any(match in e for e in spec.validate(valid_spec(**overrides), project, executable=True))


def test_duplicate_ids_and_unsafe_lot_paths(project):
    d = valid_spec()
    d["lots"] *= 2
    d["criteria"] *= 2
    errors = spec.validate(d, project)
    assert any("duplicate lot" in e for e in errors)
    assert any("duplicate criterion" in e for e in errors)
    for changes in ({"key": "../escape"}, {"repo": "../elsewhere"},
                    {"base_ref": "HEAD;touch pwn"}, {"gate": {}},
                    {"depends_on": ["a"]}, {"patience": 0}):
        d = valid_spec()
        d["lots"][0].update(changes)
        assert spec.validate(d, project)


def test_symlink_cannot_escape_the_workspace(project, tmp_path):
    external = project.parent / (project.name + "-outside")
    external.mkdir()
    (external / ".git").mkdir()
    (project / "escape").symlink_to(external, target_is_directory=True)
    d = valid_spec()
    d["lots"][0]["repo"] = "escape"
    assert any("inside the workspace" in e for e in spec.validate(d, project))


def test_explicit_run_scope_and_code_only_evidence(project):
    d = valid_spec(phases=["code"])
    d["criteria"].append({"id": "RUN1", "scope": "run", "verify_in": "a",
                          "statement": "integrates", "verify": "true"})
    assert spec.validate(d, project, executable=True) == []
    d["criteria"][0].pop("verify")
    assert any("without review" in e for e in spec.validate(d, project))


@pytest.mark.parametrize("checks", [[], [{"id": "AC1", "met": True}],
    [{"id": "AC1", "met": True}, {"id": "AC1", "met": True}],
    [{"id": "AC1", "met": True}, {"id": "invented", "met": True}]])
def test_review_cannot_omit_or_invent_acceptance_criteria(checks):
    v = {"verdict": "pass", "criteria_check": checks}
    n = fsm.decide(fsm.LotState.REVIEWING, 1, fsm.Ctx(
        deps_met=True, verdict=v, gate={"exit_code": 0, "diff_files": 1},
        expected_criteria=("AC1", "AC2")))
    assert n.kind != "close"


@pytest.mark.parametrize("verdict", [{"status": "blocked", "open_questions": ["Which tenant?"]},
                                     {"status": "complete", "needs_human": True, "open_questions": ["Which tenant?"]}])
@pytest.mark.parametrize("mode", [fsm.Mode.ATTEMPT, fsm.Mode.CONVERGE])
def test_coder_question_stops_before_any_gate(verdict, mode):
    nxt = fsm.decide(fsm.LotState.CODING, 1, fsm.Ctx(deps_met=True, verdict=verdict, mode=mode))
    assert nxt.kind == "human" and "Which tenant?" in nxt.reason


def test_disabled_review_and_configured_retry_limit():
    gate = {"exit_code": 0, "diff_files": 1}
    assert fsm.decide(fsm.LotState.GATE, 1, fsm.Ctx(
        deps_met=True, gate=gate, phases=frozenset({"code"}))).kind == "close"
    assert fsm.decide(fsm.LotState.GATE, 1, fsm.Ctx(
        deps_met=True, gate={"exit_code": 1}, max_attempts=1)).kind == "human"
    assert fsm.decide(fsm.LotState.GATE, 3, fsm.Ctx(
        deps_met=True, gate={"exit_code": 1}, max_attempts=4)).attempt == 4


def test_converge_review_retries_cannot_exceed_ceiling():
    nxt = fsm.decide(fsm.LotState.REVIEWING, 2, fsm.Ctx(
        deps_met=True, mode=fsm.Mode.CONVERGE, max_iterations=2,
        verdict={"verdict": "changes_requested"}))
    assert nxt.kind == "human"


def test_silent_agent_and_its_stubborn_child_time_out(tmp_path):
    script = ("import subprocess, sys, time, signal; "
              "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              "time.sleep(60)")
    start = time.monotonic()
    rc, _, err, _ = spawn([sys.executable, "-c", script], tmp_path, 0.3)
    assert rc == 124 and "TIMEOUT" in err
    assert time.monotonic() - start < 3


def test_failed_probe_never_counts_its_error_number(tmp_path):
    assert gates.probe(tmp_path, "echo error 99; exit 1") is None


def test_reviewer_fingerprint_catches_changes_to_already_dirty_files(project):
    path = project / "services/a"
    (path / "app.txt").write_text("dirty before")
    before = runner._worktree_fingerprint(path)
    (path / "app.txt").write_text("dirty after")
    assert before != runner._worktree_fingerprint(path)


@pytest.fixture
def case(project, monkeypatch):
    # Only connection failure skips. Schema/behavior errors must fail the test.
    try:
        engine = DB.engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"Postgres unavailable: {exc}")
    schema = "m0_" + uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    isolated = create_engine(engine.url, connect_args={"options": f"-csearch_path={schema}"})
    monkeypatch.setattr(DB, "_engine", isolated)
    monkeypatch.setattr(DB, "_Session", sessionmaker(bind=isolated, expire_on_commit=False))
    DB.init()
    monkeypatch.setenv("PLAT_HOME", str(project / "plat-home"))
    roles = project / "roles.yaml"
    roles.write_text(yaml.safe_dump({"roles": {
        "coder": {"provider": "claude", "model": "default", "resume_session": True, "effort": "medium"},
        "reviewer.correctness": {"provider": "codex", "model": "default"}}}))
    cfg = Config(workspace_root=project, database_url=str(engine.url), broker_url="",
                 roles_path=roles, origin_id="test", notify={"enabled": False})
    monkeypatch.setattr(cli, "load", lambda: cfg)
    calls = []
    def fake(role, prompt_file, cwd, **kwargs):
        calls.append((role, prompt_file.read_text()))
        phase = "code" if role.name == "coder" else "review"
        if phase == "code":
            (cwd / "app.txt").write_text("changed\n" + str(len(calls)))
            git(cwd, "add", "app.txt"); git(cwd, "commit", "-qm", "agent change")
            verdict = {"lot": "a", "phase": "code", "status": "complete", "summary": "changed",
                       "tests": {"cmd": "true", "ran": True},
                       "acceptance_criteria": [{"id": "AC1", "met": True}]}
        else:
            verdict = {"verdict": "pass", "findings": [], "criteria_check": [{"id": "AC1", "met": True}]}
        (prompt_file.parent / f"{phase}.json").write_text(json.dumps(verdict))
        (prompt_file.parent / f"{phase}.md").write_text("test narrative")
        return AgentResult(True, 0, .1, provider=role.provider, session_id=f"sid-{len(calls)}", stdout="test transcript")
    monkeypatch.setattr(adapters, "run", fake)
    obj = SimpleNamespace(cfg=cfg, project=project, calls=calls, fake=fake)
    yield obj
    isolated.dispose()
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))


def plan(case, d=None, commit=True):
    path = case.project / "plat.yaml"
    path.write_text(yaml.safe_dump(d or valid_spec()))
    result = CliRunner().invoke(cli.app, ["plan", str(path), *( ["--commit"] if commit else [])])
    assert result.exit_code == 0, result.output + str(result.exception)
    return result


def rows(model):
    with DB.session() as db:
        return db.scalars(select(model).order_by(model.id)).all()


def start():
    result = CliRunner().invoke(cli.app, ["start", "fix_m0-test"])
    assert result.exit_code == 0, result.output + str(result.exception)
    return result


def test_cli_pipeline_records_real_independent_evidence(case):
    plan(case)
    start()
    p = rows(Plat)[0]
    assert p.status == "delivered" and p.delivered_at and p.closed_at is None
    assert [a.phase for a in rows(Attempt)] == ["code", "gate", "review"]
    gate = rows(Verdict)[1].raw
    assert gate["criteria_check"][0]["met"] is True
    assert gate["criteria_check"][0]["cmd"] == "grep -q changed app.txt"
    assert rows(Attempt)[0].role_binding["effort"] == "medium"
    assert len(rows(ProviderSession)) == 2
    assert rows(Transcript) and rows(Artifact)
    assert {d.actor_detail for d in rows(Decision)} >= {
        "cli.plan", "cli.start", "runner.dispatch", "gates.start", "gates.run", "runner.complete", "fsm.deliver_plat"}
    result = CliRunner().invoke(cli.app, ["close", "fix_m0-test", "--note", "merged and shipped to local pilot"])
    assert result.exit_code == 0, result.output
    assert rows(Plat)[0].status == "closed"


def test_cli_code_only_still_verifies_every_criterion(case):
    plan(case, valid_spec(phases=["code"]))
    start()
    assert [a.phase for a in rows(Attempt)] == ["code", "gate"]
    assert rows(Plat)[0].status == "delivered"


@pytest.mark.parametrize("failure", ["provider", "exit", "permission", "question", "missing_criterion"])
def test_false_success_never_reaches_the_gate(case, monkeypatch, failure):
    def fake(role, prompt_file, cwd, **kwargs):
        result = case.fake(role, prompt_file, cwd, **kwargs)
        if failure == "provider":
            result.ok, result.error = False, "turn.failed"
        elif failure == "exit":
            result.exit_code = 42
        elif failure == "permission":
            result.permission_denials = ["Bash"]
        else:
            path = prompt_file.parent / "code.json"
            verdict = json.loads(path.read_text())
            if failure == "question":
                verdict.update(status="blocked", open_questions=["Which tenant?"], acceptance_criteria=[])
            else:
                verdict["acceptance_criteria"] = []
            path.write_text(json.dumps(verdict))
        return result
    monkeypatch.setattr(adapters, "run", fake)
    plan(case)
    result = start()
    assert rows(Lot)[0].state == "BLOCKED"
    assert [a.phase for a in rows(Attempt)] == ["code"]
    assert rows(Plat)[0].delivered_at is None
    assert rows(Transcript)
    if failure == "question":
        assert "Which tenant?" in result.output
    else:
        assert any("BLOCKED" in d.decision for d in rows(Decision))


def test_independent_verify_failure_retries_then_blocks_at_configured_limit(case, monkeypatch):
    monkeypatch.setattr(cli, "load", lambda: replace(case.cfg, max_attempts=2))
    d = valid_spec()
    d["criteria"][0]["verify"] = "echo unmet; exit 1"
    plan(case, d)
    start()
    assert [a.phase for a in rows(Attempt)] == ["code", "gate", "code", "gate"]
    assert rows(Lot)[0].state == "BLOCKED"
    assert rows(Plat)[0].delivered_at is None
    assert case.calls[1][0].session_id == "sid-1"
    assert "unmet" in case.calls[1][1]


def test_run_criteria_are_checked_after_lots_finish(case):
    d = valid_spec()
    d["criteria"].append({"id": "RUN1", "scope": "run", "verify_in": "a",
                          "statement": "deployment contract", "verify": "echo missing; exit 1"})
    plan(case, d)
    start()
    assert rows(Lot)[0].state == "DONE"
    assert rows(Plat)[0].status == "blocked"
    assert rows(Plat)[0].delivered_at is None
    assert all("RUN1" not in prompt for _, prompt in case.calls)
    assert rows(Decision)[-1].inputs["criteria_check"][0]["met"] is False


def test_aborted_required_lot_is_incomplete_and_cannot_be_closed(case):
    plan(case)
    result = CliRunner().invoke(cli.app, ["abort", "fix_m0-test", "a", "--yes", "--note", "stopped"])
    assert result.exit_code == 0, result.output
    start()
    assert rows(Plat)[0].status == "incomplete" and rows(Plat)[0].delivered_at is None
    result = CliRunner().invoke(cli.app, ["close", "fix_m0-test", "--note", "claim shipped"])
    assert result.exit_code == 1
    assert rows(Plat)[0].closed_at is None


def test_replan_preserves_base_and_records_revision_before_work_starts(case):
    plan(case)
    old = rows(Lot)[0]
    path = Path(old.worktree_path)
    (path / "app.txt").write_text("manual change")
    git(path, "add", "."); git(path, "commit", "-qm", "manual")
    d = valid_spec(title="clarified plan")
    plan(case, d)
    assert rows(Lot)[0].base_ref == old.base_ref != git(path, "rev-parse", "HEAD")
    assert rows(Decision)[-1].decision == "unstarted plan revised"
    start()
    result = CliRunner().invoke(cli.app, ["plan", str(case.project / "plat.yaml"), "--commit"])
    assert result.exit_code == 1 and "immutable" in result.output
    assert rows(Lot)[0].base_ref == old.base_ref


def test_dry_run_with_tracker_never_commits_rows(case, monkeypatch):
    from plat import tracker
    monkeypatch.setattr(tracker, "configured", lambda cfg: True)
    monkeypatch.setattr(tracker, "lookup", lambda *a: {"key": "fix_m0-test", "status": "In Progress", "summary": "existing"})
    result = plan(case, commit=False)
    assert "already exists" in result.output
    assert rows(Plat) == [] and rows(Decision) == []


def test_invalid_spec_has_no_side_effects(case, monkeypatch):
    monkeypatch.setattr(DB, "session", lambda: pytest.fail("opened database for invalid spec"))
    d = valid_spec(phases=["code", "unknown"])
    path = case.project / "bad.yaml"; path.write_text(yaml.safe_dump(d))
    result = CliRunner().invoke(cli.app, ["plan", str(path), "--commit"])
    assert result.exit_code == 1 and "unsupported" in result.output
    assert not case.cfg.worktrees_root.exists()


def test_interrupted_attempt_requires_reconciliation_before_gating(case):
    plan(case)
    with DB.session() as db:
        lot = db.scalars(select(Lot)).one()
        lot.state, lot.attempt = "CODING", 1
        db.add(Attempt(lot_id=lot.id, phase="code", n=1, provider="claude", model="default"))
    start()
    assert rows(Lot)[0].state == "BLOCKED"
    assert len(rows(Attempt)) == 1
    assert rows(Attempt)[0].finished_at is not None
    result = CliRunner().invoke(cli.app, ["reopen", "fix_m0-test", "a", "--note", "Use the demo tenant"])
    assert result.exit_code == 0, result.output
    start()
    assert "Use the demo tenant" in case.calls[0][1]
    assert rows(Attempt)[1].n == 2
    assert rows(Plat)[0].status == "delivered"


def test_provider_change_never_resumes_the_previous_provider_session(case):
    plan(case)
    with DB.session() as db:
        lot = db.scalars(select(Lot)).one()
        a = Attempt(lot_id=lot.id, phase="code", n=1, provider="claude", model="default")
        db.add(a); db.flush()
        db.add(ProviderSession(attempt_id=a.id, provider="claude", provider_session_id="claude-only"))
        db.flush()
        assert runner._prior_session(db, lot, "codex") is None
        assert runner._prior_session(db, lot, "claude") == "claude-only"


def test_stale_threshold_and_retry_limit_are_used_by_sql(case):
    plan(case)
    with DB.session() as db:
        lot = db.scalars(select(Lot)).one()
        lot.state, lot.attempt = "CODING", 2
        lot.config = {**lot.config, "max_attempts": 2, "stale_after_s": 1}
        lot.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db.add(Attempt(lot_id=lot.id, phase="code", n=2, provider="claude", model="default"))
    with DB.engine().connect() as conn:
        row = conn.execute(text("SELECT stale, last_chance FROM v_live_lots")).one()
        assert row.stale and row.last_chance


def test_per_lot_criteria_and_reopened_dependency_invalidate_delivery(case, monkeypatch):
    repo(case.project / "services/b")
    d = valid_spec()
    d["lots"].append({"key": "b", "repo": "services/b", "depends_on": ["a"],
                      "gate": {"setup": "true", "test": "true"}})
    d["criteria"].append({"id": "AC2", "lot": "b", "statement": "second lot requirement",
                          "verify": "grep -q changed app.txt"})
    def fake(role, prompt_file, cwd, **kwargs):
        result = case.fake(role, prompt_file, cwd, **kwargs)
        phase = "code" if role.name == "coder" else "review"
        key = "acceptance_criteria" if phase == "code" else "criteria_check"
        path = prompt_file.parent / f"{phase}.json"
        v = json.loads(path.read_text())
        v[key] = [{"id": "AC1" if cwd.name == "a" else "AC2", "met": True}]
        if phase == "code":
            v["lot"] = cwd.name
        path.write_text(json.dumps(v))
        return result
    monkeypatch.setattr(adapters, "run", fake)
    plan(case, d)
    start()
    assert "second lot requirement" not in case.calls[0][1]
    assert "app changes" not in case.calls[2][1]
    assert rows(Plat)[0].status == "delivered"
    result = CliRunner().invoke(cli.app, ["reopen", "fix_m0-test", "a", "--note", "Contract changed"])
    assert result.exit_code == 0, result.output
    assert all(l.state == "PENDING" for l in rows(Lot))
    assert rows(Plat)[0].delivered_at is None
    start()
    assert rows(Plat)[0].status == "delivered"
    assert all(l.attempt == 2 for l in rows(Lot))


def test_a_living_orphan_is_left_alone_until_it_exits(case):
    plan(case)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        with DB.session() as db:
            lot = db.scalars(select(Lot)).one()
            lot.state, lot.attempt = "CODING", 1
            db.add(Attempt(lot_id=lot.id, phase="code", n=1, provider="claude", model="default",
                           pid=process.pid, role_binding={"cmd": sys.executable}))
        result = start()
        assert "still running" in result.output
        assert rows(Lot)[0].state == "CODING"
        assert len(rows(Attempt)) == 1
        assert process.poll() is None
    finally:
        process.terminate(); process.wait()
    start()
    assert rows(Lot)[0].state == "BLOCKED"
    assert rows(Attempt)[0].pid is None


@pytest.mark.parametrize("failure", [None, "provider", "mutation", "gate"])
def test_standalone_review_uses_execution_guards_and_preserves_evidence(case, monkeypatch, failure):
    from plat import review
    path = case.project / "services/a"
    (path / "app.txt").write_text("changed")
    git(path, "add", "."); git(path, "commit", "-qm", "change")
    def fake(role, prompt_file, cwd, **kwargs):
        result = case.fake(role, prompt_file, cwd, **kwargs)
        vf = prompt_file.parent / "review.json"
        v = json.loads(vf.read_text()); v["criteria_check"] = []
        vf.write_text(json.dumps(v))
        if failure == "provider":
            result.ok = False
        if failure == "mutation":
            (cwd / "app.txt").write_text("reviewer changed the code")
        return result
    monkeypatch.setattr(adapters, "run", fake)
    with DB.session() as db:
        if failure in ("provider", "mutation"):
            with pytest.raises(RuntimeError):
                review.run(db, case.cfg, repo=path, base="HEAD~1", role_name="reviewer.correctness", test_cmd=None)
        else:
            verdict, _ = review.run(db, case.cfg, repo=path, base="HEAD~1", role_name="reviewer.correctness",
                                    test_cmd="false" if failure == "gate" else None)
            assert verdict == ("blocked" if failure == "gate" else "pass")
    assert rows(Transcript)
    assert rows(ProviderSession)
    assert rows(Plat)[0].closed_at is None
    if failure:
        assert rows(Lot)[0].state == "BLOCKED"
    else:
        assert "NO independent verification was run" in case.calls[0][1]


def test_codex_needs_a_completed_turn_even_with_zero_exit(tmp_path, monkeypatch):
    from plat.adapters import codex
    from plat.adapters.base import Role
    p = tmp_path / "prompt.md"; p.write_text("hello")
    monkeypatch.setattr(codex, "spawn", lambda *a, **k: (0, '{"type":"thread.started","thread_id":"id"}', "", .1))
    result = codex.run(Role("coder", "codex", "default"), p, tmp_path)
    assert not result.ok and "no completed turn" in result.error


def test_decision_and_transition_roll_back_together(case):
    from plat.decisions import record
    plan(case)
    with pytest.raises(RuntimeError):
        with DB.session() as db:
            lot = db.scalars(select(Lot)).one()
            def fail():
                lot.state = "DONE"
                db.flush()
                raise RuntimeError("write failed")
            record(db, plat_id=lot.plat_id, lot_id=lot.id, actor="system", actor_detail="test",
                   kind="transition", decision="must roll back", inputs={}, apply=fail)
    assert rows(Lot)[0].state == "PENDING"
    assert all(d.decision != "must roll back" for d in rows(Decision))


def test_environment_phase_list_is_not_split_into_characters(tmp_path, monkeypatch):
    from plat.config import load
    monkeypatch.setenv("PLAT_HOME", str(tmp_path))
    monkeypatch.setenv("PLAT_PHASES", "code,review")
    assert load().phases == ["code", "review"]


def test_codex_coder_does_not_escalate_to_a_claude_model():
    from plat.roles import bind
    from plat.setup import build_roles
    roles = build_roles({}, "codex", "default", "claude", "sonnet", "balanced", ["code", "review"])
    assert bind(roles, "coder", 3).model == "default"


async def test_tui_reopen_requires_and_records_the_human_answer(case):
    from textual.widgets import Input

    from plat.tui import PlatTop, ReopenReason
    plan(case)
    with DB.session() as db:
        lot = db.scalars(select(Lot)).one()
        lot.state = "BLOCKED"
        lot_id = lot.id
    app = PlatTop()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.sel = lot_id
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, ReopenReason)
        assert rows(Lot)[0].state == "BLOCKED"
        app.screen.query_one(Input).value = "Use test tenant"
        await pilot.press("enter")
        await pilot.pause()
        assert rows(Lot)[0].state == "PENDING"
        assert rows(Lot)[0].human_note == "Use test tenant"


def test_finished_attempt_clears_a_pid_written_by_another_connection(case, monkeypatch):
    def fake(role, prompt_file, cwd, **kwargs):
        kwargs["on_pid"](123456, "test-provider")
        return case.fake(role, prompt_file, cwd, **kwargs)
    monkeypatch.setattr(adapters, "run", fake)
    plan(case)
    start()
    assert all(a.pid is None and a.finished_at is not None for a in rows(Attempt))
    assert rows(Attempt)[0].role_binding["cmd"] == "test-provider"


def test_missing_narrative_blocks_even_when_json_is_valid(case, monkeypatch):
    def fake(role, prompt_file, cwd, **kwargs):
        result = case.fake(role, prompt_file, cwd, **kwargs)
        (prompt_file.parent / "code.md").unlink()
        return result
    monkeypatch.setattr(adapters, "run", fake)
    plan(case)
    start()
    assert rows(Lot)[0].state == "BLOCKED"
    assert len(rows(Attempt)) == 1
    assert any(a.name == "code.json" for a in rows(Artifact))


def test_prompt_contract_example_is_valid_json():
    from plat.prompts import CODE_SHAPE
    assert json.loads(CODE_SHAPE)["handoff"][0]["detail"].startswith('returns {"version"')


def test_gate_is_unfinished_and_tracks_its_process_until_criteria_finish(case, monkeypatch):
    original = gates.verify
    observed = []
    def verify(worktree, criteria, **kwargs):
        with DB.session() as db:
            gate_attempt = db.scalars(select(Attempt).where(Attempt.phase == "gate")).one()
            observed.append((gate_attempt.finished_at, gate_attempt.pid))
        return original(worktree, criteria, **kwargs)
    monkeypatch.setattr(gates, "verify", verify)
    plan(case)
    start()
    assert len(observed) == 1 and observed[0][0] is None and observed[0][1] is not None
    assert all(a.pid is None and a.finished_at is not None for a in rows(Attempt))


def test_standalone_review_does_not_dispatch_after_gate_is_aborted(case, monkeypatch):
    from plat import review
    path = case.project / "services/a"
    (path / "app.txt").write_text("changed")
    git(path, "add", "."); git(path, "commit", "-qm", "change")
    original = gates.run
    def cancelled(*args, **kwargs):
        result = original(*args, **kwargs)
        abort = CliRunner().invoke(cli.app, ["abort", "review/a@main", "--yes", "--note", "stop"])
        assert abort.exit_code == 0, abort.output
        return result
    monkeypatch.setattr(gates, "run", cancelled)
    with DB.session() as db:
        with pytest.raises(RuntimeError, match="aborted"):
            review.run(db, case.cfg, repo=path, base="HEAD~1", role_name="reviewer.correctness", test_cmd="true")
    assert not case.calls
    assert rows(Plat)[0].status == "aborted"
    assert rows(Lot)[0].state == "ABORTED"
