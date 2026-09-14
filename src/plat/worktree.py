"""Plat POINTS at worktrees. It never houses them.

A worktree belongs to the repo it was cut from and lives under the workspace's
own .worktrees/ convention, alongside the ones you create by hand. Two homes for
worktrees depending on who made them is exactly the drift to avoid.
"""
from __future__ import annotations
import re, subprocess
from pathlib import Path
from .config import Config


def path_for(cfg: Config, anchor: str, lot_key: str) -> Path:
    return cfg.worktrees_root / anchor / lot_key


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, stdin=subprocess.DEVNULL)


def default_branch(repo: Path) -> str:
    r = _git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo)
    return r.stdout.strip().split("/")[-1] if r.returncode == 0 and r.stdout.strip() else "master"


def ensure(cfg: Config, anchor: str, lot_key: str, repo_rel: str,
           slug: str, base_ref: str | None = None) -> tuple[Path, str, str]:
    """Return (worktree_path, branch, base_sha), creating the worktree if absent.

    Always cut from a FRESHLY fetched origin/<default> -- a worktree based on a
    stale local ref is the quiet start of cross-repo drift. `base_ref` overrides
    that for a shadow run against a historical commit.
    """
    repo = cfg.workspace_root / repo_rel
    if not (repo / ".git").exists():
        raise FileNotFoundError(f"no git repo at {repo}")
    wt = path_for(cfg, anchor, lot_key)
    # Ticketed work is dev-<ticket>-<slug>; unticketed already carries its own
    # <prefix>_<name> and must not be wrapped in a fake ticket shape.
    ticketed = re.fullmatch(r"[A-Z]+-\d+", anchor) is not None
    branch = f"dev-{anchor.lower()}-{slug}" if ticketed else anchor
    if not wt.exists():
        _git(["fetch", "origin", "--quiet"], repo)
        base = base_ref or f"origin/{default_branch(repo)}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        if base_ref:
            r = _git(["worktree", "add", "--detach", str(wt), base], repo)
        else:
            r = _git(["worktree", "add", "-b", branch, str(wt), base], repo)
        if r.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {r.stderr.strip()}")
    sha = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    return wt, branch, sha


def head(wt: Path) -> str:
    return _git(["rev-parse", "HEAD"], wt).stdout.strip()
