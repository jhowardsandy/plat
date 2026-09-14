"""The ONLY mutation path.

No CLI command and no task writes a bare UPDATE. Every change to a plat goes
through record(), which writes the decision row and applies the change in ONE
transaction -- so it is structurally impossible to change a plat without leaving
a record of who decided, what else was on the table, and what they were looking at.
"""
from __future__ import annotations
from typing import Any, Callable
from sqlalchemy.orm import Session as DbSession
from .models import Decision, Event


def record(
    db: DbSession,
    *,
    plat_id: int,
    actor: str,                      # system | agent | human
    actor_detail: str,               # fsm rule name | provider/model | user
    kind: str,
    decision: str,
    inputs: dict[str, Any],
    alternatives: list[Any] | None = None,
    rationale: str = "",
    lot_id: int | None = None,
    attempt_id: int | None = None,
    parent_id: int | None = None,
    reversible: bool = True,
    apply: Callable[[], None] | None = None,
) -> Decision:
    if actor == "agent" and not alternatives:
        raise ValueError(
            "an agent decision with no alternatives is not a decision, it is just "
            "doing the work -- do not record it"
        )
    d = Decision(
        plat_id=plat_id, lot_id=lot_id, attempt_id=attempt_id, parent_id=parent_id,
        actor=actor, actor_detail=actor_detail, kind=kind, decision=decision,
        alternatives=alternatives or [], rationale=rationale, inputs=inputs,
        reversible=reversible,
    )
    db.add(d)
    if apply is not None:
        apply()
    db.add(Event(plat_id=plat_id, lot_id=lot_id, kind=f"decision.{kind}",
                 message=decision, payload={"actor": actor, "detail": actor_detail}))
    db.flush()
    return d
