# ruff: noqa: F811
"""Invitation access, replay, races and real HTTP/CLI enrollment."""
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest
from sqlalchemy import func, select
from test_m0 import case, project  # noqa: F401
from typer.testing import CliRunner

from plat import db as DB
from plat import team
from plat.models import Decision, TeamInvitation, TeamMember
from plat.team_cli import app, call
from plat.team_server import Handler, ThreadingHTTPServer


def setup_invite():
    with DB.session() as db:
        admin, token = team.add_member(db, 'admin', 'admin')
        result = team.invite(db, admin, 'second-laptop')
    return result, token


def test_invitation_replay_revocation_and_no_secret_audit(case):
    invitation, _ = setup_invite()
    token = secrets.token_urlsafe(32)
    with DB.session() as db:
        joined = team.join(db, invitation['code'], token)
        assert joined == {'member': 'second-laptop', 'role': 'engineer'}
        assert team.authenticate(db, token).name == 'second-laptop'
        assert team.join(db, invitation['code'], token) == joined
        with pytest.raises(team.Forbidden):
            team.join(db, invitation['code'], secrets.token_urlsafe(32))
        assert db.scalar(select(func.count()).select_from(TeamMember)) == 2
        audits = str([(d.inputs, d.decision) for d in db.scalars(select(Decision))])
        assert token not in audits and invitation['code'] not in audits
        member = team.authenticate(db, token)
        member.active = False
        with pytest.raises(team.Forbidden):
            team.join(db, invitation['code'], token)


def test_expired_revoked_unknown_invites_and_privilege_escalation(case):
    invitation, _ = setup_invite()
    token = secrets.token_urlsafe(32)
    with DB.session() as db:
        admin = db.scalars(select(TeamMember).where(TeamMember.name == 'admin')).one()
        viewer, _ = team.add_member(db, 'viewer', 'viewer')
        with pytest.raises(team.Forbidden):
            team.invite(db, viewer, 'outsider')
        with pytest.raises(team.Forbidden):
            team.revoke_invitation(db, viewer, invitation['id'])
        with pytest.raises(ValueError):
            team.invite(db, admin, 'new-admin', role='admin')
        with pytest.raises(ValueError):
            team.invite(db, admin, 'forever', hours=169)
        with pytest.raises(team.Forbidden):
            team.join(db, secrets.token_urlsafe(32), token)
        row = db.scalars(select(TeamInvitation)).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(team.Forbidden):
            team.join(db, invitation['code'], token)
        row.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        team.revoke_invitation(db, admin, invitation['id'])
        with pytest.raises(team.Forbidden):
            team.join(db, invitation['code'], token)
        assert db.scalar(select(func.count()).select_from(TeamMember)) == 2


def test_concurrent_redemption_creates_exactly_one_member(case):
    invitation, _ = setup_invite()
    barrier = threading.Barrier(2)
    def redeem(_):
        with DB.session() as db:
            barrier.wait()
            try:
                team.join(db, invitation['code'], secrets.token_urlsafe(32))
                return True
            except team.Forbidden:
                return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(redeem, range(2))) == [False, True]
    with DB.session() as db:
        assert db.scalar(select(func.count()).select_from(TeamMember)) == 2


def test_http_cli_join_without_sql_and_retry(case, tmp_path):
    _, admin_token = setup_invite()
    admin_file = tmp_path / 'admin.token'; admin_file.write_text(admin_token)
    invite_file, token_file = tmp_path / 'invitation.json', tmp_path / 'member.token'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    cli = CliRunner()
    try:
        with pytest.raises(HTTPError) as error:
            call('invitations', None, url, {'name': 'outsider'})
        assert error.value.code == 403
        with pytest.raises(HTTPError) as error:
            call('join', None, url, {'name': 'outsider', 'role': 'admin'})
        assert error.value.code == 403
        result = cli.invoke(app, ['invite', 'laptop-three', '--token-file', str(admin_file),
            '--invitation-file', str(invite_file), '--role', 'viewer', '--url', url])
        assert result.exit_code == 0, result.output
        invitation = json.loads(invite_file.read_text())
        assert invitation['code'] not in result.output
        args = ['join', '--invitation-file', str(invite_file), '--token-file', str(token_file), '--url', url]
        result = cli.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert token_file.read_text().strip() not in result.output
        assert token_file.stat().st_mode & 0o077 == 0
        assert invite_file.stat().st_mode & 0o077 == 0
        assert cli.invoke(app, args + ['--retry']).exit_code == 0
        assert call('board', token_file, url)['member'] == {'name': 'laptop-three', 'role': 'viewer'}
        with pytest.raises(HTTPError) as error:
            call('invitations', token_file, url, {'name': 'outsider'})
        assert error.value.code == 403
        result = cli.invoke(app, ['invite-revoke', invitation['id'], '--token-file', str(admin_file), '--url', url])
        assert result.exit_code == 0, result.output
        assert cli.invoke(app, args + ['--retry']).exit_code != 0
        # Revoking an invite does not silently revoke its previously admitted member.
        assert call('board', token_file, url)['member']['name'] == 'laptop-three'
    finally:
        server.shutdown(); server.server_close(); thread.join()
