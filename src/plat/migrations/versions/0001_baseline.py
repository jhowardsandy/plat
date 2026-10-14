"""Adopt the existing pre-migration schema without deleting records."""
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = '0001'
down_revision = None


def upgrade():
    from plat.db import _split
    ADDITIVE = ["ALTER TABLE criteria ADD COLUMN IF NOT EXISTS scope varchar(8) DEFAULT 'lot' NOT NULL", 'ALTER TABLE criteria ADD COLUMN IF NOT EXISTS verify_in varchar(64)', 'ALTER TABLE attempts ADD COLUMN IF NOT EXISTS cost_estimated boolean DEFAULT false', 'ALTER TABLE attempts ADD COLUMN IF NOT EXISTS permission_denials integer DEFAULT 0', 'ALTER TABLE lots ADD COLUMN IF NOT EXISTS base_ref varchar(64)', "ALTER TABLE lots ADD COLUMN IF NOT EXISTS config jsonb DEFAULT '{}'::jsonb", "ALTER TABLE plats ADD COLUMN IF NOT EXISTS kind varchar(8) DEFAULT 'plat'", 'ALTER TABLE plats ADD COLUMN IF NOT EXISTS delivered_at timestamptz', 'ALTER TABLE attempts ADD COLUMN IF NOT EXISTS pid integer', 'ALTER TABLE plats ADD COLUMN IF NOT EXISTS ticket_status varchar(64)', 'ALTER TABLE plats ADD COLUMN IF NOT EXISTS ticket_summary text', 'ALTER TABLE plats ADD COLUMN IF NOT EXISTS ticket_url text', 'ALTER TABLE plats ADD COLUMN IF NOT EXISTS ticket_checked_at timestamptz', 'ALTER TABLE plats ALTER COLUMN anchor TYPE varchar(160)', 'ALTER TABLE findings ALTER COLUMN anchor TYPE varchar(160)']
    VIEWS = ['v_live_lots', 'v_plat_summary', 'v_decision_tree', 'v_delivered_plats', 'v_reviews']
    c = op.get_bind()
    baseline = _split((Path(__file__).parents[1] / 'baseline.sql').read_text())
    for statement in baseline:
        if not statement.startswith('CREATE INDEX'):
            c.execute(text(statement))
    for view in VIEWS:
        c.execute(text(f'DROP VIEW IF EXISTS {view} CASCADE'))
    for statement in ADDITIVE:
        c.execute(text(statement))
    # Legacy tables can predate indexed columns such as plats.kind.
    for statement in baseline:
        if statement.startswith('CREATE INDEX'):
            c.execute(text(statement))


def downgrade():
    raise RuntimeError('Restore a database backup to roll back the baseline; data is never dropped.')
