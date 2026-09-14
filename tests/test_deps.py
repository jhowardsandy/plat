"""Every module we import at runtime must be a declared dependency.

The root cause of `plat top` dying with ModuleNotFoundError in a fresh install:
the dependency was added to pyproject and the test suite ran through `uv run`
against the PROJECT venv, which already had it. The installed tool is a separate
environment and nobody looked at it.
"""
import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SRC = ROOT / "src" / "plat"


def _declared() -> set[str]:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    out = set()
    for spec in meta["project"]["dependencies"]:
        name = spec.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip()
        out.add(name.lower().replace("_", "-"))
    return out


def _imported() -> set[str]:
    mods = set()
    for f in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return {m for m in mods
            if m not in sys.stdlib_module_names and m not in ("plat", "__future__")}


def test_every_runtime_import_is_a_declared_dependency():
    declared, mapping = _declared(), packages_distributions()
    missing = []
    for mod in sorted(_imported()):
        dists = {d.lower().replace("_", "-") for d in mapping.get(mod, [])}
        if not dists:
            pytest.skip(f"{mod} not installed here; cannot map it to a distribution")
        if not (dists & declared):
            missing.append(f"{mod} (from {sorted(dists)})")
    assert not missing, (
        "imported but not declared in pyproject: " + ", ".join(missing)
        + " — this passes under `uv run` and breaks the installed tool")
