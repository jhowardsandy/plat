"""Configuration. Nothing about one machine or one organisation is baked in.

Resolution order, first hit wins:
    1. environment  (PLAT_DATABASE_URL, PLAT_WORKSPACE, ...)
    2. ~/.plat/config.toml, written by `plat init` on first run
    3. the shipped defaults below

The config file exists so that a working install is captured somewhere you can
read and edit, rather than living in whichever shell happened to export the right
variables.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULTS = {
    # A plain local Postgres. `docker compose -f docker-compose.plat.yml up -d`
    # brings up exactly this if you do not already have one.
    "database_url": "postgresql+psycopg://plat:plat@localhost:5432/plat",
    "broker_url": "redis://localhost:6379/2",
    # Where your repos live. Plat POINTS at worktrees under
    # <workspace_root>/.worktrees/ ; it never creates or owns that directory.
    "workspace_root": str(Path.home() / "src"),
    "stale_after_s": 600,
    "max_attempts": 3,
    # Default phases for a plat that does not name its own.
    "phases": ["code", "review"],
    # Notify YOU when a run needs you. Plat never announces to other people.
    "notify": {"enabled": True, "handlers": ["desktop"],
               "on": ["blocked", "delivered", "budget"]},
    # How Plat asks your issue tracker what a ticket says. Empty = it does not ask.
    "tracker": {},
}

CONFIG_TEMPLATE = '''# plat configuration -- environment variables override every value here.

# Postgres. `plat init` creates the schema; it does not create the database.
database_url = "{database_url}"

# Redis, for the Celery dispatcher (v1). Unused by the inline runner.
broker_url = "{broker_url}"

# The root your repositories live under. Plat resolves worktrees to
# <workspace_root>/.worktrees/<ANCHOR>/<lot>/ and never owns that directory.
workspace_root = "{workspace_root}"

# A lot whose heartbeat has been cold longer than this is flagged stale --
# but only as a FLOOR: the real threshold is relative to how long that phase
# usually takes in that repo. See v_live_lots.
stale_after_s = {stale_after_s}

# Code -> review -> code cycles before a lot stops and asks for a human.
max_attempts = {max_attempts}

# Which phases a plat runs when its own spec does not say. The ORDER is fixed --
# code, review, docs, quality -- because it is semantic, not a preference: you
# cannot review before you code. You choose which of them run.
#   docs needs a gemini adapter, which is not implemented yet.
phases = {phases}

# Plat interrupts YOU when a run needs you, and never announces to anyone else --
# a pull request, a ticket transition or a team-channel post speaks in your name
# and stays a deliberate act.
#
#   handlers: desktop | webhook | exec
#   on:       blocked | delivered | closed | budget | review_findings
[notify]
enabled  = true
handlers = ["desktop"]
on       = ["blocked", "delivered", "budget"]

# [notify.webhook]                     # posts outward only, nothing to run
# url = "https://hooks.slack.com/services/..."   # point at a DM or your own channel

# [notify.exec]                        # anything else: bot token, ntfy, pager.
# cmd = "my-notifier"                  # the event arrives on stdin as JSON

# How Plat asks your issue tracker what a ticket actually says. It stays
# tracker-agnostic: give it a command that takes {anchor} and prints
#   {"key": "...", "status": "...", "summary": "...", "url": "..."}
# on stdout. Without it Plat knows its own lifecycle and nothing about the ticket's
# -- which is how an invented anchor once collided with someone else's live work.
# [tracker]
# cmd = "my-ticket-lookup {anchor}"
'''


def template_blocks() -> dict[str, str]:
    """Split the shipped template into addressable blocks, keyed by what each
    defines, with its explanatory comments attached.

    This is what makes backfilling possible: a config written before a setting
    existed can gain that setting AND the paragraph explaining it, without
    touching anything already there.
    """
    blocks: dict[str, str] = {}
    buf: list[str] = []
    table: str | None = None          # once inside [notify], its keys are ITS keys
    for line in CONFIG_TEMPLATE.splitlines():
        s = line.strip()
        is_table = s.startswith("[") and s.endswith("]") and not s.startswith("#")
        if is_table and table:        # a new table closes the previous one
            blocks[table] = "\n".join(buf).strip("\n")
            buf = []
        buf.append(line)
        if is_table:
            table = s[1:-1].split(".")[0]
            continue
        if table:
            continue                  # belongs to the open table, not top level
        if "=" in s and not s.startswith("#"):
            blocks[s.split("=", 1)[0].strip()] = "\n".join(buf).strip("\n")
            buf = []
    if table:
        blocks[table] = "\n".join(buf).strip("\n")
    return blocks


def backfill(path: Path | None = None) -> list[str]:
    """Add settings the file predates. Never touches what is already there.

    write_default_config only writes when the file is ABSENT, so every setting
    added afterwards was invisible: a config could have a desktop notifier armed
    and no mention of it, which makes "what is actually running" unanswerable by
    reading the file.
    """
    p = path or config_path()
    if not p.exists():
        write_default_config()
        return ["(created)"]
    # read from the file being backfilled, not the global one: a path argument
    # that is quietly ignored works right up until someone passes a different path
    have = _file_values(p)
    body = p.read_text().rstrip("\n")
    scalars, tables, added = [], [], []
    for key, block in template_blocks().items():
        if key in have:
            continue
        added.append(key)
        (tables if block.lstrip().startswith("[") or "\n[" in block
         else scalars).append(_fill(block))
    if not added:
        return []

    # A bare `key = value` appended after an existing [table] header becomes a
    # key OF THAT TABLE -- phases once landed as tracker.phases. Scalars must go
    # in before the first table; only tables are safe to append.
    lines = body.splitlines()
    first_table = next((i for i, ln in enumerate(lines)
                        if re.match(r"^\s*\[[^#]", ln)), len(lines))
    # rewind past the comment paragraph that introduces that table
    while first_table > 0 and lines[first_table - 1].lstrip().startswith("#"):
        first_table -= 1
    head, tail = lines[:first_table], lines[first_table:]
    if scalars:
        head = head + [""] + "\n\n".join(scalars).splitlines()
    out = "\n".join(head + ([""] if tail and head else []) + tail).rstrip("\n")
    if tables:
        out += "\n\n" + "\n\n".join(tables)
    p.write_text(out + "\n")
    return added


def _fill(block: str) -> str:
    for k, v in DEFAULTS.items():
        block = block.replace("{" + k + "}", json.dumps(v) if isinstance(v, list) else str(v))
    return block


@dataclass(frozen=True)
class Config:
    workspace_root: Path
    database_url: str
    broker_url: str
    roles_path: Path
    origin_id: str
    notify: dict = field(default_factory=dict)
    tracker: dict = field(default_factory=dict)          # per-install uuid; keeps findings mergeable across installs
    stale_after_s: int = 600
    max_attempts: int = 3
    phases: list = field(default_factory=lambda: ["code", "review"])
    # Where each value came from: "env", "config.toml", or "default". Without it
    # you cannot answer "why is this happening" from the file alone.
    sources: dict = field(default_factory=dict)

    @property
    def worktrees_root(self) -> Path:
        return self.workspace_root / ".worktrees"

    def roles(self) -> dict:
        return yaml.safe_load(self.roles_path.read_text())


def home() -> Path:
    p = Path(os.environ.get("PLAT_HOME", Path.home() / ".plat"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return home() / "config.toml"


def write_default_config(**overrides) -> Path:
    """Write config.toml if absent. Never overwrites an existing one."""
    p = config_path()
    if not p.exists():
        # Substitute by replace, not .format(): the template documents a tracker
        # command containing a literal {anchor}, and .format() tried to resolve it.
        body = CONFIG_TEMPLATE
        for k, v in {**DEFAULTS, **overrides}.items():
            body = body.replace("{" + k + "}", str(v))
        p.write_text(body)
    return p


def _file_values(p: Path | None = None) -> dict:
    p = p or config_path()
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text())
    except Exception:
        return {}


def load() -> Config:
    h = home()
    f = _file_values()

    src: dict[str, str] = {}

    def pick(key: str, env: str):
        if os.environ.get(env):
            src[key] = f"env {env}"
            return os.environ[env]
        if f.get(key) is not None:
            src[key] = "config.toml"
            return f[key]
        src[key] = "default"
        return DEFAULTS[key]

    origin = h / "origin_id"
    if not origin.exists():
        origin.write_text(str(uuid.uuid4()))

    return Config(
        workspace_root=Path(pick("workspace_root", "PLAT_WORKSPACE")).expanduser(),
        database_url=str(pick("database_url", "PLAT_DATABASE_URL")),
        broker_url=str(pick("broker_url", "PLAT_BROKER_URL")),
        roles_path=Path(os.environ.get("PLAT_ROLES", h / "roles.yaml")),
        origin_id=origin.read_text().strip(),
        notify=_pick_table(f, src, "notify"),
        tracker=_pick_table(f, src, "tracker", {}),
        phases=list(pick("phases", "PLAT_PHASES")),
        stale_after_s=int(pick("stale_after_s", "PLAT_STALE_AFTER_S")),
        max_attempts=int(pick("max_attempts", "PLAT_MAX_ATTEMPTS")),
        sources=src,
    )


def _pick_table(f: dict, src: dict, key: str, fallback=None):
    if f.get(key):
        src[key] = "config.toml"
        return f[key]
    src[key] = "default"
    return DEFAULTS.get(key, fallback if fallback is not None else {})


def redact(dsn: str) -> str:
    """Hide the password so `plat config` can be pasted into an issue."""
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", dsn)
