"""Reviewed, explicitly supplied story snapshots shared by CLI and Room.

This does not fetch Jira or attest to its current contents. The source reference
and revision identify the engineer-supplied baseline, preserved unchanged.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select, text

from . import lifecycle as L
from .decisions import record
from .models import Artifact, Plat
from .ownership import actor
from .spec import NAME

TEXT = ('key', 'title', 'captain', 'source_ref', 'source_revision', 'requirements', 'scope', 'estimate')
REQUIRED = set(TEXT) - {'estimate'}


def preview(data):
    if not isinstance(data, dict) or set(data) - set(TEXT) - {'outcomes', 'context_refs'}:
        raise L.WorkflowError('story must contain only the documented intake fields')
    if len(json.dumps(data)) > 36000:
        raise L.WorkflowError('story must be at most 36000 characters')
    story = {}
    for key in TEXT:
        value = data.get(key, '')
        if not isinstance(value, str) or (key in REQUIRED and not value.strip()):
            raise L.WorkflowError(f'{key} requires nonempty text')
        story[key] = value.strip()
    if not NAME.fullmatch(story['key']) or len(story['key']) > 160:
        raise L.WorkflowError('use a safe ticket key')
    if len(story['captain']) > 128:
        raise L.WorkflowError('captain must be at most 128 characters')
    for key in ('outcomes', 'context_refs'):
        values = data.get(key, [])
        if (not isinstance(values, list) or len(values) > 100 or
                any(not isinstance(v, str) or not v.strip() for v in values)):
            raise L.WorkflowError(f'{key} must be a list of at most 100 nonempty text entries')
        story[key] = [v.strip() for v in values]
    if not story['outcomes']:
        raise L.WorkflowError('at least one business outcome is required')
    return {'story': story, 'sha256': L.digest(story), 'provenance': 'engineer_supplied'}


def start(db, cfg, data, expected_hash, *, who=None):
    reviewed = preview(data)
    if expected_hash != reviewed['sha256']:
        raise L.WorkflowError('story changed; preview and review the current snapshot')
    story = reviewed['story']
    who = who or actor()
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),
               {'key': f'intake:{cfg.origin_id}:{story["key"]}'})
    existing = db.scalars(select(Plat).where(Plat.origin_id == cfg.origin_id,
        Plat.anchor == story['key'], Plat.kind == 'plat')).first()
    if existing:
        raise L.WorkflowError('this ticket already has a plat on this installation; open it instead')
    p = Plat(origin_id=cfg.origin_id, anchor=story['key'], title=story['title'], status='planned')
    record(db, plat_id=None, actor='human', actor_detail=who, kind='workflow',
           decision='story intake created', inputs={'anchor': story['key'], 'sha256': reviewed['sha256']},
           apply=lambda: db.add(p))
    L.begin(db, p, story['captain'], guided=True, who=who)
    body = {**reviewed, 'recorded_by': who, 'captured_at': datetime.now(timezone.utc).isoformat()}
    row = Artifact(plat_id=p.id, kind='story_baseline', name=story['key'],
                   body=json.dumps(body), sha256=L.digest(body))
    record(db, plat_id=p.id, actor='human', actor_detail=who, kind='workflow',
           decision='story baseline recorded; context review required',
           inputs={'sha256': row.sha256, 'source_hash': reviewed['sha256']}, apply=lambda: db.add(row))
    return {'plat': p.public_id, 'stage': 'context', 'version': 0, 'sha256': reviewed['sha256']}


def project(db, p):
    row = db.scalars(select(Artifact).where(Artifact.plat_id == p.id,
        Artifact.kind == 'story_baseline').order_by(Artifact.id)).first()
    return json.loads(row.body) if row else None


def suggestions(baseline):
    """Editable starting text, never approval or a substituted accepted record."""
    if not baseline:
        return {}
    s = baseline['story']
    return {
        'context': {'summary': s['title'], 'sources': [
            {'reference': s['source_ref'], 'status': 'collected',
             'snapshot': f"Source revision: {s['source_revision']}\n{s['requirements']}\nOutcomes:\n" + '\n'.join(s['outcomes'])},
            *[{'reference': ref, 'status': 'unavailable', 'reason': ''} for ref in s['context_refs']]],
            'blockers': []},
        'definition': {'problem': s['requirements'], 'outcome': '\n'.join(s['outcomes']),
                       'scope': s['scope'], 'success_measures': s['outcomes'], 'open_questions': []},
        'specification': {'outcomes': [{'id': f'O{i}', 'statement': value}
                                     for i, value in enumerate(s['outcomes'], 1)]},
    }
