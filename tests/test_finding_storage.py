# ruff: noqa: F811
"""Valid provider evidence must not crash ingestion because its identity is long."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select, text
from test_m0 import case, plan, project, rows, start, valid_spec  # noqa: F401

from plat import adapters
from plat import db as DB
from plat.models import Attempt, Finding, Lot


@pytest.mark.parametrize('phase', ['review', 'docs', 'quality'])
@pytest.mark.parametrize('length', [107, 16000])
def test_long_fingerprint_survives_phase_ingestion(case, monkeypatch, phase, length):
    # Non-repeating content also catches the btree index-entry limit after widening.
    fingerprint = ''.join(hashlib.sha256(str(i).encode()).hexdigest()
                          for i in range(251))[:length]
    config = yaml.safe_load(case.cfg.roles_path.read_text())
    config['roles']['documenter'] = {'provider': 'claude', 'model': 'default'}
    config['roles']['reviewer.quality'] = {'provider': 'codex', 'model': 'default'}
    case.cfg.roles_path.write_text(yaml.safe_dump(config))
    mapping = {'reviewer.correctness': 'review', 'documenter': 'docs', 'reviewer.quality': 'quality'}

    emitted = False

    def fake(role, prompt_file, cwd, **kwargs):
        nonlocal emitted
        from plat.adapters.base import AgentResult
        current = mapping.get(role.name)
        if current is None:
            return case.fake(role, prompt_file, cwd, **kwargs)
        findings = [] if current != phase or emitted else [{'id': 'F1', 'fingerprint': fingerprint,
            'severity': 'low', 'file': 'app.txt', 'claim': 'Optional documentation improvement',
            'required_fix': 'Explain the change'}]
        emitted = emitted or bool(findings)
        (prompt_file.parent / f'{current}.json').write_text(json.dumps({
            'verdict': 'changes_requested' if findings else 'pass', 'findings': findings, 'criteria_check': [{'id': 'AC1', 'met': True}]}))
        (prompt_file.parent / f'{current}.md').write_text('Observed evidence')
        return AgentResult(True, 0, .1, provider=role.provider)

    monkeypatch.setattr(adapters, 'run', fake)
    plan(case, valid_spec(phases=['code', 'review', 'docs', 'quality']))
    result = start()
    assert rows(Lot)[0].state == 'DONE', result.output
    assert all(a.finished_at for a in rows(Attempt))
    assert [f.fingerprint for f in rows(Finding)] == [fingerprint]
    # The full identity remains queryable; the index hash is not its replacement.
    with DB.session() as db:
        assert db.scalar(select(Finding).where(Finding.fingerprint == fingerprint)) is not None


def test_upgrade_preserves_existing_fingerprint(case):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.orm import Session

    from plat.models import Plat
    with DB.engine().connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text('CREATE SCHEMA fingerprint_upgrade'))
            connection.execute(text('SET LOCAL search_path TO fingerprint_upgrade'))
            config = Config()
            config.set_main_option('script_location', str(Path(DB.__file__).parent / 'migrations'))
            config.attributes['connection'] = connection
            command.upgrade(config, '0005')
            with Session(connection) as db:
                p = Plat(anchor='old', origin_id='test'); db.add(p); db.flush()
                lot = Lot(plat_id=p.id, key='a', repo='a', worktree_path='/tmp/test'); db.add(lot); db.flush()
                attempt = Attempt(lot_id=lot.id, n=1, phase='review', provider='codex', model='test'); db.add(attempt); db.flush()
                db.add(Finding(attempt_id=attempt.id, origin_id='test', anchor='old', repo='a',
                               fingerprint='original:identity', severity='low', file='a', claim='keep'))
                db.flush()
            command.upgrade(config, 'head')
            assert connection.scalar(text('SELECT fingerprint FROM findings')) == 'original:identity'
            assert connection.scalar(text('SELECT version_num FROM alembic_version')) == '0007'
            connection.execute(text("UPDATE findings SET fingerprint = :value"), {'value': 'long:' + 'x' * 107})
            command.upgrade(config, 'head')
            assert connection.scalar(text('SELECT fingerprint FROM findings')) == 'long:' + 'x' * 107
        finally:
            transaction.rollback()
