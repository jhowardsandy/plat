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

from . import adapters, gates, ingest, prompts as P, roles as R
from .config import Config, home
from .models import Plat, Lot, Attempt, Verdict, Finding

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
    p = Plat(anchor=anchor, title=branch, kind="review",
             status="closed", tickets=[], related=[], budget_usd=0.0,
             origin_id=cfg.origin_id, started_at=now, closed_at=now)
    db.add(p); db.flush()
    lot = Lot(plat_id=p.id, key=repo_name, repo=repo_name, worktree_path=str(repo),
              state="REVIEWING", attempt=1, base_ref=base, depends_on=[], config={})
    db.add(lot); db.flush()

    gate = None
    if test_cmd:
        log(f"  gate: {test_cmd}")
        g = gates.run(repo, test_cmd, base)
        gate = g.as_dict()
        log(f"    {g.tests_passed} passed, {g.tests_failed} failed")
    else:
        # The review prompt tells the reviewer the tests were verified
        # independently. With no gate that is FALSE, and a reviewer who believes
        # it will skip exactly what it should be checking.
        gate = {"cmd": "(not run)", "exit_code": None, "tests_passed": 0,
                "tests_failed": 0, "diff_files": len(changed),
                "note": "NO independent verification was run. Do not assume the "
                        "tests pass; if correctness depends on them, say so."}

    out = home() / "reviews" / f"{p.id}"
    out.mkdir(parents=True, exist_ok=True)
    art = prior_art(db, repo_name)
    if art:
        log(f"  prior art: {len(art)} earlier finding(s) in this repo")

    prompt = P.build_review(lot, p, repo, base, "", [], gate, out, prior=art)
    pf = out / "prompt.md"; pf.write_text(prompt)

    role = R.bind(cfg.roles(), role_name, 1)
    a = Attempt(lot_id=lot.id, phase="review", n=1, provider=role.provider,
                model=role.model, role_binding={"role": role_name, "standalone": True})
    db.add(a); db.commit()
    log(f"  review: {role.provider}/{role.model} ...")

    res = adapters.run(role, pf, repo)
    a.exit_code, a.cost_usd, a.cost_estimated = res.exit_code, res.cost_usd, res.cost_estimated
    a.tokens_in, a.tokens_out = res.tokens_in, res.tokens_out
    a.finished_at = datetime.now(timezone.utc)
    a.permission_denials = len(res.permission_denials)
    log(f"    exit={res.exit_code} {res.duration_s:.0f}s "
        f"${res.cost_usd:.2f}{'~' if res.cost_estimated else ''}")

    verdict = ingest.load_verdict(out, "review")
    db.add(Verdict(attempt_id=a.id, raw=verdict, verdict=verdict["verdict"]))
    for f in verdict.get("findings", []):
        db.add(Finding(attempt_id=a.id, origin_id=cfg.origin_id, anchor=p.anchor,
                       repo=repo_name, fingerprint=f["fingerprint"],
                       severity=f["severity"], file=f["file"], line=f.get("line"),
                       claim=f["claim"], required_fix=f.get("required_fix", "")))
    lot.state = "DONE"
    db.commit()
    found = db.scalars(select(Finding).where(Finding.attempt_id == a.id)).all()
    return verdict["verdict"], list(found)
