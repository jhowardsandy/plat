from __future__ import annotations
from .adapters.base import Role


def bind(roles_cfg: dict, role_name: str, attempt: int,
         session_id: str | None = None) -> Role:
    """Resolve a role name to a concrete provider+model for THIS attempt.

    Escalation is the point: run the cheap model first, and spend the expensive
    one only on work that has already proven hard once.
    """
    defaults = roles_cfg.get("defaults", {}) or {}
    spec = dict((roles_cfg.get("roles", {}) or {}).get(role_name) or {})
    if not spec:
        raise KeyError(f"role {role_name!r} is not defined in roles.yaml")
    for key, override in (spec.get("escalate") or {}).items():
        if key == f"attempt_{attempt}":
            spec.update(override)
    spec.pop("escalate", None)
    resume = spec.pop("resume_session", False)
    spec.pop("queue", None)
    spec.pop("prompt_pack", None)
    return Role(
        name=role_name,
        provider=spec.pop("provider"),
        model=spec.pop("model"),
        permission_mode=spec.pop("permission_mode", "acceptEdits"),
        sandbox=spec.pop("sandbox", "workspace-write"),
        approval_mode=spec.pop("approval_mode", "auto_edit"),
        session_id=session_id if resume else None,
        timeout_s=int(spec.pop("timeout_s", defaults.get("timeout_s", 3600))),
        allowed_tools=spec.pop("allowed_tools", []) or [],
        extra=spec,
    )
