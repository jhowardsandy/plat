"""Plat configuration. Everything machine-specific lives here, nothing is hardcoded."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
import yaml

DEFAULT_WORKSPACE = Path.home() / "development" / "mediciland" / "source"


@dataclass(frozen=True)
class Config:
    workspace_root: Path
    database_url: str
    broker_url: str
    roles_path: Path
    vault_root: Path | None
    origin_id: str          # per-install UUID. keeps findings mergeable later.
    stale_after_s: int = 600
    max_attempts: int = 3

    @property
    def worktrees_root(self) -> Path:
        return self.workspace_root / ".worktrees"

    def roles(self) -> dict:
        return yaml.safe_load(self.roles_path.read_text())


def load() -> Config:
    home = Path(os.environ.get("PLAT_HOME", Path.home() / ".plat"))
    home.mkdir(parents=True, exist_ok=True)
    origin_file = home / "origin_id"
    if not origin_file.exists():
        import uuid
        origin_file.write_text(str(uuid.uuid4()))
    return Config(
        workspace_root=Path(os.environ.get("PLAT_WORKSPACE", DEFAULT_WORKSPACE)),
        database_url=os.environ.get("PLAT_DATABASE_URL",
            "postgresql+psycopg://postgres:mlgdev@localhost:5432/plat"),
        broker_url=os.environ.get("PLAT_BROKER_URL", "redis://localhost:6379/2"),
        roles_path=Path(os.environ.get("PLAT_ROLES", home / "roles.yaml")),
        vault_root=Path(os.environ["PLAT_VAULT"]) if "PLAT_VAULT" in os.environ else None,
        origin_id=origin_file.read_text().strip(),
    )
