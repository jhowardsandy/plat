"""Invariant II lives here. The supervisor runs these; the agent's claim is never consulted.

A gate that cannot RUN is worse than no gate: it fails every attempt and blocks a
lot for reasons that have nothing to do with the code. So smoke() runs first, on
the clean worktree, before a single token is spent.
"""
from __future__ import annotations
import re, subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

PASSED = re.compile(r"(\d+)\s+passed")
FAILED = re.compile(r"(\d+)\s+(?:failed|error)")


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


def _sh(cmd: str, cwd: Path, timeout: int = 1800):
    return subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=timeout)


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


def probe(worktree: Path, cmd: str) -> float | None:
    """Measure progress for a converge lot.

    The command may print anything; the LAST number in its output is the reading.
    That is deliberately forgiving -- `pytest --cov | awk '/TOTAL/{print $NF}'`
    emits "78%", and demanding a bare float would make every probe a shell puzzle.
    """
    p = _sh(cmd, worktree)
    nums = NUMBER.findall((p.stdout or "") + (p.stderr or ""))
    return float(nums[-1]) if nums else None


def run(worktree: Path, test_cmd: str, base: str | None,
        probe_cmd: str | None = None, baseline_passed: int = 0) -> GateResult:
    p = _sh(test_cmd, worktree)
    blob = (p.stdout or "") + (p.stderr or "")
    passed = int(m.group(1)) if (m := PASSED.search(blob)) else 0
    failed = int(m.group(1)) if (m := FAILED.search(blob)) else 0
    counted = bool(passed or failed)
    if not counted:
        # Unparseable runner -- a junit/json reporter writes counts to a file and
        # prints none. Fall back to the exit code, which is never ambiguous, but
        # record that the numbers are unknown rather than zero.
        failed = 0 if p.returncode == 0 else 1
    diff_files = 0
    if base:
        d = _sh(f"git diff --name-only {base}..HEAD", worktree)
        diff_files = len([x for x in d.stdout.splitlines() if x.strip()])
    prog = probe(worktree, probe_cmd) if probe_cmd else None
    return GateResult(test_cmd, True, p.returncode, passed, failed, diff_files,
                      blob[-1500:], progress=prog, baseline_passed=baseline_passed,
                      counted=counted)
