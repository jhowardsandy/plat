# ruff: noqa: F811
import json
import subprocess
import sys
from dataclasses import replace

import pytest
from sqlalchemy import select
from test_intake import command, story
from test_m0 import case, project  # noqa: F401

from plat import config, intake, planning, team, workspace
from plat import db as DB
from plat import lifecycle as L
from plat import story_sources as S
from plat.models import Plat, Workflow


def adapter(tmp_path, data):
    source=tmp_path/'source.json'; source.write_text(json.dumps(data))
    script=tmp_path/'read.py'; script.write_text('import pathlib,sys\nprint(pathlib.Path(sys.argv[1]).read_text())\n')
    return {'story_argv':[sys.executable,str(script),str(source),'{anchor}']}


def test_read_adapter_exact_identity_and_normalized_output(tmp_path):
    cfg=type('Cfg',(),{'tracker':adapter(tmp_path,story())})()
    observed=S.fetch(cfg,'DEV-1234','local-owner')
    assert observed['story']['captain']=='local-owner'
    assert observed['provenance']=='configured_source' and observed['observed_at']
    assert observed['sha256']==L.digest(observed['story'])
    with pytest.raises(ValueError,match='different ticket'):
        S.fetch(cfg,'DEV-5678','owner')
    with pytest.raises(ValueError,match='safe ticket'):
        S.fetch(cfg,'x; touch /tmp/should-not-exist','owner')


@pytest.mark.parametrize('argv', [None,'echo {anchor}',[],['echo'],['echo','fixed'],[None,'{anchor}'],['{anchor}','{anchor}']])
def test_adapter_must_be_configured_argv(argv):
    cfg=type('Cfg',(),{'tracker':{'story_argv':argv}})()
    with pytest.raises(ValueError,match='configure'):
        S.fetch(cfg,'DEV-1234','owner')


@pytest.mark.parametrize('script,message', [
    ('import sys; print("secret-token",file=sys.stderr); sys.exit(1)','failed'),
    ('print("not-json")','JSON'),('print("x"*48001)','exceeded'),
    ('print("[]")','different ticket'),('print("{}")','different ticket')])
def test_bad_source_never_leaks_stderr(tmp_path,script,message):
    f=tmp_path/'adapter.py'; f.write_text(script)
    cfg=type('Cfg',(),{'tracker':{'story_argv':[sys.executable,str(f),'{anchor}']}})()
    with pytest.raises(ValueError,match=message) as exc:
        S.fetch(cfg,'DEV-1234','owner')
    assert 'secret-token' not in str(exc.value)


def test_timeout_is_unavailable(monkeypatch):
    def timeout(*args,**kwargs):
        assert kwargs['shell'] is False and kwargs['stdin']==subprocess.DEVNULL
        raise subprocess.TimeoutExpired('private adapter',30)
    monkeypatch.setattr(S.subprocess,'run',timeout)
    cfg=type('Cfg',(),{'tracker':{'story_argv':['adapter','{anchor}']}})()
    with pytest.raises(ValueError,match='unavailable'):
        S.fetch(cfg,'DEV-1234','owner')


def seed(case,db):
    data=story(); intake.start(db,case.cfg,data,intake.preview(data)['sha256'])
    return db.scalars(select(Plat)).one()


def test_source_import_room_receipt_preserves_provenance_and_retries(case,monkeypatch):
    cfg=replace(case.cfg,tracker=adapter(case.project,story()))
    monkeypatch.setattr(config,'load',lambda:cfg)
    with DB.session() as db:
        member,_=team.add_member(db,'engineer','engineer')
        request=command('story_fetch',anchor='DEV-1234',captain='engineer')
        source=workspace.command(db,member,request)
        assert db.scalars(select(Plat)).first() is None
        create=command('story_start',preview_key=request['key'],sha256=source['sha256'])
        first=workspace.command(db,member,create)
        assert workspace.command(db,member,create)==first
        p=db.scalars(select(Plat)).one()
        assert intake.project(db,p)['provenance']=='configured_source'
        assert db.get(Workflow,p.id).stage=='context'


def test_observation_diff_does_not_change_baseline_or_workflow(case):
    changed=story(); changed['outcomes']=['Different capability']; changed['source_revision']='v5'
    cfg=replace(case.cfg,tracker=adapter(case.project,changed))
    with DB.session() as db:
        p=seed(case,db); baseline=intake.project(db,p)
        S.observe(db,cfg,p,0,who='reviewer')
        shown=planning.project(db,p)['source_review']['observation']
        assert shown['changes']['outcomes']=={'before':baseline['story']['outcomes'],'after':changed['outcomes']}
        assert intake.project(db,p)==baseline
        assert db.get(Workflow,p.id).version==0
        assert not case.calls
        with pytest.raises(ValueError,match='stale version'):
            S.observe(db,cfg,p,99)
        assert len(S.rows(db,p,'story_observation'))==1


def change(disposition='proposed'):
    return {'before':'Existing requirement','after':'Changed requirement','reason':'Clarification',
            'source':'Slack discussion with product','participants':'Engineer and product owner',
            'impact':'Review specification, plan and regression coverage', 'disposition':disposition}


@pytest.mark.parametrize('disposition',['proposed','decided','engineering_detail'])
def test_change_is_attributed_record_not_approval_and_concurrent_edits_fail(case,disposition):
    with DB.session() as db:
        p=seed(case,db); baseline=intake.project(db,p)
        member,_=team.add_member(db,'engineer','engineer')
        request=command('requirement_change',plat=p.public_id,version=0,ledger_version=0,fields=change(disposition))
        first=workspace.command(db,member,request)
        assert workspace.command(db,member,request)==first
        shown=planning.project(db,p)['source_review']
        assert shown['ledger_version']==first['id']
        assert shown['changes'][0]['application']=='record_only'
        assert shown['changes'][0]['recorded_by']=='engineer'
        assert shown['changes'][0]['disposition']==disposition
        assert db.get(Workflow,p.id).version==0 and db.get(Workflow,p.id).stage=='context'
        assert intake.project(db,p)==baseline
        with pytest.raises(ValueError,match='refresh the ledger'):
            S.record_change(db,p,change(),0,0)
        with pytest.raises(ValueError,match='stale version'):
            S.record_change(db,p,change(),99,first['id'])


def test_viewer_denied_and_started_plan_stays_immutable(case):
    with DB.session() as db:
        p=seed(case,db); viewer,_=team.add_member(db,'viewer','viewer')
        for action in ['story_fetch','story_refresh','requirement_change']:
            with pytest.raises(team.Forbidden):
                workspace.command(db,viewer,command(action,plat=p.public_id))
        p.status='running'  # fixture: ledger remains usable during execution, without revising scope
        S.record_change(db,p,change('decided'),0,0)
        assert p.status=='running' and db.get(Workflow,p.id).version==0


def test_cli_import_refresh_and_record_change_share_browser_evidence(case,monkeypatch):
    import yaml
    from typer.testing import CliRunner

    from plat import cli, workflow_cli
    cfg=replace(case.cfg,tracker=adapter(case.project,story()))
    monkeypatch.setattr(workflow_cli,'load',lambda:cfg)
    runner=CliRunner()
    args=['workflow','import-story','DEV-1234','--captain','engineer']
    first=runner.invoke(cli.app,args)
    assert first.exit_code==0,first.output
    hashed=json.loads(first.output)['sha256']
    commit=runner.invoke(cli.app,[*args,'--accept-hash',hashed])
    assert commit.exit_code==0,commit.output
    refreshed=runner.invoke(cli.app,['workflow','refresh-story','DEV-1234','--version','0'])
    assert refreshed.exit_code==0,refreshed.output
    path=case.project/'change.yaml'; path.write_text(yaml.safe_dump(change('decided')))
    recorded=runner.invoke(cli.app,['workflow','record-change','DEV-1234','--file',str(path),'--version','0','--ledger-version','0'])
    assert recorded.exit_code==0,recorded.output
    with DB.session() as db:
        p=db.scalars(select(Plat)).one()
        shown=workspace.room(db,p.public_id)['planning']['source_review']
        assert shown['observation']['changes']=={}
        assert shown['changes'][0]['disposition']=='decided'


def test_failed_refresh_retains_last_observation_and_closed_ledger_rejects(case,monkeypatch):
    from datetime import datetime, timezone

    cfg=replace(case.cfg,tracker=adapter(case.project,story()))
    with DB.session() as db:
        p=seed(case,db)
        S.observe(db,cfg,p,0)
        prior=S.project(db,p)['observation']
        def unavailable(*args):
            raise L.WorkflowError('unavailable')
        monkeypatch.setattr(S,'fetch',unavailable)
        with pytest.raises(ValueError,match='unavailable'):
            S.observe(db,cfg,p,0)
        assert S.project(db,p)['observation']==prior
        p.closed_at=datetime.now(timezone.utc); db.flush()
        with pytest.raises(ValueError,match='immutable'):
            S.record_change(db,p,change(),0,0)


def test_cli_source_error_does_not_print_configuration_or_connector_output(case,monkeypatch):
    from typer.testing import CliRunner

    from plat import cli, workflow_cli
    script=case.project/'broken.py'; script.write_text('import sys\nprint("private-token",file=sys.stderr)\nsys.exit(1)\n')
    cfg=replace(case.cfg,tracker={'story_argv':[sys.executable,str(script),'{anchor}','private-argument']})
    monkeypatch.setattr(workflow_cli,'load',lambda:cfg)
    result=CliRunner().invoke(cli.app,['workflow','import-story','DEV-1234','--captain','engineer'])
    assert result.exit_code!=0 and 'source failed' in result.output
    assert 'private-token' not in result.output and 'private-argument' not in result.output
