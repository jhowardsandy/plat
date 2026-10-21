"""Authenticated requests, machine acknowledgements, and fenced local dispatch.

HTTP carries a fixed vocabulary, never shell commands. Execution still uses the
trusted installation's database and independently gated runner.
"""
import hashlib
import json
import os
import secrets
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, text

from . import lifecycle
from .decisions import record
from .models import (
    Artifact,
    Attempt,
    CommandReceipt,
    ExecutionClaim,
    ExecutionCommand,
    Executor,
    Lot,
    Plat,
    TeamMember,
    TrackedSession,
    Workflow,
)
from .ownership import actor, host_id
from .team import Conflict, Forbidden

OPEN = ('requested', 'acknowledged', 'running', 'uncertain')


def provision(db, cfg, name, *, rotate=False):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError('machine name must contain 1–80 characters')
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),
               {'key': 'executor:' + cfg.origin_id + ':' + host_id()})
    existing = db.scalars(select(Executor).where(Executor.installation == cfg.origin_id,
                                                Executor.host_id == host_id()).with_for_update()).first()
    if existing:
        if not rotate:
            raise Conflict('machine already registered; use register --rotate to replace its credential')
        if existing.lease_until and existing.lease_until > db.scalar(select(func.clock_timestamp())):
            raise Conflict('stop the executor and wait for its lease to expire before rotation')
        if db.scalar(select(ExecutionCommand.id).where(ExecutionCommand.executor_id == existing.id,
                                                       ExecutionCommand.state.in_(OPEN)).limit(1)):
            raise Conflict('cancel or reconcile outstanding commands before credential rotation')
    token = secrets.token_urlsafe(32)
    if existing:
        def apply():
            existing.token_hash = hashlib.sha256(token.encode()).hexdigest()
            existing.active, existing.name = True, name.strip()
        record(db, plat_id=None, actor='human', actor_detail=actor(), kind='executor',
               decision='executor credential rotated', inputs={'executor': existing.public_id}, apply=apply)
        return existing, token
    row = Executor(name=name.strip(), installation=cfg.origin_id, host_id=host_id(),
                   token_hash=hashlib.sha256(token.encode()).hexdigest())
    record(db, plat_id=None, actor='human', actor_detail=actor(), kind='executor',
           decision='executor registered', inputs={'name': name, 'installation': cfg.origin_id, 'host': host_id()},
           apply=lambda: db.add(row))
    return row, token


def authenticate(db, token):
    row = db.scalars(select(Executor).where(Executor.token_hash == hashlib.sha256(token.encode()).hexdigest(),
                                          Executor.active.is_(True))).first()
    if not row:
        raise Forbidden('active executor credential required')
    return row


def _change(db, command, state, outcome=''):
    now = db.scalar(select(func.clock_timestamp()))
    def apply():
        command.state, command.outcome = state, outcome
        if state == 'acknowledged':
            command.acknowledged_at = now
        if state in ('completed', 'failed', 'cancelled'):
            command.finished_at = now
    record(db, plat_id=command.plat_id, actor='system', actor_detail='executor', kind='execution',
           decision=f'{command.action}: {state}', inputs={'command': command.public_id, 'outcome': outcome}, apply=apply)


def scope(db, p):
    plan_hash = db.scalar(select(Artifact.sha256).where(Artifact.plat_id == p.id, Artifact.kind == 'plat_map')
                          .order_by(Artifact.id.desc()).limit(1))
    return {'budget': p.budget_usd, 'plan': plan_hash, 'lots': [
        {'key': l.key, 'repo': l.repo, 'base': l.base_ref, 'path': l.worktree_path,
         'config': {k: v for k, v in (l.config or {}).items() if k not in ('observed_revision', 'observed_at')}}
        for l in db.scalars(select(Lot).where(Lot.plat_id == p.id).order_by(Lot.key))]}


def delivery_targets(db, p):
    from .delivery import current
    targets = []
    for lot in db.scalars(select(Lot).where(Lot.plat_id == p.id).order_by(Lot.key)):
        row = current(db, p, lot)
        if row and json.loads(row.body).get('url'):
            targets.append({'lot': lot.key, 'version': row.id, 'url': json.loads(row.body)['url']})
    return targets


def preview_hash(db, p, action):
    value = scope(db, p)
    if action == 'refresh_delivery':
        value = {'scope': value, 'targets': delivery_targets(db, p)}
    if action in ('integrate', 'record_package'):
        from .delivery_actions import reference
        value = {'scope': value, 'content': reference(db, p, action)}
    return lifecycle.digest(value)


def _validate(db, p, e, action, version, expected_status, scope_hash):
    w = db.get(Workflow, p.id)
    if version != (w.version if w else 0) or p.status != expected_status:
        raise Conflict('workflow or run state changed; refresh before requesting control')
    if p.closed_at or p.status not in {'start': ('planned',), 'resume': ('paused',),
                                      'pause': ('running', 'planned'),
                                      'refresh_delivery': ('planned', 'running', 'paused', 'delivered'),
                                      'integrate': ('delivered',), 'record_package': ('delivered',)}[action]:
        raise Conflict(f'{action} is not available while the run is {p.status}')
    lots = db.scalars(select(Lot).where(Lot.plat_id == p.id)).all()
    if not lots or any((l.config or {}).get('executor_id') != e.installation or
                       (l.config or {}).get('host_id') != e.host_id for l in lots):
        raise Conflict('all lots must be bound to this registered machine')
    if scope_hash != preview_hash(db, p, action):
        raise Conflict('plan or budget changed, or shared delivery content changed; review the current preview and request again')
    if action == 'refresh_delivery':
        if not delivery_targets(db, p):
            raise Conflict('link at least one PR/MR before requesting refresh')
        return
    if action in ('integrate', 'record_package'):
        from .delivery_actions import validate
        validate(db, p, action)
        return
    if action != 'pause':
        lifecycle.require_accepted(db, p)
        allowed = ('PENDING', 'DONE') if action == 'start' else ('PENDING', 'DONE', 'CODING', 'GATE', 'REVIEWING', 'DOCS', 'QUALITY')
        if any(l.state not in allowed for l in lots):
            raise Conflict('reconcile or reopen stopped work locally before starting')
        if db.scalar(select(ExecutionClaim.lot_id).join(Lot).where(
                Lot.plat_id == p.id, ExecutionClaim.active.is_(True)).limit(1)):
            raise Conflict('execution claim still active; wait or reconcile locally')
        if db.scalar(select(Attempt.id).join(Lot).where(Lot.plat_id == p.id,
                                                      Attempt.finished_at.is_(None)).limit(1)):
            raise Conflict('unfinished attempts require local reconciliation')
        writers = db.execute(select(TrackedSession, Lot.worktree_path).join(Lot).where(
            TrackedSession.source == 'interactive',
            TrackedSession.state.in_(('active', 'idle', 'unknown', 'interrupted')),
            (TrackedSession.plat_id == p.id) | (TrackedSession.host_id == e.host_id))).all()
        if any(s.lot_id in {l.id for l in lots} or path in {l.worktree_path for l in lots}
               for s, path in writers):
            raise Conflict('attached interactive writers must finish before starting')


def request(db, member, payload):
    if not member.active or member.role not in ('engineer', 'admin'):
        raise Forbidden('an active engineer or administrator is required')
    key = payload.get('key')
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise ValueError('a stable idempotency key is required')
    hashed = lifecycle.digest(payload)
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'command:' + key})
    prior = db.get(CommandReceipt, key)
    if prior:
        if prior.actor != member.name or prior.request_hash != hashed:
            raise Conflict('idempotency key already used')
        return prior.result
    p = db.scalars(select(Plat).where(Plat.public_id == payload.get('plat')).with_for_update()).one_or_none()
    if not p:
        raise ValueError('unknown run')
    action = payload.get('action')
    if action == 'cancel':
        cmd = db.scalars(select(ExecutionCommand).where(ExecutionCommand.public_id == payload.get('command'),
                                                       ExecutionCommand.plat_id == p.id).with_for_update()).one_or_none()
        if not cmd or cmd.state != 'requested':
            raise Conflict('only a queued request can be cancelled')
        _change(db, cmd, 'cancelled', f'Cancelled by {member.name}')
    else:
        if action not in ('start', 'pause', 'resume', 'refresh_delivery', 'integrate', 'record_package'):
            raise ValueError('supported controls: start, pause, resume, refresh_delivery, integrate, record_package, cancel')
        note = payload.get('note')
        if not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise ValueError('a reason of 1–4000 characters is required')
        e = db.scalars(select(Executor).where(Executor.public_id == payload.get('executor'),
                                             Executor.active.is_(True))).one_or_none()
        if not e:
            raise ValueError('choose an active registered executor')
        now = db.scalar(select(func.clock_timestamp()))
        if not e.lease_until or e.lease_until <= now:
            raise Conflict('executor offline; connect it before requesting execution')
        _validate(db, p, e, action, payload.get('version'), payload.get('status'), payload.get('scope_hash'))
        existing = db.scalars(select(ExecutionCommand).where(ExecutionCommand.plat_id == p.id,
                                                            ExecutionCommand.state.in_(OPEN))).all()
        if any(action != 'pause' or c.action == 'pause' or c.state == 'uncertain' for c in existing):
            raise Conflict('an execution request is already outstanding')
        cmd = ExecutionCommand(plat_id=p.id, executor_id=e.id, actor=member.name, action=action,
            version=payload['version'], expected_status=payload['status'], scope_hash=payload['scope_hash'], note=note.strip())
        record(db, plat_id=p.id, actor='human', actor_detail=member.name, kind='execution',
               decision=f'{action}: requested', rationale=note,
               inputs={'executor': e.public_id, 'version': cmd.version, 'scope_hash': cmd.scope_hash}, apply=lambda: db.add(cmd))
    result = {'id': cmd.public_id, 'state': cmd.state}
    db.add(CommandReceipt(key=key, actor=member.name, request_hash=hashed, result=result))
    return result


def poll(db, e, session):
    try:
        UUID(session)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('executor process session must be a UUID') from None
    db.refresh(e, with_for_update=True)
    if not e.active:
        raise Forbidden('executor credential revoked')
    now = db.scalar(select(func.clock_timestamp()))
    if e.session != session and e.lease_until and e.lease_until > now:
        raise Conflict('another executor process holds the machine lease')
    # A replacement process never redispatches ambiguous work, even if no claim
    # was created in the crash window between acknowledgement and process spawn.
    if e.session != session:
        abandoned = db.scalars(select(ExecutionCommand).where(ExecutionCommand.executor_id == e.id,
            ExecutionCommand.state.in_(('acknowledged', 'running')))).all()
        # Do not lock plats after holding the executor row. Recovery is per command
        # below only after releasing this telemetry transaction in the daemon.
        if abandoned:
            return {'recover': [c.public_id for c in abandoned], 'commands': []}
    if e.session != session:
        record(db, plat_id=None, actor='system', actor_detail='executor', kind='executor',
               decision='executor connected', inputs={'executor': e.public_id, 'session': session},
               apply=lambda: setattr(e, 'session', session))
    e.last_seen, e.lease_until = now, now + timedelta(seconds=30)
    return {'commands': [c.public_id for c in db.scalars(select(ExecutionCommand).where(
        ExecutionCommand.executor_id == e.id,
        (ExecutionCommand.state == 'requested') | ((ExecutionCommand.state == 'acknowledged') &
        (ExecutionCommand.delivery_session == session))).order_by(ExecutionCommand.id))],
        'installation': e.installation, 'host': e.host_id}


def acknowledge(db, e, public_id, session, *, abandon=False):
    cmd = db.scalars(select(ExecutionCommand).where(ExecutionCommand.public_id == public_id,
                                                   ExecutionCommand.executor_id == e.id)).one_or_none()
    if not cmd:
        raise ValueError('command not assigned to this executor')
    p = db.scalars(select(Plat).where(Plat.id == cmd.plat_id).with_for_update()).one()
    db.refresh(cmd, with_for_update=True)
    db.refresh(e, with_for_update=True)
    if not e.active:
        raise Forbidden('executor credential revoked')
    now = db.scalar(select(func.clock_timestamp()))
    if abandon:
        if e.session != session and e.lease_until and e.lease_until > now:
            raise Conflict('executor lease still held')
        if cmd.state in ('acknowledged', 'running'):
            _change(db, cmd, 'uncertain', 'Executor disconnected; inspect locally. Execution will not be retried automatically.')
        return {'state': cmd.state}
    if e.session != session or not e.lease_until or e.lease_until <= now:
        raise Conflict('executor lease expired')
    if cmd.state != 'requested':
        if cmd.delivery_session == session:
            return {'state': cmd.state, 'id': cmd.public_id}
        raise Conflict('command already acknowledged or cancelled')
    member = db.scalars(select(TeamMember).where(TeamMember.name == cmd.actor)).first()
    try:
        if not member or not member.active or member.role not in ('engineer', 'admin'):
            raise Forbidden('requesting member no longer authorized')
        if now - cmd.requested_at > timedelta(minutes=5):
            raise Conflict('request expired after five minutes; request again explicitly')
        _validate(db, p, e, cmd.action, cmd.version, cmd.expected_status, cmd.scope_hash)
    except (ValueError, lifecycle.WorkflowError) as exc:
        _change(db, cmd, 'failed', str(exc))
        return {'id': cmd.public_id, 'state': cmd.state}
    cmd.delivery_session = session
    _change(db, cmd, 'acknowledged', 'Owning machine received the request; execution has not started yet.')
    return {'id': cmd.public_id, 'state': cmd.state}


def execute(cfg, public_id, session):
    from . import db as DB
    from .dispatch.inline import InlineDispatcher
    with DB.session() as db:
        cmd = db.scalars(select(ExecutionCommand).where(ExecutionCommand.public_id == public_id)).one()
        p = db.scalars(select(Plat).where(Plat.id == cmd.plat_id).with_for_update()).one()
        db.refresh(cmd, with_for_update=True)
        e = db.get(Executor, cmd.executor_id)
        db.refresh(e, with_for_update=True)
        if e.installation != cfg.origin_id or e.host_id != host_id():
            raise Forbidden('command belongs to another machine')
        if cmd.state != 'acknowledged' or cmd.delivery_session != session:
            raise Conflict('command is not awaiting this execution; never replay it')
        if not e.active:
            _change(db, cmd, 'failed', 'Executor credential revoked before execution')
            return
        if e.session != session or not e.lease_until or e.lease_until <= db.scalar(select(func.clock_timestamp())):
            _change(db, cmd, 'failed', 'Machine lease expired before execution')
            return
        try:
            member = db.scalars(select(TeamMember).where(TeamMember.name == cmd.actor).with_for_update()).first()
            if not member or not member.active or member.role not in ('engineer', 'admin'):
                raise Forbidden('requesting member no longer authorized')
            _validate(db, p, e, cmd.action, cmd.version, cmd.expected_status, cmd.scope_hash)
            if cmd.action in ('start', 'resume'):
                from .cli import _check_roles
                for lot in db.scalars(select(Lot).where(Lot.plat_id == p.id)):
                    _check_roles(cfg, (lot.config or {}).get('phases', cfg.phases))
        except Exception as exc:
            _change(db, cmd, 'failed', str(exc) or 'Local role or workflow preflight failed')
            return
        cmd.pid, cmd.started_at = os.getpid(), db.scalar(select(func.clock_timestamp()))
        # State and command outcome belong to the same audited transaction.
        if cmd.action in ('start', 'pause', 'resume'):
            p.status = 'paused' if cmd.action == 'pause' else 'running'
        _change(db, cmd, 'completed' if cmd.action == 'pause' else 'running',
                'Pause applied; in-flight agents may finish.' if cmd.action == 'pause' else 'Provider refresh started.' if cmd.action == 'refresh_delivery' else 'Delivery action started.' if cmd.action in ('integrate', 'record_package') else 'Local runner started.')
        plat_id, command_id, action = p.id, cmd.id, cmd.action
        targets = delivery_targets(db, p) if action == 'refresh_delivery' else []
    if action == 'pause':
        return
    if action in ('integrate', 'record_package'):
        _delivery_action(cfg, plat_id, command_id, session)
        return
    if action == 'refresh_delivery':
        _refresh_delivery(cfg, plat_id, command_id, session, targets)
        return
    try:
        states = InlineDispatcher().run(cfg, plat_id, workers=1)
        outcome, state = f'Runner finished: {states}. Delivery requires the recorded gates and workflow evidence.', 'completed'
    except BaseException:
        outcome, state = 'Runner exited unexpectedly; inspect claims and attempts before recovery.', 'uncertain'
        raise
    finally:
        with DB.session() as db:
            db.execute(select(Plat.id).where(Plat.id == plat_id).with_for_update())
            cmd = db.get(ExecutionCommand, command_id)
            if cmd.delivery_session == session:
                _change(db, cmd, state, outcome)


def _delivery_action(cfg, plat_id, command_id, session):
    from . import db as DB
    from . import delivery_actions
    state, outcome = 'uncertain', 'Delivery action interrupted; inspect evidence and claims before reconciling.'
    try:
        with DB.session() as db:
            p = db.scalars(select(Plat).where(Plat.id == plat_id).with_for_update()).one()
            cmd = db.get(ExecutionCommand, command_id)
            e = db.get(Executor, cmd.executor_id)
            member = db.scalars(select(TeamMember).where(TeamMember.name == cmd.actor)).one()
            if cmd.state != 'running' or cmd.delivery_session != session or not e.active or not member.active or member.role not in ('engineer', 'admin'):
                raise Conflict('delivery authorization changed')
            _validate(db, p, e, cmd.action, cmd.version, cmd.expected_status, cmd.scope_hash)
            row = delivery_actions.execute(db, cfg, p, cmd.action, cmd.version, cmd.actor)
            state = 'failed' if row.content.get('passed') is False else 'completed'
            outcome = f'{row.stage} recorded at workflow v{row.version}; ' + ('checks failed or revisions changed.' if state == 'failed' else 'inspect the recorded evidence. Deployment workflow not invoked.')
    except (ValueError, OSError):
        state, outcome = 'failed', 'Delivery preflight or checks could not complete. Inspect workflow evidence and owning-machine configuration before retrying.'
    finally:
        with DB.session() as db:
            db.execute(select(Plat.id).where(Plat.id == plat_id).with_for_update())
            cmd = db.get(ExecutionCommand, command_id)
            if cmd.delivery_session == session and cmd.state == 'running':
                _change(db, cmd, state, outcome)


def _refresh_delivery(cfg, plat_id, command_id, session, targets):
    from . import db as DB
    from . import delivery
    results = []
    state = 'completed'
    try:
        for target in targets:
            with DB.session() as db:
                p = db.get(Plat, plat_id)
                lot = db.scalars(select(Lot).where(Lot.plat_id == plat_id, Lot.key == target['lot'])).one()
                result = delivery.refresh(db, cfg, p, lot, expected_version=target['version'])
                observation = json.loads(db.get(Artifact, result['version']).body)['observation']
                failed = bool(observation.get('error'))
                if failed:
                    state = 'failed'
                results.append(target['lot'] + (': refresh failed' if failed else ': observed'))
        outcome = '; '.join(results) + '. Inspect Delivery for revision warnings and CI results; no workflow stages changed.'
    except (ValueError, OSError):
        state, outcome = 'failed', '; '.join(results + ['Refresh stopped: delivery context changed or provider unavailable. Inspect Delivery and request again.'])
    except BaseException:
        state, outcome = 'uncertain', 'Refresh interrupted; inspect locally before reconciling. No automatic replay.'
        raise
    finally:
        with DB.session() as db:
            db.execute(select(Plat.id).where(Plat.id == plat_id).with_for_update())
            cmd = db.get(ExecutionCommand, command_id)
            if cmd.delivery_session == session and cmd.state == 'running':
                _change(db, cmd, state, outcome)


def room(db, p):
    online = dict(db.execute(text('SELECT id, online FROM v_executor_status')).all())
    lost = dict(db.execute(text('SELECT id, connection_lost FROM v_execution_command_status')).all())
    lots = db.scalars(select(Lot).where(Lot.plat_id == p.id)).all()
    executors = db.scalars(select(Executor).where(Executor.active.is_(True))).all()
    preview = scope(db, p)
    public_preview = {'budget': preview['budget'], 'lots': [
        {'key': l['key'], 'repo': l['repo'], 'phases': (l['config'] or {}).get('phases', [])}
        for l in preview['lots']]}
    return {'action_scope_hashes': {a: preview_hash(db, p, a) for a in ('integrate', 'record_package')},
            'delivery_targets': delivery_targets(db, p),
            'delivery_scope_hash': preview_hash(db, p, 'refresh_delivery'), 'scope': public_preview, 'scope_hash': lifecycle.digest(preview), 'executors': [{'id': e.public_id, 'name': e.name, 'online': online[e.id],
                          'seen': e.last_seen, 'matches': bool(lots) and all(
                              (l.config or {}).get('executor_id') == e.installation and
                              (l.config or {}).get('host_id') == e.host_id for l in lots)} for e in executors],
            'commands': [{'id': c.public_id, 'action': c.action, 'state': c.state, 'actor': c.actor,
                          'executor': db.get(Executor, c.executor_id).name, 'note': c.note, 'outcome': c.outcome,
                          'requested': c.requested_at, 'acknowledged': c.acknowledged_at,
                          'started': c.started_at, 'finished': c.finished_at,
                          'connection_lost': lost[c.id]}
                         for c in db.scalars(select(ExecutionCommand).where(ExecutionCommand.plat_id == p.id)
                                             .order_by(ExecutionCommand.id.desc()).limit(50))]}
