"""Repository classification and personal/shared workspace views."""
from alembic import op
from sqlalchemy import text

revision = '0007'
down_revision = '0006'


def upgrade():
    for statement in (
        '''CREATE TABLE repository_catalog (
            identity TEXT PRIMARY KEY, public_id VARCHAR(36) NOT NULL UNIQUE,
            products JSONB NOT NULL, services JSONB NOT NULL, version INTEGER NOT NULL)''',
        '''CREATE TABLE saved_views (
            id SERIAL PRIMARY KEY, public_id VARCHAR(36) NOT NULL UNIQUE,
            name VARCHAR(80) NOT NULL, owner VARCHAR(128) NOT NULL,
            shared BOOLEAN NOT NULL, settings JSONB NOT NULL)''',
    ):
        op.get_bind().execute(text(statement))


def downgrade():
    raise RuntimeError('Workspace decisions must be retained; restore a backup instead.')
