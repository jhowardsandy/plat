"""The inline loop. Identical logic to what the Celery tasks will call.

decide() -> act -> ingest -> decide(). No LLM is ever asked what to do next.
"""
from __future__ import annotations

import itertools
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from . import adapters, fsm, gates, ingest, notify
from . import prompts as P
from . import roles as R
from .config import Config
from .decisions import record
from .fsm import LotState
from .models import Attempt, Criterion, Finding, LogChunk, Lot, Plat, Transcript, Verdict
from .models import Session as SessionRow


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


def progress_history(db, lot) -> tuple[float, ...]:
    """Every probe reading so far, oldest first. This is what steers a converge
    lot -- both the prompt and the stop condition read it."""
    rows = db.execute(select(Verdict.raw).join(Attempt, Attempt.id == Verdict.attempt_id)
                      .where(Attempt.lot_id == lot.id, Attempt.phase == "gate")
                      .order_by(Attempt.id)).all()
    out = [r[0].get("progress") for r in rows]
    return tuple(float(x) for x in out if x is not None)


def build_ctx(db, plat, lot) -> fsm.Ctx:
    cfg_l = lot.config or {}
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
        mode=fsm.Mode(cfg_l.get("mode", "attempt")),
        objective=cfg_l.get("objective"),
        progress=progress_history(db, lot),
        patience=int(cfg_l.get("patience", fsm.PATIENCE)),
        max_iterations=int(cfg_l.get("max_iterations", fsm.MAX_ITERATIONS)),
        budget_exhausted=(plat.budget_usd > 0 and spent(db, plat) >= plat.budget_usd),
        paused=(plat.status == "paused"),
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
        db.refresh(plat)          # pause may have been set from another process
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
            # The whole point of notifying: a lot that stops while nobody is
            # looking costs the rest of the run in wall-clock.
            notify.send(notify.Event(
                kind="budget" if "budget" in nxt.reason else "blocked",
                title=nxt.reason, anchor=plat.anchor, lot=lot.key, urgent=True,
                detail=_ask(db, lot) or f"plat reopen {plat.anchor} {lot.key} --note '...'"),
                cfg.notify)
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


def _ask(db, lot) -> str | None:
    """The reviewer's question, if it raised one — that is what you need to read."""
    a = _latest(db, lot, phases=["review", "quality"])
    v = _verdict_of(db, a) or {}
    return v.get("blocking_question")


def maybe_deliver_plat(db: DbSession, plat: Plat, log=print, cfg=None) -> bool:
    """Mark a plat DELIVERED once every lot is terminal.

    Delivered is not closed. Plat has finished its work; the branch is unpushed,
    unreviewed and unmerged, and a human owns it from here. Calling that "closed"
    made three finished plats read as shipped when not one of them had merged --
    the ledger was lying, which is worse than the ledger being empty.

    `plat close` is how a human says it actually shipped. v1 inserts closing the
    traverse, recording and sign-off ahead of delivery.
    """
    lots = db.scalars(select(Lot).where(Lot.plat_id == plat.id)).all()
    if not lots or any(l.state not in (LotState.DONE.value, LotState.ABORTED.value)
                       for l in lots):
        return False
    if plat.delivered_at is not None:
        return False
    done = sum(1 for l in lots if l.state == LotState.DONE.value)
    record(db, plat_id=plat.id, actor="system", actor_detail="fsm.deliver_plat",
           kind="transition",
           decision=f"plat DELIVERED - {done}/{len(lots)} lots DONE",
           rationale="every lot is terminal, so Plat's work is finished. Nothing is "
                     "pushed, reviewed or merged -- that is a human's, and `plat "
                     "close` is how they say it shipped.",
           inputs={"lots": len(lots), "done": done,
                   "spent_usd": round(spent(db, plat), 2)})
    plat.status = "delivered"
    plat.delivered_at = datetime.now(timezone.utc)
    db.commit()
    log(f"  [bold green]plat delivered[/bold green] ({done}/{len(lots)} lots) "
        f"[dim]— review it, then `plat close {plat.anchor}`[/dim]")
    if cfg is not None:
        notify.send(notify.Event(kind="delivered", anchor=plat.anchor,
                                 title=f"delivered — {done}/{len(lots)} lots, "
                                       f"waiting on you",
                                 detail=f"${spent(db, plat):.2f}"), cfg.notify)
    return True


def _run_gate(db, cfg, plat, lot, parent, log) -> int:
    lot.state = LotState.GATE.value
    db.commit()
    wt = Path(lot.worktree_path)
    gcfg = (lot.config or {}).get("gate", {})
    cmd = gcfg.get("test", "true")
    a = Attempt(lot_id=lot.id, phase="gate", n=lot.attempt,
                provider="supervisor", model="local", role_binding={})
    db.add(a)
    db.commit()          # visible to the monitor NOW, not when the gate finishes
    log(f"  gate: {cmd}")   # the command is ours; its OUTPUT never goes through log()
    g = gates.run(wt, cmd, lot.base_ref, probe_cmd=gcfg.get("probe"),
                  baseline_passed=int(gcfg.get("baseline_passed", 0)))
    a.exit_code = g.exit_code
    a.finished_at = datetime.now(timezone.utc)
    db.add(Verdict(attempt_id=a.id, raw=g.as_dict(),
                   tests_passed=g.tests_passed, tests_failed=g.tests_failed))
    ok = g.tests_failed == 0 and g.diff_files > 0
    prog = f" · probe {g.progress}" if g.progress is not None else ""
    if g.progress is not None:
        log(f"  probe: {g.progress}")
    d = record(db, plat_id=plat.id, lot_id=lot.id, attempt_id=a.id, parent_id=parent,
               actor="system", actor_detail="gates.run", kind="transition",
               decision=(("gate passed - %d files, 0 failing" % g.diff_files) if ok
                         else ("gate FAILED - %d failing, %d files changed"
                               % (g.tests_failed, g.diff_files))) + prog,
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
        if cfgl.get("mode") == "converge":
            prompt = prompt.replace(
                "## Rules",
                P.converge_block(cfgl.get("objective", ""), progress_history(db, lot),
                                 cfgl.get("gate", {}).get("probe", ""),
                                 lot.attempt, int(cfgl.get("max_iterations",
                                                           fsm.MAX_ITERATIONS)))
                + "## Rules", 1)
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
    db.add(a)
    db.commit()          # an in-flight attempt must be visible while it is in flight
    note = " (resuming session)" if sid else ""
    log(f"  {nxt.phase}: {role.provider}/{role.model}{note} ...")

    before = _worktree_fingerprint(wt) if nxt.phase != "code" else None
    seq = itertools.count()
    residue = [""]          # a chunk can end mid-line; the tail belongs to the next

    def _chunk(raw: str):
        # Own session, own transaction. Writing through the runner's session would
        # keep every chunk inside its open transaction until the agent finished --
        # which is exactly the window a live tail exists to cover.
        from .db import session as _s
        buf = residue[0] + raw
        keep = "" if buf.endswith("\n") else buf[buf.rfind("\n") + 1:]
        residue[0] = keep
        lines = adapters.narrate(role.provider, buf[:len(buf) - len(keep)])
        if not lines:
            return          # protocol chatter with nothing a person would read
        try:
            with _s() as s2:
                s2.add(LogChunk(attempt_id=a.id, seq=next(seq),
                                body="\n".join(lines)))
        except Exception:
            pass

    with _Heartbeat(lot.id):
        res = adapters.run(role, pf, wt, on_chunk=_chunk)
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
        record(db, plat_id=plat.id, lot_id=lot.id, attempt_id=a.id, parent_id=parent,
               actor="system", actor_detail="ingest.validate", kind="transition",
               decision="BLOCKED - termination contract not honoured",
               rationale=str(e),
               inputs={"permission_denials": len(res.permission_denials),
                       "exit_code": res.exit_code, "stderr": res.stderr[-600:]},
               apply=lambda: setattr(lot, "state", LotState.BLOCKED.value))
        db.commit()
        # This path blocked a lot silently while every other block notified. A
        # contract failure is usually environmental and always needs a person.
        notify.send(notify.Event(
            kind="blocked", anchor=plat.anchor, lot=lot.key, urgent=True,
            title="termination contract not honoured",
            detail=str(e)[:300]), cfg.notify)
        raise Halt(str(e))

    last = ingest.persist(db, plat=plat, lot=lot, attempt=a, verdict=verdict,
                          result=res, out_dir=out, parent_decision_id=parent)
    db.commit()
    return last or parent
