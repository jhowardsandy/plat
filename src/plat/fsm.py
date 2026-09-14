"""The rules. Pure functions, no I/O, no LLM anywhere in this file.

If a routing decision needs judgement, it does NOT belong here -- it goes to a
`router` role: a cheap model asked ONE narrow question returning an enum.
Never "read this and decide what to do."
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import operator
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

# converge defaults
PATIENCE = 2            # iterations without improvement before stopping
MAX_ITERATIONS = 8      # hard ceiling regardless of progress


class Mode(StrEnum):
    ATTEMPT = "attempt"     # code -> gate -> review, capped at MAX_ATTEMPTS
    CONVERGE = "converge"   # iterate toward a measured objective


_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt,
        "<": operator.lt, "==": operator.eq}


def parse_objective(spec: str):
    """'>= 80' -> (ge, 80.0). The direction matters: coverage climbs, a backlog
    of unmigrated call sites falls, and 'improvement' means the opposite thing."""
    spec = spec.strip()
    for sym, op in _OPS.items():
        if spec.startswith(sym):
            return op, float(spec[len(sym):].strip())
    raise ValueError(f"objective must start with one of {sorted(_OPS)}: {spec!r}")


def improving(history: tuple[float, ...], op) -> bool:
    """Is the last reading better than the best before it?

    'Better' follows the objective's direction, so a falling backlog counts as
    progress exactly as a rising coverage figure does.
    """
    if len(history) < 2:
        return True
    best_before = (max if op in (operator.ge, operator.gt) else min)(history[:-1])
    return op(history[-1], best_before) and history[-1] != best_before

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


ALL_PHASES = frozenset({"code", "review", "docs", "quality"})


@dataclass(frozen=True)
class Ctx:
    """Everything decide() is allowed to look at. Assembled by the caller."""
    deps_met: bool
    mode: Mode = Mode.ATTEMPT
    objective: str | None = None
    progress: tuple[float, ...] = ()        # one reading per completed gate
    patience: int = PATIENCE
    max_iterations: int = MAX_ITERATIONS
    # Which phases this plat runs. v0 ships code+review; docs and quality arrive
    # with their providers. A phase with no configured role must not be reachable.
    phases: frozenset[str] = ALL_PHASES
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
    if ctx.mode == Mode.CONVERGE:
        return _converge(state, attempt, ctx)

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
                return _after("review", ctx.phases, attempt, "review passed",
                              {"verdict": v})
            return _retry(attempt, ctx, why="changes_requested",
                          extra={"findings": len(fingerprints(v))})

        case LotState.DOCS:
            return _after("docs", ctx.phases, attempt, "docs complete", {})

        case LotState.QUALITY:
            if ctx.verdict and ctx.verdict.get("verdict") == "pass":
                return Next("close", LotState.DONE, reason="lot complete")
            return _retry(attempt, ctx, why="quality review requested changes")

        case LotState.DONE | LotState.ABORTED:
            return Next("noop", reason="terminal")

        case LotState.BLOCKED:
            return Next("wait", reason="awaiting a human")

    return Next("noop", reason="unreachable")


_ORDER = ["code", "review", "docs", "quality"]


def _after(done: str, phases: frozenset[str], attempt: int, why: str,
           inputs: dict) -> Next:
    """The next ENABLED phase after `done`, or close the lot if there is none."""
    state_of = {"docs": LotState.DOCS, "quality": LotState.QUALITY,
                "review": LotState.REVIEWING}
    for nxt in _ORDER[_ORDER.index(done) + 1:]:
        if nxt in phases:
            return Next("agent", state_of[nxt], phase=nxt, role=PHASE_ROLE[nxt],
                        attempt=attempt, resume_session=False, reason=why,
                        inputs=inputs)
    return Next("close", LotState.DONE, reason=f"{why}; no further phases enabled")


def _converge(state: LotState, n: int, ctx: Ctx) -> Next:
    """Iterate toward a measured objective.

    The stopping condition is the METRIC, not an attempt cap: "migrate 40 call
    sites" may take nine passes or two, and capping at three would abandon it
    mid-way while capping at nothing would grind forever on a stuck problem.
    """
    op, target = parse_objective(ctx.objective or ">= 1")

    match state:
        case LotState.PENDING:
            if not ctx.deps_met:
                return Next("wait", reason="dependencies not satisfied")
            return _code(1, resume=False, why="converge: first pass")

        case LotState.CODING:
            return Next("gate", LotState.GATE,
                        reason="iteration finished; measuring independently")

        case LotState.GATE:
            g = ctx.gate or {}
            if not gate_passed(g):
                return _retry(n, ctx, why="gate failed", extra={"gate": g})
            # Guard the metric being optimised. An agent told to raise coverage
            # can do it by deleting the tests that fail; the number goes up and
            # the suite stays green. A dropped test count is not progress.
            if g.get("tests_passed", 0) < g.get("baseline_passed", 0):
                return Next("human", LotState.BLOCKED,
                            reason=f"test count fell from {g.get('baseline_passed')} "
                                   f"to {g.get('tests_passed')} — the metric is being "
                                   f"gamed, not met",
                            inputs={"gate": g})
            reading = ctx.progress[-1] if ctx.progress else None
            if reading is None:
                return Next("human", LotState.BLOCKED,
                            reason="probe produced no reading; a converge lot "
                                   "cannot be steered without one")
            if op(reading, target):
                # the next ENABLED phase after code -- i.e. review it, do not
                # skip past review to docs
                return _after("code", ctx.phases, n,
                              f"objective met: {reading} {ctx.objective}",
                              {"progress": list(ctx.progress)})
            if n >= ctx.max_iterations:
                return Next("human", LotState.BLOCKED,
                            reason=f"{n} iterations, still {reading} (want {ctx.objective})",
                            inputs={"progress": list(ctx.progress)})
            recent = ctx.progress[-(ctx.patience + 1):]
            if len(recent) > ctx.patience and not improving(recent, op):
                return Next("human", LotState.BLOCKED,
                            reason=f"no improvement in {ctx.patience} iterations "
                                   f"(stuck at {reading}, want {ctx.objective})",
                            inputs={"progress": list(ctx.progress)})
            return _code(n + 1, resume=True,
                         why=f"{reading} → target {ctx.objective}",
                         extra={"progress": list(ctx.progress)})

        case LotState.REVIEWING:
            v = ctx.verdict
            if v and v.get("verdict") == "pass":
                return Next("close", LotState.DONE, reason="objective met and reviewed")
            if v and v.get("verdict") == "blocked":
                return Next("human", LotState.BLOCKED, reason="reviewer blocked")
            return _code(n + 1, resume=True, why="review requested changes")

        case LotState.DOCS | LotState.QUALITY:
            return _after(state.value.lower(), ctx.phases, n, "phase complete", {})

    return Next("noop", reason="terminal")


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
