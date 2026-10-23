"""Engineer-led guidance. Evidence is attributed, never inferred from a checklist."""
import subprocess

from sqlalchemy import select

from . import lifecycle as L
from .models import Attempt, Criterion, ExecutionClaim, Lot, Workflow

STEPS = {
    'context': 'Read the ticket and related sources; resolve blocking questions.',
    'definition': 'Agree on the problem, intended outcome, scope, and success measures.',
    'specification': 'Review behavior, outcomes, acceptance criteria, and exclusions with the engineer.',
    'planning': 'Review the executable plan and map every approved outcome to its criteria.',
    'development': 'Execute the approved plan, add regression coverage, and review the resulting changes.',
    'integration': 'Run the integration checks against the current committed worktrees.',
    'recording': 'Review and record rollout, rollback, configuration, risks, and test instructions.',
    'deployment': 'Review the target and commands before authorizing a test deployment.',
    'deploying': 'Observe the deployment; inspect interrupted work before recovery.',
    'validation': 'Review the deployed behavior against the ticket outcomes and record acceptance.',
    'ready': 'Review remaining handoff items and record shipment when it actually happens.',
    'closed': 'Shipment recorded; retain the evidence for future work.',
}
CHECKS = {
    'testing': 'New or existing automated tests cover the intended outcomes',
    'engineer_review': 'An engineer reviewed the changes, results, and tradeoffs',
    'conformance': 'Implementation meets the approved specification and plan',
    'documentation': 'Required repository and operational documentation is updated',
    'merge_request': 'A reviewable PR or MR is created and linked',
    'communication': 'The appropriate people received the reviewed handoff',
}


def guided(w):
    return bool(w and (w.requirements or {}).get('process') == 'guided-v1')


def specification(db, plat, data):
    L._required(data, 'note', 'behavior', 'outcomes', 'exclusions')
    if not all(isinstance(data[k], str) and data[k].strip() for k in ('note', 'behavior', 'exclusions')):
        raise L.WorkflowError('note, behavior, and exclusions must be nonempty text')
    outcomes = data['outcomes']
    if not isinstance(outcomes, list) or not outcomes:
        raise L.WorkflowError('outcomes must be a nonempty list of id/statement objects')
    ids = []
    for outcome in outcomes:
        if not isinstance(outcome, dict) or any(not isinstance(outcome.get(k), str) or not outcome[k].strip()
                                               for k in ('id', 'statement')):
            raise L.WorkflowError('each outcome requires an id and statement')
        ids.append(outcome['id'])
    if len(set(ids)) != len(ids):
        raise L.WorkflowError('outcome IDs must be unique')
    if data.get('open_questions'):
        raise L.WorkflowError('resolve specification questions before accepting')
    return {**data, 'context_hash': L.latest(db, plat, 'context').sha256,
            'definition_hash': L.latest(db, plat, 'definition').sha256}


def planning(db, plat, data):
    L._required(data, 'note', 'alignment')
    if not isinstance(data['note'], str) or not data['note'].strip():
        raise L.WorkflowError('note must be nonempty text')
    spec = L.latest(db, plat, 'specification')
    if not spec or 'outcomes' not in spec.content:
        raise L.WorkflowError('accept the behavior specification first')
    mapping = data['alignment']
    criteria = {c.ac_id for c in db.scalars(select(Criterion).where(Criterion.plat_id == plat.id))}
    expected = {o['id'] for o in spec.content['outcomes']}
    if not isinstance(mapping, dict) or set(mapping) != expected:
        raise L.WorkflowError('alignment must map every approved outcome exactly once')
    for ids in mapping.values():
        if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids)
                or len(set(ids)) != len(ids) or not set(ids) <= criteria):
            raise L.WorkflowError('each outcome must name existing acceptance criterion IDs')
    if set().union(*(set(ids) for ids in mapping.values())) != criteria:
        raise L.WorkflowError('map every criterion to an outcome; revise the specification for new scope')
    return {**data, 'specification_hash': spec.sha256, 'plan_hash': L.current_spec(db, plat)}


def revise(db, plat, stage, note, expected):
    w = L.locked(db, plat, expected)
    stages = ('context', 'definition', 'specification', 'planning', 'development')
    if not guided(w) or stage not in stages[:-1] or w.stage not in stages:
        raise L.WorkflowError('revision is available for unstarted guided context, definition, specification, or planning')
    if stages.index(stage) > stages.index(w.stage):
        raise L.WorkflowError('revision cannot skip ahead')
    if not isinstance(note, str) or not note.strip():
        raise L.WorkflowError('explain the requested revision')
    from .ownership import interactive_writers
    lots = db.scalars(select(Lot).where(Lot.plat_id == plat.id)).all()
    if (plat.status != 'planned' or db.scalar(select(Attempt.id).join(Lot).where(Lot.plat_id == plat.id).limit(1))
            or db.scalar(select(ExecutionClaim.lot_id).join(Lot).where(
                Lot.plat_id == plat.id, ExecutionClaim.active.is_(True)).limit(1))
            or any(interactive_writers(db, lot) for lot in lots)):
        raise L.WorkflowError('stop active work; started scope remains immutable, use a new plat for changed scope')
    # This is flushed by save's audited decision transaction, never independently.
    w.spec_hash = None
    return L.save(db, plat, w, 'revision', {'note': note, 'from': w.stage, 'to': stage}, stage)


def record_check(db, plat, check, data, expected, *, who=None):
    w = L.locked(db, plat, expected)
    if check not in CHECKS:
        raise L.WorkflowError('unknown guidance check')
    if plat.status != 'delivered' or not plat.delivered_at:
        raise L.WorkflowError('record handoff evidence after local delivery')
    L._required(data, 'note', 'disposition')
    if not isinstance(data['note'], str) or not data['note'].strip():
        raise L.WorkflowError('a concrete evidence note or exception reason is required')
    if data['disposition'] not in ('recorded', 'not_applicable'):
        raise L.WorkflowError('disposition must be recorded or not_applicable')
    refs = data.get('references', [])
    if not isinstance(refs, list) or any(not isinstance(r, str) or not r.strip() for r in refs):
        raise L.WorkflowError('references must be a list of nonempty strings')
    snap = L.revisions(db, plat)
    if any(r['dirty'] for r in snap.values()) or w.spec_hash != L.current_spec(db, plat):
        raise L.WorkflowError('commit changes and accept the current plan before recording handoff evidence')
    return L.save(db, plat, w, 'check_' + check, {**data, 'references': refs,
                  'revisions': snap, 'plan_hash': w.spec_hash}, w.stage, who=who, human=True)


def project(db, plat):
    w = db.get(Workflow, plat.id)
    stage = w.stage if w else 'legacy'
    checks = []
    try:
        snap = L.revisions(db, plat) if w else None
    except (OSError, ValueError, subprocess.CalledProcessError):
        snap = None
    for key, title in CHECKS.items():
        row = L.latest(db, plat, 'check_' + key)
        status = 'pending'
        if row:
            status = ('unverified' if snap is None else 'stale' if
                      row.content.get('revisions') != snap or row.content.get('plan_hash') != w.spec_hash
                      else row.content['disposition'])
        checks.append({'key': key, 'title': title, 'status': status,
                       'actor': row.actor if row else None, 'version': row.version if row else None})
    return {'process': 'guided-v1' if guided(w) else 'classic', 'stage': stage,
            'next_step': STEPS.get(stage, 'Use workflow begin for the guided process on new work.'),
            'checks': checks, 'advisory': True}
