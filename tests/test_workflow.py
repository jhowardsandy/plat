# ruff: noqa: F811
"""Real Postgres, git, shell and HTTP qualification of the expanded workflow."""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml
from sqlalchemy import select, text
from test_m0 import case, git, plan, project, rows, start, valid_spec  # noqa: F401

from plat import db as DB
from plat import lifecycle as L
from plat import ownership, runner, sessions, team
from plat.models import (
    Attempt,
    CommandReceipt,
    ExecutionClaim,
    Lot,
    Plat,
    TrackedSession,
    Workflow,
)
from plat.team_server import Handler, ThreadingHTTPServer


def objects(db):
    return db.scalars(select(Plat)).one(), db.scalars(select(Lot)).one()


def advance_to_development(db, p):
    L.begin(db, p, 'justin')
    L.accept(db, p, 'context', {'summary': 'Test context', 'sources': [
        {'reference': 'ticket:test', 'status': 'collected', 'snapshot': 'The requested behavior'}]}, 0)
    L.accept(db, p, 'definition', {'problem': 'old behavior', 'outcome': 'new behavior',
        'scope': 'app.txt', 'success_measures': ['tests pass']}, 1)
    L.accept(db, p, 'specification', {'note': 'scope accepted'}, 2)


def test_migrations_repeat_without_losing_rows(case):
    plan(case)
    before = rows(Plat)[0].public_id
    DB.init(); DB.init()
    assert rows(Plat)[0].public_id == before
    with DB.session() as db:
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0007'


def test_exclusive_claim_and_fenced_old_result(case):
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        info = ownership.acquire(db, case.cfg, p, lot)
    with DB.session() as db:
        p, lot = objects(db)
        with pytest.raises(ownership.OwnershipError):
            ownership.acquire(db, case.cfg, p, lot)
    with DB.session() as db:
        claim = db.get(ExecutionClaim, info[0])
        claim.token = str(uuid.uuid4())
        claim.generation += 1
    with DB.session() as db:
        token = ownership.current.set(info)
        try:
            with pytest.raises(ownership.OwnershipError):
                ownership.fence(db)
        finally:
            ownership.current.reset(token)


def test_concurrent_claim_exactly_one_winner(case):
    plan(case)
    barrier = threading.Barrier(2)
    def claim():
        with DB.session() as db:
            p, lot = objects(db)
            barrier.wait()
            try:
                ownership.acquire(db, case.cfg, p, lot)
                return True
            except ownership.OwnershipError:
                return False
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(results) == [False, True]


def test_expiry_does_not_transfer_claim_and_remote_reconcile_denied(case):
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        ownership.acquire(db, case.cfg, p, lot)
        claim = db.get(ExecutionClaim, lot.id)
        claim.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        claim.host_id = 'another-host'
    with DB.session() as db:
        p, lot = objects(db)
        with pytest.raises(ownership.OwnershipError, match='owning installation'):
            ownership.reconcile(db, case.cfg, p, lot, 'inspected')


def test_launched_and_interactive_sessions(case):
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        one = sessions.attach(db, case.cfg, p, lot, 'codex', 'real-session')
        two = sessions.attach(db, case.cfg, p, lot, 'codex', 'real-session')
        assert one.id == two.id
        with pytest.raises(ownership.OwnershipError, match='interactive'):
            ownership.acquire(db, case.cfg, p, lot)
    assert not rows(Attempt)
    with DB.session() as db:
        p, lot = objects(db)
        sessions.attach(db, case.cfg, p, lot, 'codex', 'real-session', state='completed', note='committed and stopped')
    start()
    tracked = rows(TrackedSession)
    assert len(tracked) == 3
    assert {r.provider for r in tracked} == {'claude', 'codex'}
    assert all(r.state == 'completed' for r in tracked)
    assert all(a.host_id and a.executor_id and a.claim_generation for a in rows(Attempt))


def test_context_definition_and_version_gate_development(case):
    plan(case)
    with DB.session() as db:
        p, _ = objects(db)
        L.begin(db, p, 'justin')
        with pytest.raises(L.WorkflowError, match='accept'):
            L.require_accepted(db, p)
        with pytest.raises(L.WorkflowError, match='stale'):
            L.accept(db, p, 'context', {}, 9)
        with pytest.raises(L.WorkflowError, match='sources'):
            L.accept(db, p, 'context', {}, 0)
    assert not rows(Attempt)


def test_lifecycle_through_ready_and_stale_commit(case):
    plan(case)
    with DB.session() as db:
        p, _ = objects(db)
        advance_to_development(db, p)
    start()
    with DB.session() as db:
        p, lot = objects(db)
        result = L.integration(db, case.cfg, p, {'checks': [{'lot': 'a', 'cmd': 'grep -q changed app.txt'}]}, 3)
        assert result.content['passed']
        w = db.get(Workflow, p.id)
        L.accept(db, p, 'recording', {'summary': 'test change', 'rollout': 'local test',
            'rollback': 'restore prior file', 'test_instructions': 'read the test output',
            'configuration': 'none', 'risks': 'local fixture only'}, w.version)
        deployed = L.deploy(db, case.cfg, p, {'environment': 'local', 'lot': 'a',
            'deploy': 'true', 'smoke': 'grep -q changed app.txt', 'rollback': 'true', 'artifact': 'fixture'}, w.version)
        assert deployed.content['passed']
        L.accept(db, p, 'validation', {'note': 'observed local fixture', 'checks': ['smoke passed'], 'accepted': True}, w.version)
        assert L.readiness(db, p)['ready']
        (Path(lot.worktree_path) / 'app.txt').write_text('new change')
        assert not L.readiness(db, p)['ready']
        assert 'integration evidence missing or stale' in L.readiness(db, p)['missing']
    assert {'integration', 'deploy', 'smoke'} <= {a.phase for a in rows(Attempt)}


def test_failed_deployment_rolls_back_and_cannot_be_ready(case):
    plan(case)
    with DB.session() as db:
        p, _ = objects(db)
        advance_to_development(db, p)
    start()
    with DB.session() as db:
        p, lot = objects(db)
        L.integration(db, case.cfg, p, {'checks': [{'lot': 'a', 'cmd': 'true'}]}, 3)
        w = db.get(Workflow, p.id)
        L.accept(db, p, 'recording', {k: 'fixture' for k in ['summary','rollout','rollback','test_instructions','configuration','risks']}, w.version)
        result = L.deploy(db, case.cfg, p, {'environment': 'dev', 'lot': 'a', 'deploy': 'true',
            'smoke': 'exit 7', 'rollback': 'echo rolled-back', 'artifact': 'fixture'}, w.version)
        assert not result.content['passed']
        assert result.content['results']['rollback']['exit_code'] == 0
        assert w.stage == 'deployment'
        assert not L.readiness(db, p)['ready']


def test_team_idempotency_versions_and_review_invalidation(case):
    plan(case); start()
    with DB.session() as db:
        p, lot = objects(db)
        alice, _ = team.add_member(db, 'alice', 'engineer')
        bob, _ = team.add_member(db, 'bob', 'engineer')
        req = {'key':'request-1', 'action':'review', 'plat':p.public_id, 'owner':'bob', 'body':{'text':'review this change'}}
        first = team.command(db, alice, req)
        assert team.command(db, alice, req) == first
        assert len(db.scalars(select(CommandReceipt)).all()) == 1
        with pytest.raises(team.Forbidden):
            team.command(db, alice, {'key':'bad-owner', 'action':'review_result', 'plat':p.public_id,
                'item':first['id'], 'version':1, 'note':'done', 'outcome':'approved'})
        with pytest.raises(team.Conflict, match='stale'):
            team.command(db, bob, {'key':'bad-version', 'action':'review_result', 'plat':p.public_id,
                'item':first['id'], 'version':2, 'note':'done', 'outcome':'approved'})
        (Path(lot.worktree_path) / 'app.txt').write_text('different')
        with pytest.raises(team.Conflict, match='revisions changed'):
            team.command(db, bob, {'key':'stale-review', 'action':'review_result', 'plat':p.public_id,
                'item':first['id'], 'version':1, 'note':'done', 'outcome':'approved'})


def test_http_authentication_read_only_and_replay(case):
    plan(case)
    with DB.session() as db:
        viewer, token = team.add_member(db, 'reader', 'viewer')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(base + '/api/board')
        assert error.value.code == 403
        headers = {'Authorization': 'Bearer ' + token}
        with urlopen(Request(base + '/api/board', headers=headers)) as response:
            result = json.load(response)
        assert result['plats'][0]['anchor'] == 'fix_m0-test'
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + '/api/commands', data=b'{}', headers=headers))
        assert error.value.code == 403
        with urlopen(Request(base + '/api/events?after=0', headers=headers)) as response:
            result = json.load(response)
        assert result['events']
        with urlopen(Request(base + '/api/events?after='+str(result['cursor']), headers=headers)) as response:
            assert not json.load(response)['events']
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_full_phase_contracts(case, monkeypatch):
    from plat import adapters
    cfg = yaml.safe_load(case.cfg.roles_path.read_text())
    cfg['roles']['documenter'] = {'provider':'claude', 'model':'default'}
    cfg['roles']['reviewer.quality'] = {'provider':'codex', 'model':'default'}
    case.cfg.roles_path.write_text(yaml.safe_dump(cfg))
    def fake(role, prompt_file, cwd, **kwargs):
        if role.name in ('documenter', 'reviewer.quality'):
            from plat.adapters.base import AgentResult
            phase = 'docs' if role.name == 'documenter' else 'quality'
            (prompt_file.parent / (phase+'.md')).write_text('Evidence-backed documentation or quality assessment')
            (prompt_file.parent / (phase+'.json')).write_text(json.dumps({'verdict':'pass','findings':[],
                'criteria_check':[{'id':'AC1','met':True}]}))
            return AgentResult(True, 0, .1, provider=role.provider)
        return case.fake(role, prompt_file, cwd, **kwargs)
    monkeypatch.setattr(adapters, 'run', fake)
    plan(case, valid_spec(phases=['code','review','docs','quality']))
    start()
    assert [a.phase for a in rows(Attempt)] == ['code','gate','review','docs','quality']
    assert rows(Lot)[0].state == 'DONE'


def test_upgrade_from_real_legacy_schema_preserves_history(case):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    schema = 'legacy_' + uuid.uuid4().hex
    engine = DB.engine()
    with engine.begin() as c:
        c.execute(text(f'CREATE SCHEMA {schema}'))
    isolated = create_engine(engine.url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        config = Config()
        config.set_main_option('script_location', str(Path(DB.__file__).parent / 'migrations'))
        with isolated.begin() as c:
            config.attributes['connection'] = c
            command.upgrade(config, '0001')
            c.execute(text("INSERT INTO plats (anchor, tickets, related, title, kind, status, budget_usd, origin_id) VALUES ('old', '{}', '{}', 'history', 'plat', 'planned', 0, 'old-install')"))
        with isolated.begin() as c:
            config.attributes['connection'] = c
            command.upgrade(config, 'head')
            old = c.execute(text('SELECT anchor, title, public_id FROM plats')).one()
            assert old.anchor == 'old' and old.title == 'history' and len(old.public_id) == 36
    finally:
        isolated.dispose()
        with engine.begin() as c:
            c.execute(text(f'DROP SCHEMA {schema} CASCADE'))


def test_fenced_mutation_is_rolled_back(case):
    from plat.decisions import record
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        info = ownership.acquire(db, case.cfg, p, lot)
        claim = db.get(ExecutionClaim, lot.id)
        claim.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(ownership.OwnershipError):
        with DB.session() as db:
            p, lot = objects(db)
            token = ownership.current.set(info)
            try:
                record(db, plat_id=p.id, lot_id=lot.id, actor='system', actor_detail='test',
                    kind='transition', decision='should never happen', inputs={},
                    apply=lambda: setattr(lot, 'state', 'DONE'))
            finally:
                ownership.current.reset(token)
    assert rows(Lot)[0].state == 'PENDING'


def test_wrong_host_never_resumes_provider_session(case):
    from plat.models import Session
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        a = Attempt(lot_id=lot.id, phase='code', n=1, provider='codex', model='default',
                    host_id='remote', executor_id=case.cfg.origin_id)
        db.add(a); db.flush()
        db.add(Session(attempt_id=a.id, provider='codex', provider_session_id='remote-session'))
        assert runner._prior_session(db, lot, 'codex', case.cfg) is None


def test_parallel_dispatch_overlaps_independent_lots(case, monkeypatch):
    from test_m0 import repo

    from plat import adapters
    from plat.adapters.base import AgentResult
    from plat.dispatch.inline import InlineDispatcher
    repo(case.project / 'services/b')
    d = valid_spec(budget_usd=0)
    d['lots'].append({**d['lots'][0], 'key':'b', 'repo':'services/b'})
    d['criteria'][0]['lot'] = 'a'
    d['criteria'].append({'id':'AC2', 'lot':'b', 'statement':'b changed', 'verify':'grep -q changed app.txt'})
    barrier = threading.Barrier(2)
    def fake(role, prompt_file, cwd, **kwargs):
        key = cwd.name
        ac = 'AC1' if key == 'a' else 'AC2'
        phase = 'code' if role.name == 'coder' else 'review'
        if phase == 'code':
            barrier.wait(timeout=10)  # serial dispatch cannot satisfy this
            (cwd / 'app.txt').write_text('changed')
            git(cwd, 'add', 'app.txt'); git(cwd, 'commit', '-qm', 'change')
            verdict = {'lot':key, 'phase':'code', 'status':'complete', 'summary':'changed',
                       'tests':{'cmd':'true','ran':True}, 'acceptance_criteria':[{'id':ac,'met':True}]}
        else:
            verdict = {'verdict':'pass','findings':[], 'criteria_check':[{'id':ac,'met':True}]}
        (prompt_file.parent / (phase+'.json')).write_text(json.dumps(verdict))
        (prompt_file.parent / (phase+'.md')).write_text('evidence')
        return AgentResult(True,0,.1,provider=role.provider)
    monkeypatch.setattr(adapters, 'run', fake)
    plan(case,d)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one(); p.status = 'running'; pid=p.id
    states=InlineDispatcher().run(case.cfg,pid,workers=2,log=lambda _:None)
    assert list(states.values()) == ['DONE','DONE']
    assert rows(Plat)[0].status == 'delivered'


def test_blocking_docs_contract_cannot_close(case, monkeypatch):
    from plat import adapters
    from plat.adapters.base import AgentResult
    cfg=yaml.safe_load(case.cfg.roles_path.read_text())
    cfg['roles']['documenter']={'provider':'claude','model':'default'}
    case.cfg.roles_path.write_text(yaml.safe_dump(cfg))
    def fake(role, prompt_file, cwd, **kwargs):
        if role.name != 'documenter':
            return case.fake(role,prompt_file,cwd,**kwargs)
        (prompt_file.parent/'docs.json').write_text(json.dumps({'verdict':'blocked', 'blocking_question':'Which rollout?',
            'findings':[], 'criteria_check':[{'id':'AC1','met':False}]}))
        (prompt_file.parent/'docs.md').write_text('Need rollout instructions')
        return AgentResult(True,0,.1,provider=role.provider)
    monkeypatch.setattr(adapters,'run',fake)
    plan(case,valid_spec(phases=['code','review','docs'])); start()
    assert rows(Lot)[0].state=='BLOCKED'
    assert rows(Plat)[0].delivered_at is None


def test_skill_install_preserves_unrelated_directories(tmp_path, monkeypatch):
    from plat.skill_links import install
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    existing=tmp_path/'.agents/skills/plat-up'
    existing.mkdir(parents=True)
    (existing/'mine').write_text('keep')
    install()
    assert (tmp_path/'.claude/skills/plat-run').is_symlink()
    assert (tmp_path/'.agents/skills/plat-run').is_symlink()
    install(unlink=True)
    assert (existing/'mine').read_text()=='keep'
    assert not (tmp_path/'.agents/skills/plat-run').exists()


def test_investigation_completes_with_evidence_without_a_code_diff(case, monkeypatch):
    from plat import adapters
    from plat.adapters.base import AgentResult
    def fake(role, prompt_file, cwd, **kwargs):
        phase='code' if role.name=='coder' else 'review'
        v=({'lot':'a','phase':'code','status':'complete','summary':'Observed the baseline',
            'tests':{'cmd':'test -f app.txt','ran':True},'acceptance_criteria':[{'id':'AC1','met':True}]}
           if phase=='code' else {'verdict':'pass','findings':[],'criteria_check':[{'id':'AC1','met':True}]})
        (prompt_file.parent/(phase+'.json')).write_text(json.dumps(v))
        (prompt_file.parent/(phase+'.md')).write_text('app.txt exists and contains baseline evidence')
        return AgentResult(True,0,.1,provider=role.provider)
    monkeypatch.setattr(adapters,'run',fake)
    d=valid_spec();d['lots'][0]['kind']='investigation';d['criteria'][0]['verify']='test -f app.txt'
    plan(case,d);start()
    assert rows(Lot)[0].state=='DONE'
    from plat.models import Verdict
    gate=rows(Verdict)[1].raw
    assert gate['diff_files']==0 and gate['diff_required'] is False


def test_live_probe_failure_has_nonzero_exit(case, monkeypatch):
    from typer.testing import CliRunner

    from plat import cli
    from plat.adapters.base import AgentResult
    monkeypatch.setattr(cli,'run_agent',lambda *a,**kw:AgentResult(False,0,.1,error='turn failed'))
    result=CliRunner().invoke(cli.app,['probe','--provider','codex','--model','gpt-5.5'])
    assert result.exit_code==1
    assert 'turn failed' in result.output


def test_command_retry_different_body_rejected(case):
    plan(case)
    with DB.session() as db:
        p,_=objects(db)
        member,_=team.add_member(db,'alice','engineer')
        request={'key':'stable','action':'question','plat':p.public_id,'owner':'alice','body':{'text':'Which env?'}}
        team.command(db,member,request)
        with pytest.raises(team.Conflict):
            team.command(db,member,{**request,'body':{'text':'Changed question'}})


def test_team_answer_survives_reopen_context(case):
    plan(case)
    with DB.session() as db:
        p,lot=objects(db);lot.state='BLOCKED'
        member,_=team.add_member(db,'alice','engineer')
        q=team.command(db,member,{'key':'question','action':'question','plat':p.public_id,'lot':lot.public_id,
                                 'owner':'alice','body':{'text':'Which environment?'}})
        team.command(db,member,{'key':'answer','action':'answer','plat':p.public_id,'item':q['id'],
                               'version':1,'note':'Use the dev environment'})
        runner.reopen_lot(db,p,lot,'Proceed with the recorded answer')
        assert 'Use the dev environment' in lot.human_note
        assert 'Proceed with the recorded answer' in lot.human_note


def test_team_client_rejects_credential_url_and_insecure_remote(tmp_path):
    import typer

    from plat.team_cli import call
    token=tmp_path/'token';token.write_text('not-a-real-token')
    for url in ['http://localhost:password@example.com','http://example.com:7318','https://user:pw@example.com']:
        with pytest.raises(typer.BadParameter):
            call('board',token,url)


def test_queued_job_is_idempotent_and_host_routed(case,monkeypatch):
    from plat.dispatch import tasks
    plan(case)
    with DB.session() as db:
        p,_=objects(db);p.status='running';pid=p.id
    monkeypatch.setattr(tasks,'load',lambda:case.cfg)
    with pytest.raises(ValueError,match='different installation'):
        tasks.execute.run(pid,'another-installation')
    tasks.execute.run(pid,case.cfg.origin_id)
    before=len(rows(Attempt))
    tasks.execute.run(pid,case.cfg.origin_id)
    assert len(rows(Attempt))==before
    assert rows(Plat)[0].status=='delivered'


def test_abstract_survives_worktree_removal(case):
    import shutil
    plan(case)
    with DB.session() as db:
        p,lot=objects(db)
        advance_to_development(db,p)
        shutil.rmtree(lot.worktree_path)
        data=L.abstract(db,p)
        assert data['records']
        assert data['readiness']['ready'] is False
        assert 'historical' in data['readiness']['missing'][0]


def test_context_can_be_recorded_before_a_plan(case):
    # The module resolves its own config; do not depend on the operator's settings.
    from unittest.mock import patch

    from typer.testing import CliRunner

    from plat import cli, workflow_cli
    with patch.object(workflow_cli,'load',lambda:case.cfg):
        result=CliRunner().invoke(cli.app,['workflow','begin','fix_m0-test','--owner','justin'])
    assert result.exit_code==0,result.output
    assert not rows(Lot)
    with DB.session() as db:
        p=db.scalars(select(Plat)).one()
        L.accept(db,p,'context',{'summary':'scope','sources':[{'reference':'ticket','status':'collected','snapshot':'requirement'}]},0)
        L.accept(db,p,'definition',{'problem':'bug','outcome':'fix','scope':'one file','success_measures':['test']},1)
    plan(case)
    with DB.session() as db:
        p,_=objects(db)
        L.accept(db,p,'specification',{'note':'approved'},2)
        L.require_accepted(db,p)


def test_integration_cannot_bypass_failed_run_criteria(case):
    d=valid_spec();d['criteria'].append({'id':'RUN1','scope':'run','verify_in':'a',
        'statement':'integration passes','verify':'exit 1'})
    plan(case,d)
    with DB.session() as db:
        p,_=objects(db);advance_to_development(db,p)
    start()
    with DB.session() as db:
        p,_=objects(db)
        assert p.status=='blocked'
        with pytest.raises(L.WorkflowError,match='run-level'):
            L.integration(db,case.cfg,p,{'checks':[{'lot':'a','cmd':'true'}]},3)
        assert L.readiness(db,p)['ready'] is False


def test_cancellation_prevents_further_deployment_commands(case,monkeypatch):
    import subprocess

    from plat import gates
    plan(case)
    with DB.session() as db:
        p,_=objects(db);advance_to_development(db,p)
    start()
    with DB.session() as db:
        p,lot=objects(db)
        L.integration(db,case.cfg,p,{'checks':[{'lot':'a','cmd':'true'}]},3)
        w=db.get(Workflow,p.id)
        L.accept(db,p,'recording',{k:'fixture' for k in ['summary','rollout','rollback','test_instructions','configuration','risks']},w.version)
    calls=[]
    def cancel(cmd,cwd,**kw):
        calls.append(cmd)
        with DB.session() as other:
            p,lot=objects(other);p.status='aborted';lot.state='ABORTED'
        return subprocess.CompletedProcess(cmd,143,'cancelled','')
    monkeypatch.setattr(gates,'_sh',cancel)
    with pytest.raises(L.WorkflowError,match='stopped'):
        with DB.session() as db:
            p,_=objects(db);w=db.get(Workflow,p.id)
            L.deploy(db,case.cfg,p,{'environment':'local','lot':'a','artifact':'fixture',
                'deploy':'deploy-command','smoke':'smoke-command','rollback':'rollback-command'},w.version)
    assert calls==['deploy-command']
