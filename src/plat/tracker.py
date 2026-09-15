"""What the issue tracker thinks. Plat does not know, and must not guess.

Plat tracks its own lifecycle — delivered, closed — and had no idea what the
TICKET said. That gap is not theoretical: an anchor invented for a plat turned out
to be a real, in-progress ticket belonging to someone else's work. Nothing was
written to it, but only by luck.

Plat stays tracker-agnostic. You give it a command that takes an anchor and prints
JSON; whether that is Jira, Linear, GitHub or a shell script reading a spreadsheet
is not Plat's business.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any

FIELDS = ("key", "status", "summary", "url")


def configured(cfg) -> bool:
    return bool((cfg.tracker or {}).get("cmd"))


def lookup(cfg, anchor: str, timeout: int = 45) -> dict[str, Any] | None:
    """Ask the tracker about one anchor. None means "could not tell", never "absent".

    A tracker that is unreachable must not read as a ticket that does not exist:
    the first would be a reason to stop and check, the second a reason to proceed.
    """
    cmd = (cfg.tracker or {}).get("cmd")
    if not cmd:
        return None
    try:
        p = subprocess.run(cmd.replace("{anchor}", anchor), shell=True,
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or not d.get("key"):
        return None
    return {k: d.get(k) for k in FIELDS}


def record(db, plat, info: dict | None) -> None:
    if not info:
        return
    plat.ticket_status = info.get("status")
    plat.ticket_summary = (info.get("summary") or "")[:400]
    plat.ticket_url = info.get("url")
    plat.ticket_checked_at = datetime.now(timezone.utc)
    db.commit()


def collides(info: dict | None, title: str) -> bool:
    """Does this anchor already name someone else's work?

    Deliberately crude: any existing ticket that is not already finished is worth
    stopping for. Plat cannot tell "the ticket I mean" from "a ticket that happens
    to have this key", so it asks rather than deciding.
    """
    if not info:
        return False
    status = (info.get("status") or "").lower()
    return status not in ("done", "closed", "resolved", "cancelled", "canceled")
