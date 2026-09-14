"""The inline loop. Identical logic to what the Celery tasks will call.

decide() -> act -> ingest -> decide(). No LLM is ever asked what to do next.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

import subprocess, threading

from . import fsm, gates, ingest, prompts as P, roles as R, adapters
from .config import Config
from .decisions import record
from .fsm import LotState
from .models import (Plat, Lot, Attempt, Verdict, Finding, Criterion,
                     Session as SessionRow, Transcript)


class Halt(Exception):
    """Stop this lot. The reason is already recorded as a decision."""


def _latest(db, lot, phases=None, exclude=None):
    q = select(Attempt).where(Attempt.lot_id == lot.id)
    if phases:
        q = q.where(Attempt.phase.in_(phases))
    if exclude:
        q = q.where(Attempt.phase.notin_(exclude))
    return db.scalars(q.order_by(Attempt.id.desc())).first()


def _verdict_of(db, attempt) -> dict | None:
    if attempt is None:
        return None
    v = db.scalars(select(Verdict).where(Verdict.attempt_id == attempt.id)
                   .order_by(Verdict.id.desc())).first()
    return v.raw if v else None


def spent(db, plat) -> float:
    rows = db.scalars(select(Attempt).join(Lot).where(Lot.plat_id == plat.id)).all()
    return sum(a.cost_usd for a in rows)


def build_ctx(db, plat, lot) -> fsm.Ctx:
    gate_a = _latest(db, lot, phases=["gate"])
    agent_a = _latest(db, lot, exclude=["gate"])
    prev = db.scalars(
        select(Finding).join(Attempt).where(
            Attempt.lot_id == lot.id, Attempt.n < lot.attempt)).all()
    return fsm.Ctx(
        deps_met=True,
        phases=frozenset((lot.config or {}).get("phases") or ["code", "review"]),                      # v0: one lot, no DAG
        verdict=_verdict_of(db, agent_a),
        gate=_verdict_of(db, gate_a),
        prev_fingerprints=frozenset(f.fingerprint for f in prev),
        heartbeat_at=lot.heartbeat_at,
        budget_exhausted=(plat.budget_usd > 0 and spent(db, plat) >= plat.budget_usd),
    )


def _out_dir(cfg: Config, plat, lot, phase, n) -> Path:
    d = (cfg.worktrees_root / plat.anchor / ".plat" / "lots" / lot.key
         / f"attempt-{n}" / (phase if phase != "code" else ""))
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_lot(db: DbSession, cfg: Config, plat: Plat, lot: Lot, roles_cfg: dict,
            log=print) -> str:
    parent = None
    while True:
        ctx = build_ctx(db, plat, lot)
        nxt = fsm.decide(LotState(lot.state), lot.attempt, ctx)
        log(f"  [{lot.state} a{lot.attempt}] -> {nxt.kind}: {nxt.reason}")

        if nxt.kind in ("noop", "wait"):
            return lot.state
        if nxt.kind == "human":
            parent = record(db, plat_id=plat.id, lot_id=lot.id, parent_id=parent,
                            actor="system", actor_detail="fsm.block", kind="transition",
                            decision="BLOCKED - needs a human", inputs=nxt.inputs,
                            rationale=nxt.reason,
                            apply=lambda: setattr(lot, "state", LotState.BLOCKED.value)).id
            db.commit()
            return LotState.BLOCKED.value
        if nxt.kind == "close":
            record(db, plat_id=plat.id, lot_id=lot.id, parent_id=parent,
                   actor="system", actor_detail="fsm.close", kind="transition",
                   decision="lot DONE", inputs={}, rationale=nxt.reason,
                   apply=lambda: setattr(lot, "state", LotState.DONE.value))
            db.commit()
            return LotState.DONE.value
        if nxt.kind == "gate":
            parent = _run_gate(db, cfg, plat, lot, parent, log)
            continue
        parent = _run_agent(db, cfg, plat, lot, roles_cfg, nxt, parent, log)


def maybe_close_plat(db: DbSession, plat: Plat, log=print) -> bool:
    """Close a plat once every lot is terminal.

    In v0 "closed" means exactly that: all lots DONE. v1 inserts closing the
    traverse, recording and sign-off ahead of this, and a human makes the call.
    Until then a finished plat must still leave the live view, or the monitor
    fills up with work that ended days ago.
    """
    lots = db.scalars(select(Lot).where(Lot.plat_id == plat.id)).all()
    if not lots or any(l.state not in (LotState.DONE.value, LotState.ABORTED.value)
                       for l in lots):
        return False
    if plat.closed_at is not None:
        return False
    done = sum(1 for l in lots if l.state == LotState.DONE.value)
    record(db, plat_id=plat.id, actor="system", actor_detail="fsm.close_plat",
           kind="transition",
           decision=f"plat closed - {done}/{len(lots)} lots DONE",
           rationale="every lot is terminal; v0 has no traverse or recording phase "
                     "ahead of this, so closing is mechanical rather than a judgement",
           inputs={"lots": len(lots), "done": done,
                   "spent_usd": round(spent(db, plat), 2)})
    plat.status = "closed"
    plat.closed_at = datetime.now(timezone.utc)
    db.commit()
    log(f"  [bold green]plat closed[/bold green] ({done}/{len(lots)} lots)")
    return True


def _run_gate(db, cfg, plat, lot, parent, log) -> int:
    lot.state = LotState.GATE.value
    db.commit()
    wt = Path(lot.worktree_path)
    cmd = (lot.config or {}).get("gate", {}).get("test", "true")
    a = Attempt(lot_id=lot.id, phase="gate", n=lot.attempt,
                provider="supervisor", model="local", role_binding={})
    db.add(a); db.flush()
    log(f"  gate: {cmd}")
    g = gates.run(wt, cmd, lot.base_ref)
    a.exit_code = g.exit_code
    a.finished_at = datetime.now(timezone.utc)
    db.add(Verdict(attempt_id=a.id, raw=g.as_dict(),
                   tests_passed=g.tests_passed, tests_failed=g.tests_failed))
    ok = g.tests_failed == 0 and g.diff_files > 0
    d = record(db, plat_id=plat.id, lot_id=lot.id, attempt_id=a.id, parent_id=parent,
               actor="system", actor_detail="gates.run", kind="transition",
               decision=("gate passed - %d files, 0 failing" % g.diff_files) if ok
                        else ("gate FAILED - %d failing, %d files changed"
                              % (g.tests_failed, g.diff_files)),
               rationale="the supervisor ran the tests; the agent's claim is not consulted",
               inputs=g.as_dict())
    db.commit()
    return d.id


class _Heartbeat:
    """Tick lots.heartbeat_at while an agent runs.

    Without this the column only moves either side of a subprocess that may run
    for an hour, so "stale" -- the monitor's headline signal -- can never fire
    during the one period it exists to cover. The thread uses its own session:
    the caller's is not thread-safe.
    """

    def __init__(self, lot_id: int, every: float = 15.0):
        self.lot_id, self.every = lot_id, every
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _beat(self):
        from .db import session as _s
        while not self._stop.wait(self.every):
            try:
                with _s() as s2:
                    s2.execute(update(Lot).where(Lot.id == self.lot_id)
                               .values(heartbeat_at=datetime.now(timezone.utc)))
            except Exception:
                pass          # a missed beat must never take down the lot

    def __enter__(self):
        self._t = threading.Thread(target=self._beat, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join(timeout=2)


def _worktree_fingerprint(wt: Path) -> str:
    """HEAD plus dirty state. A reviewer that changes either has overstepped."""
    def g(*a):
        return subprocess.run(["git", *a], cwd=str(wt), capture_output=True,
                              text=True, stdin=subprocess.DEVNULL).stdout
    return g("rev-parse", "HEAD").strip() + "|" + g("status", "--porcelain")


def _prior_session(db, lot) -> str | None:
    a = db.scalars(select(Attempt).where(Attempt.lot_id == lot.id,
                                         Attempt.phase == "code")
                   .order_by(Attempt.id.desc())).first()
    if not a:
        return None
    s = db.scalars(select(SessionRow).where(SessionRow.attempt_id == a.id)).first()
    return s.provider_session_id if s else None


def _run_agent(db, cfg, plat, lot, roles_cfg, nxt, parent, log) -> int:
    lot.state = nxt.state.value
    lot.attempt = nxt.attempt or lot.attempt or 1
    lot.heartbeat_at = datetime.now(timezone.utc)
    db.commit()

    sid = _prior_session(db, lot) if nxt.resume_session else None
    role = R.bind(roles_cfg, nxt.role, lot.attempt, session_id=sid)
    out = _out_dir(cfg, plat, lot, nxt.phase, lot.attempt)
    wt = Path(lot.worktree_path)
    crit = db.scalars(select(Criterion).where(Criterion.plat_id == plat.id)).all()
    cfgl = lot.config or {}

    if nxt.phase == "code":
        findings = db.scalars(
            select(Finding).join(Attempt).where(
                Attempt.lot_id == lot.id, Attempt.n == lot.attempt - 1)).all()
        prompt = P.build_code(lot, plat, wt, cfgl.get("branch", ""), lot.base_ref or "HEAD",
                              cfgl.get("plat_map", ""), cfgl.get("plan", ""), crit,
                              findings, cfgl.get("gate", {}).get("test", "true"), out)
    else:
        gate_a = _latest(db, lot, phases=["gate"])
        prompt = P.build_review(lot, plat, wt, lot.base_ref or "HEAD",
                                cfgl.get("plan", ""), crit,
                                _verdict_of(db, gate_a) or {}, out)
    pf = out / "prompt.md"
    pf.write_text(prompt)

    a = Attempt(lot_id=lot.id, phase=nxt.phase, n=lot.attempt,
                provider=role.provider, model=role.model,
                role_binding={"role": role.name, "resumed": bool(sid)})
    db.add(a); db.flush()
    note = " (resuming session)" if sid else ""
    log(f"  {nxt.phase}: {role.provider}/{role.model}{note} ...")

    before = _worktree_fingerprint(wt) if nxt.phase != "code" else None
    with _Heartbeat(lot.id):
        res = adapters.run(role, pf, wt)
    a.exit_code = res.exit_code
    a.cost_usd = res.cost_usd
    a.cost_estimated = res.cost_estimated
    a.permission_denials = len(res.permission_denials)
    a.tokens_in, a.tokens_out = res.tokens_in, res.tokens_out
    a.finished_at = datetime.now(timezone.utc)
    lot.heartbeat_at = datetime.now(timezone.utc)
    db.refresh(lot)          # the heartbeat thread moved it underneath us
    log(f"    exit={res.exit_code} {res.duration_s:.0f}s "
        f"${res.cost_usd:.2f}{'~' if res.cost_estimated else ''}")

    if before is not None and _worktree_fingerprint(wt) != before:
        record(db, plat_id=plat.id, lot_id=lot.id, attempt_id=a.id, parent_id=parent,
               actor="system", actor_detail="runner.verify", kind="transition",
               decision=f"{nxt.phase} agent MODIFIED the worktree",
               rationale="a reviewer must not edit the code it reviews; "
                         "the verdict is not trustworthy and the diff is contaminated",
               inputs={"phase": nxt.phase},
               apply=lambda: setattr(lot, "state", LotState.BLOCKED.value))
        db.commit()
        raise Halt(f"{nxt.phase} agent modified the worktree")

    if res.permission_denials:
        log(f"    !! {len(res.permission_denials)} permission denial(s) - "
            f"the agent was blocked, not merely unproductive")

    try:
        verdict = ingest.load_verdict(out, nxt.phase)
    except ingest.ContractError as e:
        if res.stdout:
            db.add(Transcript(attempt_id=a.id, body=res.stdout[:4_000_000]))
        d = record(db, plat_id=plat.id, lot_id=lot.id, attempt_id=a.id, parent_id=parent,
                   actor="system", actor_detail="ingest.validate", kind="transition",
                   decision="BLOCKED - termination contract not honoured",
                   rationale=str(e),
                   inputs={"permission_denials": len(res.permission_denials),
                           "exit_code": res.exit_code, "stderr": res.stderr[-600:]},
                   apply=lambda: setattr(lot, "state", LotState.BLOCKED.value))
        db.commit()
        raise Halt(str(e))

    last = ingest.persist(db, plat=plat, lot=lot, attempt=a, verdict=verdict,
                          result=res, out_dir=out, parent_decision_id=parent)
    db.commit()
    return last or parent
