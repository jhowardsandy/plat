# ruff: noqa: F811
import json
import uuid

import pytest
from sqlalchemy import select
from test_guidance import approve_spec, begin
from test_m0 import case, plan, project, valid_spec  # noqa: F401

from plat import db as DB
from plat import lifecycle as L
from plat import planning, team, workspace
from plat.models import Plat, Workflow


def request(p, action, stage, version, **extra):
    return dict(key=str(uuid.uuid4()), plat=p.public_id, action='workflow_'+action,
                stage=stage, version=version, **extra)


def test_shared_spec_room_accept_and_cli_plan_share(case, monkeypatch):
    begin(case, monkeypatch)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        member, _ = team.add_member(db, 'reviewer', 'engineer')
        data = {'note': 'approved', 'behavior': 'Change app', 'exclusions': 'None',
                'outcomes': [{'id': 'O1', 'statement': 'Changed app'}], 'open_questions': []}
        saved = workspace.command(db, member, request(p, 'share', 'specification', 2, fields=data, draft_version=0))
        assert db.get(Workflow, p.id).stage == 'specification'
        cmd = request(p, 'accept', 'specification', 2, **{k:v for k,v in saved.items() if k != 'workflow_version'})
        accepted = workspace.command(db, member, cmd)
        assert workspace.command(db, member, cmd) == accepted
        row = L.latest(db,p,'specification')
        assert row.actor == 'reviewer' and row.content['shared_draft']['sha256'] == saved['sha256']
        assert db.get(Workflow,p.id).stage == 'planning'
    plan(case)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        fields = {'note':'Reviewed', 'alignment':{'O1':['AC1']}}
        with pytest.raises(L.WorkflowError, match='owning CLI'):
            planning.share(db,p,'planning',fields,3,0)
        saved = planning.share(db,p,'planning',fields,3,0,publish_plan=True)
        shown = workspace.room(db,p.public_id)['planning']
        assert shown['plan_preview']['current']
        assert shown['plan_preview']['plan']['lots'][0]['repo'] == 'services/a'
        planning.accept(db,p,'planning',3,saved['draft_version'],saved['sha256'])
        L.require_accepted(db,p)


def test_stale_draft_hash_workflow_and_changed_plan_rejected(case, monkeypatch):
    begin(case,monkeypatch)
    with DB.session() as db:
        p=db.scalars(select(Plat)).one(); approve_spec(db,p)
    plan(case)
    with DB.session() as db:
        p=db.scalars(select(Plat)).one()
        data={'note':'plan','alignment':{'O1':['AC1']}}
        first=planning.share(db,p,'planning',data,3,0,publish_plan=True)
        second=planning.share(db,p,'planning',{**data,'note':'reviewed again'},3,first['draft_version'])
        with pytest.raises(L.WorkflowError,match='changed'):
            planning.accept(db,p,'planning',3,first['draft_version'],first['sha256'])
        with pytest.raises(L.WorkflowError,match='changed'):
            planning.accept(db,p,'planning',3,second['draft_version'],'wrong hash')
    plan(case,valid_spec(title='new executable plan'))  # No accepted plan hash yet: workflow version is still 3.
    with DB.session() as db:
        p=db.scalars(select(Plat)).one()
        assert not planning.project(db,p)['plan_preview']['current']
        with pytest.raises(L.WorkflowError,match='plan changed'):
            planning.accept(db,p,'planning',3,second['draft_version'],second['sha256'])
        with pytest.raises(L.WorkflowError,match='owning CLI'):
            planning.share(db,p,'planning',data,3,second['draft_version'])


def test_revision_blocks_old_draft_and_old_spec_preview(case,monkeypatch):
    begin(case,monkeypatch)
    with DB.session() as db:
        p=db.scalars(select(Plat)).one(); approve_spec(db,p)
    plan(case)
    with DB.session() as db:
        p=db.scalars(select(Plat)).one()
        data={'note':'plan','alignment':{'O1':['AC1']}}
        first=planning.share(db,p,'planning',data,3,0,publish_plan=True)
        member,_=team.add_member(db,'reviewer','engineer')
        workspace.command(db,member,request(p,'revise','specification',3,note='Clarify behavior'))
        assert L.latest(db,p,'revision').actor == 'reviewer'
        assert planning.project(db,p)['revision']['note'] == 'Clarify behavior'
        L.accept(db,p,'specification',{'note':'updated','behavior':'Different behavior','exclusions':'None',
            'outcomes':[{'id':'O1','statement':'Different outcome'}]},4)
        with pytest.raises(L.WorkflowError,match='earlier workflow'):
            planning.accept(db,p,'planning',5,first['draft_version'],first['sha256'])
        with pytest.raises(L.WorkflowError,match='owning CLI'):
            planning.share(db,p,'planning',data,5,first['draft_version'])
        assert not planning.project(db,p)['plan_preview']['current']


def test_private_history_never_becomes_shared_and_viewers_cannot_write(case,monkeypatch):
    begin(case,monkeypatch)
    with DB.session() as db:
        p=db.scalars(select(Plat)).one()
        viewer,_=team.add_member(db,'viewer','viewer')
        member,_=team.add_member(db,'editor','engineer')
        for action in ['share','accept','revise']:
            with pytest.raises(team.Forbidden):
                workspace.command(db,viewer,request(p,action,'specification',2,fields={},draft_version=0))
        shown=planning.project(db,p)
        assert shown['drafts']=={} and shown['plan_preview'] is None
        assert 'change the app' not in json.dumps(shown)
        with pytest.raises(L.WorkflowError,match='list of text'):
            planning.share(db,p,'specification',{'open_questions':'invalid'},2,0)
        saved=planning.share(db,p,'specification',{'open_questions':['Which behavior?']},2,0)
        with pytest.raises(L.WorkflowError):
            planning.accept(db,p,'specification',2,saved['draft_version'],saved['sha256'])
        assert db.get(Workflow,p.id).stage=='specification'
        with pytest.raises(ValueError,match='version'):
            workspace.command(db,member,request(p,'revise','context',None,note='change'))


def test_classic_stays_classic(case):
    plan(case)
    with DB.session() as db:
        p=db.scalars(select(Plat)).one(); L.begin(db,p,'owner')
        with pytest.raises(L.WorkflowError,match='classic'):
            planning.share(db,p,'context',{},0,0)
        assert not planning.project(db,p)['enabled']


def test_cli_accepts_browser_context_and_questions_must_be_resolved(case,monkeypatch):
    from typer.testing import CliRunner

    from plat import cli, workflow_cli
    monkeypatch.setattr(workflow_cli,'load',lambda:case.cfg)
    result=CliRunner().invoke(cli.app,['workflow','begin','fix_m0-test','--owner','owner'])
    assert result.exit_code==0,result.output
    with DB.session() as db:
        p=db.scalars(select(Plat)).one(); member,_=team.add_member(db,'editor','engineer')
        fields={'summary':'Ticket context','sources':[{'reference':'ticket:1','status':'collected','snapshot':'Requirements'}], 'blockers':['Clarify outcome']}
        saved=workspace.command(db,member,request(p,'share','context',0,fields=fields,draft_version=0))
        with pytest.raises(L.WorkflowError,match='blocking'):
            planning.accept(db,p,'context',0,saved['draft_version'],saved['sha256'])
        saved=workspace.command(db,member,request(p,'share','context',0,fields={**fields,'blockers':[]},draft_version=saved['draft_version']))
    result=CliRunner().invoke(cli.app,['workflow','accept-shared','fix_m0-test','context','--version','0','--draft-version',str(saved['draft_version']),'--sha256',saved['sha256']])
    assert result.exit_code==0,result.output
    with DB.session() as db:
        p=db.scalars(select(Plat)).one()
        assert db.get(Workflow,p.id).stage=='definition'
        assert L.latest(db,p,'context').content['summary']=='Ticket context'
