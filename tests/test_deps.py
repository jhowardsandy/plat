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


def test_spawn_streams_chunks_while_the_process_runs(monkeypatch):
    """A behavioural test, not a source scan.

    The streaming rewrite was once silently no-applied by a failed string
    replace: the adapters passed on_chunk, spawn never accepted it, and the suite
    stayed green because nothing exercised the path. plat top's live log tail was
    dead for days as a result.
    """
    import sys
    from pathlib import Path

    from plat.adapters import base
    from plat.adapters.base import spawn

    # Drive the interval rather than the wall clock, so the test measures the
    # mechanism and not how fast this machine happens to be.
    monkeypatch.setattr(base, "CHUNK_EVERY_S", 0.3)
    got: list[str] = []
    script = ("import sys,time\n"
              "for i in range(3):\n"
              "    print('line', i, flush=True)\n"
              "    time.sleep(0.9)\n")
    rc, out, _err, _dt = spawn([sys.executable, "-c", script], Path.cwd(), 30,
                               on_chunk=got.append)
    assert rc == 0
    assert "line 0" in out and "line 2" in out
    assert got, "on_chunk was never called — nothing to tail"
    assert len(got) >= 2, f"output arrived in one lump ({len(got)}), so it did not stream"
    assert "line 0" in "".join(got)


def test_every_adapter_accepts_the_streaming_callback():
    import inspect

    from plat import adapters
    for name, mod in adapters.PROVIDERS.items():
        assert "on_chunk" in inspect.signature(mod.run).parameters, name


def test_the_test_dependencies_are_declared():
    """pytest, pytest-asyncio and ruff were installed by hand for the whole of
    development and declared nowhere, so CI could not run a single test."""
    import tomllib
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dev = " ".join(meta["project"].get("optional-dependencies", {}).get("dev", []))
    for pkg in ("pytest", "pytest-asyncio", "ruff"):
        assert pkg in dev, f"{pkg} is used but not declared in the dev extra"
