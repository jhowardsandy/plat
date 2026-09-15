"""`plat setup` — a guided first run that discovers rather than interrogates.

A wizard that only asks questions is a form, and a form will happily record an
answer that cannot work: a model your account cannot reach, a workspace with no
repositories, a gate command for a package manager this repo does not use. Every
step here checks the world first and offers what is actually available.

Re-runnable. It reads your current settings, proposes them as defaults, and never
overwrites something you did not confirm.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

# Candidates worth probing per provider. Reachability varies by ACCOUNT, not just
# by install -- gpt-5.1-codex-max is real, and 400s four seconds in on a ChatGPT
# plan -- so the only honest way to build this list is to try them.
CANDIDATES = {
    "claude": ["sonnet", "opus", "haiku"],
    "codex": ["gpt-5.5", "gpt-5.1-codex-max", "gpt-5.1-codex"],
    "gemini": ["gemini-flash-latest", "gemini-2.5-pro"],
}

PRESETS = {
    "thrifty": {
        "label": "cheapest that still cross-reviews",
        "coder": ("sonnet", "medium"),
        "escalate": {"attempt_2": {"effort": "xhigh"}},
        "reviewer_effort": "medium",
    },
    "balanced": {
        "label": "cheap first pass, think harder on retry, change model last",
        "coder": ("sonnet", "medium"),
        "escalate": {"attempt_2": {"effort": "xhigh"},
                     "attempt_3": {"model": "opus", "effort": "high"}},
        "reviewer_effort": "high",
    },
    "thorough": {
        "label": "strongest model throughout; for work you cannot afford to redo",
        "coder": ("opus", "high"),
        "escalate": {"attempt_2": {"effort": "max"}},
        "reviewer_effort": "high",
    },
}


def installed() -> dict[str, bool]:
    return {p: shutil.which(p) is not None for p in CANDIDATES}


def reachable(provider: str, model: str, timeout: int = 90) -> bool:
    """Can this account actually use this model? One trivial call, a few cents.

    Cheaper than discovering it mid-plat, which is how we found that a model can
    be installed, spelled correctly, and still refused by the plan behind it.
    """
    try:
        if provider == "claude":
            r = subprocess.run(["claude", "-p", "ok", "--model", model,
                                "--output-format", "json"],
                               capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=timeout)
            return r.returncode == 0 and not json.loads(r.stdout or "{}").get("is_error")
        if provider == "codex":
            r = subprocess.run(["codex", "exec", "ok", "-m", model, "--json",
                                "--skip-git-repo-check"],
                               capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=timeout)
            # codex exits 0 on a failed turn, so the exit code proves nothing
            return "turn.completed" in r.stdout and "turn.failed" not in r.stdout
        if provider == "gemini":
            r = subprocess.run(["gemini", "-p", "ok", "-m", model, "-o", "json"],
                               capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=timeout)
            return r.returncode == 0
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return False
    return False


def discover_models(providers: list[str], log=print) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for prov in providers:
        found = []
        for m in CANDIDATES.get(prov, []):
            ok = reachable(prov, m)
            log(f"    {prov}/{m:<20} {'reachable' if ok else 'not available'}")
            if ok:
                found.append(m)
        out[prov] = found
    return out


def build_roles(base: dict, coder_provider: str, coder_model: str,
                reviewer_provider: str, reviewer_model: str,
                preset: str, phases: list[str]) -> dict:
    """Assemble roles.yaml. The coder and reviewer must differ where possible --
    a model is blind to its own failure modes and will rationalise its own code."""
    p = PRESETS[preset]
    roles = dict(base.get("roles") or {})
    roles.setdefault("coder", {})
    roles["coder"].update({"provider": coder_provider, "model": coder_model,
                           "effort": p["coder"][1], "resume_session": True,
                           "permission_mode": "acceptEdits",
                           "escalate": dict(p["escalate"])})
    if coder_model != p["coder"][0] and coder_provider == "claude":
        roles["coder"]["model"] = coder_model
    roles.setdefault("reviewer.correctness", {})
    roles["reviewer.correctness"].update({
        "provider": reviewer_provider, "model": reviewer_model,
        "effort": p["reviewer_effort"], "resume_session": False,
        "sandbox": "workspace-write"})
    return {**base, "roles": roles}


HEADER = """# Written by `plat setup`. Safe to edit by hand.
#
# Comments are not preserved when setup rewrites this file; the fully annotated
# reference — what every key means and why the defaults are what they are — ships
# with the package as roles.default.yaml. A backup of the previous file is kept
# alongside this one as roles.yaml.bak.
"""


def write_yaml(path: Path, data: dict) -> None:
    path.write_text(HEADER + yaml.safe_dump(data, sort_keys=False, width=100))
