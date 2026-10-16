"""Machine-scoped credentials and durable execution requests."""
from alembic import op
from sqlalchemy import text

revision = '0008'
down_revision = '0007'


def upgrade():
    for statement in (
        '''CREATE TABLE executors (
            id SERIAL PRIMARY KEY, public_id VARCHAR(36) NOT NULL UNIQUE,
            name VARCHAR(80) NOT NULL, installation VARCHAR(64) NOT NULL,
            host_id VARCHAR(64) NOT NULL, token_hash VARCHAR(64) NOT NULL UNIQUE,
            active BOOLEAN NOT NULL, session VARCHAR(36), lease_until TIMESTAMPTZ,
            last_seen TIMESTAMPTZ, CONSTRAINT uq_executor_machine UNIQUE(installation, host_id))''',
        '''CREATE TABLE execution_commands (
            id SERIAL PRIMARY KEY, public_id VARCHAR(36) NOT NULL UNIQUE,
            plat_id INTEGER NOT NULL REFERENCES plats(id), executor_id INTEGER NOT NULL REFERENCES executors(id),
            actor VARCHAR(128) NOT NULL, action VARCHAR(16) NOT NULL, version INTEGER NOT NULL,
            expected_status VARCHAR(24) NOT NULL, note TEXT NOT NULL, state VARCHAR(24) NOT NULL,
            delivery_session VARCHAR(36), requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            acknowledged_at TIMESTAMPTZ, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
            pid INTEGER, outcome TEXT NOT NULL)''',
        'CREATE INDEX ix_execution_commands_plat_id ON execution_commands(plat_id)',
        'CREATE INDEX ix_execution_commands_executor_id ON execution_commands(executor_id)',
    ):
        op.get_bind().execute(text(statement))


def downgrade():
    raise RuntimeError('Execution history must be retained; restore a backup instead.')
