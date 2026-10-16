"""Bind execution requests to their previewed plan, including legacy runs."""
import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'


def upgrade():
    # Existing acknowledgements have no preview binding and fail preflight closed.
    op.add_column('execution_commands', sa.Column('scope_hash', sa.String(64), nullable=True))


def downgrade():
    raise RuntimeError('Execution history must be retained; restore a backup instead.')
