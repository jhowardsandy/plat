from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import load

_engine = None
_Session = None


def engine():
    global _engine, _Session
    if _engine is None:
        _engine = create_engine(load().database_url, future=True)
        _Session = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


@contextmanager
def session() -> Session:
    engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


VIEWS = ["v_live_lots", "v_plat_summary", "v_decision_tree",
         "v_delivered_plats", "v_reviews", "v_claim_status", "v_session_status"]

ADDITIVE = [
    "ALTER TABLE criteria ADD COLUMN IF NOT EXISTS scope varchar(8) DEFAULT 'lot' NOT NULL",
    "ALTER TABLE criteria ADD COLUMN IF NOT EXISTS verify_in varchar(64)",
    "ALTER TABLE attempts ADD COLUMN IF NOT EXISTS cost_estimated boolean DEFAULT false",
    "ALTER TABLE attempts ADD COLUMN IF NOT EXISTS permission_denials integer DEFAULT 0",
    "ALTER TABLE lots ADD COLUMN IF NOT EXISTS base_ref varchar(64)",
    "ALTER TABLE lots ADD COLUMN IF NOT EXISTS config jsonb DEFAULT \'{}\'::jsonb",
    "ALTER TABLE plats ADD COLUMN IF NOT EXISTS kind varchar(8) DEFAULT \'plat\'",
    # Widenings are safe and idempotent. Narrowing never belongs here.
    "ALTER TABLE plats ADD COLUMN IF NOT EXISTS delivered_at timestamptz",
    "ALTER TABLE attempts ADD COLUMN IF NOT EXISTS pid integer",
    "ALTER TABLE plats ADD COLUMN IF NOT EXISTS ticket_status varchar(64)",
    "ALTER TABLE plats ADD COLUMN IF NOT EXISTS ticket_summary text",
    "ALTER TABLE plats ADD COLUMN IF NOT EXISTS ticket_url text",
    "ALTER TABLE plats ADD COLUMN IF NOT EXISTS ticket_checked_at timestamptz",
    "ALTER TABLE plats ALTER COLUMN anchor TYPE varchar(160)",
    "ALTER TABLE findings ALTER COLUMN anchor TYPE varchar(160)",
]


def init() -> list[str]:
    """Transactionally upgrade the schema and reinstall projections."""
    from alembic import command
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    done = []
    with engine().begin() as c:
        # Serialize upgrades, including two first-ever init calls.
        c.execute(text("SELECT pg_advisory_xact_lock(739217450)"))
        cfg.attributes["connection"] = c
        command.upgrade(cfg, "head")
        for view in VIEWS:
            c.execute(text(f"DROP VIEW IF EXISTS {view} CASCADE"))
        for stmt in _split((Path(__file__).parent / "views.sql").read_text()):
            c.execute(text(stmt))
            done.append(" ".join(stmt.split()[:4]))
    return done


def _split(sql: str) -> list[str]:
    """Split on ';', keeping $$ ... $$ function bodies intact.

    Leading comment lines are stripped from each statement rather than causing it
    to be discarded -- getting that wrong silently installs no views at all, which
    looks exactly like an empty database.
    """
    out, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        buf.append(line)
        if not in_dollar and line.rstrip().endswith(";"):
            out.append(buf)
            buf = []
    if buf:
        out.append(buf)
    stmts = []
    for lines in out:
        while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
            lines.pop(0)
        stmt = "\n".join(lines).strip().rstrip(";").strip()
        if stmt:
            stmts.append(stmt)
    return stmts
