"""Single-use, named invitations for the one-team service."""
from alembic import op
from sqlalchemy import text

revision = '0005'
down_revision = '0004'


def upgrade():
    op.get_bind().execute(text('''CREATE TABLE team_invitations (
        id SERIAL PRIMARY KEY,
        public_id VARCHAR(36) NOT NULL UNIQUE,
        code_hash VARCHAR(64) NOT NULL UNIQUE,
        name VARCHAR(64) NOT NULL,
        role VARCHAR(16) NOT NULL,
        issuer VARCHAR(128) NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        revoked BOOLEAN NOT NULL,
        member_id INTEGER REFERENCES team_members(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )'''))


def downgrade():
    raise RuntimeError('Invitation audit history must be retained; restore a backup instead.')
