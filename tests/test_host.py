# ruff: noqa: F811
"""Host upgrades must neither orphan local work nor authorize remote writers."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from test_m0 import case, plan, project  # noqa: F401

from plat import db as DB
from plat import host, ownership
from plat.models import Decision, ExecutionClaim, Lot, Plat


def test_machine_identity_survives_python_network_and_hostname_changes(monkeypatch):
    host.machine_id.cache_clear()
    monkeypatch.setattr(host.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(host, '_read', lambda _: '"IOPlatformUUID" = "BF892558-86B1-5D0F-B7F4-DD04E93CEE7A"')
    first = host.machine_id()
    monkeypatch.setattr(host.platform, 'node', lambda: 'renamed-laptop')
    host.machine_id.cache_clear()
    try:
        assert host.machine_id() == first
        monkeypatch.setattr(host, '_read', lambda _: '"IOPlatformUUID" = "AF892558-86B1-5D0F-B7F4-DD04E93CEE7A"')
        host.machine_id.cache_clear()
        assert host.machine_id() != first
    finally:
        host.machine_id.cache_clear()


def test_missing_os_identifier_fails_closed(monkeypatch):
    host.machine_id.cache_clear()
    monkeypatch.setattr(host.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(host, '_read', lambda _: '')
    with pytest.raises(RuntimeError, match='stable host identity'):
        host.machine_id()


def test_legacy_aliases_require_local_interface_evidence(monkeypatch):
    monkeypatch.setattr(host, 'machine_id', lambda: 'stable')
    monkeypatch.setattr(host.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(host.platform, 'node', lambda: 'laptop')
    monkeypatch.setattr(host, '_read', lambda _: 'ether 22:0e:47:c6:47:0f\nether fc:b2:14:b6:a3:26')
    expected = {hashlib.sha256(f'laptop:{n}'.encode()).hexdigest()
                for n in [37444729063183, 277841781891878]}
    assert host.local_ids() == expected | {'stable'}


def test_migration_preserves_claim_history_and_records_decision(case, monkeypatch):
    plan(case)
    monkeypatch.setattr(ownership, 'local_host_ids', lambda: {'legacy', ownership.host_id()})
    with DB.session() as db:
        p, lot = db.scalars(select(Plat)).one(), db.scalars(select(Lot)).one()
        ownership.acquire(db, case.cfg, p, lot)
        lot.config = {**lot.config, 'host_id': 'legacy'}
        claim = db.get(ExecutionClaim, lot.id)
        claim.host_id = 'legacy'
        claim.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(ownership.OwnershipError, match='reconcile active'):
            ownership.migrate_host(db, case.cfg, p, lot, 'upgrade')
        ownership.reconcile(db, case.cfg, p, lot, 'local interface verified; supervisor stopped')
        assert ownership.migrate_host(db, case.cfg, p, lot, 'stable OS identity')
        assert lot.config['host_id'] == ownership.host_id()
        assert claim.host_id == 'legacy' and not claim.active
        assert not ownership.migrate_host(db, case.cfg, p, lot, 'already upgraded')
        assert db.scalar(select(Decision.id).where(Decision.decision == 'local host identity upgraded'))


def test_remote_host_or_installation_cannot_migrate(case, monkeypatch):
    plan(case)
    monkeypatch.setattr(ownership, 'local_host_ids', lambda: {'legacy', ownership.host_id()})
    with DB.session() as db:
        p, lot = db.scalars(select(Plat)).one(), db.scalars(select(Lot)).one()
        for cfg in [{'host_id': 'remote', 'executor_id': case.cfg.origin_id},
                    {'host_id': 'legacy', 'executor_id': 'another-installation'}]:
            lot.config = cfg
            with pytest.raises(ownership.OwnershipError, match='local interface evidence'):
                ownership.migrate_host(db, case.cfg, p, lot, 'upgrade')


def test_legacy_resource_claim_blocks_new_lot_on_same_path(case, monkeypatch):
    plan(case)
    monkeypatch.setattr(ownership, 'local_host_ids', lambda: {'legacy', ownership.host_id()})
    with DB.session() as db:
        p, lot = db.scalars(select(Plat)).one(), db.scalars(select(Lot)).one()
        ownership.acquire(db, case.cfg, p, lot)
        claim = db.get(ExecutionClaim, lot.id)
        claim.host_id = 'legacy'
        claim.resource = f'legacy:{lot.worktree_path}'
        other = Lot(plat_id=p.id, key='other', repo=lot.repo, worktree_path=lot.worktree_path,
                    base_ref=lot.base_ref, config=lot.config)
        db.add(other)
        db.flush()
        with pytest.raises(ownership.OwnershipError, match='already claimed'):
            ownership.acquire(db, case.cfg, p, other)


def test_legacy_reconcile_rejects_live_lease_and_live_agent(case, monkeypatch):
    from plat.adapters import base
    from plat.models import Attempt
    plan(case)
    monkeypatch.setattr(ownership, 'local_host_ids', lambda: {'legacy', ownership.host_id()})
    with DB.session() as db:
        p, lot = db.scalars(select(Plat)).one(), db.scalars(select(Lot)).one()
        ownership.acquire(db, case.cfg, p, lot)
        claim = db.get(ExecutionClaim, lot.id)
        claim.host_id = 'legacy'
        with pytest.raises(ownership.OwnershipError, match='still live'):
            ownership.reconcile(db, case.cfg, p, lot, 'upgrade')
        claim.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.add(Attempt(lot_id=lot.id, n=1, phase='docs', provider='claude', model='test',
                       host_id='legacy', executor_id=case.cfg.origin_id, pid=12345))
        db.flush()
        monkeypatch.setattr(base, 'process_alive', lambda *args, **kwargs: True)
        with pytest.raises(ownership.OwnershipError, match='agent is still alive'):
            ownership.reconcile(db, case.cfg, p, lot, 'upgrade')
        assert claim.active


def test_interactive_legacy_session_must_be_finished_before_migration(case, monkeypatch):
    from plat import sessions
    from plat.models import TrackedSession
    plan(case)
    def aliases():
        return {'legacy', ownership.host_id()}
    monkeypatch.setattr(ownership, 'local_host_ids', aliases)
    monkeypatch.setattr(sessions, 'local_host_ids', aliases)
    with DB.session() as db:
        p, lot = db.scalars(select(Plat)).one(), db.scalars(select(Lot)).one()
        lot.config = {**lot.config, 'host_id': 'legacy'}
        db.add(TrackedSession(plat_id=p.id, lot_id=lot.id, installation=case.cfg.origin_id,
            host_id='legacy', owner=ownership.actor(), provider='claude', session_key='legacy-session',
            source='interactive', state='active'))
        db.flush()
        with pytest.raises(ownership.OwnershipError, match='interactive sessions'):
            ownership.migrate_host(db, case.cfg, p, lot, 'upgrade')
        sessions.attach(db, case.cfg, p, lot, 'claude', 'legacy-session', state='completed')
        assert ownership.migrate_host(db, case.cfg, p, lot, 'upgrade')
