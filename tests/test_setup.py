"""`plat setup`. A wizard that only asks is a form, and a form records answers
that cannot work."""
import inspect

from plat import setup as S
from plat.config import CONFIG_TEMPLATE, DEFAULTS, write_default_config


def test_the_config_template_survives_literal_braces(tmp_path, monkeypatch):
    """The template documents a tracker command containing {anchor}. Substituting
    with .format() tried to resolve it and the whole wizard died on KeyError."""
    monkeypatch.setenv("PLAT_HOME", str(tmp_path))
    p = write_default_config()
    body = p.read_text()
    assert "{anchor}" in body, "the documented example lost its placeholder"
    for key in ("database_url", "workspace_root", "max_attempts", "phases"):
        assert "{" + key + "}" not in body, f"{key} was never substituted"


def test_every_template_placeholder_has_a_default():
    import re
    placeholders = set(re.findall(r"\{(\w+)\}", CONFIG_TEMPLATE)) - {"anchor"}
    assert placeholders <= set(DEFAULTS), f"no default for {placeholders - set(DEFAULTS)}"


def test_a_fresh_roles_file_keeps_every_shipped_role():
    """The wizard sets coder and reviewer. If it starts from an empty dict instead
    of the shipped defaults, planner vanishes and `plat draft` fails."""
    src = inspect.getsource(__import__("plat.cli", fromlist=["x"]).setup)
    assert "roles.default.yaml" in src


def test_presets_are_real_ladders_not_one_rung():
    for name, p in S.PRESETS.items():
        assert p["coder"][1] in ("low", "medium", "high", "xhigh", "max")
        assert p["escalate"], f"{name} has no escalation at all"


def test_build_roles_preserves_unrelated_roles():
    base = {"roles": {"planner": {"provider": "claude", "model": "opus"},
                      "coder": {"provider": "claude", "model": "haiku"}}}
    out = S.build_roles(base, "claude", "sonnet", "codex", "gpt-5.5", "balanced",
                        ["code", "review"])
    assert out["roles"]["planner"]["model"] == "opus", "an unrelated role was dropped"
    assert out["roles"]["coder"]["model"] == "sonnet"
    assert out["roles"]["reviewer.correctness"]["provider"] == "codex"


def test_the_reviewer_is_never_resumed_and_the_coder_always_is():
    out = S.build_roles({"roles": {}}, "claude", "sonnet", "codex", "gpt-5.5",
                        "balanced", ["code", "review"])
    assert out["roles"]["coder"]["resume_session"] is True
    assert out["roles"]["reviewer.correctness"]["resume_session"] is False


def test_written_roles_say_where_the_annotated_reference_is():
    """setup rewrites roles.yaml with yaml.safe_dump, which drops every comment."""
    assert "roles.default.yaml" in S.HEADER and "backup" in S.HEADER.lower()


def test_phase_order_is_not_configurable():
    """You choose WHICH phases run; the order is semantic, not a preference."""
    from plat.fsm import _ORDER
    assert _ORDER == ["code", "review", "docs", "quality"]
    src = inspect.getsource(__import__("plat.cli", fromlist=["x"]).setup)
    assert "Order is fixed" in src


# ------------------------------------------------------------------ backfill

def _write(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def test_backfill_adds_only_what_is_missing(tmp_path):
    from plat.config import backfill
    p = _write(tmp_path, 'database_url = "x"\nworkspace_root = "/w"\n')
    added = backfill(p)
    assert "phases" in added and "notify" in added
    assert "database_url" not in added


def test_backfill_preserves_what_was_already_there(tmp_path):
    import tomllib

    from plat.config import backfill
    p = _write(tmp_path, 'database_url = "keepme"\nworkspace_root = "/w"\n\n'
                         '[tracker]\ncmd = "mine {anchor}"\n')
    backfill(p)
    d = tomllib.loads(p.read_text())
    assert d["database_url"] == "keepme"
    assert d["tracker"]["cmd"] == "mine {anchor}"


def test_a_scalar_is_never_appended_into_an_existing_table(tmp_path):
    """A bare `key = value` written after a [table] header becomes a key OF that
    table. `phases` once landed as tracker.phases, silently."""
    import tomllib

    from plat.config import backfill
    p = _write(tmp_path, 'database_url = "x"\n\n[tracker]\ncmd = "t"\n')
    backfill(p)
    d = tomllib.loads(p.read_text())
    assert "phases" in d, "phases did not land at the top level"
    assert "phases" not in d.get("tracker", {}), "phases was swallowed by [tracker]"


def test_backfill_is_idempotent(tmp_path):
    from plat.config import backfill
    p = _write(tmp_path, 'database_url = "x"\n')
    backfill(p)
    first = p.read_text()
    assert backfill(p) == []
    assert p.read_text() == first


def test_backfill_output_is_still_valid_toml(tmp_path):
    import tomllib

    from plat.config import backfill
    p = _write(tmp_path, 'database_url = "x"\n\n[tracker]\ncmd = "t {anchor}"\n')
    backfill(p)
    tomllib.loads(p.read_text())          # must not raise


def test_table_blocks_keep_their_own_keys(tmp_path):
    """[notify]'s enabled/handlers/on must not be split out as top-level blocks."""
    from plat.config import template_blocks
    b = template_blocks()
    assert "notify" in b
    for sub in ("enabled", "handlers", "on"):
        assert sub not in b, f"{sub} escaped its table"


def test_sources_record_where_each_value_came_from(monkeypatch, tmp_path):
    from plat.config import load
    monkeypatch.setenv("PLAT_HOME", str(tmp_path))
    monkeypatch.setenv("PLAT_WORKSPACE", "/from/env")
    (tmp_path / "config.toml").write_text('database_url = "from-file"\n')
    cfg = load()
    assert cfg.sources["workspace_root"].startswith("env")
    assert cfg.sources["database_url"] == "config.toml"
    assert cfg.sources["max_attempts"] == "default"


def test_the_password_is_redacted():
    from plat.config import redact
    assert redact("postgresql+psycopg://u:hunter2@h:5432/d") == \
        "postgresql+psycopg://u:***@h:5432/d"
    assert "hunter2" not in redact("postgresql://u:hunter2@h/d")
