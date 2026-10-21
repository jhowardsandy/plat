# ruff: noqa: F811
"""Shared prose is explicit; browser execution only uses owner-published checks."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_executor import setup
from test_m0 import case, plan, project, start  # noqa: F401
from test_workflow import advance_to_development

from plat import db as DB
from plat import delivery_actions as D
from plat import executor, team, workspace
from plat import lifecycle as L
from plat.models import Artifact, Attempt, ExecutionCommand, Executor, Lot, Plat, Workflow


def delivered(case):
    plan(case)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        advance_to_development(db, p)
    start()


def recipe(cmd='grep -q changed app.txt'):
    return {'checks': [{'name': 'Contract', 'lot': 'a', 'cmd': cmd}]}


def fields():
    return {k: 'Explicit shared ' + k for k in D.FIELDS}


def submit(db, member, e, p, action):
    w = db.get(Workflow, p.id)
    return executor.request(db, member, {'key': str(uuid4()), 'plat': p.public_id,
        'executor': e.public_id, 'action': action, 'version': w.version,
        'status': p.status, 'scope_hash': executor.preview_hash(db, p, action), 'note': 'Review and execute shared delivery content'})


def test_browser_integration_and_package_use_real_gates(case):
    delivered(case)
    before_calls = len(case.calls)
    with DB.session() as db:
        member, e, _, session, p, _ = setup(db, case)
        D.publish_recipe(db, case.cfg, p, recipe(), 0)
        D.share_package(db, p, fields(), 0, member.name)
        req = submit(db, member, e, p, 'integrate')
        executor.acknowledge(db, e, req['id'], session)
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        assert db.get(Workflow, p.id).stage == 'recording'
        assert L.latest(db, p, 'integration').content['checks'][0]['exit_code'] == 0
        assert db.scalars(select(ExecutionCommand)).one().state == 'completed'
        assert db.scalars(select(Attempt).where(Attempt.phase == 'integration')).one().provider == 'supervisor'
        e = db.get(Executor, e.id)
        req = submit(db, member, e, p, 'record_package')
        executor.acknowledge(db, e, req['id'], session)
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        assert db.get(Workflow, p.id).stage == 'deployment'
        accepted = L.latest(db, p, 'recording')
        assert accepted.content['summary'] == fields()['summary']
        assert accepted.content['requested_by'] == member.name
        assert accepted.content['shared_artifact'] == D.reference(db, p, 'record_package')
        assert p.status == 'delivered' and not L.readiness(db, p)['ready']
        assert all(c.state == 'completed' for c in db.scalars(select(ExecutionCommand)))
    assert len(case.calls) == before_calls  # No additional agents and no deployment.


def test_failed_integration_cannot_record_package(case):
    delivered(case)
    with DB.session() as db:
        member, e, _, session, p, _ = setup(db, case)
        D.publish_recipe(db, case.cfg, p, recipe('exit 7'), 0)
        D.share_package(db, p, fields(), 0, member.name)
        req = submit(db, member, e, p, 'integrate')
        executor.acknowledge(db, e, req['id'], session)
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        assert db.scalars(select(ExecutionCommand)).one().state == 'failed'
        assert db.get(Workflow, p.id).stage == 'integration'
        with pytest.raises(team.Conflict):
            submit(db, member, e, p, 'record_package')
        # Even corrupted stage routing must not turn a failed integration into acceptance.
        w = db.get(Workflow, p.id); w.stage = 'recording'
        with pytest.raises(L.WorkflowError, match='stale'):
            L.accept(db, p, 'recording', fields(), w.version)


@pytest.mark.parametrize('change', ['recipe', 'revision'])
def test_acknowledged_integration_rejects_changed_inputs(case, change):
    delivered(case)
    with DB.session() as db:
        member, e, _, session, p, _ = setup(db, case)
        saved = D.publish_recipe(db, case.cfg, p, recipe(), 0)
        req = submit(db, member, e, p, 'integrate')
        executor.acknowledge(db, e, req['id'], session)
        if change == 'recipe':
            D.publish_recipe(db, case.cfg, p, recipe('false'), saved['version'])
        else:
            lot = db.scalars(select(Lot)).one()
            (Path(lot.worktree_path) / 'app.txt').write_text('changed again')
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        assert db.scalars(select(ExecutionCommand)).one().state == 'failed'
        assert not db.scalars(select(Attempt).where(Attempt.phase == 'integration')).all()


def test_share_permissions_idempotency_privacy_and_recipe_owner(case):
    delivered(case)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        editor, _ = team.add_member(db, 'editor', 'engineer')
        viewer, _ = team.add_member(db, 'viewer', 'viewer')
        req = {'key': str(uuid4()), 'action': 'share_package', 'plat': p.public_id, 'version': 0, 'fields': fields()}
        with pytest.raises(team.Forbidden):
            workspace.command(db, viewer, req)
        result = workspace.command(db, editor, req)
        assert workspace.command(db, editor, req) == result
        with pytest.raises(team.Conflict):
            workspace.command(db, editor, {**req, 'key': str(uuid4())})
        db.add(Artifact(plat_id=p.id, kind='docs', name='private.md', sha256='a'*64, body='PRIVATE TRANSCRIPT'))
        room = workspace.room(db, p.public_id)
        assert room['delivery_actions']['record_package']['fields'] == fields()
        assert 'PRIVATE TRANSCRIPT' not in json.dumps(room, default=str)
        assert db.get(Workflow, p.id).stage == 'development'
        with pytest.raises(ValueError, match='owning'):
            D.publish_recipe(db, SimpleNamespace(origin_id='other'), p, recipe(), 0)
        with pytest.raises(ValueError, match='unsupported'):
            workspace.command(db, editor, {'key': str(uuid4()), 'action': 'publish_recipe', 'checks': recipe()['checks']})
        for invalid in ({'checks': []}, {'checks': [{'lot':'a', 'name':'bad', 'cmd':'true', 'extra':'shell'}]}):
            with pytest.raises(ValueError):
                D.publish_recipe(db, case.cfg, p, invalid, 0)


def test_package_changed_after_ack_is_not_accepted(case):
    delivered(case)
    with DB.session() as db:
        member, e, _, session, p, _ = setup(db, case)
        L.integration(db, case.cfg, p, recipe(), db.get(Workflow, p.id).version)
        original = D.share_package(db, p, fields(), 0, member.name)
        req = submit(db, member, e, p, 'record_package')
        executor.acknowledge(db, e, req['id'], session)
        D.share_package(db, p, {**fields(), 'summary': 'Revised after preview'}, original['version'], member.name)
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        assert db.scalars(select(ExecutionCommand)).one().state == 'failed'
        assert L.latest(db, p, 'recording') is None
        assert db.get(Workflow, p.id).stage == 'recording'
