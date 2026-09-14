from __future__ import annotations
import json
from pathlib import Path
from .base import Role, AgentResult, spawn

NAME = "claude"


def build(role: Role, prompt_file: Path, cwd: Path) -> list[str]:
    cmd = [
        "claude", "-p", prompt_file.read_text(),
        "--model", role.model,
        "--output-format", "json",
        "--permission-mode", role.permission_mode,
        "--add-dir", str(cwd),
    ]
    if role.allowed_tools:
        cmd += ["--allowedTools", *role.allowed_tools]
    if role.session_id:
        cmd += ["--resume", role.session_id]
    return cmd


def run(role: Role, prompt_file: Path, cwd: Path) -> AgentResult:
    rc, out, err, dt = spawn(build(role, prompt_file, cwd), cwd, role.timeout_s)
    res = AgentResult(ok=(rc == 0), exit_code=rc, duration_s=dt, stdout=out, stderr=err)
    try:
        d = json.loads(out)
    except Exception:
        res.ok = False
        res.error = "claude did not return parseable JSON"
        return res
    res.session_id = d.get("session_id")
    res.cost_usd = float(d.get("total_cost_usd") or 0.0)
    u = d.get("usage") or {}
    res.tokens_in = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
    res.tokens_out = int(u.get("output_tokens", 0))
    # the silent failure mode: the agent was blocked on a permission prompt and
    # "finished" having done nothing. Surface it loudly rather than trusting rc=0.
    res.permission_denials = d.get("permission_denials") or []
    if d.get("is_error"):
        res.ok = False
        res.error = f"claude reported is_error (subtype={d.get('subtype')})"
    return res
