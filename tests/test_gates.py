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


VITEST = """
 ✓ src/a.test.tsx (6 tests) 12ms
 ⚠️ No token available for request. 2 errors suppressed
 Test Files  85 passed (85)
      Tests  489 passed (489)
   Duration  20.32s
"""

JEST = """
Test Suites: 12 passed, 12 total
Tests:       72 passed, 72 total
"""


def test_the_test_count_is_not_the_file_count(tmp_path):
    """vitest prints 'Test Files 85 passed' BEFORE 'Tests 489 passed'. Taking the
    first match reported 85 tests for a suite of 489."""
    wt, base = repo(tmp_path, failing=False)
    script = tmp_path / "fake.sh"
    script.write_text("#!/bin/sh\ncat <<'EOT'\n" + VITEST + "EOT\nexit 0\n")
    script.chmod(0o755)
    g = gates.run(wt, str(script), base)
    assert g.tests_passed == 489, f"read the file count instead: {g.tests_passed}"
    assert g.tests_failed == 0, "console noise must not invent failures"


def test_a_zero_exit_overrides_a_parsed_failure(tmp_path):
    """Counts are scraped text; the exit code is unambiguous. When they disagree
    the count is wrong — this blocked a correct lot for three attempts."""
    wt, base = repo(tmp_path, failing=False)
    script = tmp_path / "noisy.sh"
    script.write_text("#!/bin/sh\necho 'saw 2 errors while warming up'\n"
                      "echo '      Tests  400 passed (400)'\nexit 0\n")
    script.chmod(0o755)
    g = gates.run(wt, str(script), base)
    assert g.tests_failed == 0
    from plat.fsm import gate_passed
    d = g.as_dict(); d["diff_files"] = 1
    assert gate_passed(d), "a suite that exited 0 must pass the gate"


def test_a_nonzero_exit_fails_even_with_no_parsed_failures(tmp_path):
    wt, base = repo(tmp_path, failing=False)
    script = tmp_path / "boom.sh"
    script.write_text("#!/bin/sh\necho 'crashed before reporting'\nexit 1\n")
    script.chmod(0o755)
    g = gates.run(wt, str(script), base)
    from plat.fsm import gate_passed
    d = g.as_dict(); d["diff_files"] = 1
    assert not gate_passed(d)


def test_jest_style_summaries_still_parse(tmp_path):
    wt, base = repo(tmp_path, failing=False)
    script = tmp_path / "jest.sh"
    script.write_text("#!/bin/sh\ncat <<'EOT'\n" + JEST + "EOT\nexit 0\n")
    script.chmod(0o755)
    g = gates.run(wt, str(script), base)
    assert g.tests_passed == 72, f"got {g.tests_passed}"
