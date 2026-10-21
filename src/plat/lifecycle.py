"""Versioned human acceptance and independently observed delivery evidence."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select

from .decisions import record
from .models import Artifact, CollaborationItem, Lot, Plat, StageRecord, Workflow
from .ownership import actor


class WorkflowError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def latest(db, plat, stage):
    return db.scalars(select(StageRecord).where(StageRecord.plat_id == plat.id,
        StageRecord.stage == stage).order_by(StageRecord.version.desc())).first()


def current_spec(db, plat):
    row = db.scalars(select(Artifact).where(Artifact.plat_id == plat.id, Artifact.kind == 'plat_map')
                     .order_by(Artifact.id.desc())).first()
    if not row:
        raise WorkflowError('commit a plan before accepting its specification')
    return row.sha256


def canonical_repo(wt):
    result = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=wt,
                            capture_output=True, text=True)
    raw = result.stdout.strip()
    if not raw:
        return 'local:' + str(Path(wt).resolve())
    if '://' in raw:
        url = urlsplit(raw)
        if url.hostname:
            return f'{url.hostname.lower()}{url.path.removesuffix(".git").rstrip("/")}'
        return 'local:' + str(Path(url.path).resolve())
    if '@' in raw and ':' in raw:
        host, path = raw.split(':', 1)
        return host.split('@')[-1].lower() + '/' + path.removesuffix('.git').rstrip('/')
    return 'local:' + str(Path(raw).resolve())


def local_revision(lot):
    from .runner import _worktree_fingerprint
    wt = Path(lot.worktree_path)
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=wt, capture_output=True,
                          text=True, check=True).stdout.strip()
    dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=wt, capture_output=True,
                           text=True, check=True).stdout.strip()
    return {'repository': canonical_repo(wt), 'commit': head,
            'fingerprint': _worktree_fingerprint(wt), 'dirty': bool(dirty)}


def revisions(db, plat):
    from .ownership import host_id
    out = {}
    for lot in db.scalars(select(Lot).where(Lot.plat_id == plat.id).order_by(Lot.key)):
        config = lot.config or {}
        if config.get('host_id') and config['host_id'] != host_id():
            observed = config.get('observed_revision')
            if not observed:
                raise WorkflowError(f'{lot.key}: no revision observation from the owning host')
            out[lot.key] = observed
        else:
            out[lot.key] = local_revision(lot)
    return out


def begin(db, plat, owner):
    if db.get(Workflow, plat.id):
        raise WorkflowError('workflow already exists')
    if plat.status != 'planned':
        raise WorkflowError('begin the lifecycle before development')
    if not owner.strip():
        raise WorkflowError('a responsible owner is required')
    w = Workflow(plat_id=plat.id, owner=owner, stage='context', version=0)
    record(db, plat_id=plat.id, actor='human', actor_detail=actor(), kind='workflow',
           decision='full lifecycle enabled', inputs={'owner': owner}, apply=lambda: db.add(w))
    return w


def locked(db, plat, expected=None):
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    db.refresh(plat)
    if plat.closed_at is not None:
        raise WorkflowError('shipped work is immutable; use a new plat')
    w = db.scalars(select(Workflow).where(Workflow.plat_id == plat.id).with_for_update()
                   .execution_options(populate_existing=True)).first()
    if not w:
        raise WorkflowError('enable the lifecycle with workflow begin first')
    if expected is not None and w.version != expected:
        raise WorkflowError(f'stale version {expected}; current version is {w.version}')
    return w


def save(db, plat, w, stage, content, next_stage, *, who=None):
    row = StageRecord(plat_id=plat.id, stage=stage, version=w.version + 1,
                      content=content, sha256=digest(content), actor=who or actor())
    def apply():
        w.version += 1
        w.stage = next_stage
        w.updated_at = datetime.now(timezone.utc)
        db.add(row)
    record(db, plat_id=plat.id, actor='human' if who is None else 'system',
           actor_detail=who or actor(), kind='workflow', decision=f'{stage} recorded; next: {next_stage}',
           inputs={'version': row.version, 'sha256': row.sha256}, apply=apply)
    return row


def _required(data, *keys):
    if not isinstance(data, dict):
        raise WorkflowError('stage input must be an object')
    for key in keys:
        if not data.get(key):
            raise WorkflowError(f'{key} is required')


def accept(db, plat, stage, data, expected):
    w = locked(db, plat, expected)
    if stage != w.stage:
        raise WorkflowError(f'expected {w.stage}, received {stage}')
    if stage == 'context':
        _required(data, 'sources', 'summary')
        if not isinstance(data['sources'], list):
            raise WorkflowError('sources must be a list')
        for source in data['sources']:
            _required(source, 'reference', 'status')
            if source['status'] not in ('collected', 'unavailable'):
                raise WorkflowError('source status must be collected or unavailable')
            _required(source, 'snapshot' if source['status'] == 'collected' else 'reason')
        if data.get('blockers'):
            raise WorkflowError('resolve blocking context questions before accepting')
        next_stage = 'definition'
    elif stage == 'definition':
        _required(data, 'problem', 'outcome', 'scope', 'success_measures')
        if data.get('open_questions'):
            raise WorkflowError('answer definition questions before accepting')
        next_stage = 'specification'
    elif stage == 'specification':
        _required(data, 'note')
        spec_hash = current_spec(db, plat)
        data = {**data, 'spec_hash': spec_hash,
                'context_hash': latest(db, plat, 'context').sha256,
                'definition_hash': latest(db, plat, 'definition').sha256}
        w.spec_hash = spec_hash
        next_stage = 'development'
    elif stage == 'recording':
        _required(data, 'summary', 'rollout', 'rollback', 'test_instructions', 'configuration', 'risks')
        integration = latest(db, plat, 'integration')
        snap = revisions(db, plat)
        if not integration or not integration.content.get('passed') or integration.content['revisions'] != snap:
            raise WorkflowError('integration evidence is stale; rerun integration')
        data = {**data, 'revisions': snap, 'spec_hash': w.spec_hash,
                'integration_hash': integration.sha256}
        next_stage = 'deployment'
    elif stage == 'validation':
        _required(data, 'note', 'checks', 'accepted')
        if data['accepted'] is not True:
            raise WorkflowError('validation has not been accepted')
        deployment = latest(db, plat, 'deployment')
        snap = revisions(db, plat)
        if not deployment or not deployment.content.get('passed') or deployment.content['revisions'] != snap:
            raise WorkflowError('a successful deployment of current revisions is required')
        data = {**data, 'revisions': snap, 'deployment_hash': deployment.sha256}
        next_stage = 'ready'
    else:
        raise WorkflowError('this stage requires an independently executed command')
    return save(db, plat, w, stage, data, next_stage)


def require_accepted(db, plat):
    w = db.get(Workflow, plat.id)
    if w and (w.stage != 'development' or w.spec_hash != current_spec(db, plat)):
        raise WorkflowError('accept the current context, definition, and specification before starting')


def integration(db, cfg, plat, commands, expected):
    from .ownership import execution
    w = locked(db, plat, expected)
    if w.stage not in ('development', 'integration', 'recording', 'deployment', 'validation', 'ready'):
        raise WorkflowError('development must be accepted before integration')
    lots = db.scalars(select(Lot).where(Lot.plat_id == plat.id)).all()
    if not lots or any(l.state != 'DONE' for l in lots) or plat.status != 'delivered' or plat.delivered_at is None:
        raise WorkflowError('all lots and run-level criteria must be locally delivered')
    snap = revisions(db, plat)
    if any(r['dirty'] for r in snap.values()):
        raise WorkflowError('commit the worktrees before integration')
    if current_spec(db, plat) != w.spec_hash:
        raise WorkflowError('accepted specification changed')
    _required(commands, 'checks')
    if not isinstance(commands['checks'], list):
        raise WorkflowError('checks must be a list')
    by_key = {l.key: l for l in lots}
    for check in commands['checks']:
        _required(check, 'lot', 'cmd')
        if check['lot'] not in by_key or not isinstance(check['cmd'], str):
            raise WorkflowError('each integration check needs a valid lot and command')
    save(db, plat, w, 'integration_requested', {'revisions': snap, **commands}, 'integration')
    request_version = w.version
    db.commit()
    results = []
    for check in commands['checks']:
        lot = by_key[check['lot']]
        with execution(db, cfg, plat, lot):
            from .runner import _Heartbeat
            with _Heartbeat(lot.id):
                rc, output = execute_command(db, cfg, plat, lot, 'integration', check['cmd'])
            results.append({**check, 'exit_code': rc, 'output': output[-12000:]})
    w = locked(db, plat, request_version)
    passed = all(r['exit_code'] == 0 for r in results) and revisions(db, plat) == snap
    return save(db, plat, w, 'integration', {'checks': results, 'revisions': snap, 'passed': passed},
                'recording' if passed else 'integration', who='supervisor.integration')


def deploy(db, cfg, plat, data, expected):
    from .ownership import execution
    w = locked(db, plat, expected)
    if w.stage != 'deployment':
        raise WorkflowError('record the delivery package before deploying')
    db.refresh(plat)
    if plat.status != 'delivered' or plat.delivered_at is None:
        raise WorkflowError('local delivery must still be valid before deployment')
    _required(data, 'environment', 'lot', 'deploy', 'smoke', 'rollback', 'artifact')
    if data['environment'] not in ('local', 'dev', 'demo', 'beta'):
        raise WorkflowError('only local/dev/demo/beta test environments are supported')
    for key in ('deploy', 'smoke', 'rollback', 'artifact'):
        if not isinstance(data[key], str) or not data[key].strip():
            raise WorkflowError(f'{key} must be a nonempty string')
    package = latest(db, plat, 'recording')
    snap = revisions(db, plat)
    if not package or package.content['revisions'] != snap:
        raise WorkflowError('package is stale; rerun integration and recording')
    lot = db.scalars(select(Lot).where(Lot.plat_id == plat.id, Lot.key == data['lot'])).first()
    if not lot:
        raise WorkflowError('deployment lot does not exist')
    with execution(db, cfg, plat, lot):
        w = locked(db, plat, expected)
        save(db, plat, w, 'deployment_requested', {**data, 'revisions': snap}, 'deploying')
        request_version = w.version
        db.commit()  # record authorization before a command can have an external effect
        results = {}
        from .runner import _Heartbeat
        with _Heartbeat(lot.id):
            for key in ('deploy', 'smoke'):
                rc, output = execute_command(db, cfg, plat, lot, key, data[key])
                results[key] = {'exit_code': rc, 'output': output[-12000:]}
                if rc:
                    break
            passed = len(results) == 2 and all(r['exit_code'] == 0 for r in results.values())
            if not passed:
                rc, output = execute_command(db, cfg, plat, lot, 'rollback', data['rollback'])
                results['rollback'] = {'exit_code': rc, 'output': output[-12000:]}
        w = locked(db, plat, request_version)
        passed = passed and revisions(db, plat) == snap
        result = save(db, plat, w, 'deployment', {**data, 'results': results, 'passed': passed,
            'revisions': snap, 'package_hash': package.sha256}, 'validation' if passed else 'deployment',
            who='supervisor.deployment')
        db.commit()
        return result


def readiness(db, plat):
    w = db.get(Workflow, plat.id)
    missing = []
    if not w:
        return {'ready': False, 'missing': ['full lifecycle is not enabled']}
    try:
        snap = revisions(db, plat)
    except (OSError, subprocess.CalledProcessError, WorkflowError):
        return {'ready': False, 'missing': ['live worktrees unavailable; historical stage evidence is retained'],
                'version': w.version}
    if w.stage not in ('ready', 'closed'):
        missing.append(f'workflow stage: {w.stage}')
    try:
        if w.spec_hash != current_spec(db, plat):
            missing.append('specification changed')
    except WorkflowError:
        missing.append('committed specification is missing')
    if plat.status not in ('delivered', 'closed') or plat.delivered_at is None:
        missing.append('local run-level delivery is not established')
    lots = db.scalars(select(Lot).where(Lot.plat_id == plat.id)).all()
    if any(l.state != 'DONE' for l in lots):
        missing.append('incomplete lots')
    for stage in ('integration', 'recording', 'deployment', 'validation'):
        row = latest(db, plat, stage)
        if not row or row.content.get('revisions') != snap:
            missing.append(f'{stage} evidence missing or stale')
    for item in db.scalars(select(CollaborationItem).where(CollaborationItem.plat_id == plat.id)):
        if item.kind in ('question', 'review') and item.state != 'resolved':
            missing.append(f'open {item.kind}: {item.public_id}')
        if item.kind == 'review' and item.state == 'resolved' and item.body.get('revisions') != snap:
            missing.append(f'stale review: {item.public_id}')
    return {'ready': not missing, 'missing': missing, 'revisions': snap, 'version': w.version}


def abstract(db, plat):
    w = db.get(Workflow, plat.id)
    stages = db.scalars(select(StageRecord).where(StageRecord.plat_id == plat.id)
                        .order_by(StageRecord.version)).all()
    return {'anchor': plat.anchor, 'title': plat.title, 'status': plat.status,
            'stage': w.stage if w else 'legacy', 'readiness': readiness(db, plat),
            'records': [{'stage': r.stage, 'version': r.version, 'actor': r.actor,
                         'sha256': r.sha256, 'content': r.content} for r in stages]}


def execute_command(db, cfg, plat, lot, phase, command):
    from sqlalchemy import update

    from . import db as DB
    from . import gates
    from .models import Attempt, Verdict
    from .ownership import current, host_id
    claim = current.get()
    db.refresh(plat)
    db.refresh(lot)
    if plat.status in ('aborted', 'closed', 'paused') or lot.state == 'ABORTED':
        raise WorkflowError('execution stopped before the next command; inspect the environment before recovery')
    attempt = Attempt(lot_id=lot.id, phase=phase, n=lot.attempt, provider="supervisor", model="local",
                      executor_id=cfg.origin_id, host_id=host_id(), claim_generation=claim[2] if claim else None,
                      role_binding={"cmd": command})
    record(db, plat_id=plat.id, lot_id=lot.id, actor="system", actor_detail="lifecycle.execute",
           kind="deployment" if phase != "integration" else "integration", decision=f"{phase} command started",
           inputs={"cmd": command}, apply=lambda: db.add(attempt))
    db.commit()
    def pid(number, cmd):
        with DB.session() as telemetry:
            telemetry.execute(update(Attempt).where(Attempt.id == attempt.id).values(pid=number))
    process = gates._sh(command, Path(lot.worktree_path), on_pid=pid)
    db.refresh(attempt)
    def finish():
        attempt.finished_at = datetime.now(timezone.utc)
        attempt.exit_code, attempt.pid = process.returncode, None
        db.add(Verdict(attempt_id=attempt.id, raw={"cmd": command, "exit_code": process.returncode,
            "output": (process.stdout + process.stderr)[-12000:]}))
    record(db, plat_id=plat.id, lot_id=lot.id, actor="system", actor_detail="lifecycle.execute",
           kind="observation", decision=f"{phase} command finished", inputs={"exit_code": process.returncode},
           apply=finish)
    db.commit()
    return process.returncode, process.stdout + process.stderr


def recover(db, plat, note, expected):
    from .models import ExecutionClaim
    w = locked(db, plat, expected)
    if w.stage not in ("integration", "deploying") or not note.strip():
        raise WorkflowError("recovery requires an interrupted integration/deployment and an inspection note")
    active = db.scalars(select(ExecutionClaim).join(Lot).where(
        Lot.plat_id == plat.id, ExecutionClaim.active.is_(True))).first()
    if active:
        raise WorkflowError("reconcile execution ownership on the owning host first")
    return save(db, plat, w, "recovery", {"note": note, "was": w.stage},
                "deployment" if w.stage == "deploying" else "integration")
