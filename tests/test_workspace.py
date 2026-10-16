# ruff: noqa: F811
"""Workspace organization is shared, versioned and scoped to authenticated members."""
import uuid

import pytest
from sqlalchemy import select
from test_m0 import case, plan, project, rows  # noqa: F401

from plat import db as DB
from plat import team, workspace
from plat.models import Artifact, Decision, Lot, Plat, RepositoryCatalog, SavedView


def request(**kwargs):
    return {'key': str(uuid.uuid4()), **kwargs}


def test_catalog_groups_canonical_repositories_and_conflicts(case):
    plan(case)
    with DB.session() as db:
        member, _ = team.add_member(db, 'editor', 'engineer')
        p, lot = db.scalars(select(Plat)).one(), db.scalars(select(Lot)).one()
        identity = workspace.repository_key(lot, p)
        cmd = request(action='classify', repository=identity, version=0,
                      products=['Recording', 'Recording'], services=['Queue', 'Telemetry'])
        assert workspace.command(db, member, cmd) == {'version': 1}
        assert workspace.command(db, member, cmd) == {'version': 1}
        result = team.board(db)['plats'][0]['lots'][0]
        assert result['classification'] == {'products': ['Recording'], 'services': ['Queue', 'Telemetry'], 'version': 1}
        assert result['repository'] == identity
        with pytest.raises(team.Conflict):
            workspace.command(db, member, request(**{k: v for k, v in cmd.items() if k != 'key'}))
        assert len(db.scalars(select(RepositoryCatalog)).all()) == 1
        assert db.scalars(select(Decision).where(Decision.kind == 'organization')).one().actor_detail == 'editor'


def test_saved_views_private_shared_permissions_and_replay(case):
    with DB.session() as db:
        alice, _ = team.add_member(db, 'alice', 'engineer')
        bob, _ = team.add_member(db, 'bob', 'engineer')
        viewer, _ = team.add_member(db, 'viewer', 'viewer')
        settings = {'view': 'engineer', 'sort': 'attention', 'product': '', 'query': '', 'closed': False}
        cmd = request(action='save_view', name='My reviews', settings=settings, shared=False)
        first = workspace.command(db, alice, cmd)
        assert workspace.command(db, alice, cmd) == first
        assert workspace.preferences(db, bob)['views'] == []
        with pytest.raises(team.Forbidden):
            workspace.command(db, bob, request(action='delete_view', id=first['id']))
        workspace.command(db, alice, request(action='save_view', name='Team', settings=settings, shared=True))
        assert [v['name'] for v in workspace.preferences(db, bob)['views']] == ['Team']
        workspace.command(db, viewer, request(action='save_view', name='Personal', settings=settings, shared=False))
        with pytest.raises(team.Forbidden):
            workspace.command(db, viewer, request(action='save_view', name='Shared', settings=settings, shared=True))
        with pytest.raises(ValueError):
            workspace.command(db, alice, request(action='save_view', name='bad', settings={**settings, 'view': []}))
        workspace.command(db, alice, request(action='delete_view', id=first['id']))
        assert db.get(SavedView, 1) is None


def test_room_exposes_manifest_not_private_bodies(case):
    plan(case)
    with DB.session() as db:
        p = db.scalars(select(Plat)).one()
        db.add(Artifact(plat_id=p.id, kind='prompt', name='prompt.md', body='PRIVATE PROMPT', sha256='a'*64))
        db.flush()
        room = workspace.room(db, p.public_id)
        assert room['criteria'][0]['id'] == 'AC1'
        assert any(a['name'] == 'prompt.md' for a in room['artifacts'])
        assert 'PRIVATE PROMPT' not in str(room)
        with pytest.raises(ValueError, match='not found'):
            workspace.room(db, str(uuid.uuid4()))


def test_multiline_assignment_and_versioned_reassignment(case):
    plan(case)
    with DB.session() as db:
        alice, _ = team.add_member(db, 'alice', 'engineer')
        bob, _ = team.add_member(db, 'bob', 'engineer')
        p = db.scalars(select(Plat)).one()
        body = 'Expected result\n\n- Check tests\n- Record evidence'
        item = team.command(db, alice, request(action='assign', plat=p.public_id, owner='alice', body={'text': body}))
        assert team.board(db)['plats'][0]['items'][0]['body']['text'] == body
        team.command(db, alice, request(action='reassign', plat=p.public_id, item=item['id'], version=1, owner='bob', note='New context\nKeep the evidence'))
        assignment = team.board(db)['plats'][0]['items'][0]
        assert assignment['owner'] == 'bob'
        assert assignment['body']['text'] == 'New context\nKeep the evidence'
        with pytest.raises(team.Conflict):
            team.command(db, bob, request(action='reassign', plat=p.public_id, item=item['id'], version=1, owner='alice', note='stale'))


def test_viewer_cannot_classify(case):
    plan(case)
    with DB.session() as db:
        viewer, _ = team.add_member(db, 'viewer', 'viewer')
        with pytest.raises(team.Forbidden):
            workspace.command(db, viewer, request(action='classify', repository='x', version=0, products=[], services=[]))


def test_workspace_signals_distinguish_blocked_pending_and_done(case):
    plan(case)
    with DB.session() as db:
        lot = db.scalars(select(Lot)).one()
        lot.state = 'BLOCKED'; db.flush()
        projected = team.board(db)['plats'][0]['lots'][0]
        assert projected['needs_attention'] is True
        assert projected['next_step'] == 'Inspect blocker and recovery decision'
        lot.state = 'DONE'; db.flush()
        assert team.board(db)['plats'][0]['lots'][0]['next_step'] == 'Inspect evidence and readiness'


def test_workspace_http_requires_authentication(case):
    import json
    import threading
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from plat.team_server import Handler, ThreadingHTTPServer
    plan(case)
    with DB.session() as db:
        _, token = team.add_member(db, 'reader', 'viewer')
        public_id = db.scalars(select(Plat)).one().public_id
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(base + '/api/room?id=' + public_id)
        assert error.value.code == 403
        with urlopen(Request(base + '/api/room?id=' + public_id, headers=headers)) as response:
            assert json.load(response)['criteria'][0]['id'] == 'AC1'
        cmd = request(action='classify', repository='nope', version=0, products=[], services=[])
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + '/api/workspace', data=json.dumps(cmd).encode(), headers=headers))
        assert error.value.code == 403
    finally:
        server.shutdown(); server.server_close(); thread.join()
