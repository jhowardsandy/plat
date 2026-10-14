"""Durable claims plus transaction fencing. Expiry never licenses another writer."""
from __future__ import annotations

import hashlib
import os
import platform
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select, text

from .models import Attempt, ExecutionClaim, Lot, Plat, TrackedSession

current = ContextVar('execution_claim', default=None)


class OwnershipError(RuntimeError):
    pass


def host_id():
    # Host identity is independent of PLAT_HOME / database / installation.
    import uuid
    return hashlib.sha256(f'{platform.node()}:{uuid.getnode()}'.encode()).hexdigest()


def actor():
    import getpass
    return os.environ.get('PLAT_ACTOR') or getpass.getuser()


def _lock(db, key):
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': key})


def lock_resource(db, lot):
    """Serialize automated and interactive writers, including different lot records."""
    db.execute(select(Plat.id).where(Plat.id == lot.plat_id).with_for_update())
    host = host_id()
    resource = f'{host}:{Path(lot.worktree_path).resolve()}'
    _lock(db, resource)
    _lock(db, f'lot:{lot.id}')
    db.execute(select(Lot.id).where(Lot.id == lot.id).with_for_update())
    db.refresh(lot)
    if resource != f'{host}:{Path(lot.worktree_path).resolve()}':
        raise OwnershipError('worktree assignment changed; retry against its current location')
    return resource


def interactive_writers(db, lot):
    candidates = db.execute(select(TrackedSession, Lot.worktree_path).join(Lot).where(
        TrackedSession.source == 'interactive',
        TrackedSession.state.in_(['active', 'idle', 'unknown', 'interrupted']),
        (TrackedSession.lot_id == lot.id) | (TrackedSession.host_id == host_id()))).all()
    path = Path(lot.worktree_path).resolve()
    return [session for session, worktree in candidates
            if session.lot_id == lot.id or Path(worktree).resolve() == path]


def acquire(db, cfg, plat, lot):
    from .decisions import record
    host = host_id()
    resource = lock_resource(db, lot)
    configured = lot.config or {}
    if configured.get('executor_id') and configured['executor_id'] != cfg.origin_id:
        raise OwnershipError('lot belongs to another installation; localize a pending lot before execution')
    if configured.get('host_id') and configured['host_id'] != host:
        raise OwnershipError('lot belongs to another host; paths and PIDs are not portable')
    claim = db.get(ExecutionClaim, lot.id, populate_existing=True)
    other = db.scalars(select(ExecutionClaim).where(
        ExecutionClaim.resource == resource, ExecutionClaim.active.is_(True))).first()
    if (claim and claim.active) or other:
        raise OwnershipError('execution already claimed; expired claims require reconciliation on the owning host')
    if interactive_writers(db, lot):
        raise OwnershipError('an attached interactive session must be completed or failed before automated execution')
    now = db.scalar(select(func.clock_timestamp()))
    if not claim:
        claim = ExecutionClaim(lot_id=lot.id, generation=0)
    def apply():
        claim.resource, claim.host_id, claim.executor_id = resource, host, cfg.origin_id
        claim.generation += 1
        claim.token, claim.active = str(uuid4()), True
        claim.expires_at, claim.updated_at = now + timedelta(seconds=90), now
        db.add(claim)
    record(db, plat_id=plat.id, lot_id=lot.id, actor='system', actor_detail='ownership.acquire',
           kind='claim', decision='execution claimed', inputs={'executor': cfg.origin_id, 'host': host},
           apply=apply)
    db.commit()
    return (lot.id, claim.token, claim.generation, cfg.origin_id, host)


def fence(db):
    info = current.get()
    if info is None:
        return
    with db.no_autoflush:
        plat_id = db.scalar(select(Lot.plat_id).where(Lot.id == info[0]))
        db.execute(select(Plat.id).where(Plat.id == plat_id).with_for_update())
        db.execute(select(Lot.id).where(Lot.id == info[0]).with_for_update())
        claim = db.scalars(select(ExecutionClaim).where(ExecutionClaim.lot_id == info[0])
                           .with_for_update().execution_options(populate_existing=True)).one()
    if not claim.active or claim.token != info[1] or claim.generation != info[2]:
        raise OwnershipError('execution ownership changed; this result cannot be committed')
    if claim.expires_at <= db.scalar(select(func.clock_timestamp())):
        raise OwnershipError('execution claim expired; reconcile before retrying')


def renew(db, info):
    # Same lot-before-claim order as fencing, so a heartbeat cannot deadlock a result.
    db.execute(select(Lot.id).where(Lot.id == info[0]).with_for_update())
    claim = db.scalars(select(ExecutionClaim).where(ExecutionClaim.lot_id == info[0])
                       .with_for_update()).one()
    if not claim.active or claim.token != info[1] or claim.generation != info[2]:
        raise OwnershipError('execution ownership lost')
    if claim.expires_at <= db.scalar(select(func.clock_timestamp())):
        raise OwnershipError('expired execution cannot renew; reconcile before retrying')
    claim.expires_at = db.scalar(select(func.clock_timestamp())) + timedelta(seconds=90)
    claim.updated_at = datetime.now(timezone.utc)


def release(db, plat, info):
    from .decisions import record
    db.rollback()
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    claim = db.scalars(select(ExecutionClaim).where(ExecutionClaim.lot_id == info[0])
                       .with_for_update()).one()
    if claim.active and claim.token == info[1] and claim.generation == info[2]:
        # Preserve a claim while a subprocess may still write after an exception.
        unfinished = db.scalars(select(Attempt).where(Attempt.lot_id == info[0],
            Attempt.finished_at.is_(None), Attempt.pid.isnot(None))).first()
        if unfinished:
            return
        record(db, plat_id=plat.id, lot_id=info[0], actor='system', actor_detail='ownership.release',
               kind='claim', decision='execution released', inputs={'generation': info[2]},
               apply=lambda: setattr(claim, 'active', False))
        db.commit()


@contextmanager
def execution(db, cfg, plat, lot):
    info = acquire(db, cfg, plat, lot)
    token = current.set(info)
    try:
        yield info
    finally:
        current.reset(token)
        release(db, plat, info)


def reconcile(db, cfg, plat, lot, note):
    from .adapters.base import process_alive
    from .decisions import record
    if not note.strip():
        raise OwnershipError('a reconciliation note is required')
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    _lock(db, f'lot:{lot.id}')
    db.execute(select(Lot.id).where(Lot.id == lot.id).with_for_update())
    claim = db.scalars(select(ExecutionClaim).where(ExecutionClaim.lot_id == lot.id)
                       .with_for_update()).first()
    if not claim or not claim.active:
        raise OwnershipError('no active claim to reconcile')
    if claim.host_id != host_id() or claim.executor_id != cfg.origin_id:
        raise OwnershipError('reconcile on the owning installation; remote PIDs are never inspected')
    if claim.expires_at > db.scalar(select(func.clock_timestamp())):
        raise OwnershipError('claim is still live; stop the supervisor and wait for its lease to expire')
    pending = db.scalars(select(Attempt).where(Attempt.lot_id == lot.id,
                                             Attempt.finished_at.is_(None))).all()
    for a in pending:
        if a.host_id and a.host_id != host_id():
            raise OwnershipError('unfinished attempt belongs to another host')
        if a.pid and process_alive(a.pid, (a.role_binding or {}).get('cmd', ''), since=a.started_at):
            raise OwnershipError('an agent is still alive; stop it on this host first')
    def apply():
        claim.active = False
        claim.generation += 1
        lot.state = 'BLOCKED'
        for tracked in db.scalars(select(TrackedSession).where(TrackedSession.lot_id == lot.id,
                TrackedSession.source == 'launched', TrackedSession.state == 'active')):
            tracked.state = 'interrupted'
        for a in pending:
            a.finished_at = datetime.now(timezone.utc)
            a.exit_code, a.pid = -1, None
    record(db, plat_id=plat.id, lot_id=lot.id, actor='human', actor_detail=actor(), kind='claim',
           decision='expired execution reconciled', rationale=note,
           inputs={'generation': claim.generation}, apply=apply)
