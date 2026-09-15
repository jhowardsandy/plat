"""`plat draft` — turn a ticket into a draft plat.yaml.

This exists because planning was the threshold problem. Writing a plat.yaml by
hand costs roughly fifteen minutes — read the ticket, find the repos, read enough
code to scope it, work out each gate command, phrase criteria that can actually be
checked — which is more than small work saves. Plat only paid off above a size.

What this removes is the typing and the reading. It does **not** remove the
judgement: it writes a draft you read, edit and confirm, and `plat plan` still
smoke-tests every gate before anything runs. A planner that ran its own plan would
be the one change that could make a bad decomposition expensive instead of cheap.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml
from sqlalchemy import text

from . import adapters
from . import prompts as P
from . import roles as R
from .config import Config, home


class DraftError(RuntimeError):
    pass


def discover_repos(root: Path, limit: int = 60) -> list[str]:
    """Every git repo under the workspace, as paths relative to it.

    The planner may only choose from this list. Left to invent paths it will
    produce plausible ones that do not exist, and the failure surfaces much later
    as a confusing worktree error.
    """
    out = []
    for git in sorted(root.glob("*/*/.git")) + sorted(root.glob("*/.git")):
        rel = git.parent.relative_to(root)
        if str(rel).startswith((".worktrees", ".")):
            continue
        out.append(str(rel))
    return sorted(set(out))[:limit]


def prior_context(db, limit: int = 10) -> str:
    rows = db.execute(text(
        "SELECT p.anchor, p.title, count(l.id) AS lots "
        "FROM plats p LEFT JOIN lots l ON l.plat_id = p.id "
        "WHERE p.kind = 'plat' GROUP BY p.id ORDER BY p.id DESC LIMIT :n"),
        {"n": limit}).mappings().all()
    if not rows:
        return ""
    lines = ["## How work here has been decomposed before", ""]
    for r in rows:
        lines.append(f"- **{r['anchor']}** ({r['lots']} lot(s)) — {r['title']}")
    lines.append("")
    return "\n".join(lines)


REQUIRED_LOT = ("key", "repo", "gate")


def validate(spec: dict, root: Path) -> list[str]:
    """Check the draft against what `plat plan` will actually require.

    Catching this here means the failure is a line you can edit, rather than a
    traceback twenty minutes later with a worktree half-created.
    """
    errs: list[str] = []
    if not spec.get("anchor"):
        errs.append("anchor is missing")
    if spec.get("needs_human"):
        return errs                      # an honest refusal is not a broken draft
    lots = spec.get("lots") or []
    if not lots:
        errs.append("no lots, and needs_human is not set — say which it is")
    keys = {l.get("key") for l in lots}
    for i, l in enumerate(lots):
        where = f"lots[{i}] ({l.get('key', '?')})"
        for k in REQUIRED_LOT:
            if not l.get(k):
                errs.append(f"{where}: {k} is missing")
        repo = l.get("repo")
        if repo and not (root / repo / ".git").exists():
            errs.append(f"{where}: repo {repo!r} does not exist under the workspace")
        g = l.get("gate") or {}
        if not g.get("test"):
            errs.append(f"{where}: gate.test is missing")
        if not g.get("setup"):
            errs.append(f"{where}: gate.setup is missing — a fresh worktree has no venv")
        for dep in l.get("depends_on") or []:
            if dep not in keys:
                errs.append(f"{where}: depends_on {dep!r} is not a lot in this plat")
        if l.get("mode") == "converge":
            if not l.get("objective"):
                errs.append(f"{where}: converge needs an objective")
            if not g.get("probe"):
                errs.append(f"{where}: converge needs gate.probe — nothing to steer by")
    for i, c in enumerate(spec.get("criteria") or []):
        if not c.get("statement"):
            errs.append(f"criteria[{i}]: statement is missing")
        if c.get("lot") and c["lot"] not in keys:
            errs.append(f"criteria[{i}]: lot {c['lot']!r} is not a lot in this plat")
    return errs


def unverifiable(spec: dict) -> list[str]:
    """Criteria with no verify command — allowed, but worth seeing before you run."""
    return [c.get("id", "?") for c in (spec.get("criteria") or []) if not c.get("verify")]


def run(db, cfg: Config, *, anchor: str, ticket: str, role_name: str = "planner",
        log=print) -> tuple[Path, dict, list[str]]:
    repos = discover_repos(cfg.workspace_root)
    if not repos:
        raise DraftError(f"no git repositories found under {cfg.workspace_root} — "
                         f"check workspace_root in {home() / 'config.toml'}")
    log(f"  {len(repos)} repo(s) under {cfg.workspace_root}")

    out = home() / "drafts" / anchor
    out.mkdir(parents=True, exist_ok=True)
    prompt = ((P.PACKS / "plan.md").read_text()
              .replace("{ticket}", ticket.strip() or "(no ticket text supplied)")
              .replace("{repos}", "\n".join(f"- {r}" for r in repos))
              .replace("{prior_art}", prior_context(db))
              .replace("{out_dir}", str(out))
              .replace("{anchor}", anchor))
    pf = out / "prompt.md"; pf.write_text(prompt)

    role = R.bind(cfg.roles(), role_name, 1)
    # cwd is the OUTPUT directory, not the workspace: the natural place to write
    # is then the one place writing is wanted, and the repos are added read-only.
    role.extra_dirs = [str(cfg.workspace_root)]
    log(f"  planner: {role.provider}/{role.model} ...")
    res = adapters.run(role, pf, out)
    log(f"    exit={res.exit_code} {res.duration_s:.0f}s "
        f"${res.cost_usd:.2f}{'~' if res.cost_estimated else ''}")

    f = out / "plat.yaml"
    if not f.exists():
        raise DraftError(
            f"the planner wrote no plat.yaml (see {out}). If the role uses "
            f"permission_mode: plan, that is why -- plan mode blocks every write, "
            f"including this one, and says nothing about it.")
    try:
        spec = yaml.safe_load(f.read_text())
    except yaml.YAMLError as e:
        raise DraftError(f"{f} is not valid YAML: {e}") from e
    if not isinstance(spec, dict):
        raise DraftError(f"{f} is not a mapping")

    # Observed, not trusted: a planner that edited a repo has done the work
    # instead of describing it, and the draft is no longer a plan.
    dirty = [l["repo"] for l in (spec.get("lots") or [])
             if l.get("repo") and _dirty(cfg.workspace_root / l["repo"])]
    if dirty:
        raise DraftError(f"the planner MODIFIED {', '.join(dirty)} — it was asked to "
                         f"plan, not to work. Discarding the draft; check those repos.")
    return f, spec, validate(spec, cfg.workspace_root)


def _dirty(repo: Path) -> bool:
    if not (repo / ".git").exists():
        return False
    r = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return bool(r.stdout.strip())
