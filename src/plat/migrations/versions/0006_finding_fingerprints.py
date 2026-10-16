"""Preserve arbitrary-length agent fingerprints without oversized index entries."""
from alembic import op
from sqlalchemy import text

revision = '0006'
down_revision = '0005'


def upgrade():
    connection = op.get_bind()
    for statement in (
        'DROP INDEX ix_findings_identity',
        'DROP INDEX ix_findings_fingerprint',
        'ALTER TABLE findings ALTER COLUMN fingerprint TYPE TEXT',
        'CREATE INDEX ix_findings_fingerprint ON findings USING hash (fingerprint)',
        'CREATE INDEX ix_findings_identity ON findings (origin_id, anchor, repo, md5(fingerprint))',
    ):
        connection.execute(text(statement))


def downgrade():
    raise RuntimeError('Narrowing fingerprints loses evidence; restore a backup instead.')
