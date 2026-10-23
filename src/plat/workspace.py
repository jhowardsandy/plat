"""Team workspace projections and audited organization commands; never execution."""
from sqlalchemy import or_, select, text

from .decisions import record
from .lifecycle import digest
from .models import (
    Artifact,
    Attempt,
    CommandReceipt,
    Criterion,
    Decision,
    Finding,
    Lot,
    Plat,
    RepositoryCatalog,
    SavedView,
    StageRecord,
    TeamMember,
)
from .team import Conflict, Forbidden

VIEWS = {'attention', 'mine', 'ticket', 'product', 'engineer', 'service', 'repo', 'review', 'delivery'}


def repository_key(lot, plat):
    # Never merge unrelated legacy repositories just because their local names match.
    return (lot.config or {}).get('repository_identity') or f'legacy:{plat.origin_id}:{lot.repo}'


def preferences(db, member):
    return {'members': [{'name': m.name, 'role': m.role} for m in db.scalars(
        select(TeamMember).where(TeamMember.active.is_(True)).order_by(TeamMember.name))],
        'views': [{'id': v.public_id, 'name': v.name, 'owner': v.owner, 'shared': v.shared,
                   'settings': v.settings} for v in db.scalars(select(SavedView).where(
                       or_(SavedView.shared.is_(True), SavedView.owner == member.name)).order_by(SavedView.name))]}


def command(db, member, request):
    key = request.get('key')
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise ValueError('a stable idempotency key is required')
    hashed = digest(request)
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'command:' + key})
    prior = db.get(CommandReceipt, key)
    if prior:
        if prior.actor != member.name or prior.request_hash != hashed:
            raise Conflict('idempotency key was already used for another command')
        return prior.result
    action = request.get('action')
    if action == 'record_guidance':
        from . import guidance
        if not member.active or member.role not in ('engineer', 'admin'):
            raise Forbidden('an active engineer or administrator is required')
        if type(request.get('version')) is not int:
            raise ValueError('current workflow version is required')
        p = db.scalars(select(Plat).where(Plat.public_id == request.get('plat'))).first()
        if not p:
            raise ValueError('plat not found')
        if not isinstance(request.get('evidence'), dict) or not isinstance(request.get('check'), str):
            raise ValueError('check and evidence are required')
        row = guidance.record_check(db, p, request['check'], request['evidence'], request['version'], who=member.name)
        result = {'version': row.version, 'sha256': row.sha256}
    elif action == 'share_publication':
        from . import publication
        if not member.active or member.role not in ('engineer', 'admin'):
            raise Forbidden('an active engineer or administrator is required')
        p = db.scalars(select(Plat).where(Plat.public_id == request.get('plat'))).first()
        if not p:
            raise ValueError('plat not found')
        result = publication.share(db, p, request.get('target'), request.get('body'),
                                   request.get('version'), member.name)
    elif action == 'share_package':
        from . import delivery_actions
        if member.role == 'viewer':
            raise Forbidden('viewers cannot share delivery drafts')
        p = db.scalars(select(Plat).where(Plat.public_id == request.get('plat'))).first()
        if not p:
            raise ValueError('plat not found')
        result = delivery_actions.share_package(db, p, request.get('fields'), request.get('version'), member.name)
    elif action == 'delivery_link':
        from . import delivery
        if member.role == 'viewer':
            raise Forbidden('viewers cannot change delivery links')
        p = db.scalars(select(Plat).where(Plat.public_id == request.get('plat'))).first()
        lot = db.scalars(select(Lot).where(Lot.plat_id == p.id, Lot.key == request.get('lot'))).first() if p else None
        if not lot:
            raise ValueError('plat or lot not found')
        result = delivery.link(db, p, lot, request.get('url'), request.get('version'), member.name)
    elif action == 'classify':
        if member.role == 'viewer':
            raise Forbidden('viewers cannot classify repositories')
        identity = request.get('repository')
        known = {repository_key(l, p) for l, p in db.execute(select(Lot, Plat).join(Plat, Lot.plat_id == Plat.id))}
        if identity not in known:
            raise ValueError('choose a repository from the board')
        db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'catalog:' + identity})
        row = db.get(RepositoryCatalog, identity)
        current = row.version if row else 0
        if request.get('version') != current:
            raise Conflict(f'stale classification; current version is {current}')
        values = {}
        for field in ('products', 'services'):
            value = request.get(field)
            if not isinstance(value, list) or len(value) > 20 or any(
                not isinstance(x, str) or not x.strip() or len(x) > 80 for x in value):
                raise ValueError(f'{field} must contain at most 20 nonempty labels of at most 80 characters')
            values[field] = sorted(set(x.strip() for x in value))
        def apply():
            nonlocal row
            if row is None:
                row = RepositoryCatalog(identity=identity)
                db.add(row)
            row.products, row.services, row.version = values['products'], values['services'], current + 1
        record(db, plat_id=None, actor='human', actor_detail=member.name, kind='organization',
               decision='repository classified', inputs={'repository': identity, **values}, apply=apply)
        result = {'version': current + 1}
    elif action == 'save_view':
        name, settings, shared = request.get('name'), request.get('settings'), request.get('shared', False)
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('view name must be 1–80 characters')
        if not isinstance(shared, bool) or (shared and member.role == 'viewer'):
            raise Forbidden('viewers may save personal views only')
        if not isinstance(settings, dict) or set(settings) != {'view', 'sort', 'product', 'query', 'closed'}:
            raise ValueError('invalid view settings')
        if not isinstance(settings['view'], str) or not isinstance(settings['sort'], str) or settings['view'] not in VIEWS or settings['sort'] not in ('attention', 'ticket', 'engineer', 'repo'):
            raise ValueError('invalid grouping or sort')
        if not isinstance(settings['closed'], bool) or any(not isinstance(settings[x], str) or len(settings[x]) > 200 for x in ('product', 'query')):
            raise ValueError('invalid filter')
        row = SavedView(name=name.strip(), owner=member.name, shared=shared, settings=settings)
        record(db, plat_id=None, actor='human', actor_detail=member.name, kind='organization',
               decision='workspace view saved', inputs={'name': name, 'shared': shared}, apply=lambda: db.add(row))
        db.flush()
        result = {'id': row.public_id}
    elif action == 'delete_view':
        row = db.scalars(select(SavedView).where(SavedView.public_id == request.get('id')).with_for_update()).first()
        if not row or (row.owner != member.name and not (row.shared and member.role == 'admin')):
            raise Forbidden('only the owner or a shared-view administrator may remove this view')
        record(db, plat_id=None, actor='human', actor_detail=member.name, kind='organization',
               decision='workspace view removed', inputs={'name': row.name}, apply=lambda: db.delete(row))
        result = {'deleted': True}
    else:
        raise ValueError('unsupported workspace action')
    db.add(CommandReceipt(key=key, actor=member.name, request_hash=hashed, result=result))
    return result


def room(db, public_id):
    p = db.scalars(select(Plat).where(Plat.public_id == public_id)).first()
    if not p:
        raise ValueError('plat not found')
    attempts = db.scalars(select(Attempt).join(Lot).where(Lot.plat_id == p.id).order_by(Attempt.id)).all()
    ids = [a.id for a in attempts]
    from .delivery import project as delivery_project
    from .delivery_actions import project as action_project
    from .executor import room as execution_room
    from .guidance import project as guidance_project
    from .publication import project as publication_project
    return {
        'guidance': guidance_project(db, p),
        'execution': execution_room(db, p),
        'delivery': delivery_project(db, p),
        'delivery_actions': {**action_project(db, p), 'publication': publication_project(db, p)},
        'attempts': [{'id': a.id, 'lot': db.get(Lot, a.lot_id).key, 'phase': a.phase,
                      'attempt': a.n, 'provider': a.provider, 'model': a.model,
                      'exit_code': a.exit_code, 'started': a.started_at, 'finished': a.finished_at,
                      'cost': a.cost_usd, 'estimated': a.cost_estimated} for a in attempts],
        'criteria': [{'id': c.ac_id, 'lot': c.lot_key, 'statement': c.statement,
                      'verified_attempt': c.met_in_attempt_id} for c in db.scalars(select(Criterion).where(Criterion.plat_id == p.id))],
        'findings': [{'fingerprint': f.fingerprint, 'severity': f.severity, 'file': f.file,
                      'line': f.line, 'claim': f.claim, 'required_fix': f.required_fix,
                      'attempt': f.attempt_id, 'resolved': f.resolved_in_attempt_id,
                      'dismissed': f.dismissed_reason} for f in db.scalars(select(Finding).where(Finding.attempt_id.in_(ids)))],
        # Metadata only: transcripts/prompts and raw artifacts remain local until a sharing policy is configured.
        'artifacts': [{'name': a.name, 'kind': a.kind, 'sha256': a.sha256, 'attempt': a.attempt_id,
                       'created': a.created_at} for a in db.scalars(select(Artifact).where(Artifact.plat_id == p.id).order_by(Artifact.id))],
        'stages': [{'stage': s.stage, 'version': s.version, 'actor': s.actor, 'sha256': s.sha256,
                    'created': s.created_at} for s in db.scalars(select(StageRecord).where(StageRecord.plat_id == p.id).order_by(StageRecord.id))],
        'decisions': [{'actor': d.actor, 'by': d.actor_detail, 'decision': d.decision,
                       'rationale': d.rationale, 'at': d.ts} for d in db.scalars(select(Decision).where(
                           Decision.plat_id == p.id).order_by(Decision.id.desc()).limit(200))],
    }
