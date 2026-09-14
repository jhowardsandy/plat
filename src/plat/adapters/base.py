"""Uniform agent invocation. Same contract in, same result shape out.

This is the layer that makes providers swappable: the FSM knows role names,
roles.yaml maps a role to a provider, and only this package knows how to run one.
"""
from __future__ import annotations
import subprocess, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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


def spawn(cmd: list[str], cwd: Path, timeout_s: int, env: dict | None = None) -> tuple[int, str, str, float]:
    """Run an agent CLI to completion.

    stdin=DEVNULL is load-bearing: `codex exec` appends anything on stdin as a
    <stdin> block, so an inherited terminal stdin silently corrupts the prompt.
    start_new_session gives the child its own process group so an abort can
    signal the whole tree rather than orphaning the CLI underneath us.
    """
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=timeout_s,
            start_new_session=True, env=env,
        )
        return p.returncode, p.stdout, p.stderr, time.monotonic() - t0
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or ""), f"TIMEOUT after {timeout_s}s", time.monotonic() - t0
