# ruff: noqa: F811
"""Regressions for release review: authority cannot race observation or cancellation."""
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from test_m0 import case, plan, project, rows, valid_spec  # noqa: F401
from typer.testing import CliRunner

from plat import cli, gates, lifecycle, ownership, runner, sessions, workflow_cli
from plat import db as DB
from plat.models import Criterion, ExecutionClaim, Lot, Plat


def objects(db):
    return db.scalars(select(Plat)).one(), db.scalars(select(Lot).order_by(Lot.id)).first()


def test_status_before_plan_explains_missing_evidence(case, monkeypatch):
    monkeypatch.setattr(workflow_cli, 'load', lambda: case.cfg)
    cli_runner = CliRunner()
    assert cli_runner.invoke(cli.app, ['workflow', 'begin', 'fix_m0-test', '--owner', 'owner']).exit_code == 0
    result = cli_runner.invoke(cli.app, ['workflow', 'status', 'fix_m0-test'])
    assert result.exit_code == 0, result.output
    assert 'committed specification is missing' in result.output


def test_interactive_attach_blocks_concurrent_automated_claim(case, monkeypatch):
    plan(case)
    observed, finish = threading.Event(), threading.Event()
    original = sessions.record
    def hold_before_record(*args, **kwargs):
        observed.set()
        assert finish.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(sessions, 'record', hold_before_record)
    def attach():
        with DB.session() as db:
            p, lot = objects(db)
            sessions.attach(db, case.cfg, p, lot, 'codex', 'interactive')
    with ThreadPoolExecutor(1) as pool:
        job = pool.submit(attach)
        try:
            assert observed.wait(5)
            with pytest.raises(OperationalError):
                with DB.session() as db:
                    db.execute(text("SET LOCAL lock_timeout='300ms'"))
                    p, lot = objects(db)
                    ownership.acquire(db, case.cfg, p, lot)
        finally:
            finish.set()
        job.result(timeout=5)
    with DB.session() as db:
        p, lot = objects(db)
        with pytest.raises(ownership.OwnershipError, match='interactive'):
            ownership.acquire(db, case.cfg, p, lot)


def test_interactive_writer_protects_same_path_in_another_lot(case):
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        sessions.attach(db, case.cfg, p, lot, 'claude', 'writer')
        other = Lot(plat_id=p.id, key='review', repo=lot.repo, worktree_path=lot.worktree_path,
                    state='PENDING', config=dict(lot.config))
        db.add(other)
    with DB.session() as db:
        p, _ = objects(db)
        other = db.scalars(select(Lot).where(Lot.key == 'review')).one()
        with pytest.raises(ownership.OwnershipError, match='interactive'):
            ownership.acquire(db, case.cfg, p, other)
        with pytest.raises(ValueError, match='another interactive'):
            sessions.attach(db, case.cfg, p, other, 'codex', 'second-writer')


def test_attach_rejects_copied_installation_identity_on_another_host(case):
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        lot.config = {**lot.config, 'host_id': 'another-host'}
    with DB.session() as db:
        p, lot = objects(db)
        with pytest.raises(ValueError, match='owning host'):
            sessions.attach(db, case.cfg, p, lot, 'codex', 'session')


def test_expired_lease_cannot_be_revived_by_a_delayed_heartbeat(case):
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        info = ownership.acquire(db, case.cfg, p, lot)
    with DB.session() as db:
        db.get(ExecutionClaim, info[0]).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with DB.session() as db:
        with pytest.raises(ownership.OwnershipError, match='expired'):
            ownership.renew(db, info)


@pytest.mark.parametrize('state', ['paused', 'aborted'])
def test_final_run_check_cannot_overwrite_a_stop(case, monkeypatch, state):
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        lot.state = 'DONE'; p.status = 'running'
        db.add(Criterion(plat_id=p.id, ac_id='RUN', statement='run check', scope='run',
                         verify_cmd='true', verify_in=lot.key))
    def stop_during_check(cmd, cwd, **kwargs):
        with DB.session() as other:
            p, _ = objects(other)
            p.status = state
        return subprocess.CompletedProcess(cmd, 0, '', '')
    monkeypatch.setattr(gates, '_sh', stop_during_check)
    with DB.session() as db:
        p, _ = objects(db)
        assert not runner.maybe_deliver_plat(db, p, cfg=case.cfg)
    assert rows(Plat)[0].status == state
    assert rows(Plat)[0].delivered_at is None


def test_rejected_parallel_budget_start_does_not_mark_running(case):
    plan(case, valid_spec(budget_usd=10))
    result = CliRunner().invoke(cli.app, ['start', 'fix_m0-test', '--workers', '2'])
    assert result.exit_code != 0
    assert rows(Plat)[0].status == 'planned'


def test_closed_workflow_cannot_be_changed_by_stale_reader(case):
    plan(case)
    with DB.session() as db:
        p, _ = objects(db)
        lifecycle.begin(db, p, 'owner')
    with DB.session() as stale:
        p, _ = objects(stale)
        with DB.session() as other:
            closed, _ = objects(other)
            closed.closed_at = datetime.now(timezone.utc); closed.status = 'closed'
        with pytest.raises(lifecycle.WorkflowError, match='immutable'):
            lifecycle.accept(stale, p, 'context', {'summary': 'x', 'sources': [
                {'reference': 'x', 'status': 'collected', 'snapshot': 'x'}]}, 0)


def test_upgrade_pre_kind_legacy_schema_preserves_records(case):
    from alembic import command
    from alembic.config import Config
    directory = Path(DB.__file__).parent / 'migrations'
    schema = 'legacy_' + uuid.uuid4().hex
    with DB.engine().connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(f'CREATE SCHEMA {schema}'))
            conn.execute(text(f'SET LOCAL search_path TO {schema}'))
            for statement in DB._split((directory / 'baseline.sql').read_text()):
                conn.execute(text(statement))
            conn.execute(text('ALTER TABLE plats DROP COLUMN kind CASCADE'))
            conn.execute(text("INSERT INTO plats (anchor,tickets,related,title,status,budget_usd,origin_id) "
                              "VALUES ('old-run','{}','{}','Preserve me','planned',0,'legacy')"))
            config = Config(); config.set_main_option('script_location', str(directory))
            config.attributes['connection'] = conn
            command.upgrade(config, 'head')
            assert conn.scalar(text('SELECT version_num FROM alembic_version')) == '0009'
            row = conn.execute(text('SELECT title,kind,public_id FROM plats')).one()
            assert row.title == 'Preserve me' and row.kind == 'plat' and row.public_id
        finally:
            transaction.rollback()


def test_replan_refuses_an_attached_writer_before_smoke(case, monkeypatch):
    plan(case)
    with DB.session() as db:
        p, lot = objects(db)
        sessions.attach(db, case.cfg, p, lot, 'claude', 'writer')
    monkeypatch.setattr(gates, 'smoke', lambda *args, **kwargs: pytest.fail('must not run setup against an active writer'))
    result = CliRunner().invoke(cli.app, ['plan', str(case.project / 'plat.yaml'), '--commit'])
    assert result.exit_code != 0
    assert 'active sessions' in result.output
