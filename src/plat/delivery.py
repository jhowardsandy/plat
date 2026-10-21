"""Audited external delivery observations. Provider reads run only on an owner CLI."""
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import select

from .decisions import record
from .lifecycle import digest, latest, local_revision, readiness
from .models import Artifact, Lot, Plat
from .ownership import actor, host_id
from .team import Conflict


def parse_link(url):
    if not isinstance(url, str) or len(url) > 500:
        raise ValueError('provide a GitHub PR or GitLab MR URL')
    match = re.fullmatch(r'https://(github\.com)/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)/?', url)
    if not match:
        match = re.fullmatch(r'https://(gitlab\.com)/([\w.-]+(?:/[\w.-]+)+)/-/merge_requests/([1-9][0-9]*)/?', url)
    if not match or any(p in ('.', '..') for p in match[2].split('/')):
        raise ValueError('use an HTTPS github.com PR or gitlab.com MR URL without query parameters')
    return {'provider': 'github' if match[1] == 'github.com' else 'gitlab',
            'repository': match[1] + '/' + match[2], 'project': match[2],
            'number': int(match[3]), 'url': url.rstrip('/')}


def current(db, plat, lot):
    return db.scalars(select(Artifact).where(Artifact.plat_id == plat.id,
        Artifact.kind == 'delivery_link', Artifact.name == lot.key).order_by(Artifact.id.desc())).first()


def save(db, plat, lot, content, who, decision):
    row = Artifact(plat_id=plat.id, kind='delivery_link', name=lot.key,
                   body=json.dumps(content), sha256=digest(content))
    record(db, plat_id=plat.id, lot_id=lot.id, actor='human', actor_detail=who,
           kind='observation', decision=decision,
           inputs={'sha256': row.sha256, 'url': content.get('url')}, apply=lambda: db.add(row))
    db.flush()
    return {'version': row.id}


def link(db, plat, lot, url, version, who):
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    db.refresh(plat)
    if plat.closed_at:
        raise ValueError('shipped work is immutable')
    prior = current(db, plat, lot)
    if version != (prior.id if prior else 0):
        raise Conflict('delivery link changed; refresh before saving')
    info = parse_link(url) if url else {'url': None}
    identity = (lot.config or {}).get('repository_identity')
    if url and info['repository'] != identity:
        raise ValueError('PR/MR repository must match the lot repository identity')
    return save(db, plat, lot, info, who, 'delivery link updated')


def cli_json(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=40,
                                stdin=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError('provider read failed; check CLI authentication and repository access')
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ValueError('provider unavailable or returned invalid JSON') from exc


def fetch(info):
    if info['provider'] == 'github':
        raw = cli_json(['gh', 'pr', 'view', str(info['number']), '--repo', info['repository'],
                        '--json', 'headRefOid,state,isDraft,reviewDecision,statusCheckRollup'])
        checks = [{'name': c.get('name') or c.get('context') or 'Check',
                   'status': (c.get('conclusion') or c.get('state') or c.get('status') or 'unknown').lower()}
                  for c in raw.get('statusCheckRollup') or []]
        return {'head': raw['headRefOid'], 'state': raw['state'].lower(),
                'draft': raw['isDraft'], 'review': (raw.get('reviewDecision') or 'unknown').lower(),
                'checks': checks, 'checks_head': raw['headRefOid']}
    endpoint = f"projects/{quote(info['project'], safe='')}/merge_requests/{info['number']}"
    raw = cli_json(['glab', 'api', endpoint, '--hostname', 'gitlab.com', '--method', 'GET'])
    # Approval rules may be unavailable on a host/account; never infer approval from mergeability.
    try:
        approvals = cli_json(['glab', 'api', endpoint + '/approvals', '--hostname', 'gitlab.com', '--method', 'GET'])
        review = ('approved' if approvals.get('approved') is True else
                  'review_required' if approvals.get('approved') is False else 'unknown')
    except ValueError:
        review = 'unknown'
    pipeline = raw.get('head_pipeline') or {}
    return {'head': raw['sha'], 'state': raw['state'], 'draft': raw.get('draft', False),
            'review': review, 'checks_head': pipeline.get('sha'),
            'checks': [{'name': 'Head pipeline', 'status': pipeline.get('status', 'unknown')}] if pipeline else []}


def refresh(db, cfg, plat, lot):
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    db.refresh(lot)
    config = lot.config or {}
    if config.get('host_id') != host_id() or config.get('executor_id') != cfg.origin_id:
        raise ValueError('refresh on the owning installation and host')
    db.refresh(plat)
    if plat.closed_at:
        raise ValueError('shipped work is immutable')
    prior = current(db, plat, lot)
    if not prior or not json.loads(prior.body).get('url'):
        raise ValueError('link a PR/MR first')
    info = parse_link(json.loads(prior.body)['url'])
    before = local_revision(lot)
    if before['repository'] != info['repository']:
        raise ValueError('local repository does not match the linked PR/MR')
    prior_id, owner_config = prior.id, dict(config)
    db.commit()  # Provider latency must not hold the plat lock or delay execution heartbeats.
    observation = {'observed_at': datetime.now(timezone.utc).isoformat(), 'revision': before, 'host_id': host_id(), 'executor_id': cfg.origin_id}
    try:
        observation.update(fetch(info))
        if local_revision(lot) != before:
            raise ValueError('worktree changed during refresh; retry')
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.CalledProcessError):
        observation = {'observed_at': observation['observed_at'], 'error': 'Refresh failed; verify provider access and retry on a stable worktree.'}
        # No provider output or credentials are stored in shared evidence.
    db.execute(select(Plat.id).where(Plat.id == plat.id).with_for_update())
    db.refresh(plat)
    db.refresh(lot)
    newest = current(db, plat, lot)
    if plat.closed_at or not newest or newest.id != prior_id or lot.config != owner_config:
        raise Conflict('delivery context changed during refresh; retry')
    return save(db, plat, lot, {**info, 'observation': observation}, actor(), 'delivery status observed')


def project(db, plat):
    ready = readiness(db, plat)
    revisions = ready.get('revisions', {})
    lots = []
    for lot in db.scalars(select(Lot).where(Lot.plat_id == plat.id).order_by(Lot.key)):
        row = current(db, plat, lot)
        info = json.loads(row.body) if row else {}
        obs = info.get('observation') or {}
        revision = revisions.get(lot.key)
        issues = []
        if not info.get('url'):
            issues.append('No PR/MR linked')
        elif not obs:
            issues.append('Awaiting owner refresh')
        elif obs.get('error'):
            issues.append(obs['error'])
        else:
            if not revision:
                issues.append('Current revision unavailable')
            elif revision != obs.get('revision') or revision.get('dirty') or revision.get('commit') != obs.get('head'):
                issues.append('PR/MR evidence does not match the current clean revision')
            if datetime.now(timezone.utc) - datetime.fromisoformat(obs['observed_at']) > timedelta(minutes=15):
                issues.append('Provider observation older than 15 minutes; refresh')
            if obs.get('checks_head') != obs.get('head'):
                issues.append('CI revision does not match PR/MR head')
            if not obs.get('checks'):
                issues.append('No CI checks reported')
        lots.append({'key': lot.key, 'revision': revision, 'version': row.id if row else 0,
                     'url': info.get('url'), 'observation': obs, 'warnings': issues})
    stages = []
    for stage in ('integration', 'recording', 'deployment', 'validation'):
        row = latest(db, plat, stage)
        stages.append({'stage': stage, 'version': row.version if row else None,
                       'sha256': row.sha256 if row else None,
                       'created': row.created_at if row else None,
                       'checks': [{'name': c.get('name') or c.get('lot') or 'Check',
                                   'exit_code': c.get('exit_code')}
                                  for c in (row.content.get('checks', []) if row and stage == 'integration' else [])
                                  if isinstance(c, dict)],
                       'status': 'missing' if not row else 'unverified' if not revisions else
                       'stale' if row.content.get('revisions') != revisions else
                       'failed' if row.content.get('passed') is False else 'current'})
    return {'readiness': ready, 'lots': lots, 'stages': stages}
