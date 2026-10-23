# ruff: noqa: F811
"""Guided acceptance remains a trusted CLI decision; handoff guidance is advisory."""
import pytest
from sqlalchemy import select
from test_m0 import case, git, plan, project, start, valid_spec  # noqa: F401
from typer.testing import CliRunner

from plat import cli, guidance, workflow_cli, workspace
from plat import db as DB
from plat import lifecycle as L
from plat.models import Plat, StageRecord, Workflow


def begin(case, monkeypatch):
    monkeypatch.setattr(workflow_cli, 'load', lambda: case.cfg)
    result = CliRunner().invoke(cli.app, ['workflow', 'begin', 'fix_m0-test', '--owner', 'engineer'])
    assert result.exit_code == 0, result.output
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        L.accept(db, p, 'context', {'summary': 'ticket', 'sources': [
            {'reference': 'ticket', 'status': 'collected', 'snapshot': 'change the app'}]}, 0)
        L.accept(db, p, 'definition', {'problem': 'old app', 'outcome': 'changed app',
                 'scope': 'app', 'success_measures': ['changed']}, 1)


def approve_spec(db, p, version=2):
    return L.accept(db, p, 'specification', {'note': 'Engineer approved behavior',
        'behavior': 'App changes', 'exclusions': 'No new UI',
        'outcomes': [{'id': 'O1', 'statement': 'App is changed'}]}, version)


def approve_plan(db, p, version=3):
    return L.accept(db, p, 'planning', {'note': 'Engineer approved implementation',
                                     'alignment': {'O1': ['AC1']}}, version)


def test_spec_before_plan_and_revision_invalidate_acceptance(case, monkeypatch):
    begin(case, monkeypatch)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        approve_spec(db, p)  # No executable plan needed to approve intended behavior.
        assert db.get(Workflow, p.id).stage == 'planning'
        with pytest.raises(L.WorkflowError, match='accept'):
            L.require_accepted(db, p)
    plan(case)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        approve_plan(db, p)
        L.require_accepted(db, p)
        with pytest.raises(L.WorkflowError, match='stale'):
            guidance.revise(db, p, 'specification', 'new requirement', 3)
        guidance.revise(db, p, 'specification', 'Clarify behavior', 4)
        with pytest.raises(L.WorkflowError):
            L.require_accepted(db, p)
        approve_spec(db, p, 5)
        approve_plan(db, p, 6)
        assert len(db.scalars(select(StageRecord).where(StageRecord.stage == 'specification')).all()) == 2
        assert db.get(Workflow, p.id).version == 7
    start()
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        with pytest.raises(L.WorkflowError, match='started scope'):
            guidance.revise(db, p, 'planning', 'Changed scope', 7)


@pytest.mark.parametrize('mapping', [{}, {'O2': ['AC1']}, {'O1': ['missing']}, {'O1': []}, {'O1': ['AC1', 'AC1']}])
def test_alignment_rejects_omitted_unknown_or_duplicate_criteria(case, monkeypatch, mapping):
    begin(case, monkeypatch)
    with DB.session() as db:
        approve_spec(db, db.scalars(select(Plat)).one())
    plan(case)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        with pytest.raises(L.WorkflowError):
            L.accept(db, p, 'planning', {'note': 'approved', 'alignment': mapping}, 3)
        assert db.get(Workflow, p.id).stage == 'planning'


def test_recommit_keeps_behavior_approval_but_requires_plan_review(case, monkeypatch):
    begin(case, monkeypatch)
    with DB.session() as db:
        approve_spec(db, db.scalars(select(Plat)).one())
    plan(case)
    with DB.session() as db:
        approve_plan(db, db.scalars(select(Plat)).one())
    plan(case, valid_spec(title='Clarified plan'))
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        assert db.get(Workflow, p.id).stage == 'planning'
        assert L.latest(db, p, 'specification').version == 3
        with pytest.raises(L.WorkflowError):
            L.require_accepted(db, p)
        approve_plan(db, p, 5)
        L.require_accepted(db, p)


def test_handoff_checks_are_attributed_advisory_and_revision_bound(case, monkeypatch):
    begin(case, monkeypatch)
    with DB.session() as db:
        approve_spec(db, db.scalars(select(Plat)).one())
    plan(case)
    with DB.session() as db:
        approve_plan(db, db.scalars(select(Plat)).one())
    start()
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        assert p.status == 'delivered'
        row = guidance.record_check(db, p, 'testing', {'note': 'Existing AC1 check covers this edit',
                    'disposition': 'recorded', 'references': ['test:AC1']}, 4)
        assert row.actor and row.content['references'] == ['test:AC1']
        info = workspace.room(db, p.public_id)['guidance']
        assert info['advisory'] and info['checks'][0]['status'] == 'recorded'
        assert info['checks'][1]['status'] == 'pending'
        wt = p.lots[0].worktree_path
        with open(wt + '/app.txt', 'a') as out:
            out.write('new edit')
        assert guidance.project(db, p)['checks'][0]['status'] == 'stale'
        with pytest.raises(L.WorkflowError, match='commit'):
            guidance.record_check(db, p, 'documentation', {'note': 'No docs needed', 'disposition': 'not_applicable'}, 5)


def test_context_revision_requires_all_downstream_acceptances_again(case, monkeypatch):
    begin(case, monkeypatch)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        approve_spec(db, p)
        guidance.revise(db, p, 'context', 'Ticket changed', 3)
        with pytest.raises(L.WorkflowError, match='expected context'):
            approve_spec(db, p, 4)
        assert db.get(Workflow, p.id).spec_hash is None


def test_room_records_engineer_evidence_with_permissions_and_replay(case, monkeypatch):
    from plat import team
    from plat.models import Decision
    begin(case, monkeypatch)
    with DB.session() as db:
        approve_spec(db, db.scalars(select(Plat)).one())
    plan(case)
    with DB.session() as db:
        approve_plan(db, db.scalars(select(Plat)).one())
    start()
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        engineer, _ = team.add_member(db, 'engineer', 'engineer')
        viewer, _ = team.add_member(db, 'viewer', 'viewer')
        request = {'key': 'record-note', 'action': 'record_guidance', 'plat': p.public_id,
                   'version': 4, 'check': 'documentation', 'evidence': {
                       'disposition': 'not_applicable', 'note': 'Internal change with no operator impact'}}
        with pytest.raises(team.Forbidden):
            workspace.command(db, viewer, request)
        with pytest.raises(ValueError, match='version'):
            workspace.command(db, engineer, {**request, 'version': None})
        result = workspace.command(db, engineer, request)
        assert workspace.command(db, engineer, request) == result
        with pytest.raises(L.WorkflowError, match='stale'):
            workspace.command(db, engineer, {**request, 'key': 'stale-note'})
        row = L.latest(db, p, 'check_documentation')
        assert row.actor == 'engineer'
        decision = db.scalars(select(Decision).where(Decision.decision == 'check_documentation recorded; next: development')).one()
        assert decision.actor == 'human' and decision.actor_detail == 'engineer'
