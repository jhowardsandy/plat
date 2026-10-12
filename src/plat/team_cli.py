import hashlib
import json
import os
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

app = typer.Typer(help='Authenticated team board, assignments, reviews, and questions.')


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
    token = token_file.read_text().strip()
    req = Request(url.rstrip('/') + '/api/' + path,
                  data=json.dumps(data).encode() if data is not None else None,
                  headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise HTTPError(req.full_url, code, 'redirect refused: credentials stay on the configured service', headers, fp)
    with build_opener(NoRedirect()).open(req, timeout=15) as response:
        return json.load(response)


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
