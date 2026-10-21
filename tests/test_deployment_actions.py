# ruff: noqa: F811
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_delivery_actions import delivered, fields, recipe, submit
from test_executor import setup
from test_m0 import case, project  # noqa: F401

from plat import db as DB
from plat import delivery_actions as D
from plat import executor, team, workspace
from plat import lifecycle as L
from plat.models import Attempt, ExecutionCommand, Executor, Lot, Plat, Workflow


def prepare(case):
    delivered(case)
    with DB.session() as db:
        member, e, _, session, p, _ = setup(db, case)
        L.integration(db, case.cfg, p, recipe(), db.get(Workflow, p.id).version)
        L.accept(db, p, 'recording', fields(), db.get(Workflow, p.id).version)
        return member, e, session, p


def deployment(path, smoke=True, rollback=True):
    target = shlex.quote(str(path))
    return {'environment': 'local', 'target': 'isolated local fixture',
            'artifact': 'fixture-v2', 'rollback_artifact': 'fixture-v1', 'lot': 'a',
            'deploy': f'printf v2 > {target}',
            'smoke': f'test "$(cat {target})" = v2' if smoke else 'exit 7',
            'rollback': f'printf v1 > {target}' if rollback else 'exit 9'}


@pytest.mark.parametrize('smoke,rollback', [(True, True), (False, True), (False, False)])
def test_reviewed_deployment_runs_claimed_checks_and_rollback(case, tmp_path, smoke, rollback):
    member, e, session, p = prepare(case)
    target = tmp_path / 'deployed-artifact'
    with DB.session() as db:
        p = db.get(Plat, p.id)
        D.publish_deployment(db, case.cfg, p, deployment(target, smoke, rollback), 0)
        req = submit(db, member, db.get(Executor, e.id), p, 'deploy_test')
        executor.acknowledge(db, db.get(Executor, e.id), req['id'], session)
    executor.execute(case.cfg, req['id'], session)
    assert target.read_text() == ('v1' if not smoke and rollback else 'v2')
    with DB.session() as db:
        p = db.get(Plat, p.id)
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == ('completed' if smoke else 'failed')
        assert db.get(Workflow, p.id).stage == ('validation' if smoke else 'deployment')
        row = L.latest(db, p, 'deployment')
        assert row.content['requested_by'] == member.name
        assert row.content['shared_artifact'] == D.reference(db, p, 'deploy_test')
        assert row.content['passed'] is smoke
        attempts = db.scalars(select(Attempt).where(Attempt.phase.in_(('deploy', 'smoke', 'rollback')))).all()
        assert len(attempts) == (2 if smoke else 3)
        assert all(a.claim_generation and a.provider == 'supervisor' for a in attempts)
        public = workspace.room(db, p.public_id)['delivery_actions']['deploy_test']
        assert public['result']['passed'] is smoke
        assert all('output' not in v for v in public['result']['results'].values())
        assert not L.readiness(db, p)['ready']  # Still requires explicit product validation.
    with pytest.raises(team.Conflict, match='never replay'):
        executor.execute(case.cfg, req['id'], session)


@pytest.mark.parametrize('change', ['recipe', 'revision', 'package', 'revoked'])
def test_deployment_rejects_changes_after_ack(case, tmp_path, change):
    member, e, session, p = prepare(case)
    target = tmp_path / 'must-not-deploy'
    with DB.session() as db:
        p = db.get(Plat, p.id)
        saved = D.publish_deployment(db, case.cfg, p, deployment(target), 0)
        req = submit(db, member, db.get(Executor, e.id), p, 'deploy_test')
        executor.acknowledge(db, db.get(Executor, e.id), req['id'], session)
        if change == 'recipe':
            D.publish_deployment(db, case.cfg, p, {**deployment(target), 'target': 'different target'}, saved['version'])
        elif change == 'revision':
            (Path(db.scalars(select(Lot)).one().worktree_path) / 'app.txt').write_text('new content')
        elif change == 'package':
            w = db.get(Workflow, p.id)
            L.save(db, p, w, 'recording', {**L.latest(db, p, 'recording').content, 'summary': 'Changed package'}, 'deployment')
        else:
            db.get(Executor, e.id).active = False
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        assert db.scalars(select(ExecutionCommand)).one().state == 'failed'
        assert not db.scalars(select(Attempt).where(Attempt.phase == 'deploy')).first()
    assert not target.exists()


def test_interrupted_deployment_is_uncertain_and_cannot_replay(case, tmp_path, monkeypatch):
    member, e, session, p = prepare(case)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        D.publish_deployment(db, case.cfg, p, deployment(tmp_path / 'target'), 0)
        req = submit(db, member, db.get(Executor, e.id), p, 'deploy_test')
        executor.acknowledge(db, db.get(Executor, e.id), req['id'], session)
    def interrupted(*args, **kwargs):
        raise OSError('simulated disconnect after durable authorization')
    monkeypatch.setattr(L, 'execute_command', interrupted)
    executor.execute(case.cfg, req['id'], session)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        assert db.get(Workflow, p.id).stage == 'deploying'
        assert db.scalars(select(ExecutionCommand)).one().state == 'uncertain'
        assert L.latest(db, p, 'deployment_requested')
        assert not L.latest(db, p, 'deployment')
        with pytest.raises(team.Conflict, match='outstanding'):
            submit(db, member, db.get(Executor, e.id), p, 'recover_workflow')


def test_owner_only_recipe_and_no_browser_shell_authoring(case, tmp_path):
    member, e, session, p = prepare(case)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        data = deployment(tmp_path / 'target')
        with pytest.raises(ValueError, match='owning'):
            D.publish_deployment(db, SimpleNamespace(origin_id='wrong'), p, data, 0)
        for bad in ({**data, 'environment': 'production'}, {**data, 'lot': 'missing'},
                    {**data, 'rollback_artifact': ''}, {**data, 'extra': 'shell'}):
            with pytest.raises(ValueError):
                D.publish_deployment(db, case.cfg, p, bad, 0)
        result = D.publish_deployment(db, case.cfg, p, data, 0)
        with pytest.raises(team.Conflict):
            D.publish_deployment(db, case.cfg, p, data, 0)
        assert result['sha256']
        with pytest.raises(ValueError, match='unsupported'):
            workspace.command(db, member, {'action': 'publish_deployment', 'key': 'blocked', 'data': data})
        viewer, _ = team.add_member(db, 'observer', 'viewer')
        with pytest.raises(team.Forbidden):
            submit(db, viewer, db.get(Executor, e.id), p, 'deploy_test')
        assert json.loads(D.latest(db, p, 'deploy_test').body)['target'] == data['target']


def test_new_package_invalidates_recipe_even_with_fresh_control_version(case, tmp_path):
    member, e, session, p = prepare(case)
    with DB.session() as db:
        p = db.get(Plat, p.id)
        D.publish_deployment(db, case.cfg, p, deployment(tmp_path / 'target'), 0)
        w = db.get(Workflow, p.id)
        L.save(db, p, w, 'recording', {**L.latest(db, p, 'recording').content, 'summary': 'Revised'}, 'deployment')
        assert not D.project(db, p)['deploy_test']['package_current']
        with pytest.raises(team.Conflict, match='package changed'):
            submit(db, member, db.get(Executor, e.id), p, 'deploy_test')
