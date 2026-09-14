"""L2b - the gate, against a real repo and a real test runner."""
import subprocess
from pathlib import Path
import pytest
from plat import gates


def repo(tmp_path: Path, failing: bool) -> tuple[Path, str]:
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\ndef test_add(): assert add(2,2) == "
        + ("5\n" if failing else "4\n"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path)
    subprocess.run(["git", "-c", "user.email=p@l", "-c", "user.name=plat",
                    "commit", "-qm", "base"], cwd=tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                         capture_output=True, text=True).stdout.strip()
    return tmp_path, sha


CMD = "python3 -m pytest -q"


def test_passing_suite_with_a_diff_passes(tmp_path):
    wt, base = repo(tmp_path, failing=False)
    (wt / "extra.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=wt)
    subprocess.run(["git", "-c", "user.email=p@l", "-c", "user.name=plat",
                    "commit", "-qm", "work"], cwd=wt)
    g = gates.run(wt, CMD, base)
    assert g.tests_failed == 0 and g.tests_passed == 1 and g.diff_files == 1


def test_failing_suite_is_caught(tmp_path):
    wt, base = repo(tmp_path, failing=True)
    g = gates.run(wt, CMD, base)
    assert g.tests_failed >= 1


def test_no_diff_means_no_pass(tmp_path):
    """An agent can report complete having changed nothing. The gate is what
    makes that a failure rather than a success."""
    wt, base = repo(tmp_path, failing=False)
    g = gates.run(wt, CMD, base)
    assert g.tests_failed == 0 and g.diff_files == 0
    from plat.fsm import gate_passed
    assert not gate_passed(g.as_dict())


def test_smoke_detects_an_unrunnable_gate(tmp_path):
    wt, _ = repo(tmp_path, failing=False)
    g = gates.smoke(wt, "poetry run pytest -q")   # no poetry env here
    assert g.exit_code != 0, "an unrunnable gate must be caught before any agent runs"


def test_captured_output_survives_rich_markup(capsys):
    """pytest node ids contain [brackets]. Printed through rich's markup parser
    they are read as tags and the line disappears — which is how a gate smoke
    failure once reported nothing at all."""
    from rich.console import Console
    hostile = "FAILED tests/contract/test_p.py::test_x[financial-reconciliation] 12 failed"
    con = Console(force_terminal=False, width=200)
    con.print(hostile, markup=False, highlight=False)
    out = capsys.readouterr().out
    assert "financial-reconciliation" in out and "12 failed" in out


def test_an_unreadable_reporter_is_not_reported_as_zero_tests(tmp_path):
    """vitest --reporter=junit writes counts to a file and prints a coverage table.
    Parsing 0 and calling it 0 passed makes a measured green suite and an empty
    one indistinguishable — and both look like success."""
    wt, base = repo(tmp_path, failing=False)
    g = gates.run(wt, "echo 'coverage table, no counts here'", base)
    assert g.exit_code == 0
    assert g.counted is False, "must record that the numbers are unknown"


def test_a_readable_reporter_is_counted(tmp_path):
    wt, base = repo(tmp_path, failing=False)
    g = gates.run(wt, CMD, base)
    assert g.counted is True and g.tests_passed == 1


def test_converge_baseline_is_not_taken_from_an_uncounted_gate():
    """Guarding a metric with a baseline of 0 guards nothing."""
    import inspect
    from plat import cli
    src = inspect.getsource(cli.plan)
    assert "sm.tests_passed if sm.counted else 0" in src
