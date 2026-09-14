"""Integration: the views must actually carry the columns the renderers read.

Guards the class of bug that bit twice — a view that silently stays stale
(CREATE OR REPLACE refusing a column-list change) or never installs at all
(a statement discarded by the splitter) looks exactly like an empty database.
"""
import pytest
from sqlalchemy import text

pytest.importorskip("psycopg")

EXPECTED = {
    "v_live_lots": {"ticket", "lot", "state", "phase", "agent", "attempt", "elapsed",
                    "since_heartbeat", "typical_s", "stale", "retrying", "last_chance",
                    "needs_human", "cost", "cost_estimated", "permission_denials"},
    "v_plat_summary": {"anchor", "status", "lots", "lots_done", "lots_blocked", "spent"},
    "v_decision_tree": {"depth", "actor", "kind", "decision", "observed"},
    "v_delivered_plats": {"anchor", "roster", "started", "closed", "lots",
                          "attempts", "findings", "spend", "roster_gap"},
}


@pytest.fixture(scope="module")
def conn():
    from plat.db import engine
    try:
        e = engine()
        with e.begin() as c:
            yield c
    except Exception as exc:                      # no local postgres -> skip
        pytest.skip(f"no database: {exc}")


@pytest.mark.parametrize("view,cols", EXPECTED.items())
def test_view_exposes_its_columns(conn, view, cols):
    got = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :v"),
        {"v": view})}
    assert got, f"{view} is not installed at all"
    missing = cols - got
    assert not missing, f"{view} is stale or wrong — missing {sorted(missing)}"


def test_stale_is_relative_to_history_not_a_flat_constant(conn):
    """A 95-minute indexing run is healthy; a 12-minute review is probably wedged."""
    src = conn.execute(text(
        "SELECT pg_get_viewdef('v_live_lots'::regclass, true)")).scalar()
    assert "median_s" in src and "make_interval" in src
    assert "percentile_cont" in src


def test_the_all_branch_returns_the_same_columns_as_the_view(conn):
    """`--all` is a hand-written stand-in for v_live_lots. Three renderers read
    it, so a column the view has and it lacks fails at runtime in whichever one
    happens to need it — drill-in broke on a missing lot_id exactly this way."""
    from plat.cli import _live_rows
    view_cols = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'v_live_lots'"))}
    rows = _live_rows(None, True)
    if not rows:
        pytest.skip("no lots to compare")
    missing = view_cols - set(rows[0].keys())
    assert not missing, f"--all is missing {sorted(missing)} that v_live_lots has"
