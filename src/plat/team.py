"""Authenticated collaboration over one authoritative team database.

Members never receive SQL credentials from this service. Executors are trusted
local installations administered alongside the database; remote clients issue
collaboration commands, not arbitrary shell/process controls.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from .decisions import record
from .lifecycle import digest, revisions
from .models import (
    Attempt,
    CollaborationItem,
    CommandReceipt,
    Event,
    ExecutionClaim,
    Lot,
    Plat,
    RepositoryCatalog,
    TeamInvitation,
    TeamMember,
    TrackedSession,
    Workflow,
)


class Conflict(ValueError):
    pass


class Forbidden(ValueError):
    pass


def add_member(db, name, role, *, token=None, issued_by=None):
    if role not in ('viewer', 'engineer', 'admin') or not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise ValueError('member needs a short name and viewer/engineer/admin role')
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'member:' + name})
    if db.scalars(select(TeamMember).where(TeamMember.name == name)).first():
        raise Conflict('member already exists; rotate or revoke the existing token')
    token = token or secrets.token_urlsafe(32)
    member = TeamMember(name=name, role=role, token_hash=hashlib.sha256(token.encode()).hexdigest())
    from .ownership import actor
    record(db, plat_id=None, actor='human', actor_detail=issued_by or actor(), kind='membership',
           decision=f'member provisioned: {name}', inputs={'name': name, 'role': role},
           apply=lambda: db.add(member))
    return member, token


def invite(db, member, name, role='engineer', hours=24):
    if member.role != 'admin' or not member.active:
        raise Forbidden('only administrators may invite members')
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise ValueError('a member name of 1–64 characters is required')
    if role not in ('viewer', 'engineer'):
        raise ValueError('invitations grant viewer or engineer access')
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError('invitation expiry must be 1–168 hours')
    if db.scalars(select(TeamMember).where(TeamMember.name == name)).first():
        raise Conflict('member name already exists')
    code = secrets.token_urlsafe(32)
    invitation = TeamInvitation(code_hash=hashlib.sha256(code.encode()).hexdigest(),
        name=name, role=role, issuer=member.name,
        expires_at=db.scalar(text('SELECT clock_timestamp()')) + timedelta(hours=hours))
    record(db, plat_id=None, actor='human', actor_detail=member.name, kind='membership',
           decision=f'invitation issued for {name}', inputs={'name': name, 'role': role},
           apply=lambda: db.add(invitation))
    return {'id': invitation.public_id, 'code': code, 'name': name, 'role': role,
            'expires_at': invitation.expires_at}


def revoke_invitation(db, member, public_id):
    if member.role != 'admin' or not member.active:
        raise Forbidden('only administrators may revoke invitations')
    invitation = db.scalars(select(TeamInvitation).where(
        TeamInvitation.public_id == public_id).with_for_update()).first()
    if not invitation:
        raise ValueError('invitation not found')
    record(db, plat_id=None, actor='human', actor_detail=member.name, kind='membership',
           decision=f'invitation revoked: {public_id}', inputs={'id': public_id},
           apply=lambda: setattr(invitation, 'revoked', True))
    return {'id': public_id, 'revoked': True}


def join(db, code, token):
    # Both secrets are 256-bit URL-safe values; never put them in audit records.
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', code):
        raise Forbidden('invalid or unavailable invitation')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise ValueError('a generated 256-bit member token is required')
    invitation = db.scalars(select(TeamInvitation).where(
        TeamInvitation.code_hash == hashlib.sha256(code.encode()).hexdigest()).with_for_update()).first()
    if not invitation or invitation.revoked:
        raise Forbidden('invalid or unavailable invitation')
    if invitation.member_id:
        member = db.get(TeamMember, invitation.member_id)
        if not member.active or not secrets.compare_digest(member.token_hash, hashlib.sha256(token.encode()).hexdigest()):
            raise Forbidden('invalid or unavailable invitation')
        # Only the already-joined holder can recover a lost response, even after expiry.
        return {'member': member.name, 'role': member.role}
    if invitation.expires_at <= db.scalar(text('SELECT clock_timestamp()')):
        raise Forbidden('invalid or unavailable invitation')
    member, _ = add_member(db, invitation.name, invitation.role, token=token,
                           issued_by=f'invitation:{invitation.public_id}')
    record(db, plat_id=None, actor='human', actor_detail=member.name, kind='membership',
           decision=f'invitation redeemed: {invitation.public_id}', inputs={'id': invitation.public_id},
           apply=lambda: setattr(invitation, 'member_id', member.id))
    return {'member': member.name, 'role': member.role}


def authenticate(db, token):
    if not token:
        raise Forbidden('authentication required')
    member = db.scalars(select(TeamMember).where(TeamMember.active.is_(True),
        TeamMember.token_hash == hashlib.sha256(token.encode()).hexdigest())).first()
    if not member:
        raise Forbidden('invalid credential')
    return member


def board(db):
    from .workspace import repository_key
    signals = {r['lot_id']: dict(r) for r in db.execute(text('SELECT * FROM v_workspace_lots')).mappings()}
    catalog = {r.identity: r for r in db.scalars(select(RepositoryCatalog))}
    plats = db.scalars(select(Plat).order_by(Plat.id.desc())).all()
    out = []
    for p in plats:
        w = db.get(Workflow, p.id)
        lots = db.scalars(select(Lot).where(Lot.plat_id == p.id).order_by(Lot.id)).all()
        items = db.scalars(select(CollaborationItem).where(CollaborationItem.plat_id == p.id)
                           .order_by(CollaborationItem.id)).all()
        sessions = db.scalars(select(TrackedSession).where(TrackedSession.plat_id == p.id)).all()
        current_revisions = None
        if any(i.kind == 'review' for i in items):
            try:
                current_revisions = revisions(db, p)
            except (OSError, ValueError, subprocess.CalledProcessError):
                pass
        from .guidance import STEPS, guided
        from .lifecycle import readiness
        planning_step = STEPS.get(w.stage) if guided(w) and w.stage in ("context", "definition", "specification", "planning") else None
        ready = readiness(db, p) if w else None
        attempts = db.scalars(select(Attempt).join(Lot).where(Lot.plat_id == p.id)).all()
        out.append({'planning_next_step': planning_step, 'readiness': ready, 'id': p.public_id, 'anchor': p.anchor, 'title': p.title, 'status': p.status,
                    'budget': p.budget_usd, 'spent': sum(a.cost_usd for a in attempts),
                    'estimated': any(a.cost_estimated for a in attempts),
                    'stage': w.stage if w else 'local development', 'version': w.version if w else 0,
                    'owner': next((i.owner for i in items if i.kind == 'assign' and i.lot_id is None and i.state == 'open'), w.owner if w else None),
                    'lots': [{'id': l.public_id, 'key': l.key, 'repo': l.repo, 'state': l.state,
                              'heartbeat': l.heartbeat_at, 'attempt': l.attempt,
                              'needs_attention': signals[l.id]['needs_attention'],
                              'next_step': signals[l.id]['next_step'],
                              'phases': (l.config or {}).get('phases', []),
                              'repository': repository_key(l, p),
                              'classification': ({'products': catalog[repository_key(l, p)].products,
                                  'services': catalog[repository_key(l, p)].services,
                                  'version': catalog[repository_key(l, p)].version} if repository_key(l, p) in catalog else {'products': [], 'services': [], 'version': 0}),
                              'owner': next((i.owner for i in items if i.kind == 'assign' and i.lot_id == l.id and i.state == 'open'), None),
                              'executor': claim_summary(db, db.get(ExecutionClaim, l.id))} for l in lots],
                    'items': [{**item_summary(i), 'stale': i.kind == 'review' and i.body.get('revisions') != current_revisions, 'lot_id': next((l.public_id for l in lots if l.id == i.lot_id), None), 'lot': next((l.key for l in lots if l.id == i.lot_id), 'Entire run')} for i in items],
                    'sessions': [{'id': s.public_id, 'owner': s.owner, 'provider': s.provider,
                                  'state': s.state, 'source': s.source, 'lot_id': next((l.public_id for l in lots if l.id == s.lot_id), None), 'activity': s.last_activity,
                                  'observed': True, 'stale': db.scalar(text('SELECT stale FROM v_session_status WHERE id=:id'), {'id': s.id})} for s in sessions]})
    return {'observed_at': datetime.now(timezone.utc), 'plats': out}


def claim_summary(db, claim):
    if not claim or not claim.active:
        return None
    return {'installation': claim.executor_id, 'generation': claim.generation,
            'expires_at': claim.expires_at,
            'reconciliation_required': db.scalar(text('SELECT reconciliation_required FROM v_claim_status WHERE lot_id=:id'), {'id': claim.lot_id})}


def item_summary(i):
    return {'id': i.public_id, 'kind': i.kind, 'owner': i.owner, 'author': i.author,
            'state': i.state, 'version': i.version, 'body': i.body, 'updated_at': i.updated_at}


def events(db, after=0):
    rows = db.scalars(select(Event).where(Event.id > after).order_by(Event.id).limit(200)).all()
    return {'cursor': rows[-1].id if rows else after,
            'events': [{'id': e.id, 'public_id': e.public_id, 'kind': e.kind,
                        'message': e.message, 'at': e.ts} for e in rows]}


def command(db, member, request):
    if member.role == 'viewer':
        raise Forbidden('viewers cannot issue commands')
    key, action = request.get('key'), request.get('action')
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise ValueError('a stable idempotency key is required')
    hashed = digest(request)
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'command:' + key})
    prior = db.get(CommandReceipt, key)
    if prior:
        if prior.actor != member.name or prior.request_hash != hashed:
            raise Conflict('idempotency key was already used for another command')
        return prior.result
    p = db.scalars(select(Plat).where(Plat.public_id == request.get('plat')).with_for_update()).first()
    if not p:
        raise ValueError('plat not found; use the public ID from the board')
    if p.closed_at:
        raise Conflict('shipped work is immutable')
    owner = request.get('owner', member.name)
    if not db.scalars(select(TeamMember).where(TeamMember.name == owner, TeamMember.active.is_(True))).first():
        raise ValueError('owner is not an active team member')
    if action in ('assign', 'question', 'review', 'note'):
        body = request.get('body')
        if not isinstance(body, dict) or not isinstance(body.get('text'), str) or not body['text'].strip():
            raise ValueError('body.text is required')
        lot = None
        if request.get('lot'):
            lot = db.scalars(select(Lot).where(Lot.plat_id == p.id,
                                              Lot.public_id == request['lot'])).first()
            if not lot:
                raise ValueError('lot not found in plat')
        if action == 'review':
            body = {**body, 'revisions': revisions(db, p)}
        if action == 'assign':
            # One active assignment per lot (or run); reassignment uses versioned update.
            existing = db.scalars(select(CollaborationItem).where(
                CollaborationItem.plat_id == p.id, CollaborationItem.kind == 'assign',
                CollaborationItem.lot_id == (lot.id if lot else None),
                CollaborationItem.state == 'open')).first()
            if existing:
                raise Conflict(f'assignment already exists: {existing.public_id}; update its version')
        item = CollaborationItem(plat_id=p.id, lot_id=lot.id if lot else None,
                                 kind=action, owner=owner, author=member.name, body=body)
        record(db, plat_id=p.id, lot_id=item.lot_id, actor='human', actor_detail=member.name,
               kind='collaboration', decision=f'{action} requested for {owner}', inputs={'body': body},
               apply=lambda: db.add(item))
    elif action in ('answer', 'review_result', 'reassign'):
        item = db.scalars(select(CollaborationItem).where(CollaborationItem.plat_id == p.id,
            CollaborationItem.public_id == request.get('item')).with_for_update()).first()
        if not item:
            raise ValueError('item not found')
        if request.get('version') != item.version:
            raise Conflict(f'stale item version; current version is {item.version}')
        if item.owner != member.name and member.role != 'admin':
            raise Forbidden('only the assigned owner or an administrator may resolve this item')
        if item.state != 'open':
            raise Conflict('item is already resolved')
        note = request.get('note', '')
        if not isinstance(note, str) or not note.strip():
            raise ValueError('a note is required')
        if action == 'review_result':
            if item.kind != 'review':
                raise ValueError('item is not a review request')
            if item.body.get('revisions') != revisions(db, p):
                raise Conflict('review revisions changed; request a fresh review')
            if request.get('outcome') not in ('approved', 'changes_requested'):
                raise ValueError('outcome must be approved or changes_requested')
        elif action == 'answer' and item.kind != 'question':
            raise ValueError('item is not a question')
        def apply():
            item.version += 1
            item.updated_at = datetime.now(timezone.utc)
            if action == 'reassign':
                item.owner = owner
                item.body = {**item.body, 'text': note}
            else:
                item.body = {**item.body, 'answer': note, 'outcome': request.get('outcome'), 'responded_by': member.name}
                item.state = 'resolved' if action == 'answer' or request.get('outcome') == 'approved' else 'open'
                if action == 'answer' and item.lot_id:
                    lot = db.get(Lot, item.lot_id)
                    lot.human_note = f'{member.name}: {note}'
        record(db, plat_id=p.id, lot_id=item.lot_id, actor='human', actor_detail=member.name,
               kind='collaboration', decision=f'{action}: {item.public_id}', inputs={'note': note}, apply=apply)
    else:
        raise ValueError('unsupported collaboration action')
    result = {'id': item.public_id, 'version': item.version, 'state': item.state}
    db.add(CommandReceipt(key=key, actor=member.name, request_hash=hashed, result=result))
    db.flush()
    return result
