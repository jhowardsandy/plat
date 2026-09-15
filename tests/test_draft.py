"""The draft validator. Catching a bad draft here means an editable line, not a
traceback twenty minutes later with a worktree half-created."""
import pytest

from plat.draft import unverifiable, validate


def spec(**over):
    base = {
        "anchor": "DEV-1", "title": "t",
        "lots": [{"key": "a", "repo": "core/x",
                  "gate": {"setup": "s", "test": "t"}}],
        "criteria": [{"id": "AC1", "lot": "a", "statement": "s", "verify": "v"}],
    }
    base.update(over)
    return base


@pytest.fixture
def root(tmp_path):
    (tmp_path / "core" / "x" / ".git").mkdir(parents=True)
    return tmp_path


def test_a_good_draft_validates(root):
    assert validate(spec(), root) == []


def test_a_repo_the_planner_invented_is_caught(root):
    errs = validate(spec(lots=[{"key": "a", "repo": "core/imaginary",
                                "gate": {"setup": "s", "test": "t"}}]), root)
    assert any("does not exist" in e for e in errs)


def test_missing_setup_is_caught(root):
    """A fresh worktree has no venv; a gate with no setup fails every attempt."""
    errs = validate(spec(lots=[{"key": "a", "repo": "core/x", "gate": {"test": "t"}}]), root)
    assert any("gate.setup" in e for e in errs)


def test_dangling_dependency_is_caught(root):
    errs = validate(spec(lots=[{"key": "a", "repo": "core/x", "depends_on": ["ghost"],
                                "gate": {"setup": "s", "test": "t"}}]), root)
    assert any("ghost" in e for e in errs)


def test_converge_without_an_objective_or_probe_is_caught(root):
    errs = validate(spec(lots=[{"key": "a", "repo": "core/x", "mode": "converge",
                                "gate": {"setup": "s", "test": "t"}}]), root)
    assert any("objective" in e for e in errs)
    assert any("probe" in e for e in errs)


def test_criteria_pointing_at_no_such_lot_is_caught(root):
    errs = validate(spec(criteria=[{"id": "AC1", "lot": "nope", "statement": "s"}]), root)
    assert any("nope" in e for e in errs)


def test_needs_human_is_an_honest_answer_not_a_broken_draft(root):
    """A ticket too vague to decompose should come back as a question, and that
    must not read as a validation failure."""
    assert validate({"anchor": "DEV-1", "needs_human": True,
                     "questions": ["which tenant?"], "lots": []}, root) == []


def test_empty_lots_without_needs_human_is_ambiguous(root):
    errs = validate({"anchor": "DEV-1", "lots": []}, root)
    assert any("needs_human" in e for e in errs)


def test_unverifiable_criteria_are_surfaced_not_rejected():
    s = spec(criteria=[{"id": "AC1", "statement": "s"},
                       {"id": "AC2", "statement": "s", "verify": "v"}])
    assert unverifiable(s) == ["AC1"]


def test_the_planner_is_not_run_in_plan_mode():
    """permission_mode: plan blocks every write INCLUDING the draft, and fails
    silently — exit 0, no denials, no file. $6.91 established this."""
    import pathlib

    import yaml

    from plat import roles
    spec = yaml.safe_load(
        (pathlib.Path(roles.__file__).parent / "roles.default.yaml").read_text())
    planner = spec["roles"]["planner"]
    assert planner["permission_mode"] != "plan"
    assert "Write" in planner["allowed_tools"], "it must be able to write its draft"


def test_a_planner_that_edited_a_repo_is_rejected():
    import inspect

    from plat import draft
    src = inspect.getsource(draft.run)
    assert "_dirty(" in src and "MODIFIED" in src


def test_the_planner_is_told_commands_run_in_the_worktree():
    """A draft once emitted `cd core/foo && pnpm test` as a verify command, which
    would test the untouched original checkout rather than the agent's work."""
    from pathlib import Path

    from plat import prompts
    t = (Path(prompts.PACKS) / "plan.md").read_text()
    assert "INSIDE that lot's worktree" in t
    assert "never begins with `cd <repo>`" in t
