"""One authenticated team per control-service database."""
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = '0003'
down_revision = '0002'


def upgrade():
    op.get_bind().execute(text((Path(__file__).parents[1] / 'team.sql').read_text()))


def downgrade():
    raise RuntimeError('Use a database backup; member identities are never silently removed.')
