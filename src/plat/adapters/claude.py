from __future__ import annotations

import json
from pathlib import Path

from .base import AgentResult, Role, spawn

NAME = "claude"


def build(role: Role, prompt_file: Path, cwd: Path) -> list[str]:
    cmd = [
        "claude", "-p", prompt_file.read_text(),
        "--model", role.model,
        # stream-json, not json: `json` buffers the entire run and emits one
        # object at exit, so there is nothing to tail for the hour that matters.
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", role.permission_mode,
        "--add-dir", str(cwd),
    ]
    for d in role.extra_dirs:
        cmd += ["--add-dir", str(d)]
    if role.effort:
        cmd += ["--effort", role.effort]
    if role.session_id:
        cmd += ["--resume", role.session_id]
    # --allowedTools is VARIADIC, so it goes last. A following flag happens to
    # terminate it today, but ordering a variadic anywhere but the end is the
    # kind of thing that breaks quietly when an argument stops looking like one.
    if role.allowed_tools:
        cmd += ["--allowedTools", *role.allowed_tools]
    return cmd


def run(role: Role, prompt_file: Path, cwd: Path, on_chunk=None) -> AgentResult:
    rc, out, err, dt = spawn(build(role, prompt_file, cwd), cwd,
                             role.timeout_s, on_chunk=on_chunk)
    res = AgentResult(ok=(rc == 0), exit_code=rc, duration_s=dt, stdout=out, stderr=err)
    d = None
    for line in out.splitlines():           # NDJSON; the result is the last event
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result" or "total_cost_usd" in ev:
            d = ev
    if d is None:
        res.ok = False
        res.error = "claude emitted no result event"
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


def narrate(chunk: str) -> list[str]:
    """Turn a slice of the stream into lines a person can follow.

    The raw stream is protocol, not narration: rate-limit envelopes, tool-call
    JSON, a 3KB result object. Storing that as the "live log" gave a pane nobody
    could read.
    """
    out: list[str] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    out.append(block["text"].strip())
                elif block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    hint = (inp.get("command") or inp.get("file_path")
                            or inp.get("pattern") or inp.get("description") or "")
                    out.append(f"  · {block.get('name')} {str(hint)[:100]}")
        elif kind == "result":
            out.append(f"— finished in {ev.get('duration_ms', 0) / 1000:.0f}s, "
                       f"${ev.get('total_cost_usd', 0):.2f}")
    return out
