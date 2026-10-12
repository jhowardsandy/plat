"""Administrative identity decisions are scoped to the team, not an arbitrary run."""
from alembic import op
from sqlalchemy import text

revision = '0004'
down_revision = '0003'


def upgrade():
    for table in ('decisions', 'events'):
        op.get_bind().execute(text(f'ALTER TABLE {table} ALTER COLUMN plat_id DROP NOT NULL'))


def downgrade():
    raise RuntimeError('Identity audit events must be retained; restore a backup instead.')
