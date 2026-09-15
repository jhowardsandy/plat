"""Uniform agent invocation. Same contract in, same result shape out.

This is the layer that makes providers swappable: the FSM knows role names,
roles.yaml maps a role to a provider, and only this package knows how to run one.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

SLACK_S = 5.0


@dataclass
class Role:
    name: str
    provider: str
    model: str
    permission_mode: str = "acceptEdits"
    sandbox: str = "workspace-write"
    approval_mode: str = "auto_edit"
    session_id: str | None = None       # set to resume
    timeout_s: int = 3600
    allowed_tools: list[str] = field(default_factory=list)
    extra_dirs: list[str] = field(default_factory=list)   # readable, not cwd
    # How hard to think. Claude's scale, because it is the richest; other
    # providers map down. Most attempt-2 failures are not "wrong model", they are
    # "did not think hard enough" -- and that is a much cheaper rung to climb.
    effort: str | None = None            # low | medium | high | xhigh | max
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    ok: bool
    exit_code: int
    duration_s: float
    provider: str = ""
    session_id: str | None = None
    cost_usd: float = 0.0
    cost_estimated: bool = False        # codex/gemini report tokens, not dollars
    tokens_in: int = 0
    tokens_out: int = 0
    permission_denials: list[Any] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


CHUNK_EVERY_S = 2.0
CHUNK_BYTES = 4096


def _etime_seconds(raw: str) -> float | None:
    """ps elapsed time -- [[dd-]hh:]mm:ss -- as seconds."""
    days, _, rest = raw.strip().rpartition("-")
    parts = rest.split(":")
    if not all(p.isdigit() for p in parts) or not 2 <= len(parts) <= 3:
        return None
    secs = 0.0
    for p in parts:
        secs = secs * 60 + int(p)
    return secs + (int(days) * 86400 if days.isdigit() else 0)


def process_alive(pid: int | None, expect: str = "",
                  since: datetime | None = None) -> bool:
    """Is that process still running, and still the one we started?

    PIDs are recycled, and the cost of getting this wrong is not symmetric: a
    false negative gates a worktree an agent is still writing to, while a false
    positive points `plat abort` at a stranger's process group. So identity is
    checked two ways.

    `expect` is the command we launched. It matches for the agents we run, but
    `ps` reports the resolved executable -- a wrapper or a venv shim can report a
    path that never contains the name it was invoked by -- so a mismatch alone is
    not proof.

    `since` is when we started it, and it is the check that pid reuse cannot
    fake: a recycled pid belongs to a process YOUNGER than our attempt. A process
    at least as old as the attempt is ours whatever ps calls it.
    """
    if not pid:
        return False
    r = subprocess.run(["ps", "-p", str(pid), "-o", "state=,etime=,command="],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    line = r.stdout.strip()
    if r.returncode != 0 or not line:
        return False
    state, _, rest = line.partition(" ")
    if state.startswith("Z"):
        return False        # defunct: listed by ps, running nothing
    etime, _, command = rest.strip().partition(" ")
    if since is not None:
        elapsed, age = _etime_seconds(etime), (
            datetime.now(since.tzinfo) - since).total_seconds()
        if elapsed is not None:
            # SLACK covers ps's one-second resolution and the gap between our
            # row's timestamp and the fork.
            return elapsed >= age - SLACK_S
    return (expect in command) if expect else True


def terminate(pid: int | None, grace: float = 5.0,
              expect: str = "", since: datetime | None = None) -> bool:
    """Stop an agent and everything it started.

    The whole process GROUP, because an agent spawns test runners and package
    managers of its own, and killing only the parent leaves those orphaned in turn.
    That is also why identity is re-checked here and not taken on trust from the
    caller: signalling a group is the one thing in Plat that reaches outside its
    own data, and a recycled pid would aim it at somebody else's work.
    """
    if not process_alive(pid, expect, since):
        return False        # never signal a group we have not identified
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)       # it had its chance
    except (ProcessLookupError, PermissionError):
        pass
    return True


def spawn(cmd: list[str], cwd: Path, timeout_s: int, env: dict | None = None,
          on_chunk: Callable[[str], None] | None = None,
          on_pid: Callable[[int, str], None] | None = None) -> tuple[int, str, str, float]:
    """Run an agent CLI to completion, streaming its output as it arrives.

    Reading incrementally is what makes a live log tail possible at all: an agent
    may run for an hour, and capturing everything at exit leaves nothing to watch
    for the entire hour that matters.

    stdin=DEVNULL is load-bearing: `codex exec` appends anything on stdin as a
    <stdin> block, so an inherited terminal stdin silently corrupts the prompt.
    start_new_session gives the child its own process group so a timeout kills the
    whole tree rather than orphaning the CLI underneath it.

    on_chunk is called on a coarse schedule -- roughly every 4KB, or when ~2s
    have passed AS A LINE ARRIVES. It is not a timer: an agent that goes silent
    mid-thought holds its buffered tail until it speaks again or exits. That is
    an accepted limit, not an oversight; a flush thread would buy very little,
    since agents emit output steadily, and cost a second thread per attempt.
    """
    t0 = time.monotonic()
    p = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, bufsize=1,
        start_new_session=True, env=env,
    )
    if on_pid:
        try:
            on_pid(p.pid, cmd[0])   # recorded before a single line is read, so a
        except Exception:           # session that dies at once still leaves a trail
            pass
    out: list[str] = []
    pending: list[str] = []
    last = time.monotonic()

    def flush():
        nonlocal last
        if pending and on_chunk:
            try:
                on_chunk("".join(pending))
            except Exception:
                pass          # a failed log write must never take down the agent
        pending.clear()
        last = time.monotonic()

    def kill():
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass

    try:
        assert p.stdout is not None
        for line in p.stdout:
            out.append(line)
            pending.append(line)
            if (sum(map(len, pending)) >= CHUNK_BYTES
                    or time.monotonic() - last >= CHUNK_EVERY_S):
                flush()
            if time.monotonic() - t0 > timeout_s:
                kill(); flush()
                return 124, "".join(out), f"TIMEOUT after {timeout_s}s", time.monotonic() - t0
        p.wait(timeout=max(1, int(timeout_s - (time.monotonic() - t0))))
    except subprocess.TimeoutExpired:
        kill(); flush()
        return 124, "".join(out), f"TIMEOUT after {timeout_s}s", time.monotonic() - t0
    finally:
        flush()
    return p.returncode, "".join(out), "", time.monotonic() - t0
