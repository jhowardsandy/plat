from __future__ import annotations
import json
from pathlib import Path
from .base import Role, AgentResult, spawn

NAME = "codex"

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
    # "default" means: let the CLI pick. Which models an account can reach varies,
    # and a hardcoded name fails with a 400 four seconds in.
    if role.model and role.model != "default":
        cmd += ["-m", role.model]
    return cmd


def run(role: Role, prompt_file: Path, cwd: Path) -> AgentResult:
    rc, out, err, dt = spawn(build(role, prompt_file, cwd), cwd, role.timeout_s)
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
