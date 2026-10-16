# ruff: noqa: F811
"""Recorded controls are authorization-bound and never replay ambiguous execution."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_m0 import case, plan, project  # noqa: F401

from plat import db as DB
from plat import executor, team
from plat.models import ExecutionCommand, Lot, Plat
from plat.team import Conflict, Forbidden


def setup(db, case):
    member, _ = team.add_member(db, 'operator', 'engineer')
    e, token = executor.provision(db, case.cfg, 'Test laptop')
    session = str(uuid4())
    executor.poll(db, e, session)
    p = db.scalars(select(Plat)).one()
    body = {'key': str(uuid4()), 'plat': p.public_id, 'executor': e.public_id,
            'scope_hash': executor.lifecycle.digest(executor.scope(db, p)),
            'version': 0, 'status': p.status, 'action': 'start', 'note': 'Run accepted work'}
    return member, e, token, session, p, body


def test_request_ack_and_execute_preserve_runner_gates(case):
    plan(case)
    with DB.session() as db:
        member, e, token, session, p, body = setup(db, case)
        reply = executor.request(db, member, body)
        assert reply == executor.request(db, member, body)
        assert executor.authenticate(db, token).id == e.id
        assert executor.poll(db, e, session)['commands'] == [reply['id']]
        assert executor.acknowledge(db, e, reply['id'], session)['state'] == 'acknowledged'
        assert executor.acknowledge(db, e, reply['id'], session)['state'] == 'acknowledged'
        assert db.get(Plat, p.id).status == 'planned'  # ACK is not execution.
    executor.execute(case.cfg, reply['id'], session)
    with DB.session() as db:
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == 'completed' and cmd.started_at and cmd.finished_at
        assert db.scalars(select(Lot)).one().state == 'DONE'
        assert executor.room(db, db.get(Plat, p.id))['commands'][0]['acknowledged']
    with pytest.raises(Conflict, match='never replay'):
        executor.execute(case.cfg, reply['id'], session)


def test_auth_scopes_and_stale_offline_wrong_machine(case):
    plan(case)
    with DB.session() as db:
        member, e, token, session, p, body = setup(db, case)
        viewer, member_token = team.add_member(db, 'observer', 'viewer')
        with pytest.raises(Forbidden):
            executor.request(db, viewer, body)
        with pytest.raises(Forbidden):
            executor.authenticate(db, member_token)
        with pytest.raises(Forbidden):
            team.authenticate(db, token)
        with pytest.raises(Conflict, match='workflow or run state'):
            executor.request(db, member, {**body, 'version': 88})
        lot = db.scalars(select(Lot)).one()
        original = lot.config
        lot.config = {**original, 'host_id': 'another-host'}
        with pytest.raises(Conflict, match='bound'):
            executor.request(db, member, body)
        lot.config = original
        e.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(Conflict, match='offline'):
            executor.request(db, member, body)


def test_cancel_and_changed_state_cannot_execute(case):
    plan(case)
    with DB.session() as db:
        member, e, _, session, p, body = setup(db, case)
        reply = executor.request(db, member, body)
        cancel = {**body, 'key': str(uuid4()), 'action': 'cancel', 'command': reply['id']}
        assert executor.request(db, member, cancel)['state'] == 'cancelled'
        with pytest.raises(Conflict):
            executor.acknowledge(db, e, reply['id'], session)
        reply = executor.request(db, member, {**body, 'key': str(uuid4())})
        p.status = 'paused'
        assert executor.acknowledge(db, e, reply['id'], session)['state'] == 'failed'
        assert 'changed' in db.scalars(select(ExecutionCommand).where(ExecutionCommand.public_id == reply['id'])).one().outcome


def test_revoked_requester_and_expired_requests_fail_before_ack(case):
    plan(case)
    with DB.session() as db:
        member, e, _, session, p, body = setup(db, case)
        reply = executor.request(db, member, body)
        member.active = False
        assert executor.acknowledge(db, e, reply['id'], session)['state'] == 'failed'
        member.active = True
        reply = executor.request(db, member, {**body, 'key': str(uuid4())})
        cmd = db.scalars(select(ExecutionCommand).where(ExecutionCommand.public_id == reply['id'])).one()
        cmd.requested_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        assert executor.acknowledge(db, e, reply['id'], session)['state'] == 'failed'
        assert 'expired' in cmd.outcome


def test_replacement_executor_does_not_replay_ambiguous_ack(case):
    plan(case)
    with DB.session() as db:
        member, e, _, session, p, body = setup(db, case)
        reply = executor.request(db, member, body)
        executor.acknowledge(db, e, reply['id'], session)
        other = str(uuid4())
        with pytest.raises(Conflict, match='another executor'):
            executor.poll(db, e, other)
        e.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.flush()
        assert executor.room(db, p)['commands'][0]['connection_lost']
        assert executor.poll(db, e, other)['recover'] == [reply['id']]
        assert executor.acknowledge(db, e, reply['id'], other, abandon=True)['state'] == 'uncertain'
        assert executor.poll(db, e, other)['commands'] == []
        with pytest.raises(Conflict, match='outstanding'):
            executor.request(db, member, {**body, 'key': str(uuid4())})
    with pytest.raises(Conflict):
        executor.execute(case.cfg, reply['id'], session)


def test_pause_is_explicit_and_does_not_claim_agent_termination(case):
    plan(case)
    with DB.session() as db:
        member, e, _, session, p, body = setup(db, case)
        p.status = 'running'
        reply = executor.request(db, member, {**body, 'action': 'pause', 'status': 'running'})
        executor.acknowledge(db, e, reply['id'], session)
    executor.execute(case.cfg, reply['id'], session)
    with DB.session() as db:
        assert db.get(Plat, p.id).status == 'paused'
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == 'completed' and 'in-flight' in cmd.outcome
        assert not case.calls


def test_credential_rotation_requires_quiescence_and_invalidates_old_token(case):
    plan(case)
    with DB.session() as db:
        _, e, token, _, _, _ = setup(db, case)
        with pytest.raises(Conflict, match='lease'):
            executor.provision(db, case.cfg, 'Rotated', rotate=True)
        e.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        same, replacement = executor.provision(db, case.cfg, 'Rotated', rotate=True)
        assert same.id == e.id and replacement != token
        with pytest.raises(Forbidden):
            executor.authenticate(db, token)
        assert executor.authenticate(db, replacement).name == 'Rotated'


def test_http_authentication_and_real_daemon_pause(case, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path
    from urllib.error import HTTPError

    from plat.executor_cli import _post
    from plat.team_server import Handler, ThreadingHTTPServer

    plan(case)
    with DB.session() as db:
        member, member_token = team.add_member(db, 'engineer', 'engineer')
        e, machine_token = executor.provision(db, case.cfg, 'Real local worker')
        p = db.scalars(select(Plat)).one()
        scope_hash = executor.lifecycle.digest(executor.scope(db, p))
    home = Path(os.environ['PLAT_HOME'])
    home.mkdir(exist_ok=True)
    (home / 'origin_id').write_text(case.cfg.origin_id)
    token_file = home / 'executor.token'
    token_file.write_text(machine_token)
    token_file.chmod(0o600)
    # Forward the isolated schema to the child process, never the user's database.
    with DB.engine().connect() as conn:
        from sqlalchemy import text
        schema = conn.scalar(text('SELECT current_schema()'))
    url = DB.engine().url.update_query_dict({'options': '-csearch_path=' + schema}).render_as_string(hide_password=False)
    env = {**os.environ, 'PLAT_DATABASE_URL': url, 'PLAT_WORKSPACE': str(case.project)}
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_address[1]}'
    child = subprocess.Popen([sys.executable, '-m', 'plat', 'executor', 'serve', '--url', origin,
                              '--token-file', str(token_file)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        with pytest.raises(HTTPError) as denied:
            _post(origin, member_token, '/api/executor/poll', {'session': str(uuid4())})
        assert denied.value.code == 403
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with DB.session() as db:
                connected = executor.room(db, db.get(Plat, p.id))['executors'][0]['online']
            if connected:
                break
            time.sleep(.1)
        assert connected
        body = {'key': str(uuid4()), 'plat': p.public_id, 'executor': e.public_id,
                'scope_hash': scope_hash, 'action': 'pause', 'version': 0, 'status': 'planned', 'note': 'HTTP control qualification'}
        with pytest.raises(HTTPError) as denied:
            _post(origin, machine_token, '/api/execution', body)
        assert denied.value.code == 403
        reply = _post(origin, member_token, '/api/execution', body)
        assert reply == _post(origin, member_token, '/api/execution', body)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with DB.session() as db:
                command = db.scalars(select(ExecutionCommand)).one()
                state = command.state
            if state == 'completed':
                break
            time.sleep(.1)
        assert state == 'completed'
        with DB.session() as db:
            assert db.get(Plat, p.id).status == 'paused'
            room = executor.room(db, db.get(Plat, p.id))
            assert room['commands'][0]['acknowledged'] and room['commands'][0]['finished']
            assert 'token' not in json.dumps(room, default=str)
        assert not case.calls  # Pause never launches an agent.
    finally:
        child.terminate()
        child.communicate(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join()


def test_simultaneous_requests_cannot_create_two_starts(case):
    from concurrent.futures import ThreadPoolExecutor
    plan(case)
    with DB.session() as db:
        member, e, _, _, p, body = setup(db, case)
        member_id = member.id
    def submit():
        from plat.models import TeamMember
        try:
            with DB.session() as db:
                return executor.request(db, db.get(TeamMember, member_id), {**body, 'key': str(uuid4())})['state']
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: submit(), range(2))) == ['conflict', 'requested']


def test_start_failure_stays_uncertain_and_resume_uses_current_state(case, monkeypatch):
    from plat.dispatch.inline import InlineDispatcher
    plan(case)
    with DB.session() as db:
        member, e, _, session, p, body = setup(db, case)
        p.status = 'paused'
        reply = executor.request(db, member, {**body, 'action': 'resume', 'status': 'paused'})
        executor.acknowledge(db, e, reply['id'], session)
    def crash(*args, **kwargs):
        raise RuntimeError('simulated runner crash')
    monkeypatch.setattr(InlineDispatcher, 'run', crash)
    with pytest.raises(RuntimeError):
        executor.execute(case.cfg, reply['id'], session)
    with DB.session() as db:
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == 'uncertain'
        assert not cmd.finished_at


def test_cli_start_cannot_race_a_queued_browser_command(case):
    from typer.testing import CliRunner

    from plat import cli
    plan(case)
    with DB.session() as db:
        member, _, _, _, p, body = setup(db, case)
        executor.request(db, member, body)
    result = CliRunner().invoke(cli.app, ['start', p.public_id])
    assert result.exit_code != 0 and 'browser execution request outstanding' in result.output
    assert not case.calls


def test_changed_state_after_ack_is_rechecked_before_local_execution(case):
    plan(case)
    with DB.session() as db:
        member, e, _, session, p, body = setup(db, case)
        reply = executor.request(db, member, body)
        executor.acknowledge(db, e, reply['id'], session)
        p.status = 'paused'
    executor.execute(case.cfg, reply['id'], session)
    with DB.session() as db:
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == 'failed' and 'changed' in cmd.outcome
        assert db.get(Plat, p.id).status == 'paused'
    assert not case.calls


def test_legacy_budget_or_plan_revision_invalidates_acknowledged_start(case):
    plan(case)
    with DB.session() as db:
        member, e, _, session, p, body = setup(db, case)
        reply = executor.request(db, member, body)
        executor.acknowledge(db, e, reply['id'], session)
        p.budget_usd += 10  # Legacy workflow version remains zero.
    executor.execute(case.cfg, reply['id'], session)
    with DB.session() as db:
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == 'failed' and 'plan or budget changed' in cmd.outcome
        assert db.get(Plat, p.id).status == 'planned'
    assert not case.calls


def test_executor_revocation_between_ack_and_start_prevents_launch(case):
    plan(case)
    with DB.session() as db:
        member, e, _, session, _, body = setup(db, case)
        reply = executor.request(db, member, body)
        executor.acknowledge(db, e, reply['id'], session)
        e.active = False
    executor.execute(case.cfg, reply['id'], session)
    with DB.session() as db:
        cmd = db.scalars(select(ExecutionCommand)).one()
        assert cmd.state == 'failed' and 'revoked' in cmd.outcome
    assert not case.calls


def test_result_evidence_holds_parent_lock_before_foreign_key_writes(case, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from test_m0 import start

    from plat import ingest
    plan(case)
    original = ingest.evidence
    observed = []
    def evidence(db, **kwargs):
        with DB.engine().connect() as other:
            with pytest.raises(OperationalError) as locked:
                other.execute(text('SELECT id FROM plats WHERE id=:id FOR UPDATE NOWAIT'),
                              {'id': kwargs['plat'].id})
            assert locked.value.orig.sqlstate == '55P03'
        observed.append(kwargs['attempt'].phase)
        return original(db, **kwargs)
    monkeypatch.setattr(ingest, 'evidence', evidence)
    start()
    assert observed == ['code', 'review']


def test_pause_preview_is_not_invalidated_by_agent_progress(case):
    plan(case)
    with DB.session() as db:
        member, e, _, session, p, body = setup(db, case)
        p.status = 'running'
        reply = executor.request(db, member, {**body, 'action': 'pause', 'status': 'running'})
        lot = db.scalars(select(Lot)).one()
        lot.config = {**lot.config, 'observed_revision': {'commit': 'updated'}, 'observed_at': 'now'}
        assert executor.acknowledge(db, e, reply['id'], session)['state'] == 'acknowledged'
