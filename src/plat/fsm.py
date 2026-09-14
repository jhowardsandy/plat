"""The rules. Pure functions, no I/O, no LLM anywhere in this file.

If a routing decision needs judgement, it does NOT belong here -- it goes to a
`router` role: a cheap model asked ONE narrow question returning an enum.
Never "read this and decide what to do."
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal


class LotState(StrEnum):
    PENDING   = "PENDING"
    CODING    = "CODING"
    GATE      = "GATE"        # run by the SUPERVISOR, not an agent (Invariant II)
    REVIEWING = "REVIEWING"
    DOCS      = "DOCS"
    QUALITY   = "QUALITY"
    DONE      = "DONE"
    BLOCKED   = "BLOCKED"     # waiting on a human
    ABORTED   = "ABORTED"


class PlatState(StrEnum):
    PLANNED   = "planned"
    RUNNING   = "running"
    PAUSED    = "paused"
    CLOSING   = "closing"     # closing the traverse
    RECORDING = "recording"
    SIGNOFF   = "signoff"     # hard stop. a human, always.
    CLOSED    = "closed"
    ABORTED   = "aborted"


MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(minutes=10)

PHASE_ROLE = {
    "code":    "coder",
    "review":  "reviewer.correctness",
    "docs":    "documenter",
    "quality": "reviewer.quality",
}


@dataclass(frozen=True)
class Next:
    kind: Literal["agent", "gate", "human", "wait", "close", "noop"]
    state: LotState | None = None
    phase: str | None = None
    role: str | None = None
    attempt: int | None = None
    resume_session: bool = False
    reason: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Ctx:
    """Everything decide() is allowed to look at. Assembled by the caller."""
    deps_met: bool
    verdict: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    prev_fingerprints: frozenset[str] = frozenset()
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    heartbeat_at: datetime | None = None
    budget_exhausted: bool = False
    paused: bool = False


# ---------------------------------------------------------------- predicates

def gate_passed(gate: dict[str, Any] | None) -> bool:
    """Invariant II. The supervisor ran this; the agent's opinion is not consulted."""
    if not gate:
        return False
    return gate.get("tests_failed", 1) == 0 and gate.get("diff_files", 0) > 0


def criteria_met(verdict: dict[str, Any] | None) -> bool:
    if not verdict:
        return False
    checks = verdict.get("criteria_check", [])
    return bool(checks) and all(c.get("met") for c in checks)


def advance(verdict: dict[str, Any] | None, gate: dict[str, Any] | None) -> bool:
    return (
        bool(verdict)
        and verdict.get("verdict") == "pass"
        and criteria_met(verdict)
        and gate_passed(gate)
    )


def fingerprints(verdict: dict[str, Any] | None) -> frozenset[str]:
    if not verdict:
        return frozenset()
    return frozenset(f["fingerprint"] for f in verdict.get("findings", []) if "fingerprint" in f)


def oscillating(verdict, prev: frozenset[str]) -> frozenset[str]:
    """The same finding twice means coder and reviewer are talking past each other.
    A third attempt will not fix that. Escalate to a human instead."""
    return fingerprints(verdict) & prev


def is_stale(heartbeat_at: datetime | None, now: datetime) -> bool:
    return heartbeat_at is not None and (now - heartbeat_at) > STALE_AFTER


# ---------------------------------------------------------------- the machine

def decide(state: LotState, attempt: int, ctx: Ctx) -> Next:
    if ctx.paused:
        return Next("noop", reason="plat paused")
    if ctx.budget_exhausted:
        return Next("human", LotState.BLOCKED, reason="budget ceiling reached")

    match state:
        case LotState.PENDING:
            if not ctx.deps_met:
                return Next("wait", reason="dependencies not satisfied")
            return _code(1, resume=False, why="dependencies satisfied")

        case LotState.CODING:
            return Next("gate", LotState.GATE, reason="coder finished; verifying independently")

        case LotState.GATE:
            if gate_passed(ctx.gate):
                return Next("agent", LotState.REVIEWING, phase="review",
                            role=PHASE_ROLE["review"], attempt=attempt,
                            resume_session=False,          # reviewers are ALWAYS cold
                            reason="gate passed", inputs={"gate": ctx.gate})
            return _retry(attempt, ctx, why="gate failed", extra={"gate": ctx.gate})

        case LotState.REVIEWING:
            v = ctx.verdict
            if v and v.get("verdict") == "blocked":
                return Next("human", LotState.BLOCKED, reason="reviewer blocked",
                            inputs={"verdict": v})
            repeated = oscillating(v, ctx.prev_fingerprints)
            if repeated:
                return Next("human", LotState.BLOCKED,
                            reason=f"oscillation: {sorted(repeated)} raised twice",
                            inputs={"repeated": sorted(repeated)})
            if advance(v, ctx.gate):
                return Next("agent", LotState.DOCS, phase="docs",
                            role=PHASE_ROLE["docs"], attempt=attempt,
                            reason="review passed", inputs={"verdict": v})
            return _retry(attempt, ctx, why="changes_requested",
                          extra={"findings": len(fingerprints(v))})

        case LotState.DOCS:
            return Next("agent", LotState.QUALITY, phase="quality",
                        role=PHASE_ROLE["quality"], attempt=attempt,
                        resume_session=False, reason="docs complete")

        case LotState.QUALITY:
            if ctx.verdict and ctx.verdict.get("verdict") == "pass":
                return Next("close", LotState.DONE, reason="lot complete")
            return _retry(attempt, ctx, why="quality review requested changes")

        case LotState.DONE | LotState.ABORTED:
            return Next("noop", reason="terminal")

        case LotState.BLOCKED:
            return Next("wait", reason="awaiting a human")

    return Next("noop", reason="unreachable")


def _code(attempt: int, resume: bool, why: str, extra: dict | None = None) -> Next:
    return Next("agent", LotState.CODING, phase="code", role=PHASE_ROLE["code"],
                attempt=attempt, resume_session=resume, reason=why,
                inputs=extra or {})


def _retry(attempt: int, ctx: Ctx, why: str, extra: dict | None = None) -> Next:
    nxt = attempt + 1
    if nxt > MAX_ATTEMPTS:
        return Next("human", LotState.BLOCKED,
                    reason=f"{why}; {MAX_ATTEMPTS} attempts exhausted",
                    inputs=extra or {})
    # resume the coder's own session on attempt 2 -- it keeps the context for WHY
    # it made those choices. By attempt 3 that context is a liability: start cold.
    return _code(nxt, resume=(nxt == 2), why=why, extra=extra)
