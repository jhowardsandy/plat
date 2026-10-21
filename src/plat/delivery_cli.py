"""Read external delivery status through the owner's existing provider login."""
import json

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
        output(delivery.project(db, resolve(db, anchor)))
