"""plat top, driven headlessly. A TUI you cannot test is a TUI you cannot trust."""
import pytest
from textual.widgets import DataTable, Static

from plat.tui import PlatTop

pytest.importorskip("psycopg")


async def _boot():
    app = PlatTop()
    try:
        app._q("SELECT 1")
    except Exception as e:
        pytest.skip(f"no database: {e}")
    return app


async def test_mounts_and_populates_from_the_views():
    app = await _boot()
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.query_one("#lots", DataTable)
        assert [str(c.label) for c in tbl.columns.values()][:3] == ["ticket", "lot", "state"]
        assert "plat top" in str(app.query_one("#stats", Static).content)


async def test_selecting_a_lot_resets_the_log_tail():
    """Each lot has its own stream; carrying the last id across would skip lines."""
    app = await _boot()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.sel, app._last_log_id = 1, 999
        app.on_data_table_row_highlighted(
            type("E", (), {"row_key": type("K", (), {"value": "2"})()})())
        assert app.sel == 2 and app._last_log_id == 0


async def test_l_toggles_between_agent_log_and_event_stream():
    app = await _boot()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.show_logs is True
        await pilot.press("l")
        assert app.show_logs is False
        await pilot.press("l")
        assert app.show_logs is True


async def test_reopen_refuses_anything_not_blocked():
    """Reopening a running lot would race the process that owns it."""
    app = await _boot()
    async with app.run_test() as pilot:
        await pilot.pause()
        from sqlalchemy import select

        from plat.db import session
        from plat.models import Lot
        with session() as s:
            lot = s.scalars(select(Lot).where(Lot.state == "DONE")).first()
        if lot is None:
            pytest.skip("no non-blocked lot to try")
        app.sel = lot.id
        await pilot.press("o")
        with session() as s:
            assert s.get(Lot, lot.id).state == "DONE", "a DONE lot must not be reopened"


async def test_a_dead_database_does_not_kill_the_ui(monkeypatch):
    app = await _boot()
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "_q", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        app.refresh_all()                      # must not raise
        assert "unreachable" in str(app.query_one("#stats", Static).content)


async def test_top_and_status_read_the_same_query():
    """Three renderers over one view is the whole design. A TUI with its own SQL
    would drift from `plat status` and from Grafana."""
    import inspect

    from plat import tui
    assert "_live_rows(" in inspect.getsource(tui.PlatTop.refresh_all)


async def test_an_unchanged_table_is_not_rebuilt():
    """Rebuilding once a second made the panes flicker: clear() emits a highlight
    event for a transient key, which read as a new selection and wiped the log."""
    app = await _boot()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.refresh_all()
        sig = app._sig
        app.refresh_all()
        assert app._sig is sig or app._sig == sig, "signature changed with no data change"


async def test_highlight_events_from_a_rebuild_are_ignored():
    app = await _boot()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.sel, app._last_log_id = 7, 123
        app._rebuilding = True
        app.on_data_table_row_highlighted(
            type("E", (), {"row_key": type("K", (), {"value": "9"})()})())
        assert app.sel == 7 and app._last_log_id == 123, "a rebuild moved the selection"


async def test_the_log_pane_says_which_stream_it_is_showing():
    """`l` toggles two panes that looked identical and unlabelled."""
    app = await _boot()
    async with app.run_test() as pilot:
        await pilot.pause(); app.refresh_all(); await pilot.pause()
        hdr = str(app.query_one("#logshdr", Static).content)
        assert "agent log" in hdr
        await pilot.press("l"); app.refresh_all(); await pilot.pause()
        assert "event stream" in str(app.query_one("#logshdr", Static).content)
