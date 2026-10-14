import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import typer
from sqlalchemy import select

from . import db as DB
from . import team
from .config import home
from .models import TeamMember
from .workflow_cli import output

app = typer.Typer(pretty_exceptions_show_locals=False,
                  help='Authenticated team board, assignments, reviews, and questions.')


@app.command('member-add')
def member_add(name: str, role: str = typer.Option('engineer'), token_file: Path = typer.Option(...)):
    """Database administrator: create a member and write a private bearer token."""
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with DB.session() as db:
            member, token = team.add_member(db, name, role)
            with os.fdopen(fd, 'w') as stream:
                stream.write(token + '\n')
            output({'member': member.name, 'role': member.role, 'token_file': str(token_file)})
    except BaseException:
        token_file.unlink(missing_ok=True)
        raise


@app.command('member-revoke')
def member_revoke(name: str):
    """Database administrator: revoke a member's credential."""
    with DB.session() as db:
        member = db.scalars(select(TeamMember).where(TeamMember.name == name)).one()
        from .decisions import record
        from .ownership import actor
        record(db, plat_id=None, actor='human', actor_detail=actor(), kind='membership',
               decision=f'member revoked: {name}', inputs={'name': name},
               apply=lambda: setattr(member, 'active', False))


@app.command()
def serve(host: str = '127.0.0.1', port: int = 7318,
          behind_tls: bool = typer.Option(False, '--behind-tls')):
    """Serve the board. Non-loopback binds require a TLS reverse proxy."""
    if host not in ('127.0.0.1', 'localhost', '::1') and not behind_tls:
        raise typer.BadParameter('use a TLS reverse proxy and --behind-tls for a remote bind')
    from .team_server import serve
    typer.echo(f'Plat team board: http://{host}:{port}')
    serve(host, port)


def call(path, token_file, url, data=None):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise typer.BadParameter('service URL must not contain credentials, query or fragment')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise typer.BadParameter('remote service URLs require HTTPS')
    token = token_file.read_text().strip() if token_file else ''
    req = Request(url.rstrip('/') + '/api/' + path,
                  data=json.dumps(data).encode() if data is not None else None,
                  headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise HTTPError(req.full_url, code, 'redirect refused: credentials stay on the configured service', headers, fp)
    with build_opener(NoRedirect()).open(req, timeout=15) as response:
        return json.load(response)


@app.command('invite')
def invite(name: str, invitation_file: Path = typer.Option(...),
           token_file: Path = typer.Option(..., exists=True), role: str = 'engineer',
           hours: int = 24, url: str = 'http://127.0.0.1:7318'):
    """Administrator: issue a named, expiring invitation into a private file."""
    # Refuse to replace an existing file before making the remote request.
    with os.fdopen(os.open(invitation_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        result = call('invitations', token_file, url, {'name': name, 'role': role, 'hours': hours})
        json.dump(result, stream, default=str)
    output({k: v for k, v in result.items() if k != 'code'})
    typer.echo(f'Private invitation saved to {invitation_file}')


@app.command('invite-revoke')
def invite_revoke(invitation_id: str, token_file: Path = typer.Option(..., exists=True),
                  url: str = 'http://127.0.0.1:7318'):
    """Administrator: invalidate an invitation; joined members are revoked separately."""
    output(call('invitations/revoke', token_file, url, {'id': invitation_id}))


@app.command('join')
def join(invitation_file: Path = typer.Option(..., exists=True), token_file: Path = typer.Option(...),
         retry: bool = typer.Option(False, '--retry'), url: str = 'http://127.0.0.1:7318'):
    """Redeem an invitation. Keep the token file to retry a lost response."""
    invitation = json.loads(invitation_file.read_text())
    if retry:
        if token_file.stat().st_mode & 0o077:
            raise typer.BadParameter('token file must be private (chmod 600)')
        token = token_file.read_text().strip()
    else:
        token = secrets.token_urlsafe(32)
        with os.fdopen(os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            stream.write(token + '\n')
    # Persist before sending, so an unknown response cannot strand a redeemed invite.
    result = call('join', None, url, {'code': invitation['code'], 'token': token})
    output({**result, 'token_file': str(token_file)})


@app.command()
def board(token_file: Path = typer.Option(..., exists=True), url: str = 'http://127.0.0.1:7318'):
    cache = home() / ('board-' + hashlib.sha256((url + token_file.read_text()).encode()).hexdigest()[:20] + '.json')
    try:
        result = call('board', token_file, url)
        fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(result, stream)
        output({'cached': False, **result})
    except HTTPError as exc:
        output({'error': f'service returned HTTP {exc.code}', 'cached': False})
        raise typer.Exit(1) from exc
    except URLError:
        if cache.exists():
            output({'cached': True, 'warning': 'disconnected; previous observation only', **json.loads(cache.read_text())})
        raise typer.Exit(1)


@app.command('command')
def command_file(file: Path = typer.Argument(..., exists=True), token_file: Path = typer.Option(..., exists=True),
                 url: str = 'http://127.0.0.1:7318'):
    """Send a JSON command. Reuse its key when retrying an unknown outcome."""
    output(call('commands', token_file, url, json.loads(file.read_text())))


@app.command()
def events(after: int = 0, token_file: Path = typer.Option(..., exists=True), url: str = 'http://127.0.0.1:7318'):
    output(call(f'events?after={after}', token_file, url))
