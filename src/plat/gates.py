"""Invariant II lives here. The supervisor runs these; the agent's claim is never consulted.

A gate that cannot RUN is worse than no gate: it fails every attempt and blocks a
lot for reasons that have nothing to do with the code. So smoke() runs first, on
the clean worktree, before a single token is spent.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

# Anchor on a runner's SUMMARY line. Matching "(\d+) passed" anywhere in the blob
# picked up vitest's "Test Files  85 passed" -- the file count, not the test count --
# and "(\d+) (failed|error)" matched console noise, inventing failures in a suite
# that exited 0.
SUMMARY = re.compile(
    r"^\s*(?:Tests|Test Suites)?\s*[:\s]\s*"
    r"(?:(?P<p1>\d+)\s+passed|(?P<f1>\d+)\s+failed)", re.M)
PASSED = re.compile(r"(?:^|\s)(\d+)\s+passed", re.M)
FAILED = re.compile(r"(?:^|\s)(\d+)\s+(?:failed|errored)", re.M)
TESTS_LINE = re.compile(r"^\s*Tests\s+(.+)$", re.M)


@dataclass
class GateResult:
    cmd: str
    ran: bool
    exit_code: int
    tests_passed: int
    tests_failed: int
    diff_files: int
    output_tail: str
    progress: float | None = None      # converge mode: what the probe measured
    baseline_passed: int = 0           # test count before the lot started
    # False when the runner printed no counts we could read. The exit code is
    # still a real signal, but "0 passed" would be a claim we cannot make -- and a
    # suite with no tests must not look identical to one we simply could not parse.
    counted: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def _sh(cmd: str, cwd: Path, timeout: int = 1800, on_pid=None):
    # The same bounded process-group runner as agents: a silent test process
    # must time out too, including any children inheriting its output pipe.
    from .adapters.base import spawn
    rc, out, err, _ = spawn(["/bin/sh", "-c", cmd], cwd, timeout, on_pid=on_pid)
    return subprocess.CompletedProcess(cmd, rc, out, err)


def smoke(worktree: Path, test_cmd: str, setup_cmd: str | None = None) -> GateResult:
    """Can this worktree run its own tests at all, before any agent touches it?

    A fresh worktree has no .venv, so `setup_cmd` (poetry install / pnpm i) runs
    once here. This is the step that stops Monday morning disappearing into
    dependency installs discovered three failed attempts in.
    """
    if setup_cmd:
        s = _sh(setup_cmd, worktree)
        if s.returncode != 0:
            return GateResult(setup_cmd, False, s.returncode, 0, 0, 0,
                              (s.stdout + s.stderr)[-1500:])
    return run(worktree, test_cmd, base=None)


NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def probe(worktree: Path, cmd: str, on_pid=None) -> float | None:
    """Measure progress for a converge lot.

    The command may print anything; the LAST number in its output is the reading.
    That is deliberately forgiving -- `pytest --cov | awk '/TOTAL/{print $NF}'`
    emits "78%", and demanding a bare float would make every probe a shell puzzle.
    """
    p = _sh(cmd, worktree, on_pid=on_pid)
    if p.returncode != 0:
        return None
    nums = NUMBER.findall((p.stdout or "") + (p.stderr or ""))
    return float(nums[-1]) if nums else None


def run(worktree: Path, test_cmd: str, base: str | None,
        probe_cmd: str | None = None, baseline_passed: int = 0, on_pid=None) -> GateResult:
    p = _sh(test_cmd, worktree, on_pid=on_pid)
    blob = (p.stdout or "") + (p.stderr or "")
    # Prefer a runner's own "Tests ..." summary; fall back to the LAST count in the
    # output, since summaries come at the end and per-file chatter comes first.
    scope = m.group(1) if (m := TESTS_LINE.search(blob)) else blob
    pm, fm = PASSED.findall(scope), FAILED.findall(scope)
    passed = int(pm[-1]) if pm else 0
    failed = int(fm[-1]) if fm else 0
    counted = bool(passed or failed)
    # The EXIT CODE is the ground truth for pass/fail; counts are for reporting and
    # for a converge baseline. When they disagree, the counts are a parse artifact.
    if p.returncode == 0 and failed:
        failed = 0
    if not counted:
        # Unparseable runner -- a junit/json reporter writes counts to a file and
        # prints none. Fall back to the exit code, which is never ambiguous, but
        # record that the numbers are unknown rather than zero.
        failed = 0 if p.returncode == 0 else 1
    diff_files = 0
    if base:
        d = _sh(f"git diff --name-only {shlex.quote(base + '..HEAD')} --", worktree)
        diff_files = len([x for x in d.stdout.splitlines() if x.strip()])
    prog = probe(worktree, probe_cmd, on_pid=on_pid) if probe_cmd and p.returncode == 0 else None
    return GateResult(test_cmd, True, p.returncode, passed, failed, diff_files,
                      blob[-1500:], progress=prog, baseline_passed=baseline_passed,
                      counted=counted)


def verify(worktree: Path, criteria, on_pid=None) -> list[dict]:
    """Run the declared commands ourselves, never accepting an agent's claim."""
    results = []
    for c in criteria:
        if c.verify_cmd:
            p = _sh(c.verify_cmd, worktree, on_pid=on_pid)
            results.append({"id": c.ac_id, "cmd": c.verify_cmd,
                            "exit_code": p.returncode, "met": p.returncode == 0,
                            "output_tail": (p.stdout + p.stderr)[-1500:]})
    return results
