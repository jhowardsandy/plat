from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
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


def init() -> list[str]:
    """Create tables and (re)install the views. Idempotent; safe to re-run."""
    Base.metadata.create_all(engine())
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
