"""A regression test for a bug that installed zero views and looked like an empty db."""
from pathlib import Path
from plat.db import _split

SQL = Path(__file__).parent.parent / "src" / "plat" / "views.sql"


def test_every_view_and_the_trigger_function_survive_splitting():
    stmts = _split(SQL.read_text())
    blob = "\n".join(stmts)
    for obj in ("v_live_lots", "v_plat_summary", "v_decision_tree", "v_delivered_plats"):
        assert f"VIEW {obj}" in blob, f"{obj} was dropped by the splitter"
    assert any(s.startswith("CREATE OR REPLACE FUNCTION plat_notify_event") for s in stmts)
    assert any(s.startswith("CREATE TRIGGER") for s in stmts)


def test_leading_comments_do_not_discard_a_statement():
    s = _split("-- a comment\n-- another\nCREATE VIEW v AS SELECT 1;")
    assert len(s) == 1 and s[0].startswith("CREATE VIEW")


def test_dollar_quoted_body_is_not_split_on_inner_semicolons():
    sql = ("CREATE FUNCTION f() RETURNS trigger AS $$\nBEGIN\n  PERFORM 1;\n"
           "  RETURN NEW;\nEND; $$ LANGUAGE plpgsql;")
    s = _split(sql)
    assert len(s) == 1 and "RETURN NEW" in s[0]
