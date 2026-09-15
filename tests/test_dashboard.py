"""The dashboard is a shipped artifact; treat it like one.

Grafana accepts a dashboard whose panels lack ids and then renders an empty page
with no error anywhere — the same silent-blank failure as a stale view.
"""
import json
from pathlib import Path

import pytest

DASH = Path(__file__).parent.parent / "grafana" / "dashboards" / "plat.json"
PANELS = json.loads(DASH.read_text())["panels"]


def test_every_panel_has_a_unique_id():
    ids = [p.get("id") for p in PANELS]
    assert all(isinstance(i, int) for i in ids), "a panel without an int id renders nothing"
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("panel", PANELS, ids=lambda p: p["title"][:30])
def test_panel_is_wired_to_the_plat_datasource(panel):
    assert panel["datasource"]["uid"] == "plat-pg"
    t = panel["targets"][0]
    assert t["rawQuery"] is True and t["rawSql"].strip()


def test_panels_read_the_views_not_raw_tables():
    """Derived signals live in SQL so every renderer agrees. A panel that
    recomputes 'stale' itself would drift from the terminal."""
    joined = " ".join(p["targets"][0]["rawSql"] for p in PANELS)
    assert "v_live_lots" in joined and "v_decision_tree" in joined
    assert "v_delivered_plats" in joined
    assert "interval '10 minutes'" not in joined, "stale must come from the view"


def test_cli_exposes_both_a_live_and_an_archive_view():
    """v_live_lots hides closed plats on purpose, so the terminal needs a way
    past that filter or a finished plat becomes unreachable from the CLI."""
    from plat.cli import app
    names = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert {"status", "history", "show"} <= names


def test_every_command_is_documented_somewhere():
    """Drift the other way: a command nobody wrote down is a command nobody finds.
    review, setup and sync all shipped undocumented."""
    import re
    from pathlib import Path

    from plat.cli import app
    root = Path(__file__).parent.parent
    blob = "\n".join((root / f).read_text() for f in
                     ("README.md", "QUICKSTART.md", "docs/GUIDE.md"))
    real = {c.name or c.callback.__name__ for c in app.registered_commands}
    mentioned = set(re.findall(r"`?plat ([a-z_]+)", blob))
    undocumented = real - mentioned
    assert not undocumented, f"undocumented commands: {sorted(undocumented)}"
