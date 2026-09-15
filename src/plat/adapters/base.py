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
from pathlib import Path
from typing import Any, Callable


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


def spawn(cmd: list[str], cwd: Path, timeout_s: int, env: dict | None = None,
          on_chunk: Callable[[str], None] | None = None) -> tuple[int, str, str, float]:
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
