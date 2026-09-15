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
