from __future__ import annotations

import json
from pathlib import Path

from .base import AgentResult, Role, spawn

NAME = "codex"

# Codex's reasoning scale is coarser than Claude's; xhigh and max both land on
# high rather than being silently dropped.
EFFORT = {"low": "low", "medium": "medium", "high": "high",
          "xhigh": "high", "max": "high"}

# Codex emits token counts but NEVER a dollar figure. Cost is therefore an
# ESTIMATE -- recorded as such, so a budget ceiling is never enforced against a
# number that merely looks authoritative. USD per 1M tokens.
RATES = {"gpt-5.1-codex-max": (1.25, 10.00), "_default": (1.25, 10.00)}


def build(role: Role, prompt_file: Path, cwd: Path) -> list[str]:
    head = ["codex", "exec"]
    if role.session_id:
        head += ["resume", role.session_id]
    cmd = [*head, prompt_file.read_text(), "--json",
           "-c", f'sandbox_mode="{role.sandbox}"']
    if role.sandbox == "workspace-write":
        # The reviewer reads the repo but may write ONLY its verdict. A read-only
        # sandbox cannot write at all -- it silently produces no verdict file --
        # so confinement is expressed as a writable root, not as read-only.
        out = str(prompt_file.parent).replace("\\", "/")
        cmd += ["-c", f'sandbox_workspace_write.writable_roots=["{out}"]']
    # "default" means: let the CLI pick. Which models an account can reach varies,
    # and a hardcoded name fails with a 400 four seconds in.
    if role.model and role.model != "default":
        cmd += ["-m", role.model]
    if role.effort:
        eff = EFFORT.get(role.effort)
        if eff:
            cmd += ["-c", f'model_reasoning_effort="{eff}"']
    return cmd


def run(role: Role, prompt_file: Path, cwd: Path, on_chunk=None,
        on_pid=None) -> AgentResult:
    rc, out, err, dt = spawn(build(role, prompt_file, cwd), cwd,
                             role.timeout_s, on_chunk=on_chunk, on_pid=on_pid)
    res = AgentResult(ok=(rc == 0), exit_code=rc, duration_s=dt, stdout=out, stderr=err,
                      cost_estimated=True)
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "thread.started":
            res.session_id = d.get("thread_id")
        elif d.get("type") == "turn.completed":
            u = d.get("usage") or {}
            res.tokens_in += int(u.get("input_tokens", 0))
            res.tokens_out += int(u.get("output_tokens", 0))
        elif d.get("type") == "turn.failed":
            res.ok = False
            res.error = str(d.get("error"))[:400]
    rate_in, rate_out = RATES.get(role.model, RATES["_default"])
    res.cost_usd = (res.tokens_in / 1e6) * rate_in + (res.tokens_out / 1e6) * rate_out
    return res


def narrate(chunk: str) -> list[str]:
    """Readable lines from codex's event stream."""
    out: list[str] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = ev.get("item") or {}
        kind = item.get("type") or ev.get("type")
        if kind == "agent_message" and item.get("text", "").strip():
            out.append(item["text"].strip())
        elif kind == "command_execution":
            out.append(f"  $ {str(item.get('command', ''))[:110]}")
        elif kind == "error":
            out.append(f"  !! {str(item.get('message') or ev.get('message'))[:140]}")
        elif ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            out.append(f"— turn complete ({u.get('output_tokens', 0)} output tokens)")
        elif ev.get("type") == "turn.failed":
            out.append(f"  !! turn failed: {str(ev.get('error'))[:140]}")
    return out
