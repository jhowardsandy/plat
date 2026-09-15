from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import load
from .models import Base

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
         "v_delivered_plats", "v_reviews"]

ADDITIVE = [
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
    """Create tables, apply additive columns, (re)install views. Idempotent.

    ADDITIVE is a stopgap: create_all never alters an existing table, so a new
    column would silently not exist on a database that predates it. It handles
    added columns only -- a rename or a type change needs alembic, which is the
    v1 answer.
    """
    Base.metadata.create_all(engine())
    with engine().begin() as c:
        # Views must go first: Postgres refuses to alter the type of a column a
        # view selects, and views.sql recreates every one of them below anyway.
        for v in VIEWS:
            c.execute(text(f"DROP VIEW IF EXISTS {v} CASCADE"))
        for stmt in ADDITIVE:
            c.execute(text(stmt))
    sql = (Path(__file__).parent / "views.sql").read_text()
    done = []
    with engine().begin() as c:
        for stmt in _split(sql):
            c.execute(text(stmt))
            head = " ".join(stmt.split()[:4])
            done.append(head)
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
