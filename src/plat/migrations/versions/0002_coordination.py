"""Durable identities, claims, lifecycle and collaboration records."""
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = '0002'
down_revision = '0001'


def upgrade():
    from plat.db import _split
    c = op.get_bind()
    if c.execute(text('SELECT 1 FROM lots GROUP BY plat_id, key HAVING count(*) > 1 LIMIT 1')).first():
        raise RuntimeError('Duplicate lot keys: reconcile duplicate history before migration; no records were deleted.')
    if c.execute(text("SELECT 1 FROM plats WHERE kind='plat' GROUP BY origin_id, anchor HAVING count(*) > 1 LIMIT 1")).first():
        raise RuntimeError('Duplicate anchors in one installation: reconcile before migration.')
    for statement in _split((Path(__file__).parents[1] / 'coordination.sql').read_text()):
        c.execute(text(statement))


def downgrade():
    raise RuntimeError('Restore a backup to roll back coordination records; data is never dropped.')
