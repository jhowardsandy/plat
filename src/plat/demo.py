"""A seeded demo plat, for screenshots and for a first run that is not empty.

Every row is tagged `origin_id = "demo"` and anchored `DEMO-*`, so `plat demo
--clear` removes exactly this and nothing else. No real repository, ticket or
person appears here: the names are deliberately generic so a screenshot taken
from it can go anywhere.

The states are chosen to show what the monitor is FOR: one lot working, one
wedged with a cold heartbeat, one stopped for a human, one waiting on a
dependency, one delivered. A dashboard of five green rows demonstrates nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from .models import (
    Artifact,
    Attempt,
    Criterion,
    Decision,
    Event,
    Finding,
    LogChunk,
    Lot,
    Plat,
    Transcript,
    Verdict,
)
from .models import Session as SessionRow

ORIGIN = "demo"
ANCHORS = ("DEMO-4021", "DEMO-3987")


def _now():
    return datetime.now(timezone.utc)


def clear(db) -> int:
    plats = db.scalars(select(Plat).where(Plat.origin_id == ORIGIN)).all()
    n = 0
    for p in plats:
        lots = db.scalars(select(Lot).where(Lot.plat_id == p.id)).all()
        for l in lots:
            att = db.scalars(select(Attempt).where(Attempt.lot_id == l.id)).all()
            for a in att:
                for m in (SessionRow, Verdict, Finding, Transcript, LogChunk):
                    db.execute(delete(m).where(m.attempt_id == a.id))
                db.execute(delete(Artifact).where(Artifact.attempt_id == a.id))
                db.delete(a)
                n += 1
        db.execute(delete(Decision).where(Decision.plat_id == p.id))
        db.execute(delete(Event).where(Event.plat_id == p.id))
        db.execute(delete(Criterion).where(Criterion.plat_id == p.id))
        for l in lots:
            db.delete(l)
        db.delete(p)
    db.commit()
    return n


def seed(db) -> str:
    clear(db)
    now = _now()

    # ---------------------------------------------------------------- live plat
    p = Plat(anchor="DEMO-4021", title="Tenant quotas are enforced per node, not per tenant",
             tickets=["DEMO-4022"], related=["DEMO-3901"], status="running",
             budget_usd=60.0, origin_id=ORIGIN, started_at=now - timedelta(minutes=64))
    db.add(p); db.flush()
    for ac, stmt, cmd in [
        ("AC1", "A quota is counted across every node, not per replica.",
         "pytest -q tests/unit/test_quota.py"),
        ("AC2", "Exceeding a quota returns 429 with a Retry-After the client can honour.",
         "pytest -q tests/unit/test_quota.py -k retry_after"),
        ("AC3", "Existing tenants keep their current effective limit on rollout.", None),
    ]:
        db.add(Criterion(plat_id=p.id, ac_id=ac, statement=stmt, verify_cmd=cmd))

    def lot(key, repo, state, attempt, hb_min, deps=()):
        l = Lot(plat_id=p.id, key=key, repo=repo, worktree_path=f"~/src/.worktrees/DEMO-4021/{key}",
                state=state, attempt=attempt, depends_on=list(deps),
                heartbeat_at=(now - timedelta(minutes=hb_min)) if hb_min is not None else None,
                base_ref="9f3c1ab", config={"gate": {"test": "pytest -q tests/unit"}})
        db.add(l); db.flush()
        return l

    def att(l, phase, n, provider, model, mins_ago, dur, cost, est=False,
            finished=True, resumed=False):
        a = Attempt(lot_id=l.id, phase=phase, n=n, provider=provider, model=model,
                    role_binding={"role": phase, "resumed": resumed},
                    cost_usd=cost, cost_estimated=est, tokens_in=41_000, tokens_out=3_100,
                    exit_code=0 if finished else None,
                    started_at=now - timedelta(minutes=mins_ago),
                    finished_at=(now - timedelta(minutes=mins_ago - dur)) if finished else None)
        db.add(a); db.flush()
        return a

    def dec(parent, actor, detail, kind, decision, why, alts=(), lot_id=None, mins=0):
        d = Decision(plat_id=p.id, lot_id=lot_id, parent_id=parent, actor=actor,
                     actor_detail=detail, kind=kind, decision=decision, rationale=why,
                     alternatives=list(alts), inputs={}, ts=now - timedelta(minutes=mins))
        db.add(d); db.flush()
        return d.id

    # 1 — working now: an in-flight attempt, warm heartbeat
    api = lot("api-gateway", "services/api-gateway", "CODING", 2, hb_min=0)
    att(api, "code", 1, "claude", "sonnet", 58, 9, 1.12)
    att(api, "gate", 1, "supervisor", "local", 49, 1, 0.0)
    r1 = att(api, "review", 1, "codex", "gpt-5.5", 47, 4, 0.61, est=True)
    db.add(Verdict(attempt_id=r1.id, verdict="changes_requested", raw={"verdict": "changes_requested"}))
    db.add(Finding(attempt_id=r1.id, origin_id=ORIGIN, anchor="DEMO-4021",
                   repo="services/api-gateway", fingerprint="quota:per-node-counter:check_quota",
                   severity="high", file="app/quota.py", line=74,
                   claim="The counter is process-local, so the limit is multiplied by the "
                         "replica count. Under autoscaling a tenant's effective quota "
                         "changes with load.",
                   required_fix="Count in the shared store, not in the worker."))
    live = att(api, "code", 2, "claude", "opus", 6, 0, 2.30, finished=False, resumed=True)
    for i, line in enumerate([
        "Reading app/quota.py and the limiter it wires into.",
        "The counter lives in a module-level dict — that is the per-node part.",
        "Moving the increment behind the shared store, keeping the local read as a fast path.",
        "Writing tests/unit/test_quota.py::test_counts_across_nodes first.",
    ]):
        db.add(LogChunk(attempt_id=live.id, seq=i, body=line + "\n",
                        ts=now - timedelta(minutes=5 - i)))
    d0 = dec(None, "agent", "codex/gpt-5.5", "verdict", "changes_requested · 1 finding",
             "the counter is process-local, so the limit scales with replicas",
             ["pass"], api.id, 43)
    d1 = dec(d0, "system", "fsm.retry", "transition",
             "code · attempt 2 · escalated to opus · resuming session",
             "changes_requested", (), api.id, 43)
    dec(d1, "agent", "claude/opus", "design_choice",
        "Count in the shared store and keep a local read as a fast path",
        "A purely shared counter adds a round trip to every request; the fast path keeps "
        "the common case local and only the increment authoritative.",
        ["count entirely in the shared store", "keep per-node counts and divide the limit"],
        api.id, 5)

    # 2 — wedged: heartbeat went cold 26 minutes ago
    web = lot("web-console", "apps/web-console", "REVIEWING", 1, hb_min=26)
    att(web, "code", 1, "claude", "sonnet", 40, 11, 0.94)
    att(web, "gate", 1, "supervisor", "local", 28, 1, 0.0)
    att(web, "review", 1, "codex", "gpt-5.5", 26, 0, 0.44, est=True, finished=False)
    dec(None, "system", "gates.run", "transition", "gate passed - 4 files, 0 failing",
        "the supervisor ran the tests; the agent's claim is not consulted", (), web.id, 27)

    # 3 — stopped for a human
    bill = lot("billing-service", "services/billing-service", "BLOCKED", 3, hb_min=12)
    att(bill, "code", 3, "claude", "opus", 21, 7, 3.05)
    att(bill, "gate", 3, "supervisor", "local", 13, 1, 0.0)
    r3 = att(bill, "review", 3, "codex", "gpt-5.5", 12, 3, 0.58, est=True)
    db.add(Verdict(attempt_id=r3.id, verdict="blocked", raw={"verdict": "blocked"}))
    db.add(Finding(attempt_id=r3.id, origin_id=ORIGIN, anchor="DEMO-4021",
                   repo="services/billing-service",
                   fingerprint="quota:proration-on-downgrade:apply_plan_change",
                   severity="high", file="app/plans.py", line=132,
                   claim="Mid-cycle downgrades are prorated against the NEW limit, so a "
                         "tenant who already exceeded it is billed for overage they "
                         "cannot now avoid.",
                   required_fix="A product decision, not a code one."))
    db.add(Event(plat_id=p.id, lot_id=bill.id, kind="decision.transition",
                 message="BLOCKED — reviewer raised a question only a human can answer",
                 ts=now - timedelta(minutes=9)))
    dec(None, "system", "fsm.block", "transition", "BLOCKED - needs a human",
        "reviewer blocked: proration on a mid-cycle downgrade is a product decision",
        (), bill.id, 9)

    # 4 — waiting on a dependency
    lot("deploy-manifests", "infra/deploy-manifests", "PENDING", 0, hb_min=None,
        deps=["api-gateway"])

    # 5 — finished cleanly, so the contrast is visible
    docs = lot("quota-docs", "docs/handbook", "DONE", 1, hb_min=31)
    att(docs, "code", 1, "claude", "sonnet", 35, 4, 0.38)
    att(docs, "gate", 1, "supervisor", "local", 30, 1, 0.0)
    rd = att(docs, "review", 1, "codex", "gpt-5.5", 29, 2, 0.31, est=True)
    db.add(Verdict(attempt_id=rd.id, verdict="pass", raw={"verdict": "pass"}))

    for m, msg in [(58, "code started · claude/sonnet"),
                   (49, "gate passed - 6 files, 0 failing"),
                   (43, "review → changes_requested · 1 finding(s)"),
                   (43, "attempt 2 · resuming session · escalated to opus"),
                   (31, "quota-docs lot DONE"),
                   (27, "web-console gate passed - 4 files, 0 failing"),
                   (9,  "billing-service BLOCKED — needs a human")]:
        db.add(Event(plat_id=p.id, kind="demo", message=msg, ts=now - timedelta(minutes=m)))

    # ------------------------------------------------------------ delivered plat
    q = Plat(anchor="DEMO-3987", title="Retry storms after a broker failover",
             tickets=["DEMO-3988"], related=[], status="closed", budget_usd=40.0,
             origin_id=ORIGIN, started_at=now - timedelta(days=3),
             closed_at=now - timedelta(days=2))
    db.add(q); db.flush()
    ql = Lot(plat_id=q.id, key="event-relay", repo="services/event-relay", state="DONE",
             attempt=2, worktree_path="~/src/.worktrees/DEMO-3987/event-relay",
             heartbeat_at=now - timedelta(days=2), depends_on=[], config={})
    db.add(ql); db.flush()
    for phase, n, prov, model, cost, est in [("code", 1, "claude", "sonnet", 0.71, False),
                                             ("gate", 1, "supervisor", "local", 0.0, False),
                                             ("review", 1, "codex", "gpt-5.5", 0.49, True),
                                             ("code", 2, "claude", "opus", 2.11, False),
                                             ("review", 2, "codex", "gpt-5.5", 0.53, True)]:
        a = Attempt(lot_id=ql.id, phase=phase, n=n, provider=prov, model=model,
                    cost_usd=cost, cost_estimated=est, exit_code=0, role_binding={},
                    started_at=now - timedelta(days=3), finished_at=now - timedelta(days=3))
        db.add(a); db.flush()
        if phase == "review" and n == 1:
            db.add(Finding(attempt_id=a.id, origin_id=ORIGIN, anchor="DEMO-3987",
                           repo="services/event-relay", fingerprint="relay:jitterless-backoff:reconnect",
                           severity="high", file="app/relay.py", line=58,
                           claim="Backoff is exponential but has no jitter, so every consumer "
                                 "reconnects on the same schedule and the storm repeats.",
                           required_fix="Add full jitter.", resolved_in_attempt_id=a.id + 2))
    db.commit()
    return f"seeded {', '.join(ANCHORS)}"
