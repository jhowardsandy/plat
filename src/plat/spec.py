"""Validate executable input before touching a database, worktree, or agent."""
from __future__ import annotations

import math
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path

from .fsm import parse_objective

IMPLEMENTED_PHASES = frozenset({"code", "review", "docs", "quality"})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*\Z")


def normalized(spec: dict) -> dict:
    d = deepcopy(spec)
    lots = d.get("lots") or []
    for c in d.get("criteria") or []:
        c.setdefault("scope", "lot")
        if c["scope"] == "lot" and not c.get("lot") and len(lots) == 1:
            c["lot"] = lots[0]["key"]
    return d


def validate(spec, root: Path, phases=None, *, executable=False) -> list[str]:
    errors = []
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]

    def name(value, label, limit):
        if not isinstance(value, str) or len(value) > limit or not NAME.fullmatch(value):
            errors.append(f"{label}: use 1–{limit} letters, digits, underscores or hyphens, starting with a letter or digit")
            return False
        return True

    anchor_ok = name(spec.get("anchor"), "anchor", 160)
    if "slug" in spec:
        name(spec["slug"], "slug", 64)
    for field in ("title", "plat_map"):
        if field in spec and not isinstance(spec[field], str):
            errors.append(f"{field} must be text")
    for field in ("tickets", "related", "questions"):
        if field in spec and (not isinstance(spec[field], list)
                              or any(not isinstance(x, str) for x in spec[field])):
            errors.append(f"{field} must be a list of strings")
    if "needs_human" in spec and not isinstance(spec["needs_human"], bool):
        errors.append("needs_human must be boolean")
    if spec.get("needs_human") is True:
        return errors + (["needs_human: answer the planning questions before executing"] if executable else [])
    budget = spec.get("budget_usd", 0)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget < 0:
        errors.append("budget_usd must be a finite nonnegative number")
    selected = spec.get("phases", phases if phases is not None else ["code", "review"])
    if not isinstance(selected, list) or any(not isinstance(x, str) for x in selected):
        errors.append("phases must be a list of names")
        selected = []
    if "code" not in selected:
        errors.append("phases must include code; use plat review for standalone review")
    if len(set(selected)) != len(selected):
        errors.append("phases must not contain duplicates")
    unsupported = set(selected) - IMPLEMENTED_PHASES
    if unsupported:
        errors.append(f"unsupported phases: {', '.join(sorted(unsupported))}; implemented: code, review, docs, quality")
    lots = spec.get("lots")
    if not isinstance(lots, list) or not lots:
        return errors + ["no lots, and needs_human is not set — supply a nonempty list"]
    if any(not isinstance(l, dict) for l in lots):
        return errors + ["every lot must be a mapping"]
    keys = [l.get("key") for l in lots if isinstance(l.get("key"), str)]
    for key, count in Counter(keys).items():
        if count > 1:
            errors.append(f"duplicate lot key: {key}")
    root = root.resolve()
    edges = {}
    for i, lot in enumerate(lots):
        label = f"lots[{i}] ({lot.get('key', '?')})"
        key_ok = name(lot.get("key"), f"{label}.key", 64)
        repo = lot.get("repo")
        if not isinstance(repo, str) or not repo:
            errors.append(f"{label}: repo is missing or not text")
        elif "\0" in repo or "\n" in repo:
            errors.append(f"{label}: unsafe repo path")
        else:
            path = Path(repo)
            if path.is_absolute() or ".." in path.parts or not (root / path).resolve().is_relative_to(root) or (path.parts and path.parts[0] == ".worktrees"):
                errors.append(f"{label}: repo must stay inside the workspace and outside .worktrees")
            elif not ((root / path) / ".git").exists():
                errors.append(f"{label}: repo {repo!r} does not exist under the workspace")
        if anchor_ok and key_ok:
            target = root / ".worktrees" / spec["anchor"] / lot["key"]
            if (not (root / ".worktrees").resolve().is_relative_to(root)
                    or not target.resolve().is_relative_to((root / ".worktrees").resolve())):
                errors.append(f"{label}: worktree path escapes .worktrees")
        if "plan" in lot and not isinstance(lot["plan"], str):
            errors.append(f"{label}.plan must be text")
        if lot.get("kind", "development") not in ("development", "investigation"):
            errors.append(f"{label}: kind must be development or investigation")
        ref = lot.get("base_ref")
        if ref is not None and (not isinstance(ref, str) or not REF.fullmatch(ref) or ".." in ref or "//" in ref):
            errors.append(f"{label}: unsafe base_ref")
        gate = lot.get("gate")
        if not isinstance(gate, dict):
            errors.append(f"{label}: gate must be a mapping")
            gate = {}
        for cmd in ("setup", "test"):
            if not isinstance(gate.get(cmd), str) or not gate[cmd].strip():
                errors.append(f"{label}: gate.{cmd} is missing — give an explicit command")
        for cmd in ("probe",):
            if cmd in gate and (not isinstance(gate[cmd], str) or not gate[cmd].strip()):
                errors.append(f"{label}: gate.{cmd} must be a nonempty command")
        deps = lot.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            errors.append(f"{label}: depends_on must be a list of lot keys")
            deps = []
        for dep in deps:
            if dep not in keys:
                errors.append(f"{label}: depends_on {dep!r} is not a lot in this plat")
            if dep == lot.get("key"):
                errors.append(f"{label}: depends_on itself")
        if key_ok:
            edges[lot["key"]] = deps
        mode = lot.get("mode", "attempt")
        if mode not in ("attempt", "converge"):
            errors.append(f"{label}: unknown mode {mode!r}")
        if mode == "converge":
            try:
                _, value = parse_objective(lot.get("objective", ""))
                if not math.isfinite(value):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                errors.append(f"{label}: converge needs a finite objective, e.g. '>= 80'")
            if not gate.get("probe"):
                errors.append(f"{label}: converge needs gate.probe")
        for field in ("patience", "max_iterations"):
            if field in lot and (type(lot[field]) is not int or lot[field] < 1):
                errors.append(f"{label}: {field} must be a positive integer")
    remaining = dict(edges)
    while remaining:
        ready = {k for k, deps in remaining.items() if not (set(deps) & remaining.keys())}
        if not ready:
            errors.append("dependency cycle involving: " + ", ".join(sorted(remaining)))
            break
        remaining = {k: v for k, v in remaining.items() if k not in ready}
    criteria = spec.get("criteria", [])
    if not isinstance(criteria, list) or any(not isinstance(c, dict) for c in criteria):
        return errors + ["criteria must be a list of mappings"]
    ids = []
    assigned = set()
    for i, criterion in enumerate(criteria):
        label = f"criteria[{i}]"
        if name(criterion.get("id"), f"{label}.id", 16):
            ids.append(criterion["id"])
        if not isinstance(criterion.get("statement"), str) or not criterion["statement"].strip():
            errors.append(f"{label}: statement is missing")
        verify = criterion.get("verify")
        if verify is not None and (not isinstance(verify, str) or not verify.strip()):
            errors.append(f"{label}: verify must be a nonempty command")
        scope = criterion.get("scope", "lot")
        lot_key = criterion.get("lot")
        if scope == "run":
            if lot_key is not None:
                errors.append(f"{label}: run criteria use verify_in, not lot")
            if not verify or criterion.get("verify_in") not in keys:
                errors.append(f"{label}: run criterion needs verify and verify_in naming a lot worktree")
        elif scope == "lot":
            if lot_key is None and len(lots) == 1:
                lot_key = keys[0] if keys else None
            if not isinstance(lot_key, str) or lot_key not in keys:
                errors.append(f"{label}: lot {lot_key!r} is not a lot in this plat; assign it explicitly")
            else:
                assigned.add(lot_key)
            if "review" not in selected and not verify:
                errors.append(f"{label}: without review every lot criterion needs verify")
        else:
            errors.append(f"{label}: scope must be lot or run")
    for ac_id, count in Counter(ids).items():
        if count > 1:
            errors.append(f"duplicate criterion id: {ac_id}")
    if executable:
        for key in set(keys) - assigned:
            errors.append(f"lot {key}: declare at least one owned acceptance criterion")
    return errors
