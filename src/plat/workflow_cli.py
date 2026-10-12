"""CLI surfaces for lifecycle, session tracking and recovery."""
import json
from pathlib import Path

import typer
import yaml
from sqlalchemy import select

from . import db as DB
from . import lifecycle as L
from .config import load
from .models import Lot, TrackedSession, Workflow

app = typer.Typer(help='Context, scope, integration, test deployment, and readiness.')
session_app = typer.Typer(help='Track Claude and Codex sessions with explicit provenance.')


def resolve(db, anchor):
    from .cli import _resolve
    return _resolve(db, anchor)


def output(value):
    typer.echo(json.dumps(value, indent=2, default=str))


def input_file(path):
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise typer.BadParameter('file must contain an object')
    return data


@app.command()
def begin(anchor: str, owner: str = typer.Option(...), title: str = ""):
    from .models import Plat
    from .spec import NAME
    if not NAME.fullmatch(anchor) or len(anchor) > 160:
        raise typer.BadParameter("use a safe ticket or run anchor")
    with DB.session() as db:
        cfg = load()
        p = db.scalars(select(Plat).where(Plat.anchor == anchor, Plat.origin_id == cfg.origin_id,
                                         Plat.kind == "plat")).first()
        if not p:
            p = Plat(anchor=anchor, title=title, origin_id=cfg.origin_id, status="planned")
            db.add(p); db.flush()
        w = L.begin(db, p, owner)
        output({'stage': w.stage, 'version': w.version})


@app.command('accept')
def accept_stage(anchor: str, stage: str, file: Path = typer.Option(..., exists=True),
                 version: int = typer.Option(..., help='Expected workflow version from status')):
    with DB.session() as db:
        row = L.accept(db, resolve(db, anchor), stage, input_file(file), version)
        output({'stage': row.stage, 'version': row.version, 'sha256': row.sha256})


@app.command()
def status(anchor: str):
    with DB.session() as db:
        p = resolve(db, anchor)
        w = db.get(Workflow, p.id)
        output({'stage': w.stage if w else 'legacy', 'version': w.version if w else None,
                'readiness': L.readiness(db, p)})


@app.command()
def integrate(anchor: str, file: Path = typer.Option(..., exists=True), version: int = typer.Option(...)):
    with DB.session() as db:
        row = L.integration(db, load(), resolve(db, anchor), input_file(file), version)
        output(row.content)
        failed = not row.content['passed']
    if failed:
        raise typer.Exit(1)


@app.command('deploy')
def deployment(anchor: str, file: Path = typer.Option(..., exists=True), version: int = typer.Option(...),
               execute: bool = typer.Option(False, '--execute', help='Authorize the declared deployment and rollback commands')):
    if not execute:
        output({'action': 'preview', 'commands': input_file(file), 'hint': 'pass --execute to authorize'})
        return
    with DB.session() as db:
        row = L.deploy(db, load(), resolve(db, anchor), input_file(file), version)
        output(row.content)
        failed = not row.content['passed']
    if failed:
        raise typer.Exit(1)


@app.command()
def abstract(anchor: str, out: Path = typer.Option(None)):
    with DB.session() as db:
        data = L.abstract(db, resolve(db, anchor))
    if out:
        out.write_text(json.dumps(data, indent=2, default=str) + '\n')
    else:
        output(data)


@app.command()
def reconcile(anchor: str, lot: str, note: str = typer.Option(...)):
    from .ownership import reconcile
    with DB.session() as db:
        p = resolve(db, anchor)
        row = db.scalars(select(Lot).where(Lot.plat_id == p.id, Lot.key == lot)).one()
        reconcile(db, load(), p, row, note)


@session_app.command('attach')
def attach_session(anchor: str, lot: str, provider: str, session_id: str,
                   state: str = typer.Option('active'), note: str = typer.Option('')):
    from .sessions import attach
    with DB.session() as db:
        p = resolve(db, anchor)
        row = db.scalars(select(Lot).where(Lot.plat_id == p.id, Lot.key == lot)).one()
        tracked = attach(db, load(), p, row, provider, session_id, state=state, note=note)
        output({'id': tracked.public_id, 'state': tracked.state, 'source': tracked.source})


@session_app.command('list')
def list_sessions(anchor: str):
    with DB.session() as db:
        p = resolve(db, anchor)
        rows = db.scalars(select(TrackedSession).where(TrackedSession.plat_id == p.id)).all()
        output([{'id': r.public_id, 'provider': r.provider, 'provider_session_id': r.provider_session_id,
                 'state': r.state, 'owner': r.owner, 'source': r.source, 'last_activity': r.last_activity,
                 'host': r.host_id, 'capabilities': r.capabilities} for r in rows])


@app.command('recover-stage')
def recover_stage(anchor: str, note: str = typer.Option(...), version: int = typer.Option(...)):
    with DB.session() as db:
        result = L.recover(db, resolve(db, anchor), note, version)
        output({"stage": result.stage, "version": result.version})


@app.command()
def localize(anchor: str, lot: str, repo: str, note: str = typer.Option(...)):
    """Bind a never-started lot to this installation's checkout of the same repository."""
    from . import worktree
    from .decisions import record
    from .models import Attempt, ExecutionClaim
    from .ownership import actor, host_id
    cfg = load()
    source = (cfg.workspace_root / repo).resolve()
    if not source.is_relative_to(cfg.workspace_root.resolve()) or not (source / ".git").exists():
        raise typer.BadParameter("repo must name a git checkout inside the configured workspace")
    with DB.session() as db:
        p = resolve(db, anchor)
        row = db.scalars(select(Lot).where(Lot.plat_id == p.id, Lot.key == lot).with_for_update()).one()
        if not note.strip() or row.state != "PENDING" or db.scalar(select(Attempt.id).where(Attempt.lot_id == row.id).limit(1)):
            raise L.WorkflowError("only never-started pending lots can be localized; provide a handoff note")
        claim = db.get(ExecutionClaim, row.id)
        if claim and claim.active:
            raise L.WorkflowError("reconcile active execution before changing its installation")
        if (row.config or {}).get("repository_identity") != L.canonical_repo(source):
            raise L.WorkflowError("local checkout has a different canonical origin")
        wt, branch, _ = worktree.ensure(cfg, p.anchor, row.key, repo, p.anchor.lower(), row.base_ref)
        def apply():
            row.worktree_path = str(wt)
            row.config = {**(row.config or {}), "executor_id": cfg.origin_id, "host_id": host_id(), "branch": branch}
        record(db, plat_id=p.id, lot_id=row.id, actor="human", actor_detail=actor(), kind="assignment",
               decision="pending lot localized", rationale=note, inputs={"installation": cfg.origin_id}, apply=apply)
