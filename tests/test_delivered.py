"""delivered vs closed.

Calling a finished plat "closed" made three of them read as shipped when not one
had been pushed, reviewed or merged. A ledger that is wrong is worse than a ledger
that is empty, because you stop checking it.
"""
import inspect

import pytest
from sqlalchemy import text

from plat import runner
from plat.fsm import PlatState
from plat.models import Plat


def test_the_two_dates_are_separate_columns():
    """One is when Plat finished; the other is when a person said it shipped."""
    cols = Plat.__table__.c
    assert "delivered_at" in cols and "closed_at" in cols


def test_delivered_is_a_real_state():
    assert PlatState.DELIVERED.value == "delivered"


def test_the_supervisor_delivers_and_never_closes():
    """Closing is a judgement about the world outside Plat, so Plat cannot make it."""
    src = inspect.getsource(runner.maybe_deliver_plat)
    assert 'plat.status = "delivered"' in src
    assert 'plat.status = "closed"' not in src
    assert "delivered_at" in src and "plat.closed_at" not in src


def test_only_the_cli_closes_and_records_it_as_a_human_decision():
    from plat import cli
    src = inspect.getsource(cli.close)
    assert 'actor="human"' in src and 'kind="signoff"' in src
    assert "reversible=False" in src


@pytest.fixture
def conn():
    from plat.db import engine
    try:
        with engine().begin() as c:
            yield c
    except Exception as e:
        pytest.skip(f"no database: {e}")


def test_a_delivered_plat_leaves_the_live_board(conn):
    """It is not in flight; the monitor must not fill with finished work."""
    src = conn.execute(text("SELECT pg_get_viewdef('v_live_lots'::regclass, true)")).scalar()
    assert "'delivered'" in src


def test_history_exposes_what_is_waiting_on_a_person(conn):
    cols = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'v_delivered_plats'"))}
    assert {"delivered", "closed", "awaiting_you", "status"} <= cols


def test_nothing_is_recorded_as_shipped_that_has_not_been(conn):
    """Regression on the actual bug: every plat here finished and none merged."""
    n = conn.execute(text(
        "SELECT count(*) FROM plats WHERE kind='plat' AND closed_at IS NOT NULL "
        "AND delivered_at IS NULL")).scalar()
    assert n == 0, "a plat is closed without ever having been delivered"


def test_the_demo_seeds_only_states_that_could_really_happen(conn):
    """`plat demo` once seeded a plat closed without ever being delivered — data
    the schema permits and the world does not. A fresh clone running demo then
    pytest saw a failure it had done nothing to cause."""
    from sqlalchemy import text
    bad = conn.execute(text(
        "SELECT anchor FROM plats WHERE origin_id = 'demo' "
        "AND closed_at IS NOT NULL AND delivered_at IS NULL")).scalars().all()
    assert not bad, f"demo plats closed but never delivered: {bad}"
