from __future__ import annotations

from pathlib import Path

from . import claude, codex
from .base import AgentResult, Role

PROVIDERS = {"claude": claude, "codex": codex}


def narrate(provider: str, chunk: str) -> list[str]:
    """Protocol -> prose. A provider knows its own stream; nothing else does."""
    mod = PROVIDERS.get(provider)
    fn = getattr(mod, "narrate", None) if mod else None
    if fn is None:
        return [l for l in chunk.splitlines() if l.strip()]
    try:
        return fn(chunk)
    except Exception:
        return []


def run(role: Role, prompt_file: Path, cwd: Path, on_chunk=None) -> AgentResult:
    mod = PROVIDERS.get(role.provider)
    if mod is None:
        raise ValueError(f"unknown provider {role.provider!r}; have {sorted(PROVIDERS)}")
    res = mod.run(role, prompt_file, cwd, on_chunk=on_chunk)
    res.provider = role.provider
    return res
