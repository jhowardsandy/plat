"""Explicitly shared delivery drafts and owner-published integration recipes."""
import json

from sqlalchemy import select

from . import lifecycle as L
from .decisions import record
from .models import Artifact, Lot, Workflow
from .ownership import actor, host_id
from .team import Conflict

FIELDS = ('summary', 'rollout', 'rollback', 'test_instructions', 'configuration', 'risks')
KINDS = {'integrate': 'integration_recipe', 'record_package': 'delivery_package', 'deploy_test': 'deployment_recipe'}


def latest(db, p, action):
    return db.scalars(select(Artifact).where(Artifact.plat_id == p.id,
        Artifact.kind == KINDS[action]).order_by(Artifact.id.desc())).first()


def reference(db, p, action):
    row = latest(db, p, action)
    return {'version': row.id, 'sha256': row.sha256} if row else None


def _save(db, p, action, data, version, who):
    prior = latest(db, p, action)
    if version != (prior.id if prior else 0):
        raise Conflict('shared delivery content changed; refresh before saving')
    row = Artifact(plat_id=p.id, kind=KINDS[action], name=KINDS[action] + '.json',
                   body=json.dumps(data), sha256=L.digest(data))
    record(db, plat_id=p.id, actor='human', actor_detail=who, kind='delivery',
           decision=f'{KINDS[action]} shared', inputs={'sha256': row.sha256}, apply=lambda: db.add(row))
    db.flush()
    return {'version': row.id, 'sha256': row.sha256}


def _snapshot(db, p):
    w = L.locked(db, p)
    if p.status != 'delivered' or not p.delivered_at:
        raise ValueError('complete local delivery before preparing delivery actions')
    snap = L.revisions(db, p)
    if not snap or any(r.get('dirty') for r in snap.values()):
        raise ValueError('current clean revisions are required')
    if w.spec_hash != L.current_spec(db, p):
        raise ValueError('accepted specification changed')
    return {'revisions': snap, 'spec_hash': w.spec_hash}


def publish_recipe(db, cfg, p, data, version):
    snapshot = _snapshot(db, p)
    lots = db.scalars(select(Lot).where(Lot.plat_id == p.id)).all()
    if any((l.config or {}).get('host_id') != host_id() or
           (l.config or {}).get('executor_id') != cfg.origin_id for l in lots):
        raise ValueError('publish integration recipes on the owning installation and host')
    if not isinstance(data, dict) or set(data) != {'checks'}:
        raise ValueError('recipe must contain checks only')
    checks = data['checks']
    if not isinstance(checks, list) or not 1 <= len(checks) <= 50:
        raise ValueError('provide 1–50 integration checks')
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'name', 'lot', 'cmd'}:
            raise ValueError('each check requires only name, lot, and cmd')
        if not isinstance(check['lot'], str) or check['lot'] not in {l.key for l in lots} or any(
            not isinstance(check[k], str) or not check[k].strip() or len(check[k]) > limit
            for k, limit in (('name', 160), ('cmd', 4000))):
            raise ValueError('checks need a known lot, a name, and a bounded command')
    return _save(db, p, 'integrate', {**snapshot, 'checks': checks,
        'installation': cfg.origin_id, 'host': host_id()}, version, actor())


def publish_deployment(db, cfg, p, data, version):
    snapshot = _snapshot(db, p)
    lots = db.scalars(select(Lot).where(Lot.plat_id == p.id)).all()
    if any((l.config or {}).get('host_id') != host_id() or
           (l.config or {}).get('executor_id') != cfg.origin_id for l in lots):
        raise ValueError('publish deployment recipes on the owning installation and host')
    keys = {'environment', 'target', 'artifact', 'rollback_artifact', 'lot', 'deploy', 'smoke', 'rollback'}
    if not isinstance(data, dict) or set(data) != keys or any(
            not isinstance(data[k], str) or not data[k].strip() or len(data[k]) > 4000 for k in keys):
        raise ValueError('deployment recipe requires only environment, target, artifact, rollback_artifact, lot, deploy, smoke, rollback (1–4000 characters each)')
    if data['environment'] not in ('local', 'dev', 'demo', 'beta'):
        raise ValueError('only local/dev/demo/beta test environments are supported')
    if data['lot'] not in {l.key for l in lots}:
        raise ValueError('deployment lot does not exist')
    package = L.latest(db, p, 'recording')
    if not package or package.content.get('revisions') != snapshot['revisions'] or package.content.get('spec_hash') != snapshot['spec_hash']:
        raise Conflict('record a current delivery package before publishing deployment')
    return _save(db, p, 'deploy_test', {**snapshot, **data, 'package_hash': package.sha256,
        'installation': cfg.origin_id, 'host': host_id()}, version, actor())


def share_package(db, p, fields, version, who):
    snapshot = _snapshot(db, p)
    if not isinstance(fields, dict) or set(fields) != set(FIELDS) or any(
        not isinstance(fields[k], str) or not fields[k].strip() or len(fields[k]) > 8000 for k in FIELDS):
        raise ValueError('all six package fields require 1–8000 characters')
    return _save(db, p, 'record_package', {**snapshot, 'fields': fields, 'shared_by': who}, version, who)


def validate(db, p, action, *, observe=False):
    w = db.get(Workflow, p.id)
    allowed = ('development', 'integration', 'recording', 'deployment', 'validation', 'ready') if action == 'integrate' else ('deployment',) if action == 'deploy_test' else ('recording',)
    if not w or w.stage not in allowed or p.status != 'delivered' or not p.delivered_at:
        raise Conflict('delivery action is not available at the current workflow stage')
    row = latest(db, p, action)
    if not row:
        raise Conflict('share the delivery content before requesting this action')
    data = json.loads(row.body)
    if data['spec_hash'] != w.spec_hash or L.current_spec(db, p) != w.spec_hash:
        raise Conflict('shared delivery specification is stale')
    if observe and data['revisions'] != L.revisions(db, p):
        raise Conflict('shared delivery revisions are stale; republish after reviewing changes')
    if action == 'record_package':
        integration = L.latest(db, p, 'integration')
        if not integration or not integration.content.get('passed') or integration.content.get('revisions') != data['revisions']:
            raise Conflict('successful integration of these revisions is required')
    if action == 'deploy_test':
        package = L.latest(db, p, 'recording')
        if not package or package.sha256 != data['package_hash'] or package.content.get('revisions') != data['revisions']:
            raise Conflict('deployment package changed; review and republish the recipe')
    return data


def execute(db, cfg, p, action, version, who):
    data = validate(db, p, action, observe=True)
    if action in ('integrate', 'deploy_test'):
        if data['installation'] != cfg.origin_id or data['host'] != host_id():
            raise Conflict('delivery recipe belongs to another machine')
        if action == 'deploy_test':
            return L.deploy(db, cfg, p, {**data, 'requested_by': who,
                'shared_artifact': reference(db, p, action)}, version)
        return L.integration(db, cfg, p, {'checks': data['checks']}, version)
    return L.accept(db, p, 'recording', {**data['fields'], 'requested_by': who,
        'shared_artifact': reference(db, p, action)}, version)


def project(db, p):
    out = {}
    revisions = L.readiness(db, p).get('revisions')
    recorded = L.latest(db, p, 'recording')
    deployment = L.latest(db, p, 'deployment')
    for action in KINDS:
        row = latest(db, p, action)
        out[action] = ({'version': row.id, 'sha256': row.sha256, 'created': row.created_at,
                        **json.loads(row.body)} if row else None)
        if row:
            out[action]['freshness'] = ('unverified' if revisions is None else
                'current' if out[action]['revisions'] == revisions else 'stale')
            if action == 'deploy_test':
                out[action]['package_current'] = bool(recorded and recorded.sha256 == out[action]['package_hash'])
                out[action]['result'] = ({'passed': deployment.content.get('passed'),
                    'version': deployment.version,
                    'results': {k: {'exit_code': v.get('exit_code')} for k, v in deployment.content.get('results', {}).items()}}
                    if deployment and deployment.content.get('shared_artifact', {}).get('version') == row.id else None)
            out[action]['recorded'] = bool(action == 'record_package' and recorded and
                recorded.content.get('shared_artifact', {}).get('version') == row.id)
    return out
