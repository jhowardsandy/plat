from __future__ import annotations
from pathlib import Path
from . import claude, codex
from .base import Role, AgentResult

PROVIDERS = {"claude": claude, "codex": codex}


def run(role: Role, prompt_file: Path, cwd: Path, on_chunk=None) -> AgentResult:
    mod = PROVIDERS.get(role.provider)
    if mod is None:
        raise ValueError(f"unknown provider {role.provider!r}; have {sorted(PROVIDERS)}")
    res = mod.run(role, prompt_file, cwd, on_chunk=on_chunk)
    res.provider = role.provider
    return res
