# ruff: noqa: F811
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_delivery_actions import delivered, submit
from test_executor import setup
from test_m0 import case, project  # noqa: F401

from plat import db as DB
from plat import executor, team, workspace
from plat import lifecycle as L
from plat import publication as P
from plat.models import Artifact, Attempt, ExecutionCommand, Executor, Lot, Plat, Workflow


def prepare(case):
    delivered(case)
    with DB.session() as db:
        member, e, _, session, p, _ = setup(db, case)
        p.ticket_url = f'https://test.atlassian.net/browse/{p.anchor}'
        # Fixture anchor is not a Jira key.
        p.anchor = 'DEV-123'
        p.ticket_url = 'https://test.atlassian.net/browse/DEV-123'
        P.share(db, p, P.targets(db, p)[0], 'Reviewed delivery update\nExact second line.', 0, member.name)
        return member, e, session, p


def test_publication_receipt_durable_attempt_and_no_replay(case, monkeypatch):
    member, e, session, p = prepare(case)
    monkeypatch.setattr(P, 'preflight', lambda data: None)
    calls = []
    def publish(data):
        with DB.session() as db:
            # The marker must already be committed before the external side effect.
            assert db.scalars(select(Artifact).where(Artifact.kind == 'publication_attempt')).one()
        calls.append(data)
        return {'id': '123', 'url': data['target']['url'] + '?focusedCommentId=123'}
    monkeypatch.setattr(P, 'publish', publish)
    with DB.session() as db:
        req = submit(db, member, db.get(Executor, e.id), db.get(Plat, p.id), 'publish_comment')
        executor.acknowledge(db, db.get(Executor, e.id), req['id'], session)
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == 'completed' and 'focusedCommentId=123' in cmd.outcome
        view = workspace.room(db, p.public_id)['delivery_actions']['publication']['draft']
        assert len(view['attempts']) == 2
        assert view['attempts'][1]['draft_sha256'] == view['sha256']
        assert db.get(Workflow, p.id).stage == 'development'
        with pytest.raises(team.Conflict, match='already has'):
            submit(db, member, db.get(Executor, e.id), p, 'publish_comment')
    with pytest.raises(team.Conflict):
        executor.execute(case.cfg, req['id'], session)
    assert len(calls) == 1


@pytest.mark.parametrize('failure', ['timeout', 'invalid_receipt', 'crash'])
def test_ambiguous_post_is_uncertain_and_never_retried(case, monkeypatch, failure):
    member, e, session, p = prepare(case)
    monkeypatch.setattr(P, 'preflight', lambda data: None)
    def fail(data):
        raise {'timeout': subprocess.TimeoutExpired('provider', 40),
               'invalid_receipt': ValueError('PRIVATE PROVIDER ERROR'),
               'crash': KeyboardInterrupt()}[failure]
    monkeypatch.setattr(P, 'publish', fail)
    with DB.session() as db:
        req = submit(db, member, db.get(Executor, e.id), db.get(Plat, p.id), 'publish_comment')
        executor.acknowledge(db, db.get(Executor, e.id), req['id'], session)
    if failure == 'crash':
        with pytest.raises(KeyboardInterrupt):
            executor.execute(case.cfg, req['id'], session)
    else:
        executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == 'uncertain'
        assert 'PRIVATE' not in cmd.outcome
        with pytest.raises(team.Conflict, match='already has'):
            P.validate(db, p)
        # Even sharing a fresh draft cannot bypass the outstanding uncertain command.
        P.share(db, p, P.targets(db, p)[0], 'New draft', P.latest(db, p).id, member.name)
        with pytest.raises(team.Conflict, match='outstanding'):
            submit(db, member, db.get(Executor, e.id), p, 'publish_comment')


@pytest.mark.parametrize('change', ['draft', 'target', 'revision', 'revocation', 'during_preflight'])
def test_publication_rechecks_review_and_authorization(case, monkeypatch, change):
    member, e, session, p = prepare(case)
    calls = []
    monkeypatch.setattr(P, 'publish', lambda data: calls.append(data))
    def preflight(data):
        if change == 'during_preflight':
            with DB.session() as db:
                current = db.get(Plat, p.id)
                P.share(db, current, P.targets(db, current)[0], 'Changed during read',
                        P.latest(db, current).id, member.name)
    monkeypatch.setattr(P, 'preflight', preflight)
    with DB.session() as db:
        p = db.get(Plat, p.id); e = db.get(Executor, e.id)
        req = submit(db, member, e, p, 'publish_comment')
        executor.acknowledge(db, e, req['id'], session)
        if change == 'draft':
            P.share(db, p, P.targets(db, p)[0], 'Edited', P.latest(db, p).id, member.name)
        elif change == 'target':
            p.ticket_url = 'https://other.atlassian.net/browse/DEV-123'
        elif change == 'revision':
            (Path(db.scalars(select(Lot)).one().worktree_path) / 'app.txt').write_text('dirty')
        elif change == 'revocation':
            e.active = False
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        assert db.scalars(select(ExecutionCommand)).one().state == 'failed'
        assert not db.scalars(select(Artifact).where(Artifact.kind == 'publication_attempt')).first()
    assert not calls


def test_draft_permissions_targets_and_idempotency(case):
    member, e, session, p = prepare(case)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        payload = {'action': 'share_publication', 'key': str(uuid4()), 'plat': p.public_id,
                   'target': P.targets(db, p)[0], 'version': P.latest(db, p).id, 'body': 'Reviewed'}
        viewer, _ = team.add_member(db, 'viewer', 'viewer')
        with pytest.raises(team.Forbidden):
            workspace.command(db, viewer, payload)
        result = workspace.command(db, member, payload)
        assert workspace.command(db, member, payload) == result
        with pytest.raises(team.Conflict):
            P.share(db, p, {**payload['target'], 'url': 'https://evil.example/'}, 'test', result['version'], member.name)
        p.ticket_url = 'https://test.atlassian.net/browse/OTHER-123'
        assert not P.targets(db, p)


@pytest.mark.parametrize('stage,next_stage', [('integration', 'integration'), ('deploying', 'deployment')])
def test_browser_recovery_records_inspection_without_running_work(case, stage, next_stage):
    member, e, session, p = prepare(case)
    before = len(case.calls)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        w = db.get(Workflow, p.id); w.stage = stage
        req = submit(db, member, db.get(Executor, e.id), p, 'recover_workflow')
        executor.acknowledge(db, db.get(Executor, e.id), req['id'], session)
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        assert db.scalars(select(ExecutionCommand)).one().state == 'completed'
        assert db.get(Workflow, p.id).stage == next_stage
        evidence = L.latest(db, db.get(Plat, p.id), 'recovery')
        assert member.name in evidence.content['note']
    assert len(case.calls) == before


def test_recovery_refuses_unfinished_attempt(case):
    member, e, session, p = prepare(case)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        db.get(Workflow, p.id).stage = 'integration'
        attempt = db.scalars(select(Attempt)).first()
        attempt.finished_at = None
        with pytest.raises(L.WorkflowError, match='unfinished'):
            L.recover(db, p, 'Inspected', db.get(Workflow, p.id).version)
        with pytest.raises(L.WorkflowError, match='unfinished'):
            submit(db, member, db.get(Executor, e.id), p, 'recover_workflow')


@pytest.mark.parametrize('provider', ['github', 'gitlab', 'jira'])
def test_provider_posts_exact_body_once_and_reads_receipt(monkeypatch, provider):
    body = 'Reviewed <text> & shell $(do-not-run)\nSecond line.'
    target = {'provider': provider, 'project': 'org/repo', 'number': 4, 'url': 'https://example.test/target',
              'site': 'test.atlassian.net', 'key': 'DEV-123'}
    calls = []
    def fake(args):
        calls.append(args)
        if 'POST' in args:
            assert json.loads(Path(args[args.index('--input') + 1]).read_text()) == {'body': body}
            return {'id': 77}
        if 'create' in args:
            assert args[args.index('--body') + 1] == body
            assert '--body-format' in args and 'plain' in args
            return {'data': {'id': '77'}}
        if 'query' in args:
            return {'data': [{'id': '77', 'body': {'type': 'doc', 'content': [
                {'type': 'paragraph', 'content': [{'type': 'text', 'text': line}]} for line in body.splitlines()]}}]}
        return {'id': 77, 'body': body}
    monkeypatch.setattr(P, 'cli', fake)
    assert P.publish({'target': target, 'body': body})['id'] == '77'
    assert len(calls) == 2
    assert not any('shell' == arg for call in calls for arg in call)


def test_provider_preflight_failure_does_not_dispatch(case, monkeypatch):
    member, e, session, p = prepare(case)
    def fail(data):
        raise ValueError('PRIVATE PROVIDER OUTPUT')
    monkeypatch.setattr(P, 'preflight', fail)
    monkeypatch.setattr(P, 'publish', lambda data: pytest.fail('must not publish'))
    with DB.session() as db:
        req = submit(db, member, db.get(Executor, e.id), db.get(Plat, p.id), 'publish_comment')
        executor.acknowledge(db, db.get(Executor, e.id), req['id'], session)
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == 'failed' and 'PRIVATE' not in cmd.outcome
        assert not P.attempts(db, db.get(Plat, p.id), P.latest(db, db.get(Plat, p.id)))


@pytest.mark.parametrize('change', ['claim', 'stage'])
def test_recovery_revalidates_after_ack(case, change):
    from plat.models import ExecutionClaim
    member, e, session, p = prepare(case)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        db.get(Workflow, p.id).stage = 'deploying'
        req = submit(db, member, db.get(Executor, e.id), p, 'recover_workflow')
        executor.acknowledge(db, db.get(Executor, e.id), req['id'], session)
        if change == 'claim':
            db.scalars(select(ExecutionClaim)).one().active = True
        else:
            db.get(Workflow, p.id).stage = 'validation'
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        assert db.scalars(select(ExecutionCommand)).one().state == 'failed'
        assert not L.latest(db, db.get(Plat, p.id), 'recovery')


def test_provider_preflight_requires_exact_jira_identity_and_pr_head(monkeypatch):
    monkeypatch.setattr(P, 'twg', lambda t, args: {'data': [{'key': 'DEV-1', 'url': 'https://wrong/'}]})
    with pytest.raises(ValueError, match='destination'):
        P.preflight({'target': {'provider': 'jira', 'key': 'DEV-1', 'url': 'https://test.atlassian.net/browse/DEV-1'}})
    monkeypatch.setattr(P.delivery, 'fetch', lambda t: {'head': 'different'})
    with pytest.raises(ValueError, match='head'):
        P.preflight({'target': {'provider': 'github', 'lot': 'a', 'repository': 'github.com/a/b'},
                     'revisions': {'a': {'repository': 'github.com/a/b', 'commit': 'reviewed'}}})


def test_mismatched_provider_readback_is_not_a_receipt(monkeypatch):
    calls = []
    def response(args):
        calls.append(args)
        return {'id': 1, 'body': 'Changed externally'}
    monkeypatch.setattr(P, 'cli', response)
    with pytest.raises(ValueError, match='verified'):
        P.publish({'target': {'provider': 'github', 'project': 'a/b', 'number': 2,
                             'url': 'https://github.com/a/b/pull/2'}, 'body': 'Reviewed'})
    assert len(calls) == 2  # Never retry the write after a read failure.
