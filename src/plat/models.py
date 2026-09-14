"""The system of record. Disk is scratch; this is the plat.

Every table here outlives the worktree it describes. `/mlg-worktree-down` must be
able to delete a checkout without destroying any record of how the work was done.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any
from sqlalchemy import (
    String, Text, Integer, Float, Boolean, DateTime, ForeignKey, Index, func,
)
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Plat(Base):
    __tablename__ = "plats"
    id: Mapped[int] = _pk()
    anchor: Mapped[str] = mapped_column(String(32), index=True)       # DEV-1234
    tickets: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)   # members
    related: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)   # context only
    title: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="planned")
    # planned | running | paused | closing | recording | signoff | closed | aborted
    budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    origin_id: Mapped[str] = mapped_column(String(64))   # per-install; keeps corpora mergeable
    started_at: Mapped[datetime] = _now()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lots: Mapped[list["Lot"]] = relationship(back_populates="plat")


class Lot(Base):
    """One unit of work: one agent, one worktree, one session at a time."""
    __tablename__ = "lots"
    id: Mapped[int] = _pk()
    plat_id: Mapped[int] = mapped_column(ForeignKey("plats.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))          # usually the repo name
    repo: Mapped[str] = mapped_column(String(128))
    worktree_path: Mapped[str] = mapped_column(Text)      # points INTO the workspace
    state: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    depends_on: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    human_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # diff base
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)      # gate cmds, plan

    plat: Mapped[Plat] = relationship(back_populates="lots")


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = _pk()
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), index=True)
    phase: Mapped[str] = mapped_column(String(16))        # code|review|docs|quality
    n: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(16))     # claude|codex|gemini
    model: Mapped[str] = mapped_column(String(64))
    role_binding: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Session(Base):
    """Provider session ids, so attempt 2 can resume the coder's own session.

    Rule: resume the coder, always cold-start the reviewer. Cold-start the coder
    again at attempt 3 -- by then the session carries a wrong mental model.
    """
    __tablename__ = "sessions"
    id: Mapped[int] = _pk()
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    provider_session_id: Mapped[str] = mapped_column(String(128))
    resumable_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Verdict(Base):
    __tablename__ = "verdicts"
    id: Mapped[int] = _pk()
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"), index=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(24), nullable=True)
    tests_passed: Mapped[int] = mapped_column(Integer, default=0)
    tests_failed: Mapped[int] = mapped_column(Integer, default=0)


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[int] = _pk()
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"), index=True)
    origin_id: Mapped[str] = mapped_column(String(64))   # with (anchor,repo,fingerprint):
    anchor: Mapped[str] = mapped_column(String(32))      #   a stable cross-install identity,
    repo: Mapped[str] = mapped_column(String(128))       #   so corpora can be unioned later
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    file: Mapped[str] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claim: Mapped[str] = mapped_column(Text)
    required_fix: Mapped[str] = mapped_column(Text, default="")
    resolved_in_attempt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # embedding vector(N) -- added in v2 via pgvector. A DISMISSED finding with a
    # reason is the highest-value prior-art signal there is.


class Criterion(Base):
    """An acceptance criterion that cannot be checked cannot gate a state machine."""
    __tablename__ = "criteria"
    id: Mapped[int] = _pk()
    plat_id: Mapped[int] = mapped_column(ForeignKey("plats.id"), index=True)
    lot_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ac_id: Mapped[str] = mapped_column(String(16))
    statement: Mapped[str] = mapped_column(Text)
    verify_cmd: Mapped[str | None] = mapped_column(Text, nullable=True)
    met_in_attempt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Artifact(Base):
    """Plan snapshots, prompts, and agent narrative, ingested write-through.

    Plan snapshots are taken at plat-start: the UI must show what the plat map WAS
    when the run began, not what it has been edited into since.
    """
    __tablename__ = "artifacts"
    id: Mapped[int] = _pk()
    plat_id: Mapped[int] = mapped_column(ForeignKey("plats.id"), index=True)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("attempts.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))   # plat_map|lot_desc|prompt|code|review|docs
    name: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()


class Transcript(Base):
    """Separate table on purpose: multi-MB, never wanted in a routine query."""
    __tablename__ = "transcripts"
    id: Mapped[int] = _pk()
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)


class LogChunk(Base):
    """The one exception to batch-on-completion: live tailing needs increments.

    Batched every few seconds or few KB. NEVER per token.
    """
    __tablename__ = "log_chunks"
    id: Mapped[int] = _pk()
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = _now()


class Decision(Base):
    """Why it went this way. One table, three actors, a tree via parent_id.

    'system' decisions are OBSERVED -- the supervisor knows them for certain.
    'agent' decisions are SELF-REPORTED and epistemically weaker; render them
    differently and never with equal confidence.
    `alternatives` must be non-empty: if nothing else was on the table, it was not
    a decision, it was just doing the work.
    """
    __tablename__ = "decisions"
    id: Mapped[int] = _pk()
    plat_id: Mapped[int] = mapped_column(ForeignKey("plats.id"), index=True)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), nullable=True, index=True)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("attempts.id"), nullable=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(8))        # system | agent | human
    actor_detail: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    # transition | design_choice | verdict | override | answer | signoff | dismissal
    decision: Mapped[str] = mapped_column(Text)
    alternatives: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)
    ts: Mapped[datetime] = _now()


class Event(Base):
    """Append-only. A trigger NOTIFYs on insert; that is the whole live-update path."""
    __tablename__ = "events"
    id: Mapped[int] = _pk()
    plat_id: Mapped[int] = mapped_column(ForeignKey("plats.id"), index=True)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ts: Mapped[datetime] = _now()


Index("ix_findings_identity", Finding.origin_id, Finding.anchor,
      Finding.repo, Finding.fingerprint)
