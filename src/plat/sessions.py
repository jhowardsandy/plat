"""Explicit session provenance. Attached sessions are observations, never gates."""
from datetime import datetime, timezone

from sqlalchemy import select

from .decisions import record
from .models import TrackedSession
from .ownership import actor, host_id

STATES = {'active', 'idle', 'interrupted', 'completed', 'failed', 'unknown'}


def launched(db, cfg, plat, lot, attempt):
    row = TrackedSession(plat_id=plat.id, lot_id=lot.id, attempt_id=attempt.id,
        installation=cfg.origin_id, host_id=host_id(), owner=actor(),
        provider=attempt.provider, session_key=f'attempt:{attempt.public_id}',
        source='launched', state='active', capabilities={'resume': attempt.phase == 'code', 'control': True})
    db.add(row)
    return row


def complete(db, attempt, result):
    row = db.scalars(select(TrackedSession).where(TrackedSession.attempt_id == attempt.id)).first()
    if row:
        row.provider_session_id = result.session_id
        row.state = 'completed' if result.ok and not result.permission_denials and result.exit_code == 0 else 'failed'
        row.last_activity = datetime.now(timezone.utc)


def attach(db, cfg, plat, lot, provider, session_id, *, state='active', note=''):
    if (lot.config or {}).get('executor_id') not in (None, cfg.origin_id):
        raise ValueError('attach on the owning installation, or localize a pending lot first')
    if provider not in ('claude', 'codex') or state not in STATES or not session_id.strip():
        raise ValueError('attach needs claude/codex, a real provider session ID, and a valid state')
    if len(session_id) > 128:
        raise ValueError('session ID is too long')
    row = db.scalars(select(TrackedSession).where(
        TrackedSession.installation == cfg.origin_id, TrackedSession.provider == provider,
        TrackedSession.session_key == session_id).with_for_update()).first()
    if row and (row.lot_id != lot.id or row.owner != actor()):
        raise ValueError('session is already attached to another lot or owner')
    if row and row.state == state:
        return row
    from .models import ExecutionClaim
    claim = db.get(ExecutionClaim, lot.id)
    if state in ('active', 'idle', 'unknown', 'interrupted') and claim and claim.active:
        raise ValueError('automated execution owns this lot; stop and reconcile it before attaching a writer')
    if not row:
        row = TrackedSession(plat_id=plat.id, lot_id=lot.id, installation=cfg.origin_id,
            host_id=host_id(), owner=actor(), provider=provider, session_key=session_id,
            provider_session_id=session_id, source='interactive', capabilities={'control': False, 'resume': False})
    def apply():
        row.state = state
        row.last_activity = datetime.now(timezone.utc)
        db.add(row)
    record(db, plat_id=plat.id, lot_id=lot.id, actor='human', actor_detail=actor(),
           kind='session', decision=f'interactive {provider} session observed: {state}', rationale=note,
           inputs={'session_id': session_id, 'source': 'manual observation'}, apply=apply)
    return row
