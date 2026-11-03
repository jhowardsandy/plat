# ruff: noqa: F811
import json
import uuid

import pytest
import yaml
from sqlalchemy import func, select
from test_m0 import case, project  # noqa: F401
from typer.testing import CliRunner

from plat import cli, config, intake, planning, team, workflow_cli, workspace
from plat import db as DB
from plat import lifecycle as L
from plat.models import Artifact, Decision, Lot, Plat, Workflow


def story(**overrides):
    return dict(key='DEV-1234', title='Deliver the outcome', captain='engineer',
                source_ref='https://jira.example/browse/DEV-1234', source_revision='revision 4',
                requirements='A customer can inspect the result.', scope='Existing customers',
                outcomes=['The result is visible', 'Errors are explained'],
                context_refs=['layline:architecture'], estimate='3 points', **overrides)


def command(action, **data):
    return dict(key=str(uuid.uuid4()), action=action, **data)


@pytest.mark.parametrize('field,value', [('key','../escape'),('key','a;echo bad'),('outcomes',[]),
    ('outcomes','not a list'),('captain',''),('source_revision',''),('requirements', True),
    ('context_refs',[None]),('scope',' '),('estimate',{}),('title','x'*36000)])
def test_invalid_baselines(field, value):
    data=story(); data[field]=value
    with pytest.raises(L.WorkflowError):
        intake.preview(data)


def test_preview_is_read_only_and_cli_commit_matches_room(case, monkeypatch):
    monkeypatch.setattr(workflow_cli,'load',lambda:case.cfg)
    path=case.project/'story.yaml'; path.write_text(yaml.safe_dump(story()))
    runner=CliRunner()
    result=runner.invoke(cli.app,['workflow','intake','--file',str(path)])
    assert result.exit_code==0,result.output
    preview=json.loads(result.output)
    with DB.session() as db:
        assert db.scalar(select(func.count()).select_from(Plat))==0
    args=['workflow','intake','--file',str(path),'--accept-hash',preview['sha256']]
    result=runner.invoke(cli.app,args)
    assert result.exit_code==0,result.output
    with DB.session() as db:
        p=db.scalars(select(Plat)).one()
        w=db.get(Workflow,p.id)
        assert w.stage=='context' and w.version==0 and w.owner=='engineer'
        shown=workspace.room(db,p.public_id)['planning']
        assert shown['story_baseline']['story']==preview['story']
        assert shown['drafts']=={} and shown['suggestions']['specification']['outcomes'][1]['id']=='O2'
        assert db.scalar(select(func.count()).select_from(Lot))==0
        assert not case.calls
        assert not L.latest(db,p,'context')
    assert runner.invoke(cli.app,args).exit_code!=0


def test_browser_preview_identity_hash_replay_and_approval_parity(case,monkeypatch):
    monkeypatch.setattr(config,'load',lambda:case.cfg)
    with DB.session() as db:
        member,_=team.add_member(db,'engineer','engineer')
        other,_=team.add_member(db,'other','engineer')
        viewer,_=team.add_member(db,'viewer','viewer')
        preview_cmd=command('story_preview',story=story())
        for action in ['story_preview','story_start']:
            with pytest.raises(team.Forbidden):
                workspace.command(db,viewer,command(action,story=story()))
        preview=workspace.command(db,member,preview_cmd)
        commit=command('story_start',preview_key=preview_cmd['key'],sha256=preview['sha256'])
        with pytest.raises(ValueError,match='own story preview'):
            workspace.command(db,other,commit)
        with pytest.raises(ValueError,match='story changed'):
            workspace.command(db,member,{**commit,'sha256':'wrong'})
        result=workspace.command(db,member,commit)
        assert workspace.command(db,member,commit)==result
        p=db.scalars(select(Plat)).one()
        assert all(d.actor_detail=='engineer' for d in db.scalars(select(Decision).where(Decision.kind=='workflow')))
        baseline=intake.project(db,p)
        fields=planning.project(db,p)['suggestions']['context']
        saved=planning.share(db,p,'context',fields,0,0,who='engineer')
        with pytest.raises(ValueError,match='reason'):
            planning.accept(db,p,'context',0,saved['draft_version'],saved['sha256'])
        fields['sources'][1]['reason']='Architecture unavailable; discussed the requirements with the owner.'
        saved=planning.share(db,p,'context',fields,0,saved['draft_version'],who='engineer')
        planning.accept(db,p,'context',0,saved['draft_version'],saved['sha256'])
        assert db.get(Workflow,p.id).stage=='definition'
        assert intake.project(db,p)==baseline
        assert db.scalar(select(func.count()).select_from(Artifact).where(Artifact.kind=='story_baseline'))==1
        with pytest.raises(ValueError,match='already has a plat'):
            workspace.command(db,member,{**commit,'key':str(uuid.uuid4())})


def test_missing_captain_or_unreviewed_source_cannot_create(case,monkeypatch):
    monkeypatch.setattr(config,'load',lambda:case.cfg)
    with DB.session() as db:
        member,_=team.add_member(db,'other','engineer')
        preview_cmd=command('story_preview',story=story())
        result=workspace.command(db,member,preview_cmd)
        with pytest.raises(ValueError,match='captain'):
            workspace.command(db,member,command('story_start',preview_key=preview_cmd['key'],sha256=result['sha256']))
        with pytest.raises(ValueError,match='own story preview'):
            workspace.command(db,member,command('story_start',preview_key='missing',sha256=result['sha256']))
        assert db.scalar(select(func.count()).select_from(Plat))==0


def test_changed_file_requires_new_review(case):
    original=intake.preview(story())
    changed=story(); changed['requirements']='A materially different capability'
    with DB.session() as db:
        with pytest.raises(ValueError,match='story changed'):
            intake.start(db,case.cfg,changed,original['sha256'])
        assert db.scalar(select(func.count()).select_from(Plat))==0


def test_two_intakes_for_same_ticket_create_only_one_baseline(case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier=Barrier(2)
    data=story(); reviewed=intake.preview(data)
    def create():
        barrier.wait(timeout=10)
        try:
            with DB.session() as db:
                return intake.start(db,case.cfg,data,reviewed['sha256'])['plat']
        except L.WorkflowError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:create(),range(2)))
    assert sum('already has a plat' in r for r in results)==1
    with DB.session() as db:
        assert db.scalar(select(func.count()).select_from(Plat))==1
        assert db.scalar(select(func.count()).select_from(Artifact).where(Artifact.kind=='story_baseline'))==1


def test_failed_intake_rolls_back_all_domain_and_audit_rows(case, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('storage interrupted')
    monkeypatch.setattr(L,'begin',fail)
    data=story()
    with pytest.raises(RuntimeError):
        with DB.session() as db:
            intake.start(db,case.cfg,data,intake.preview(data)['sha256'])
    with DB.session() as db:
        for model in (Plat,Artifact,Decision,Workflow):
            assert db.scalar(select(func.count()).select_from(model))==0
