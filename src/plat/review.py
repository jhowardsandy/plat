"""`plat review` — cross-model review of a branch someone already wrote.

This is the cheapest useful thing Plat does. No plan, no worktree, no coder: one
reviewer, one diff, one verdict. On both of Plat's first real runs the coder
produced something plausible that passed the tests and the REVIEWER caught the
actual defect, so the review is worth having on its own.

Deliberately NOT a state machine. A review is one shot; iterate-until-clean is
what a full plat is for.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text

from .config import Config, home
from .models import Attempt, Finding, Lot, Plat, Verdict

SEVERITY = {"low": 0, "medium": 1, "high": 2}


def _git(args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()


def repo_root(start: Path) -> Path:
    r = _git(["rev-parse", "--show-toplevel"], start)
    if not r:
        raise RuntimeError(f"{start} is not inside a git repository")
    return Path(r)


def default_base(repo: Path) -> str:
    """The merge-base with the default branch — what a reviewer should see.

    Diffing against the branch TIP would hide anything that landed on the base
    since you branched; diffing against the base BRANCH would show it as yours.
    """
    head = _git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo)
    for cand in ([head] if head else []) + ["origin/main", "origin/master", "main", "master"]:
        if _git(["rev-parse", "--verify", "--quiet", cand], repo):
            mb = _git(["merge-base", "HEAD", cand], repo)
            if mb:
                return mb
    return "HEAD~1"


def prior_art(db, repo_name: str, limit: int = 8) -> list[dict]:
    rows = db.execute(text(
        "SELECT f.fingerprint, f.severity, f.claim, f.dismissed_reason, "
        "       f.resolved_in_attempt_id "
        "FROM findings f WHERE f.repo = :r ORDER BY f.id DESC LIMIT :n"),
        {"r": repo_name, "n": limit}).mappings().all()
    return [dict(r) for r in rows]


def run(db, cfg: Config, *, repo: Path, base: str | None, role_name: str,
        test_cmd: str | None, log=print) -> tuple[str, list[Finding]]:
    repo = repo_root(repo)
    base = base or default_base(repo)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    repo_name = repo.name
    changed = [x for x in _git(["diff", "--name-only", f"{base}..HEAD"], repo).splitlines() if x]
    if not changed:
        raise RuntimeError(f"no changes between {base[:12]} and HEAD — nothing to review")
    log(f"  {repo_name} @ {branch}   {len(changed)} file(s) vs {base[:12]}")

    now = datetime.now(timezone.utc)
    anchor = f"review/{repo_name}@{branch}"[:160]
    from . import fsm, runner
    from .decisions import record
    p = Plat(anchor=anchor, title=branch, kind="review", status="running",
             tickets=[], related=[], budget_usd=0.0, origin_id=cfg.origin_id, started_at=now)
    db.add(p); db.flush()
    lot = Lot(plat_id=p.id, key=repo_name, repo=repo_name, worktree_path=str(repo),
              state="PENDING", attempt=1, base_ref=base, depends_on=[],
              config={"gate": {"test": test_cmd}} if test_cmd else {})
    record(db, plat_id=p.id, actor="human", actor_detail="cli.review", kind="transition",
           decision="standalone review requested", inputs={"base": base, "head": _git(["rev-parse", "HEAD"], repo)},
           apply=lambda: db.add(lot))
    db.commit()
    gate = {"cmd": "(not run)", "exit_code": None, "tests_passed": 0,
            "tests_failed": 0, "diff_files": len(changed),
            "note": "NO independent verification was run. Do not assume the tests pass."}
    if test_cmd:
        runner._run_gate(db, cfg, p, lot, None, log)
        gate = runner._verdict_of(db, runner._latest(db, lot, phases=["gate"]))
    else:
        a = Attempt(lot_id=lot.id, phase="gate", n=1, provider="supervisor", model="local",
                    exit_code=None, finished_at=datetime.now(timezone.utc))
        def evidence():
            db.add(a); db.flush()
            db.add(Verdict(attempt_id=a.id, raw=gate))
        record(db, plat_id=p.id, lot_id=lot.id, actor="system", actor_detail="review.gate",
               kind="observation", decision="standalone verification not run", inputs=gate, apply=evidence)
        db.commit()
    db.refresh(lot)
    db.refresh(p)
    if lot.state == "ABORTED" or p.status == "aborted":
        raise RuntimeError("standalone review was aborted")
    nxt = fsm.Next("agent", fsm.LotState.REVIEWING, phase="review", role=role_name, attempt=1,
                   reason="standalone review requested")
    try:
        runner._run_agent(db, cfg, p, lot, cfg.roles(), nxt, None, log,
                          out_dir=home() / "reviews" / str(p.id))
    except runner.Halt as exc:
        db.refresh(p)
        record(db, plat_id=p.id, actor="system", actor_detail="review.finish", kind="transition",
               decision="review BLOCKED", rationale=str(exc), inputs={},
               apply=lambda: setattr(p, "status", "blocked") if p.status != "aborted" else None)
        db.commit()
        raise RuntimeError(str(exc)) from exc
    review_attempt = runner._latest(db, lot, phases=["review"])
    verdict = runner._verdict_of(db, review_attempt)["verdict"]
    if test_cmd and gate["exit_code"] != 0:
        verdict = "blocked"
    record(db, plat_id=p.id, lot_id=lot.id, actor="system", actor_detail="review.finish",
           kind="transition", decision=f"standalone review {verdict}", inputs={"gate": gate},
           apply=lambda: (setattr(lot, "state", "BLOCKED" if verdict == "blocked" else "DONE"),
                          setattr(p, "status", "blocked" if verdict == "blocked" else "reviewed")))
    db.commit()
    found = db.scalars(select(Finding).where(Finding.attempt_id == review_attempt.id)).all()
    return verdict, list(found)
