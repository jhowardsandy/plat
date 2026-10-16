"""Provision locally, then receive authenticated controls from the team service."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

import typer
from sqlalchemy import select

from . import db as DB
from . import executor
from .config import load
from .models import Attempt, ExecutionClaim, ExecutionCommand, Executor, Lot, Plat
from .ownership import actor, host_id

app = typer.Typer(help='Register an owning machine and receive authenticated browser controls.')


@app.command()
def register(name: str, token_file: Path = typer.Option(...), rotate: bool = False):
    """Trusted database administration: create this machine's separate credential."""
    token_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with DB.session() as db:
            row, token = executor.provision(db, load(), name, rotate=rotate)
            with os.fdopen(fd, 'w') as out:
                out.write(token + '\n')
        typer.echo(f'Registered {row.name}; credential saved to {token_file}')
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        token_file.unlink(missing_ok=True)
        raise


@app.command()
def revoke():
    """Revoke this machine credential; existing agents are not terminated."""
    from .decisions import record
    cfg = load()
    with DB.session() as db:
        row = db.scalars(select(Executor).where(Executor.installation == cfg.origin_id,
                                               Executor.host_id == host_id()).with_for_update()).one()
        record(db, plat_id=None, actor='human', actor_detail=actor(), kind='executor',
               decision='executor credential revoked', inputs={'executor': row.public_id},
               apply=lambda: setattr(row, 'active', False))
    typer.echo('Credential revoked. In-flight agents may still be running; inspect locally.')


@app.command()
def reconcile(command: str, note: str = typer.Option(...)):
    """Resolve an uncertain command only after its worker and lot claims are gone."""
    from .adapters.base import process_alive
    cfg = load()
    with DB.session() as db:
        cmd = db.scalars(select(ExecutionCommand).where(ExecutionCommand.public_id == command)).one()
        db.execute(select(Plat.id).where(Plat.id == cmd.plat_id).with_for_update())
        db.refresh(cmd, with_for_update=True)
        e = db.get(Executor, cmd.executor_id)
        if e.installation != cfg.origin_id or e.host_id != host_id() or not note.strip():
            raise ValueError('reconcile on the owning installation with a reason')
        if cmd.state not in ('uncertain', 'acknowledged', 'running'):
            raise ValueError('command does not need reconciliation')
        if e.lease_until and e.lease_until > db.scalar(executor.func.clock_timestamp()):
            raise ValueError('stop the executor and wait for its lease to expire first')
        if cmd.pid and process_alive(cmd.pid, since=cmd.started_at):
            raise ValueError('command worker is still alive')
        if db.scalar(select(ExecutionClaim.lot_id).join(Lot).where(
                Lot.plat_id == cmd.plat_id, ExecutionClaim.active.is_(True)).limit(1)) or db.scalar(
                select(Attempt.id).join(Lot).where(Lot.plat_id == cmd.plat_id, Attempt.finished_at.is_(None)).limit(1)):
            raise ValueError('reconcile the lot claims and unfinished attempts first')
        executor._change(db, cmd, 'failed', f'Locally reconciled by {actor()}: {note}')


@app.command(hidden=True)
def execute(command: str, session: str):
    executor.execute(load(), command, session)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, 'executor redirects are refused', headers, fp)


def _post(url, token, path, body):
    request = Request(url.rstrip('/') + path, data=json.dumps(body).encode(), method='POST',
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with build_opener(NoRedirect).open(request, timeout=5) as response:
        return json.load(response)


@app.command()
def serve(url: str = 'http://127.0.0.1:7318', token_file: Path = typer.Option(..., exists=True)):
    """Stay connected; fixed command workers run locally with existing runner gates."""
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise typer.BadParameter('use a service origin without credentials, path, or query')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise typer.BadParameter('remote executors require HTTPS')
    if token_file.stat().st_mode & 0o077:
        raise typer.BadParameter('executor token file must be private (chmod 600)')
    token = token_file.read_text().strip()
    cfg, session = load(), str(uuid4())
    launched, children = set(), []
    logdir = token_file.parent / 'logs'
    logdir.mkdir(mode=0o700, exist_ok=True)
    typer.echo('Executor connected loop starting; Ctrl-C disconnects without killing in-flight agents.')
    while True:
        try:
            reply = _post(url, token, '/api/executor/poll', {'session': session})
            for command in reply.get('recover', []):
                _post(url, token, '/api/executor/abandon', {'session': session, 'command': command})
            if reply.get('recover'):
                continue
            if reply.get('installation') != cfg.origin_id or reply.get('host') != host_id():
                raise ValueError('credential targets another installation or host')
            for command in reply['commands']:
                if command in launched:
                    continue
                ack = _post(url, token, '/api/executor/ack', {'session': session, 'command': command})
                if ack['state'] != 'acknowledged':
                    continue
                # A duplicate or delayed spawn cannot rerun a command: execute()
                # atomically consumes acknowledged -> running under the plat lock.
                logpath = logdir / (str(uuid4()) + '.log')
                fd = os.open(logpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'ab') as log:
                    child = subprocess.Popen([sys.executable, '-m', 'plat', 'executor', 'execute', command, session],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                launched.add(command)
                children.append((child, command))
            pending = []
            for child, command in children:
                if child.poll() is None:
                    pending.append((child, command))
                elif child.returncode:
                    _post(url, token, '/api/executor/abandon', {'session': session, 'command': command})
            children = pending
        except HTTPError as exc:
            if exc.code in (401, 403, 409):
                raise typer.BadParameter('executor credential rejected or another process holds its lease') from None
            typer.echo('Service unavailable; no execution request was retried.')
        except OSError:
            typer.echo('Connection or local spawn failed; reconnecting without replaying execution.')
        time.sleep(5)
