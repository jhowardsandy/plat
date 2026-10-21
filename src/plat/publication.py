"""Reviewed comments on linked destinations, dispatched once by the owning executor."""
import json
import re
import subprocess
from tempfile import NamedTemporaryFile
from urllib.parse import quote

from sqlalchemy import select

from . import delivery
from . import lifecycle as L
from .decisions import record
from .delivery_actions import _snapshot
from .models import Artifact, Lot, Workflow
from .team import Conflict


def targets(db, p):
    result = []
    for lot in db.scalars(select(Lot).where(Lot.plat_id == p.id).order_by(Lot.key)):
        row = delivery.current(db, p, lot)
        if row and json.loads(row.body).get('url'):
            info = delivery.parse_link(json.loads(row.body)['url'])
            result.append({**info, 'lot': lot.key, 'link_version': row.id})
    match = re.fullmatch(r'https://([a-z0-9-]+\.atlassian\.net)/browse/([A-Z][A-Z0-9_]*-[1-9][0-9]*)', p.ticket_url or '')
    if match and match[2] == p.anchor:
        result.append({'provider': 'jira', 'site': match[1], 'key': match[2], 'url': p.ticket_url})
    return result


def latest(db, p):
    return db.scalars(select(Artifact).where(Artifact.plat_id == p.id,
        Artifact.kind == 'publication_draft').order_by(Artifact.id.desc())).first()


def reference(db, p):
    row = latest(db, p)
    return {'version': row.id, 'sha256': row.sha256} if row else None


def append(db, p, kind, name, data, who):
    row = Artifact(plat_id=p.id, kind=kind, name=name, body=json.dumps(data), sha256=L.digest(data))
    record(db, plat_id=p.id, actor='human', actor_detail=who, kind='publication',
           decision=kind, inputs={'sha256': row.sha256}, apply=lambda: db.add(row))
    db.flush()
    return row


def share(db, p, target, body, version, who):
    snapshot = _snapshot(db, p)
    prior = latest(db, p)
    if version != (prior.id if prior else 0):
        raise Conflict('publication draft changed; refresh before saving')
    if not isinstance(body, str) or not body.strip() or len(body) > 8000:
        raise ValueError('comment requires 1–8000 characters')
    if not isinstance(target, dict) or target not in targets(db, p):
        raise Conflict('choose a currently linked publication destination')
    row = append(db, p, 'publication_draft', 'comment', {**snapshot, 'target': target,
        'body': body, 'shared_by': who}, who)
    return {'version': row.id, 'sha256': row.sha256}


def attempts(db, p, draft):
    return db.scalars(select(Artifact).where(Artifact.plat_id == p.id,
        Artifact.kind.in_(('publication_attempt', 'publication_receipt')),
        Artifact.name == str(draft.id)).order_by(Artifact.id)).all()


def validate(db, p, *, observe=False):
    row = latest(db, p)
    w = db.get(Workflow, p.id)
    if not row or not w or p.closed_at or p.status != 'delivered' or not p.delivered_at:
        raise Conflict('share a publication draft for a delivered run first')
    data = json.loads(row.body)
    if data['target'] not in targets(db, p):
        raise Conflict('publication destination changed; review and share a new draft')
    if data['spec_hash'] != w.spec_hash or w.spec_hash != L.current_spec(db, p):
        raise Conflict('publication specification changed')
    if observe and data['revisions'] != L.revisions(db, p):
        raise Conflict('publication revisions changed; review and share a new draft')
    if attempts(db, p, row):
        raise Conflict('this draft already has a publication attempt; inspect the receipt or destination before preparing another')
    return row, data


def project(db, p):
    row = latest(db, p)
    draft = None
    if row:
        draft = {**json.loads(row.body), **reference(db, p), 'attempts': [
            {'kind': a.kind, 'created': a.created_at, **json.loads(a.body)} for a in attempts(db, p, row)]}
    return {'targets': targets(db, p), 'draft': draft}


def cli(args):
    # Never persist stdout/stderr: provider errors can contain credentials or private data.
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise ValueError('provider command failed')
    data = json.loads(result.stdout)
    if not isinstance(data, (dict, list)) or (isinstance(data, dict) and (data.get('errors') or data.get('error'))):
        raise ValueError('provider returned an error')
    return data


def twg(target, args):
    return cli(['twg', 'jira', 'workitem', *args, '--site', target['site'], '-o', 'json', '--output-summary', 'none'])


def preflight(data):
    target = data['target']
    if target['provider'] == 'jira':
        response = twg(target, ['get', target['key'], '--fields', 'summary,status'])
        rows = response['data']
        if len(rows) != 1 or rows[0].get('key') != target['key'] or rows[0].get('url') != target['url']:
            raise ValueError('Jira destination could not be verified')
    else:
        observed = delivery.fetch(target)
        revision = data['revisions'][target['lot']]
        if revision['repository'] != target['repository'] or revision['commit'] != observed['head']:
            raise ValueError('PR/MR head does not match reviewed revisions')


def _comment_id(raw):
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    value = str(raw.get('id', ''))
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise ValueError('provider did not return a comment receipt')
    return value


def _plain(adf):
    if isinstance(adf, str):
        return adf
    return (adf.get('text', '') + ''.join(_plain(child) for child in adf.get('content', [])) +
            ('\n' if adf.get('type') in ('paragraph', 'hardBreak') else ''))


def publish(data):
    """One POST, then a native read. Any exception is an uncertain external outcome."""
    t, body = data['target'], data['body']
    if t['provider'] == 'jira':
        raw = twg(t, ['comment', 'create', '--issue-id', t['key'], '--body', body, '--body-format', 'plain'])
        comment_id = _comment_id(raw['data'])
        cursor = None
        for _ in range(10):
            args = ['comment', 'query', '--issue-id', t['key'], '--first', '100']
            if cursor:
                args += ['--after', cursor]
            page = twg(t, args)
            found = next((r for r in page['data'] if str(r.get('id')) == comment_id), None)
            if found:
                if _plain(found['body']).strip() != body.strip():
                    raise ValueError('published body could not be verified')
                return {'id': comment_id, 'url': t['url'] + '?focusedCommentId=' + comment_id}
            if not page.get('pageInfo', {}).get('hasNextPage'):
                break
            cursor = page['pageInfo']['nextCursor']
        raise ValueError('published comment could not be read back')
    github = t['provider'] == 'github'
    endpoint = (f"repos/{t['project']}/issues/{t['number']}/comments" if github else
                f"projects/{quote(t['project'], safe='')}/merge_requests/{t['number']}/notes")
    command = ['gh' if github else 'glab', 'api', '--hostname', 'github.com' if github else 'gitlab.com']
    with NamedTemporaryFile(mode='w+', suffix='.json') as payload:
        json.dump({'body': body}, payload); payload.flush()
        raw = cli([*command, endpoint, '--method', 'POST', '--input', payload.name])
    comment_id = _comment_id(raw)
    read_endpoint = f"repos/{t['project']}/issues/comments/{comment_id}" if github else endpoint + '/' + comment_id
    checked = cli([*command, read_endpoint, '--method', 'GET'])
    if str(checked.get('id')) != comment_id or checked.get('body') != body:
        raise ValueError('published comment could not be verified')
    return {'id': comment_id, 'url': t['url'] + ('#issuecomment-' if github else '#note_') + comment_id}
