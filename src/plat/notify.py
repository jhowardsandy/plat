"""Tell the operator when Plat needs them. Never tell anyone else.

Plat's most expensive moment is a lot that stopped for a human while nobody was
looking. A run that blocks at minute 20 of an hour sits idle for forty minutes,
and that latency is what makes a human gate feel costly rather than safe.

The line this module will not cross: it notifies **you**. It does not announce to
a team channel, open a pull request or comment on a ticket. Those speak to other
people in your name and are a deliberate act, not a side effect of a run. Plat can
draft them; a human sends them.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# Events worth interrupting someone for. Deliberately few: a notifier that fires
# on everything is one people mute, and a muted notifier is worse than none.
KINDS = ("blocked", "closed", "budget", "review_findings")


@dataclass
class Event:
    kind: str
    title: str
    detail: str = ""
    anchor: str = ""
    lot: str = ""
    urgent: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        where = f"{self.anchor}/{self.lot}" if self.lot else self.anchor
        return f"[plat] {where}: {self.title}" if where else f"[plat] {self.title}"


# ------------------------------------------------------------------ handlers

def _desktop(ev: Event, cfg: dict) -> None:
    """macOS notification. Free, no setup, covers the single-operator case."""
    if shutil.which("terminal-notifier"):
        cmd = ["terminal-notifier", "-title", "plat", "-subtitle",
               f"{ev.anchor}/{ev.lot}".strip("/"), "-message", ev.title]
        if ev.urgent:
            cmd += ["-sound", "Basso"]
        subprocess.run(cmd, capture_output=True)
        return
    if shutil.which("osascript"):
        script = (f'display notification {json.dumps(ev.title)} '
                  f'with title "plat" subtitle {json.dumps(f"{ev.anchor}/{ev.lot}".strip("/"))}')
        subprocess.run(["osascript", "-e", script], capture_output=True)


def _webhook(ev: Event, cfg: dict) -> None:
    """A Slack (or any) incoming webhook.

    A webhook posts OUTWARD only and needs nothing listening, which is why it
    suits a local-only tool. Point it at a DM or a personal channel: see the
    module docstring about not speaking to other people.
    """
    url = cfg.get("url")
    if not url:
        return
    emoji = {"blocked": ":raised_hand:", "closed": ":white_check_mark:",
             "budget": ":moneybag:", "review_findings": ":mag:"}.get(ev.kind, ":robot_face:")
    text = f"{emoji} *{ev.line()}*"
    if ev.detail:
        text += f"\n{ev.detail}"
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except (urllib.error.URLError, OSError):
        pass          # a notification that fails must never take down a run


def _exec(ev: Event, cfg: dict) -> None:
    """Escape hatch: run any command with the event on stdin as JSON.

    Whatever you use that is not a webhook -- a Slack bot token, ntfy, a pager --
    plugs in here without Plat growing a client for it.
    """
    cmd = cfg.get("cmd")
    if not cmd:
        return
    try:
        subprocess.run(cmd, shell=True, input=json.dumps(ev.__dict__),
                       text=True, capture_output=True, timeout=20)
    except (subprocess.SubprocessError, OSError):
        pass


HANDLERS = {"desktop": _desktop, "webhook": _webhook, "exec": _exec}


def send(ev: Event, cfg: dict | None) -> list[str]:
    """Dispatch one event. Returns the handlers that ran. Never raises."""
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return []
    if ev.kind not in set(cfg.get("on", KINDS)):
        return []
    ran = []
    for name in cfg.get("handlers", ["desktop"]):
        fn = HANDLERS.get(name)
        if fn is None:
            continue
        try:
            fn(ev, cfg.get(name, {}) or {})
            ran.append(name)
        except Exception:
            pass      # one broken handler must not stop the others
    return ran
