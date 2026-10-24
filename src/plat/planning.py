"""Explicitly shared planning drafts; browser and CLI accept the same snapshots."""
import json

import yaml
from sqlalchemy import select

from . import guidance
from . import lifecycle as L
from .decisions import record
from .models import Artifact, Lot, Workflow
from .ownership import actor

STAGES = ('context', 'definition', 'specification', 'planning')


def latest(db, p, stage):
    return db.scalars(select(Artifact).where(Artifact.plat_id == p.id,
        Artifact.kind == 'workflow_draft', Artifact.name == stage).order_by(Artifact.id.desc())).first()


def preview(db, p):
    row = db.scalars(select(Artifact).where(Artifact.plat_id == p.id,
        Artifact.kind == 'planning_preview').order_by(Artifact.id.desc())).first()
    return json.loads(row.body) if row else None


def _append(db, p, kind, name, body, who):
    row = Artifact(plat_id=p.id, kind=kind, name=name, body=json.dumps(body), sha256=L.digest(body))
    record(db, plat_id=p.id, actor='human', actor_detail=who, kind='workflow',
           decision=f'{name} shared for review', inputs={'kind': kind, 'sha256': row.sha256},
           apply=lambda: db.add(row))
    return row


def _stage(db, p, stage, version):
    if type(version) is not int:
        raise L.WorkflowError('current workflow version is required')
    w = L.locked(db, p, version)
    if not guidance.guided(w):
        raise L.WorkflowError('Room planning requires a guided workflow; classic acceptance remains available in the CLI')
    if stage not in STAGES or w.stage != stage or p.status != 'planned':
        raise L.WorkflowError('refresh the current planning stage before sharing or accepting')
    return w


def validate_shape(stage, data):
    """Allow unfinished drafts, but keep the native editor's types predictable."""
    text_fields = {'context': ('summary',), 'definition': ('problem', 'outcome', 'scope'),
                   'specification': ('note', 'behavior', 'exclusions'), 'planning': ('note',)}[stage]
    list_fields = {'context': ('blockers',), 'definition': ('success_measures', 'open_questions'),
                   'specification': ('open_questions',), 'planning': ()}[stage]
    allowed = set(text_fields + list_fields) | {'context': {'sources'}, 'definition': set(),
                'specification': {'outcomes'}, 'planning': {'alignment'}}[stage]
    if set(data) - allowed:
        raise L.WorkflowError('draft contains unsupported fields')
    for key in text_fields:
        if key in data and not isinstance(data[key], str):
            raise L.WorkflowError(f'{key} must be text')
    for key in list_fields:
        if key in data and (not isinstance(data[key], list) or any(not isinstance(x, str) for x in data[key])):
            raise L.WorkflowError(f'{key} must be a list of text')
    for key, fields in [('sources', {'reference', 'status', 'snapshot', 'reason'}), ('outcomes', {'id', 'statement'})]:
        if key in data and (not isinstance(data[key], list) or any(not isinstance(x, dict) or
                set(x) - fields or any(not isinstance(v, str) for v in x.values()) for x in data[key])):
            raise L.WorkflowError(f'{key} must contain text fields only')
    if 'alignment' in data and (not isinstance(data['alignment'], dict) or any(
            not isinstance(v, list) or any(not isinstance(x, str) for x in v) for v in data['alignment'].values())):
        raise L.WorkflowError('alignment must map outcome IDs to lists of criterion IDs')


def share(db, p, stage, data, version, draft_version, *, who=None, publish_plan=False):
    w = _stage(db, p, stage, version)
    who = who or actor()
    prior = latest(db, p, stage)
    if type(draft_version) is not int or draft_version != (prior.id if prior else 0):
        raise L.WorkflowError('shared draft changed; refresh before editing')
    if not isinstance(data, dict) or len(json.dumps(data)) > 48000:
        raise L.WorkflowError('draft must be an object of at most 48000 characters')
    validate_shape(stage, data)
    # Incomplete drafts may be shared to collect questions; acceptance uses the real lifecycle validators.
    plan_hash = None
    if stage == 'planning':
        plan_hash = L.current_spec(db, p)
        if publish_plan:
            source = db.scalars(select(Artifact).where(Artifact.plat_id == p.id,
                Artifact.kind == 'plat_map').order_by(Artifact.id.desc())).first()
            plan = yaml.safe_load(source.body)
            # Explicit CLI publication shares this exact executable plan, including gate commands.
            _append(db, p, 'planning_preview', 'registered plan',
                    {'plan_hash': plan_hash, 'plan': plan, 'shared_by': who,
                     'specification': L.latest(db, p, 'specification').content,
                     'specification_hash': L.latest(db, p, 'specification').sha256,
                     'budget_usd': p.budget_usd, 'execution': [{'lot': l.key, 'phases': (l.config or {}).get('phases', []), 'base_ref': l.base_ref}
                         for l in db.scalars(select(Lot).where(Lot.plat_id == p.id).order_by(Lot.key))]}, who)
        shown = preview(db, p)
        if (not shown or shown['plan_hash'] != plan_hash or
                shown['specification_hash'] != L.latest(db, p, 'specification').sha256):
            raise L.WorkflowError('share the current registered plan from the owning CLI with workflow share planning first')
    row = _append(db, p, 'workflow_draft', stage, {'stage': stage, 'fields': data,
        'workflow_version': w.version, 'plan_hash': plan_hash, 'shared_by': who}, who)
    return {'draft_version': row.id, 'sha256': row.sha256, 'workflow_version': w.version}


def accept(db, p, stage, version, draft_version, sha256, *, who=None):
    _stage(db, p, stage, version)
    row = latest(db, p, stage)
    if not row or type(draft_version) is not int or row.id != draft_version or row.sha256 != sha256:
        raise L.WorkflowError('shared draft changed; review the latest saved content')
    body = json.loads(row.body)
    if body['workflow_version'] != version:
        raise L.WorkflowError('draft belongs to an earlier workflow revision; review and share it again')
    if stage == 'planning' and body['plan_hash'] != L.current_spec(db, p):
        raise L.WorkflowError('registered plan changed; publish and review the current plan')
    return L.accept(db, p, stage, {**body['fields'], 'shared_draft': {
        'version': row.id, 'sha256': row.sha256}}, version, who=who)


def project(db, p):
    w = db.get(Workflow, p.id)
    drafts = {}
    for stage in STAGES:
        row = latest(db, p, stage)
        if row:
            body = json.loads(row.body)
            drafts[stage] = {**body, 'draft_version': row.id, 'sha256': row.sha256,
                            'current': bool(w and body['workflow_version'] == w.version and w.stage == stage)}
    shown = preview(db, p)
    if shown:
        try:
            spec = L.latest(db, p, 'specification')
            shown['current'] = bool(spec and shown['plan_hash'] == L.current_spec(db, p)
                                    and shown['specification_hash'] == spec.sha256)
        except L.WorkflowError:
            shown['current'] = False
        if 'planning' in drafts:
            drafts['planning']['current'] = drafts['planning']['current'] and shown['current']
    revision = L.latest(db, p, 'revision')
    shared_revision = ({**revision.content, 'actor': revision.actor, 'version': revision.version}
                       if revision and revision.content.get('shared') else None)
    return {'revision': shared_revision, 'enabled': guidance.guided(w), 'stage': w.stage if w else None,
            'version': w.version if w else None, 'drafts': drafts, 'plan_preview': shown}
