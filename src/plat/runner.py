"""The inline loop. Identical logic to what the Celery tasks will call.

decide() -> act -> ingest -> decide(). No LLM is ever asked what to do next.
"""
from __future__ import annotations

import hashlib
import itertools
import subprocess
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from . import adapters, fsm, gates, ingest, notify
from . import prompts as P
from . import roles as R
from .adapters import base
from .config import Config
from .decisions import record
from .fsm import LotState
from .models import Attempt, Criterion, Finding, LogChunk, Lot, Plat, Verdict
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
                      .where(Attempt.lot_id == lot.id, Attempt.phase == "gate",
                             Attempt.n > int((lot.config or {}).get("attempt_start", 0)))
                      .order_by(Attempt.id)).all()
    out = [r[0].get("progress") for r in rows]
    return tuple(float(x) for x in out if x is not None)


def live_agent(db, lot) -> Attempt | None:
    """An agent still running on this lot, from a session that is no longer watching.

    Agents are spawned in their own process group, so closing the terminal that
    started one does not stop it. That is mostly a mercy -- no half-written files --
    but it means a later session can find a lot whose heartbeat went cold while the
    work underneath it is still moving. Gating a moving target is worse than waiting.
    """
    a = db.scalars(
        select(Attempt).where(Attempt.lot_id == lot.id, Attempt.pid.isnot(None),
                              Attempt.finished_at.is_(None))
        .order_by(Attempt.id.desc())).first()
    if a is None:
        return None
    from .ownership import host_id
    if a.host_id and a.host_id != host_id():
        return a  # a remote PID is opaque, never inspect or clear it locally
    if base.process_alive(a.pid, (a.role_binding or {}).get("cmd", ""),
                          since=a.started_at):
        return a
    # It died without finishing -- killed, crashed, or its session went down with
    # it. Clear the pid so the next caller does not have to re-check a dead one.
    a.pid = None
    db.commit()
    return None


def deps_pending(db, plat, lot) -> tuple[tuple[str, str], ...]:
    """Upstream lots of this one that have not reached DONE.

    `deps_met` was hardcoded True: the FSM had the branch, the docs described the
    guarantee, and nothing computed it. An edge bought ordering only because
    `plat start` happened to iterate in insertion order, which is not an ordering
    guarantee, it is a coincidence.
    """
    keys = list(lot.depends_on or [])
    if not keys:
        return ()
    rows = db.execute(
        select(Lot.key, Lot.state).where(Lot.plat_id == plat.id, Lot.key.in_(keys))
    ).all()
    found = {k: s for k, s in rows}
    out = [(k, found.get(k, "MISSING")) for k in keys
           if found.get(k) != LotState.DONE.value]
    return tuple(out)


def upstream_handoff(db, plat, lot) -> list[dict]:
    """What each upstream lot reported building, from its own code verdict.

    The latest code attempt wins: if a lot went round twice, the second pass is
    what is actually in the tree.
    """
    out: list[dict] = []
    for key in list(lot.depends_on or []):
        up = db.scalars(
            select(Lot).where(Lot.plat_id == plat.id, Lot.key == key)).first()
        if up is None:
            continue
        v = db.scalars(
            select(Verdict).join(Attempt, Attempt.id == Verdict.attempt_id)
            .where(Attempt.lot_id == up.id, Attempt.phase == "code")
            .order_by(Attempt.id.desc())).first()
        for h in ((v.raw if v else None) or {}).get("handoff") or []:
            if isinstance(h, dict) and h.get("name"):
                out.append({"lot": up.key, "repo": up.repo, **h})
    return out


def ordered_lots(lots) -> list:
    """Dependencies before dependents, insertion order otherwise.

    Kahn, with one rule that matters more than the sort: a lot in a cycle is
    appended rather than dropped. `plat draft` rejects cycles, but a hand-edited
    spec can still carry one, and a lot that silently disappears from a run is
    worse than a lot that waits and says why.
    """
    lots = list(lots)
    keys = {l.key for l in lots}
    pend = {l.key: {d for d in (l.depends_on or []) if d in keys} for l in lots}
    out, placed = [], set()
    while True:
        ready = [l for l in lots
                 if l.key not in placed and not (pend[l.key] - placed)]
        if not ready:
            break
        for l in ready:
            out.append(l); placed.add(l.key)
    out += [l for l in lots if l.key not in placed]     # cycles, never dropped
    return out


def lot_criteria(db, plat, lot):
    return db.scalars(select(Criterion).where(
        Criterion.plat_id == plat.id, Criterion.scope == "lot",
        (Criterion.lot_key == lot.key) | Criterion.lot_key.is_(None)
    ).order_by(Criterion.id)).all()


def build_ctx(db, plat, lot) -> fsm.Ctx:
    cfg_l = lot.config or {}
    gate_a = _latest(db, lot, phases=["gate"])
    agent_a = _latest(db, lot, exclude=["gate"])
    prev = db.scalars(
        select(Finding).join(Attempt).where(
            Attempt.lot_id == lot.id, Attempt.n < lot.attempt,
            Attempt.n > int(cfg_l.get("attempt_start", 0)))).all()
    pending = deps_pending(db, plat, lot)
    criteria = lot_criteria(db, plat, lot)
    gate = _verdict_of(db, gate_a) if gate_a and gate_a.n == lot.attempt else None
    if gate is not None:
        required = {c.ac_id for c in criteria if c.verify_cmd}
        checks = gate.get("criteria_check", [])
        if {c.get("id") for c in checks} != required or len(checks) != len(required):
            gate = {**gate, "exit_code": 1, "error": "independent criterion evidence is incomplete"}
    offset = int(cfg_l.get("attempt_start", 0))
    return fsm.Ctx(
        deps_met=not pending,
        deps_pending=pending,
        phases=frozenset((lot.config or {}).get("phases") or ["code", "review"]),
        verdict=_verdict_of(db, agent_a),
        gate=gate,
        expected_criteria=tuple(c.ac_id for c in criteria),
        attempt_start=offset,
        max_attempts=offset + int(cfg_l.get("max_attempts", fsm.MAX_ATTEMPTS)),
        prev_fingerprints=frozenset(f.fingerprint for f in prev),
        heartbeat_at=lot.heartbeat_at,
        mode=fsm.Mode(cfg_l.get("mode", "attempt")),
        objective=cfg_l.get("objective"),
        progress=progress_history(db, lot),
        patience=int(cfg_l.get("patience", fsm.PATIENCE)),
        max_iterations=offset + int(cfg_l.get("max_iterations", fsm.MAX_ITERATIONS)),
        budget_exhausted=(plat.budget_usd > 0 and spent(db, plat) >= plat.budget_usd),
        paused=(plat.status == "paused"),
    )


def _out_dir(cfg: Config, plat, lot, phase, n) -> Path:
    d = (cfg.worktrees_root / plat.anchor / ".plat" / "lots" / lot.key
         / f"attempt-{n}" / (phase if phase != "code" else ""))
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_lot(db, cfg, plat, lot, roles_cfg, log=print):
    from .lifecycle import require_accepted
    from .ownership import execution
    require_accepted(db, plat)
    if live_agent(db, lot):
        log("  an agent from a previous session is still running; no new claim taken")
        return lot.state
    with execution(db, cfg, plat, lot):
        return _run_lot(db, cfg, plat, lot, roles_cfg, log)


def _run_lot(db: DbSession, cfg: Config, plat: Plat, lot: Lot, roles_cfg: dict,
            log=print) -> str:
    parent = None
    from .spec import IMPLEMENTED_PHASES
    phases = (lot.config or {}).get("phases", ["code", "review"])
    if "code" not in phases or set(phases) - IMPLEMENTED_PHASES:
        raise Halt("unsupported phases; use code, review, docs, and quality")
    live = live_agent(db, lot)
    if live is not None:
        started = live.started_at
        age = (datetime.now(started.tzinfo) - started).total_seconds()
        log(f"  [{lot.state} a{lot.attempt}] -> wait: an agent from a previous "
            f"session is still running on this lot")
        log(f"    {live.phase}: {live.provider}/{live.model} pid {live.pid}, "
            f"{age / 60:.0f}m in")
        log(f"    let it finish, or: plat abort {plat.anchor} {lot.key}")
        return lot.state
    criteria = lot_criteria(db, plat, lot)
    if not criteria or ("review" not in phases and any(not c.verify_cmd for c in criteria)):
        record(db, plat_id=plat.id, lot_id=lot.id, actor="system", actor_detail="runner.preflight",
               kind="transition", decision="BLOCKED - incomplete criterion verification configuration",
               rationale="each lot needs owned criteria; without review every criterion needs verify",
               inputs={}, apply=lambda: setattr(lot, "state", "BLOCKED"))
        db.commit()
        return lot.state
    unfinished = db.scalars(select(Attempt).where(
        Attempt.lot_id == lot.id, Attempt.finished_at.is_(None))).all()
    if unfinished:
        def interrupted():
            lot.state = LotState.BLOCKED.value
            for a in unfinished:
                a.finished_at = datetime.now(timezone.utc)
                a.exit_code = -1
                a.pid = None
        record(db, plat_id=plat.id, lot_id=lot.id, actor="system",
               actor_detail="runner.recover", kind="transition",
               decision="BLOCKED - interrupted attempt needs reconciliation",
               rationale="the supervisor did not record completion; inspect preserved artifacts and reopen with a reason",
               inputs={"attempts": [a.id for a in unfinished]}, apply=interrupted)
        db.commit()
        log("  interrupted attempt: inspect the worktree, then plat reopen --note '...' ")
        return lot.state
    while True:
        db.refresh(lot)
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


def reopen_lot(db, plat, lot, note: str):
    """Restart deliberately, invalidating dependent work and old delivery claims."""
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    db.refresh(plat)
    db.refresh(lot)
    if not note.strip():
        raise ValueError("a recovery decision or answer is required")
    if plat.closed_at is not None:
        raise ValueError("shipped work is immutable; use a new plat")
    if lot.state not in ("BLOCKED", "ABORTED", "DONE"):
        raise ValueError(f"{lot.key} is {lot.state}; only stopped or completed work can be reopened")
    lots = db.scalars(select(Lot).where(Lot.plat_id == plat.id).order_by(Lot.id)
                      .with_for_update().execution_options(populate_existing=True)).all()
    affected = {lot.key}
    while True:
        more = {l.key for l in lots if affected.intersection(l.depends_on or [])}
        if more <= affected:
            break
        affected |= more
    targets = [l for l in lots if l.key in affected]
    for target in targets:
        from .models import ExecutionClaim
        claim = db.get(ExecutionClaim, target.id)
        if claim and claim.active:
            raise ValueError(f"{target.key} has an active execution claim; reconcile it first")
        if target.state not in ("BLOCKED", "ABORTED", "DONE", "PENDING") or live_agent(db, target):
            raise ValueError(f"{target.key} may still be executing; stop it before reopening its dependency")
    for target in targets:
        was = target.state
        unfinished = db.scalars(select(Attempt).where(
            Attempt.lot_id == target.id, Attempt.finished_at.is_(None))).all()
        def apply(target=target, unfinished=unfinished):
            for attempt in unfinished:
                attempt.finished_at = datetime.now(timezone.utc)
                attempt.exit_code, attempt.pid = -1, None
            for criterion in lot_criteria(db, plat, target):
                criterion.met_in_attempt_id = None
            target.state = "PENDING"
            target.human_note = (target.human_note + "\n\nReopen: " + note) if target.human_note else note
            target.config = {**(target.config or {}), "attempt_start": target.attempt}
            plat.status = "planned"
            plat.delivered_at = None
            from .models import Workflow
            workflow = db.get(Workflow, plat.id)
            if workflow:
                workflow.stage = "development"
                workflow.version += 1
        record(db, plat_id=plat.id, lot_id=target.id, actor="human", actor_detail="reopen",
               kind="override", decision=f"reopened {was} -> PENDING",
               rationale=note, inputs={"was": was, "upstream": lot.key},
               alternatives=["leave stopped"], apply=apply)


def _ask(db, lot) -> str | None:
    """The reviewer's question, if it raised one — that is what you need to read."""
    a = _latest(db, lot, exclude=["gate"])
    v = _verdict_of(db, a) or {}
    return v.get("blocking_question") or "; ".join(v.get("open_questions") or []) or v.get("summary")


def maybe_deliver_plat(db: DbSession, plat: Plat, log=print, cfg=None) -> bool:
    """Only fully completed, independently verified work can be delivered."""
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    db.refresh(plat)
    lots = db.scalars(select(Lot).where(Lot.plat_id == plat.id)).all()
    if not lots or plat.status in ("closed", "aborted", "paused"):
        return False
    if any(l.state not in (LotState.DONE.value, LotState.ABORTED.value) for l in lots):
        return False
    if any(l.state == LotState.ABORTED.value for l in lots):
        if plat.status != "incomplete":
            record(db, plat_id=plat.id, actor="system", actor_detail="fsm.deliver_plat",
                   kind="transition", decision="plat INCOMPLETE - required lots aborted",
                   inputs={"aborted": [l.key for l in lots if l.state == "ABORTED"]},
                   apply=lambda: (setattr(plat, "status", "incomplete"),
                                  setattr(plat, "delivered_at", None)))
            db.commit()
        return False
    if plat.delivered_at is not None:
        return False
    run_criteria = db.scalars(select(Criterion).where(
        Criterion.plat_id == plat.id, Criterion.scope == "run")).all()
    checks = []
    for criterion in run_criteria:
        target = next((l for l in lots if l.key == criterion.verify_in), None)
        if target is None or not criterion.verify_cmd:
            checks.append({"id": criterion.ac_id, "met": False,
                           "error": "run criterion needs verify and a valid verify_in worktree"})
        else:
            from .config import load
            from .lifecycle import execute_command
            from .ownership import OwnershipError, execution
            try:
                with execution(db, cfg or load(), plat, target):
                    with _Heartbeat(target.id):
                        rc, output = execute_command(db, cfg or load(), plat, target,
                                                     "run_check", criterion.verify_cmd)
                    checks.append({"id": criterion.ac_id, "cmd": criterion.verify_cmd,
                                   "met": rc == 0, "exit_code": rc, "output_tail": output[-1500:]})
            except OwnershipError as exc:
                log(f"  run criterion {criterion.ac_id} waits for its owning executor: {exc}")
                return False
    # Checks commit their own attempts and release claims. Re-read under locks:
    # cancellation or reopening may have happened while a command was running.
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    db.refresh(plat)
    lots = db.scalars(select(Lot).where(Lot.plat_id == plat.id).with_for_update()
                      .execution_options(populate_existing=True)).all()
    if (plat.status in ("closed", "aborted", "paused") or plat.delivered_at is not None
            or any(l.state != LotState.DONE.value for l in lots)):
        return False
    failed = any(c["met"] is not True for c in checks)
    def finish():
        if failed:
            plat.status = "blocked"
        else:
            plat.status = "delivered"
        plat.delivered_at = None if failed else datetime.now(timezone.utc)
    record(db, plat_id=plat.id, actor="system", actor_detail="fsm.deliver_plat",
           kind="transition",
           decision="plat BLOCKED - run criteria failed" if failed else "plat DELIVERED - all lots DONE",
           rationale="delivery records local verified completion; deployment and shipment are separate",
           inputs={"lots": len(lots), "criteria_check": checks,
                   "spent_usd": round(spent(db, plat), 2)}, apply=finish)
    db.commit()
    log("  run criteria failed; see plat show, correct the cause, then plat start" if failed
        else "  plat delivered — local work complete; record actual shipment with plat close")
    if cfg is not None:
        notify.send(notify.Event(kind="blocked" if failed else "delivered", anchor=plat.anchor,
                                 title="run criteria failed" if failed else "delivered — awaiting shipment"), cfg.notify)
    return not failed


def _run_gate(db, cfg, plat, lot, parent, log) -> int:
    wt = Path(lot.worktree_path)
    gcfg = (lot.config or {}).get("gate", {})
    cmd = gcfg.get("test", "true")
    a = Attempt(lot_id=lot.id, phase="gate", n=lot.attempt,
                provider="supervisor", model="local", role_binding={})
    from .ownership import current, host_id
    claim = current.get()
    a.host_id, a.executor_id = host_id(), cfg.origin_id
    a.claim_generation = claim[2] if claim else None
    d = record(db, plat_id=plat.id, lot_id=lot.id, parent_id=parent,
               actor="system", actor_detail="gates.start", kind="transition",
               decision="verifying independently", inputs={"cmd": cmd},
               apply=lambda: (setattr(lot, "state", LotState.GATE.value), db.add(a)))
    parent = d.id
    db.commit()          # visible to the monitor NOW, not when the gate finishes
    log(f"  gate: {cmd}")   # the command is ours; its OUTPUT never goes through log()
    def gate_pid(pid, command):
        from .db import session as _s
        with _s() as telemetry:
            telemetry.execute(update(Attempt).where(Attempt.id == a.id)
                              .values(pid=pid, role_binding={"cmd": command}))
    with _Heartbeat(lot.id):
        g = gates.run(wt, cmd, lot.base_ref, probe_cmd=gcfg.get("probe"),
                      baseline_passed=int(gcfg.get("baseline_passed", 0)), on_pid=gate_pid)
        db.refresh(lot)
        checks = ([] if lot.state == "ABORTED" else
                  gates.verify(wt, lot_criteria(db, plat, lot), on_pid=gate_pid))
    db.refresh(a)
    a.exit_code = g.exit_code
    a.finished_at = datetime.now(timezone.utc)
    a.pid = None             # finished: nothing to find alive, nothing to abort
    evidence = {**g.as_dict(), "criteria_check": checks,
                "diff_required": (lot.config or {}).get("kind") != "investigation"}
    db.add(Verdict(attempt_id=a.id, raw=evidence,
                   tests_passed=g.tests_passed, tests_failed=g.tests_failed))
    ok = fsm.gate_passed(evidence)
    failed_criteria = sum(c["met"] is not True for c in evidence["criteria_check"])
    prog = f" · probe {g.progress}" if g.progress is not None else ""
    if g.progress is not None:
        log(f"  probe: {g.progress}")
    d = record(db, plat_id=plat.id, lot_id=lot.id, attempt_id=a.id, parent_id=parent,
               actor="system", actor_detail="gates.run", kind="transition",
               decision=(("gate passed - %d files, 0 failing" % g.diff_files) if ok
                         else (f"gate FAILED - exit {g.exit_code}, {failed_criteria} criterion check(s) failed, "
                               f"{g.diff_files} files changed")) + prog,
               rationale="the supervisor ran the tests; the agent's claim is not consulted",
               inputs=evidence)
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
        from .ownership import current
        self.claim = current.get()
        self.lot_id, self.every = lot_id, every
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _beat(self):
        from .db import session as _s
        while not self._stop.wait(self.every):
            try:
                with _s() as s2:
                    if self.claim:
                        from .ownership import renew
                        renew(s2, self.claim)
                    s2.execute(update(Lot).where(Lot.id == self.lot_id)
                               .values(heartbeat_at=datetime.now(timezone.utc)))
                    from .models import TrackedSession
                    s2.execute(update(TrackedSession).where(TrackedSession.lot_id == self.lot_id,
                               TrackedSession.source == 'launched', TrackedSession.state == 'active')
                               .values(last_activity=datetime.now(timezone.utc)))
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
    """Hash HEAD, index/worktree diffs, and untracked file contents."""
    digest = hashlib.sha256()
    def git(*args):
        return subprocess.run(["git", *args], cwd=wt, capture_output=True,
                              stdin=subprocess.DEVNULL, check=True).stdout
    for data in (git("rev-parse", "HEAD"), git("diff", "--binary", "HEAD", "--"),
                 git("diff", "--cached", "--binary", "--"), git("status", "--porcelain")):
        digest.update(data)
    for raw in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if raw:
            path = wt / raw.decode()
            digest.update(raw)
            digest.update(str(path.readlink()).encode() if path.is_symlink() else path.read_bytes())
    return digest.hexdigest()


def _prior_session(db, lot, provider: str, cfg=None) -> str | None:
    a = db.scalars(select(Attempt).where(Attempt.lot_id == lot.id,
                                         Attempt.phase == "code")
                   .order_by(Attempt.id.desc())).first()
    if not a or a.provider != provider:
        return None
    if cfg is not None:
        from .ownership import host_id
        if a.executor_id != cfg.origin_id or a.host_id != host_id():
            return None
    s = db.scalars(select(SessionRow).where(SessionRow.attempt_id == a.id,
                                           SessionRow.provider == provider)).first()
    return s.provider_session_id if s else None


def _run_agent(db, cfg, plat, lot, roles_cfg, nxt, parent, log, out_dir=None) -> int:
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    db.refresh(plat)
    db.execute(select(Lot.id).where(Lot.id == lot.id).with_for_update())
    db.refresh(lot)
    if plat.status in ("paused", "aborted", "closed") or lot.state == "ABORTED":
        raise Halt("execution was paused or stopped before dispatch")
    role_attempt = (nxt.attempt or 1) - int((lot.config or {}).get("attempt_start", 0))
    role = R.bind(roles_cfg, nxt.role, role_attempt)
    sid = _prior_session(db, lot, role.provider, cfg) if nxt.resume_session else None
    role = R.bind(roles_cfg, nxt.role, role_attempt, session_id=sid)
    lot_attempt = nxt.attempt or lot.attempt or 1
    out = out_dir or _out_dir(cfg, plat, lot, nxt.phase, lot_attempt)
    out.mkdir(parents=True, exist_ok=True)
    wt = Path(lot.worktree_path)
    crit = lot_criteria(db, plat, lot)
    cfgl = lot.config or {}

    if nxt.phase == "code":
        findings = db.scalars(
            select(Finding).join(Attempt).where(
                Attempt.lot_id == lot.id, Attempt.n == lot_attempt - 1)).all()
        prompt = P.build_code(lot, plat, wt, cfgl.get("branch", ""), lot.base_ref or "HEAD",
                              cfgl.get("plat_map", ""), cfgl.get("plan", ""), crit,
                              findings, cfgl.get("gate", {}).get("test", "true"), out,
                              upstream=upstream_handoff(db, plat, lot), attempt=lot_attempt)
        if cfgl.get("mode") == "converge":
            prompt = prompt.replace(
                "## Rules",
                P.converge_block(cfgl.get("objective", ""), progress_history(db, lot),
                                 cfgl.get("gate", {}).get("probe", ""),
                                 role_attempt, int(cfgl.get("max_iterations",
                                                           fsm.MAX_ITERATIONS)))
                + "## Rules", 1)
    else:
        from .review import prior_art
        gate_a = _latest(db, lot, phases=["gate"])
        prompt = P.build_review(lot, plat, wt, lot.base_ref or "HEAD",
                                cfgl.get("plan", ""), crit,
                                _verdict_of(db, gate_a) or {}, out, prior=prior_art(db, lot.repo),
                                upstream=upstream_handoff(db, plat, lot))
    if cfgl.get("kind") == "investigation":
        if nxt.phase == "code":
            prompt = P.build_investigation(lot, plat, wt, crit, cfgl.get("plan", ""), out)
        else:
            from .models import Artifact
            evidence = db.scalars(select(Artifact).join(Attempt, Attempt.id == Artifact.attempt_id)
                .where(Attempt.lot_id == lot.id, Attempt.phase == "code", Artifact.name == "code.md")
                .order_by(Artifact.id.desc())).first()
            prompt += "\nThis is an evidence-only investigation; no repository diff is required.\n"
            if evidence:
                prompt += "\n## Investigation evidence\n" + evidence.body
    if nxt.phase in ("docs", "quality"):
        prompt = P.build_support(nxt.phase, lot, plat, wt, crit,
                                 _verdict_of(db, _latest(db, lot, phases=["gate"])) or {}, out)
    if lot.human_note:
        prompt += "\n## Human decision for this attempt\n\n" + lot.human_note + "\n"
    if nxt.phase == "code":
        previous_gate = _verdict_of(db, _latest(db, lot, phases=["gate"]))
        if previous_gate:
            prompt += "\n## Previous supervisor gate evidence\n\n" + str(previous_gate) + "\n"
    pf = out / "prompt.md"
    pf.write_text(prompt)

    a = Attempt(lot_id=lot.id, phase=nxt.phase, n=lot_attempt,
                provider=role.provider, model=role.model,
                role_binding={**asdict(role), "role": role.name, "resumed": bool(role.session_id)})
    from .ownership import current, host_id
    claim = current.get()
    a.host_id, a.executor_id = host_id(), cfg.origin_id
    a.claim_generation = claim[2] if claim else None
    d = record(db, plat_id=plat.id, lot_id=lot.id, parent_id=parent,
               actor="system", actor_detail="runner.dispatch", kind="transition",
               decision=f"{nxt.phase} attempt {lot_attempt}", rationale=nxt.reason,
               inputs={"provider": role.provider, "model": role.model, "effort": role.effort},
               apply=lambda: (setattr(lot, "state", nxt.state.value),
                              setattr(lot, "attempt", lot_attempt),
                              setattr(lot, "heartbeat_at", datetime.now(timezone.utc)), db.add(a)))
    parent = d.id
    from . import sessions
    sessions.launched(db, cfg, plat, lot, a)
    db.commit()          # an in-flight attempt must be visible while it is in flight
    note = " (resuming session)" if role.session_id else ""
    log(f"  {nxt.phase}: {role.provider}/{role.model}{note} ...")

    before = _worktree_fingerprint(wt) if nxt.phase != "code" or cfgl.get("kind") == "investigation" else None
    seq = itertools.count()
    residue = [""]          # a chunk can end mid-line; the tail belongs to the next

    def _pid(pid: int, cmd: str):
        # Its own transaction, like _chunk: the runner's session holds an open
        # transaction for the whole agent run, and a pid nobody else can read
        # until the agent finishes is a pid that tells nobody anything.
        from .db import session as _s
        try:
            with _s() as s2:
                s2.execute(update(Attempt).where(Attempt.id == a.id)
                           .values(pid=pid,
                                   role_binding={**(a.role_binding or {}), "cmd": cmd}))
                s2.commit()
        except Exception:
            pass

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

    try:
        with _Heartbeat(lot.id):
            res = adapters.run(role, pf, wt, on_chunk=_chunk, on_pid=_pid)
    except Exception as exc:
        res = base.AgentResult(ok=False, exit_code=-1, duration_s=0,
                               provider=role.provider, error=str(exc))
    _chunk("\n")  # flush the last unterminated provider event
    # PID and abort updates were committed by other sessions. Refresh before
    # clearing: assigning None to a cached None would leave the real PID behind.
    db.execute(select(Lot.id).where(Lot.id == lot.id).with_for_update())
    db.refresh(a)
    db.refresh(lot)
    sessions.complete(db, a, res)
    from .lifecycle import local_revision
    lot.config = {**(lot.config or {}), 'observed_revision': local_revision(lot),
                  'observed_at': datetime.now(timezone.utc).isoformat()}
    a.exit_code = res.exit_code
    a.cost_usd = res.cost_usd
    a.cost_estimated = res.cost_estimated
    a.permission_denials = len(res.permission_denials)
    a.tokens_in, a.tokens_out = res.tokens_in, res.tokens_out
    a.finished_at = datetime.now(timezone.utc)
    a.pid = None             # finished: nothing to find alive, nothing to abort
    lot.heartbeat_at = datetime.now(timezone.utc)
    log(f"    exit={res.exit_code} {res.duration_s:.0f}s "
        f"${res.cost_usd:.2f}{'~' if res.cost_estimated else ''}")

    # In-flight cancellation is authoritative even if the subprocess reports success.
    if lot.state == LotState.ABORTED.value:
        ingest.evidence(db, plat=plat, attempt=a, result=res, out_dir=out)
        record(db, plat_id=plat.id, lot_id=lot.id, attempt_id=a.id, parent_id=parent,
               actor="system", actor_detail="runner.cancelled", kind="transition",
               decision="attempt stopped after abort", inputs={"exit_code": res.exit_code})
        db.commit()
        raise Halt("attempt was aborted")
    failure = None
    if not res.ok or res.exit_code != 0 or res.permission_denials:
        failure = (res.error or f"provider exit {res.exit_code}") + (
            f"; {len(res.permission_denials)} permission denial(s)" if res.permission_denials else "")
    elif before is not None and _worktree_fingerprint(wt) != before:
        failure = f"{nxt.phase} agent MODIFIED the worktree"
    try:
        if failure:
            raise ingest.ContractError(failure)
        verdict = ingest.load_verdict(out, nxt.phase)
        if not (out / f"{nxt.phase}.md").is_file():
            raise ingest.ContractError(f"{nxt.phase}.md narrative was never written")
        check_key = "acceptance_criteria" if nxt.phase == "code" else "criteria_check"
        ids = [c["id"] for c in verdict.get(check_key, [])]
        expected = [c.ac_id for c in crit]
        # A blocked coder may not yet be able to assess the criteria at all.
        blocked = nxt.phase == "code" and (verdict.get("needs_human") or verdict.get("status") == "blocked")
        if not blocked and (len(ids) != len(expected) or set(ids) != set(expected)):
            raise ingest.ContractError(f"criterion IDs must match declared scope: {expected}; received {ids}")
        if nxt.phase in ("review", "quality", "docs") and verdict.get("verdict") == "pass" and verdict.get("findings"):
            raise ingest.ContractError("a passing review cannot contain unresolved findings")
        if nxt.phase == "code" and verdict.get("lot") != lot.key:
            raise ingest.ContractError("code verdict names a different lot")
    except ingest.ContractError as exc:
        ingest.evidence(db, plat=plat, attempt=a, result=res, out_dir=out)
        record(db, plat_id=plat.id, lot_id=lot.id, attempt_id=a.id, parent_id=parent,
               actor="system", actor_detail="runner.verify", kind="transition",
               decision="BLOCKED - execution or termination contract failed", rationale=str(exc),
               inputs={"permission_denials": len(res.permission_denials),
                       "exit_code": res.exit_code, "stderr": res.stderr[-600:]},
               apply=lambda: setattr(lot, "state", LotState.BLOCKED.value))
        db.commit()
        notify.send(notify.Event(kind="blocked", anchor=plat.anchor, lot=lot.key,
                                 urgent=True, title="execution or termination contract failed",
                                 detail=str(exc)[:300]), cfg.notify)
        raise Halt(str(exc))

    if nxt.phase == "code" and not verdict.get("handoff"):
        # Not an error -- plenty of lots have nothing to hand over. But a lot with
        # dependents and an empty handoff means the next agent is about to guess
        # at a contract that exists, and that is worth one line now rather than a
        # contradiction discovered two attempts later.
        dependents = db.scalars(
            select(Lot.key).where(Lot.plat_id == plat.id,
                                  Lot.depends_on.any(lot.key))).all()
        if dependents:
            log(f"    !! no handoff reported, but {', '.join(dependents)} "
                f"depend(s) on this lot")

    last = ingest.persist(db, plat=plat, lot=lot, attempt=a, verdict=verdict,
                          result=res, out_dir=out, parent_decision_id=parent)
    record(db, plat_id=plat.id, lot_id=lot.id, attempt_id=a.id, parent_id=last or parent,
           actor="system", actor_detail="runner.complete", kind="transition",
           decision=f"{nxt.phase} result ingested", inputs={"exit_code": res.exit_code,
                                                          "status": verdict.get("status"),
                                                          "verdict": verdict.get("verdict")})
    db.commit()
    return last or parent
