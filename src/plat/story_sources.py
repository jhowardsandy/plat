"""Explicit source observations and requirement decisions; never execute changed scope."""
import json
import subprocess
import tempfile
from datetime import datetime, timezone

from sqlalchemy import select

from . import intake
from . import lifecycle as L
from .decisions import record
from .models import Artifact
from .ownership import actor
from .spec import NAME


def fetch(cfg, key, captain):
    if not isinstance(key, str) or len(key) > 160 or not NAME.fullmatch(key):
        raise L.WorkflowError('use a safe ticket key')
    argv = (cfg.tracker or {}).get('story_argv')
    if (not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv)
            or argv[0].startswith('-') or '{anchor}' in argv[0] or not any('{anchor}' in a for a in argv[1:])):
        raise L.WorkflowError('configure tracker.story_argv with a read-only story adapter and {anchor} argument')
    # Only administrator configuration supplies commands. Browser input is one validated key.
    # Files bound memory usage; never expose connector stderr or credentials in a response.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run([a.replace('{anchor}', key) for a in argv],
                stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=30, shell=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise L.WorkflowError('story source unavailable; check the configured adapter') from exc
        out.seek(0); raw = out.read(48001)
    if result.returncode or len(raw) > 48000:
        raise L.WorkflowError('story source failed or exceeded 48000 bytes; no snapshot accepted')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise L.WorkflowError('story source must return a JSON story object') from exc
    if not isinstance(data, dict) or data.get('key') != key:
        raise L.WorkflowError('story source returned a different ticket key')
    snapshot = intake.preview({**data, 'captain': captain})
    return {**snapshot, 'provenance': 'configured_source',
            'observed_at': datetime.now(timezone.utc).isoformat()}


def rows(db, p, kind):
    return db.scalars(select(Artifact).where(Artifact.plat_id == p.id,
        Artifact.kind == kind).order_by(Artifact.id)).all()


def append(db, p, kind, body, who):
    row = Artifact(plat_id=p.id, kind=kind, name=p.anchor,
                   body=json.dumps(body), sha256=L.digest(body))
    record(db, plat_id=p.id, actor='human', actor_detail=who, kind='workflow',
           decision=kind.replace('_', ' ') + ' recorded', inputs={'sha256': row.sha256},
           apply=lambda: db.add(row))
    return {'id': row.id, 'sha256': row.sha256}


def observe(db, cfg, p, version, *, who=None):
    if type(version) is not int:
        raise L.WorkflowError('current workflow version is required')
    baseline = intake.project(db, p)
    if not baseline:
        raise L.WorkflowError('a story baseline is required')
    # Read the external source before taking domain locks; recheck version on return.
    current = fetch(cfg, p.anchor, baseline['story']['captain'])
    L.locked(db, p, version)
    changes = {k: {'before': v, 'after': current['story'][k]}
               for k, v in baseline['story'].items() if v != current['story'][k]}
    return append(db, p, 'story_observation', {**current, 'baseline_hash': baseline['sha256'],
        'changes': changes, 'workflow_version': version, 'recorded_by': who or actor()}, who or actor())


def record_change(db, p, data, version, ledger_version, *, who=None):
    if type(version) is not int or type(ledger_version) is not int:
        raise L.WorkflowError('current workflow and change-ledger versions are required')
    w = L.locked(db, p, version)
    history = rows(db, p, 'requirement_change')
    if ledger_version != (history[-1].id if history else 0):
        raise L.WorkflowError('requirement changes were added; refresh the ledger')
    required = {'before', 'after', 'reason', 'source', 'disposition', 'impact'}
    allowed = required | {'participants', 'layline_followup'}
    if not isinstance(data, dict) or set(data) - allowed or len(json.dumps(data)) > 24000:
        raise L.WorkflowError('change must contain only documented fields, at most 24000 characters')
    for key in required:
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise L.WorkflowError(f'{key} requires nonempty text')
    if data['disposition'] not in ('proposed', 'decided', 'engineering_detail'):
        raise L.WorkflowError('choose proposed, decided, or engineering_detail')
    for key in ('participants', 'layline_followup'):
        if key in data and not isinstance(data[key], str):
            raise L.WorkflowError(f'{key} must be text')
    baseline = intake.project(db, p)
    if not baseline:
        raise L.WorkflowError('a story baseline is required')
    return append(db, p, 'requirement_change', {**data, 'workflow_version': w.version,
        'baseline_hash': baseline['sha256'], 'recorded_by': who or actor(),
        'recorded_at': datetime.now(timezone.utc).isoformat(), 'application': 'record_only'}, who or actor())


def project(db, p):
    observations = rows(db, p, 'story_observation')
    changes = rows(db, p, 'requirement_change')
    return {'observation': json.loads(observations[-1].body) if observations else None,
            'ledger_version': changes[-1].id if changes else 0,
            'changes': [{'id': r.id, **json.loads(r.body)} for r in changes]}
