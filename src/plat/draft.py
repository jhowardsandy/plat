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
import uuid
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


def _cycles(lots: list[dict]) -> list[list[str]]:
    """Dependency cycles, as the loop you have to break.

    Now that `depends_on` actually gates a lot, a cycle is a deadlock rather than
    a curiosity: every lot in it waits forever on another member.
    """
    edges = {l.get("key"): [d for d in (l.get("depends_on") or [])] for l in lots}
    found, seen = [], set()
    def walk(key, trail):
        if key in trail:
            loop = trail[trail.index(key):] + [key]
            if tuple(sorted(set(loop))) not in seen:
                seen.add(tuple(sorted(set(loop))))
                found.append(loop)
            return
        for nxt in edges.get(key) or []:
            if nxt in edges:
                walk(nxt, trail + [key])
    for k in edges:
        walk(k, [])
    return found


def validate(spec: dict, root: Path) -> list[str]:
    from .spec import validate as check
    return check(spec, root)


def unverifiable(spec: dict) -> list[str]:
    """Criteria with no verify command — allowed, but worth seeing before you run."""
    return [c.get("id", "?") for c in (spec.get("criteria") or []) if not c.get("verify")]


def scout(db, cfg: Config, *, anchor: str, ticket: str, log=print) -> dict:
    """Cheap reconnaissance: which repos, is it specified, does it exist, is it
    measurable. Four facts, so a person can choose the shape before paying to plan
    it in detail."""
    from . import ingest
    repos = discover_repos(cfg.workspace_root)
    if not repos:
        raise DraftError(f"no git repositories under {cfg.workspace_root}")
    out = _draft_dir(anchor)
    prompt = ((P.PACKS / "scout.md").read_text()
              .replace("{ticket}", ticket.strip() or "(no ticket text supplied)")
              .replace("{repos}", "\n".join(f"- {r}" for r in repos))
              .replace("{out_dir}", str(out)))
    pf = out / "scout-prompt.md"; pf.write_text(prompt)

    role = R.bind(cfg.roles(), "scout", 1)
    role.extra_dirs = [str(cfg.workspace_root)]
    log(f"  reconnaissance: {role.provider}/{role.model} ...")
    res = adapters.run(role, pf, out)
    log(f"    {res.duration_s:.0f}s  ${res.cost_usd:.2f}"
        f"{'~' if res.cost_estimated else ''}")
    if not res.ok or res.exit_code != 0 or res.permission_denials:
        raise DraftError(f"scout failed: {res.error or res.exit_code}; permission denials: {len(res.permission_denials)}")
    info = ingest.load_verdict(out, "scout")
    # A repo the scout named but that does not exist would send the whole run into
    # the wrong tree, so drop it here rather than at worktree time.
    info["repos"] = [r for r in info.get("repos") or []
                     if (cfg.workspace_root / r / ".git").exists()]
    return info


def run(db, cfg: Config, *, anchor: str, ticket: str, role_name: str = "planner",
        shape: str = "", log=print) -> tuple[Path, dict, list[str]]:
    repos = discover_repos(cfg.workspace_root)
    if not repos:
        raise DraftError(f"no git repositories found under {cfg.workspace_root} — "
                         f"check workspace_root in {home() / 'config.toml'}")
    log(f"  {len(repos)} repo(s) under {cfg.workspace_root}")

    out = _draft_dir(anchor)
    context = prior_context(db)
    if shape:
        # Chosen by a person who can see what a planner cannot — which repo is
        # mid-refactor, what ships Thursday. Stage 2 expands it rather than
        # re-deciding it.
        context += (f"\n## The shape already chosen\n\n{shape}\n\n"
                    "A person picked this knowing things you do not. Produce that "
                    "shape. If you believe it is wrong, produce it anyway and say "
                    "why in plan.md.\n")
    prompt = ((P.PACKS / "plan.md").read_text()
              .replace("{ticket}", ticket.strip() or "(no ticket text supplied)")
              .replace("{repos}", "\n".join(f"- {r}" for r in repos))
              .replace("{prior_art}", context)
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

    if not res.ok or res.exit_code != 0 or res.permission_denials:
        raise DraftError(f"planner failed: {res.error or res.exit_code}; permission denials: {len(res.permission_denials)}")
    f = out / "plat.yaml"
    if not f.exists():
        raise DraftError(
            f"the planner wrote no plat.yaml (see {out}). If the role uses "
            f"permission_mode: plan, that is why -- plan mode blocks every write, "
            f"including this one, and says nothing about it.")
    if not (out / "plan.md").is_file():
        raise DraftError(f"the planner wrote no plan.md reasoning (see {out})")
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


def _draft_dir(anchor: str) -> Path:
    from .spec import NAME
    if not NAME.fullmatch(anchor) or len(anchor) > 160:
        raise DraftError("anchor must contain only letters, digits, underscores and hyphens")
    out = home() / "drafts" / anchor / uuid.uuid4().hex
    out.mkdir(parents=True)
    return out
