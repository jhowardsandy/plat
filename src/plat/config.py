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

import os
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
        p.write_text(CONFIG_TEMPLATE.format(**{**DEFAULTS, **overrides}))
    return p


def _file_values() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text())
    except Exception:
        return {}


def load() -> Config:
    h = home()
    f = _file_values()

    def pick(key: str, env: str):
        return os.environ.get(env) or f.get(key) or DEFAULTS[key]

    origin = h / "origin_id"
    if not origin.exists():
        origin.write_text(str(uuid.uuid4()))

    return Config(
        workspace_root=Path(pick("workspace_root", "PLAT_WORKSPACE")).expanduser(),
        database_url=str(pick("database_url", "PLAT_DATABASE_URL")),
        broker_url=str(pick("broker_url", "PLAT_BROKER_URL")),
        roles_path=Path(os.environ.get("PLAT_ROLES", h / "roles.yaml")),
        origin_id=origin.read_text().strip(),
        notify=(f.get("notify") or DEFAULTS["notify"]),
        tracker=(f.get("tracker") or {}),
        stale_after_s=int(pick("stale_after_s", "PLAT_STALE_AFTER_S")),
        max_attempts=int(pick("max_attempts", "PLAT_MAX_ATTEMPTS")),
    )
