"""The system of record. Disk is scratch; this is the plat.

Every table here outlives the worktree it describes: deleting a checkout must
never destroy the record of how the work in it was done.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    public_id: Mapped[str] = mapped_column(String(36), unique=True,
                                         default=lambda: str(uuid4()))


def _pk() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Plat(Base):
    __tablename__ = "plats"
    id: Mapped[int] = _pk()
    # Wide enough for a ticket key AND a review anchor (review/<repo>@<branch>)
    anchor: Mapped[str] = mapped_column(String(160), index=True)
    tickets: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)   # members
    related: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)   # context only
    title: Mapped[str] = mapped_column(Text, default="")
    # 'plat' is orchestrated work; 'review' is a single-shot review of a branch
    # someone already wrote. Same tables so findings join one corpus; separate
    # kind so the ledger is not drowned by them.
    kind: Mapped[str] = mapped_column(String(8), default="plat", index=True)
    status: Mapped[str] = mapped_column(String(16), default="planned")
    # planned | running | paused | closing | recording | signoff | closed | aborted
    budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    origin_id: Mapped[str] = mapped_column(String(64))   # per-install; keeps corpora mergeable
    # What the ISSUE TRACKER says, cached. Plat's own lifecycle (delivered/closed)
    # and the ticket's are different facts and neither implies the other.
    ticket_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ticket_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = _now()
    # When PLAT finished its work and handed over. Set by the supervisor.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                          nullable=True)
    # When a HUMAN declared it actually shipped. Only ever set by `plat close`.
    # Conflating these made every finished plat read as done when none had merged.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                       nullable=True)

    lots: Mapped[list["Lot"]] = relationship(back_populates="plat")


class Lot(Base):
    """One unit of work: one agent, one worktree, one session at a time."""
    __tablename__ = "lots"
    __table_args__ = (UniqueConstraint("plat_id", "key", name="uq_lot_key"),)
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
    # Codex reports tokens, never dollars. A budget must never be enforced against
    # a number that merely looks authoritative, so the estimate is marked as one.
    cost_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Non-zero means the agent was BLOCKED, not merely unproductive -- the failure
    # that otherwise reads as "it did nothing useful". Persisted, not just logged.
    permission_denials: Mapped[int] = mapped_column(Integer, default=0)
    # The agent subprocess. It is started in its OWN session, so closing the
    # terminal that launched it does not kill it -- which is good (no half-written
    # files) and bad (it keeps running, unwatched, against a lot whose heartbeat
    # has gone cold). A later session needs this to tell those apart.
    executor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    host_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    anchor: Mapped[str] = mapped_column(String(160))     #   a stable cross-install identity,
    repo: Mapped[str] = mapped_column(String(128))       #   so corpora can be unioned later
    fingerprint: Mapped[str] = mapped_column(Text)
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
    scope: Mapped[str] = mapped_column(String(8), default="lot", server_default="lot")
    verify_in: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    plat_id: Mapped[int | None] = mapped_column(ForeignKey("plats.id"), index=True, nullable=True)
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
    plat_id: Mapped[int | None] = mapped_column(ForeignKey("plats.id"), index=True, nullable=True)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ts: Mapped[datetime] = _now()


# Full fingerprints remain authoritative. Digest/hash indexes avoid btree entry
# limits for long agent-generated identifiers; neither index enforces uniqueness.
Index("ix_findings_identity", Finding.origin_id, Finding.anchor,
      Finding.repo, func.md5(Finding.fingerprint))
Index("ix_findings_fingerprint", Finding.fingerprint, postgresql_using="hash")


class ExecutionClaim(Base):
    __tablename__ = "execution_claims"
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), primary_key=True)
    resource: Mapped[str] = mapped_column(Text)
    executor_id: Mapped[str] = mapped_column(String(64))
    host_id: Mapped[str] = mapped_column(String(64))
    token: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now()


Index("uq_active_resource", ExecutionClaim.resource, unique=True,
      postgresql_where=ExecutionClaim.active.is_(True))


class Workflow(Base):
    __tablename__ = "workflows"
    plat_id: Mapped[int] = mapped_column(ForeignKey("plats.id"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(24), default="context")
    version: Mapped[int] = mapped_column(Integer, default=0)
    spec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner: Mapped[str] = mapped_column(String(128))
    requirements: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = _now()


class StageRecord(Base):
    __tablename__ = "stage_records"
    id: Mapped[int] = _pk()
    plat_id: Mapped[int] = mapped_column(ForeignKey("plats.id"), index=True)
    stage: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _now()


class TrackedSession(Base):
    __tablename__ = "tracked_sessions"
    __table_args__ = (UniqueConstraint("installation", "provider", "session_key",
                                      name="uq_session_identity"),)
    id: Mapped[int] = _pk()
    plat_id: Mapped[int] = mapped_column(ForeignKey("plats.id"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), index=True)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("attempts.id"), nullable=True)
    installation: Mapped[str] = mapped_column(String(64))
    host_id: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(16))
    session_key: Mapped[str] = mapped_column(String(128))
    provider_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    capabilities: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_activity: Mapped[datetime] = _now()


class CollaborationItem(Base):
    __tablename__ = "collaboration_items"
    id: Mapped[int] = _pk()
    plat_id: Mapped[int] = mapped_column(ForeignKey("plats.id"), index=True)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    owner: Mapped[str] = mapped_column(String(128))
    author: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), default="open")
    version: Mapped[int] = mapped_column(Integer, default=1)
    body: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class TeamMember(Base):
    __tablename__ = 'team_members'
    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now()


class TeamInvitation(Base):
    __tablename__ = 'team_invitations'
    id: Mapped[int] = _pk()
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    issuer: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    member_id: Mapped[int | None] = mapped_column(ForeignKey('team_members.id'), nullable=True)
    created_at: Mapped[datetime] = _now()


class RepositoryCatalog(Base):
    __tablename__ = 'repository_catalog'
    identity: Mapped[str] = mapped_column(Text, primary_key=True)
    products: Mapped[list] = mapped_column(JSONB, default=list)
    services: Mapped[list] = mapped_column(JSONB, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)


class SavedView(Base):
    __tablename__ = 'saved_views'
    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(80))
    owner: Mapped[str] = mapped_column(String(128))
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict] = mapped_column(JSONB)
