"""Read external delivery status through the owner's existing provider login."""
import json
from pathlib import Path

import typer
from sqlalchemy import select

from . import db as DB
from . import delivery
from .config import load
from .models import Lot
from .ownership import actor
from .workflow_cli import output, resolve

app = typer.Typer(help='Link PRs/MRs and observe review/CI status from the owning laptop.')


def find_lot(db, plat, key):
    row = db.scalars(select(Lot).where(Lot.plat_id == plat.id, Lot.key == key)).first()
    if not row:
        raise ValueError('lot not found')
    return row


@app.command()
def link(anchor: str, lot: str, url: str, version: int = typer.Option(...)):
    """Link a PR/MR; --version is the delivery link version (0 for a new link)."""
    with DB.session() as db:
        p = resolve(db, anchor)
        output(delivery.link(db, p, find_lot(db, p, lot), url, version, actor()))


@app.command()
def refresh(anchor: str, lot: str):
    """Fetch read-only provider status. Never push, merge, comment, or deploy."""
    with DB.session() as db:
        p = resolve(db, anchor)
        result = delivery.refresh(db, load(), p, find_lot(db, p, lot))
        output(result)
        failed = 'error' in json.loads(db.get(delivery.Artifact, result['version']).body)['observation']
    if failed:
        typer.echo('Refresh failed; see the Delivery tab. Check provider login and retry.')
        raise typer.Exit(1)


@app.command()
def status(anchor: str):
    with DB.session() as db:
        from . import delivery_actions
        p = resolve(db, anchor)
        output({**delivery.project(db, p), 'actions': delivery_actions.project(db, p)})


@app.command('publish-integration')
def publish_integration(anchor: str, file: Path = typer.Option(..., exists=True),
                        version: int = typer.Option(0)):
    """Share exact integration commands with the team and enable Room execution."""
    from . import delivery_actions
    from .workflow_cli import input_file
    with DB.session() as db:
        output(delivery_actions.publish_recipe(db, load(), resolve(db, anchor), input_file(file), version))


@app.command('share-package')
def share_package(anchor: str, file: Path = typer.Option(..., exists=True),
                  version: int = typer.Option(0)):
    """Explicitly share six package narrative fields, bound to current revisions."""
    from . import delivery_actions
    from .workflow_cli import input_file
    with DB.session() as db:
        data = input_file(file)
        output(delivery_actions.share_package(db, resolve(db, anchor),
            {k: data.get(k) for k in delivery_actions.FIELDS}, version, actor()))


@app.command('publish-deployment')
def publish_deployment(anchor: str, file: Path = typer.Option(..., exists=True),
                       version: int = typer.Option(0)):
    """Share an owner-authored test deployment recipe for reviewed Room execution."""
    from . import delivery_actions
    from .workflow_cli import input_file
    with DB.session() as db:
        output(delivery_actions.publish_deployment(db, load(), resolve(db, anchor), input_file(file), version))
