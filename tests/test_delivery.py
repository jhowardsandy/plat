# ruff: noqa: F811

"""External observations cannot create readiness or survive revision drift silently."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_m0 import case, plan, project  # noqa: F401
from test_workspace import request

from plat import db as DB
from plat import delivery, team, workspace
from plat.models import Artifact, Decision, Lot, Plat, StageRecord
from plat.ownership import host_id


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'https://github.com.evil/a/b/pull/1',
    'https://user:secret@github.com/a/b/pull/1', 'https://github.com/a/b/pull/1?q=secret',
    'https://gitlab.com/a/../-/merge_requests/1', 'http://github.com/a/b/pull/1', None])
def test_rejects_unsafe_links(url):
    with pytest.raises(ValueError):
        delivery.parse_link(url)


def test_provider_normalization_and_read_only_commands(monkeypatch):
    commands = []
    def query(args):
        commands.append(args)
        if args[0] == 'gh':
            return {'headRefOid': 'abc', 'state': 'OPEN', 'isDraft': False, 'reviewDecision': '',
                    'statusCheckRollup': [{'name': 'build', 'status': 'IN_PROGRESS'}, {'context': 'lint', 'state': 'FAILURE'}]}
        if args[2].endswith('/approvals'):
            raise ValueError('private error')
        return {'sha': 'abc', 'state': 'opened', 'head_pipeline': {'sha': 'older', 'status': 'success'}}
    monkeypatch.setattr(delivery, 'cli_json', query)
    github = delivery.fetch(delivery.parse_link('https://github.com/org/repo/pull/1'))
    assert github['review'] == 'unknown'
    assert [c['status'] for c in github['checks']] == ['in_progress', 'failure']
    gitlab = delivery.fetch(delivery.parse_link('https://gitlab.com/org/sub/repo/-/merge_requests/2'))
    assert gitlab['review'] == 'unknown' and gitlab['checks_head'] == 'older'
    assert commands[1][2] == 'projects/org%2Fsub%2Frepo/merge_requests/2'
    assert all(c[-2:] == ['--method', 'GET'] for c in commands[1:])


def test_links_are_audited_scoped_versioned_and_replayed(case):
    plan(case)
    with DB.session() as db:
        p, lot = db.scalars(select(Plat)).one(), db.scalars(select(Lot)).one()
        lot.config = {**lot.config, 'repository_identity': 'github.com/org/repo'}
        member, _ = team.add_member(db, 'editor', 'engineer')
        viewer, _ = team.add_member(db, 'viewer', 'viewer')
        cmd = request(action='delivery_link', plat=p.public_id, lot=lot.key, version=0, url='https://github.com/org/repo/pull/1')
        with pytest.raises(team.Forbidden):
            workspace.command(db, viewer, cmd)
        result = workspace.command(db, member, cmd)
        assert workspace.command(db, member, cmd) == result
        assert db.scalars(select(Artifact).where(Artifact.kind == 'delivery_link')).one()
        assert db.scalars(select(Decision).where(Decision.decision == 'delivery link updated')).one().actor_detail == 'editor'
        with pytest.raises(team.Conflict):
            workspace.command(db, member, {**cmd, **request()})
        with pytest.raises(ValueError, match='repository'):
            delivery.link(db, p, lot, 'https://github.com/other/repo/pull/1', result['version'], 'editor')
        delivery.link(db, p, lot, '', result['version'], 'editor')
        assert delivery.project(db, p)['lots'][0]['url'] is None


def test_refresh_failure_freshness_and_revision_fencing(case, monkeypatch):
    plan(case)
    with DB.session() as db:
        p, lot = db.scalars(select(Plat)).one(), db.scalars(select(Lot)).one()
        lot.config = {**lot.config, 'repository_identity': 'github.com/org/repo', 'host_id': host_id(), 'executor_id': 'owner'}
        delivery.link(db, p, lot, 'https://github.com/org/repo/pull/1', 0, 'editor')
        revision = {'repository': 'github.com/org/repo', 'commit': 'abc', 'fingerprint': 'xyz', 'dirty': False}
        monkeypatch.setattr(delivery, 'local_revision', lambda _: dict(revision))
        monkeypatch.setattr(delivery, 'readiness', lambda *_: {'ready': False, 'missing': ['deployment'], 'revisions': {'a': dict(revision)}})
        monkeypatch.setattr(delivery, 'fetch', lambda _: {'head': 'abc', 'review': 'approved', 'state': 'open', 'checks_head': 'abc', 'checks': [{'name': 'ci', 'status': 'success'}]})
        with pytest.raises(ValueError, match='owning'):
            delivery.refresh(db, SimpleNamespace(origin_id='other'), p, lot)
        delivery.refresh(db, SimpleNamespace(origin_id='owner'), p, lot)
        projection = delivery.project(db, p)
        assert projection['lots'][0]['warnings'] == []
        assert projection['readiness']['ready'] is False
        revision['dirty'] = True
        assert 'current clean revision' in delivery.project(db, p)['lots'][0]['warnings'][0]
        revision['dirty'] = False
        row = delivery.current(db, p, lot)
        content = json.loads(row.body)
        content['observation']['observed_at'] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        content['observation']['checks_head'] = 'older'
        delivery.save(db, p, lot, content, 'test', 'fixture')
        warnings = delivery.project(db, p)['lots'][0]['warnings']
        assert any('15 minutes' in w for w in warnings)
        assert any('CI revision' in w for w in warnings)
        def fail(_):
            raise ValueError('SECRET provider error')
        monkeypatch.setattr(delivery, 'fetch', fail)
        delivery.refresh(db, SimpleNamespace(origin_id='owner'), p, lot)
        projected = delivery.project(db, p)
        assert 'SECRET' not in str(projected)
        assert projected['lots'][0]['observation'].get('head') is None
        assert 'Refresh failed' in projected['lots'][0]['warnings'][0]
        def replace_during_fetch(_):
            with DB.session() as other:
                other_plat = other.scalars(select(Plat).where(Plat.id == p.id).with_for_update(nowait=True)).one()
                other_lot = other.get(Lot, lot.id)
                version = delivery.current(other, other_plat, other_lot).id
                delivery.link(other, other_plat, other_lot, 'https://github.com/org/repo/pull/2', version, 'editor')
            return {'head': 'abc', 'checks': [], 'checks_head': 'abc'}
        monkeypatch.setattr(delivery, 'fetch', replace_during_fetch)
        with pytest.raises(team.Conflict, match='context changed'):
            delivery.refresh(db, SimpleNamespace(origin_id='owner'), p, lot)
        assert json.loads(delivery.current(db, p, lot).body)['url'].endswith('/2')


def test_stage_projection_marks_drift_without_sharing_private_output(case, monkeypatch):
    plan(case)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        revisions = {'a': {'commit': 'abc', 'dirty': False}}
        monkeypatch.setattr(delivery, 'readiness', lambda *_: {'ready': False, 'revisions': revisions})
        db.add(StageRecord(plat_id=p.id, stage='integration', version=1, actor='test', sha256='a'*64,
            content={'revisions': revisions, 'passed': True, 'checks': [
                {'name': 'Contract', 'exit_code': 0, 'output': 'PRIVATE OUTPUT', 'cmd': 'PRIVATE COMMAND'}]}))
        db.flush()
        view = delivery.project(db, p)
        assert view['stages'][0]['status'] == 'current'
        assert view['stages'][0]['checks'] == [{'name': 'Contract', 'exit_code': 0}]
        assert 'PRIVATE' not in str(view)
        revisions = {'a': {'commit': 'def', 'dirty': False}}
        assert delivery.project(db, p)['stages'][0]['status'] == 'stale'
