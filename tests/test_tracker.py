"""Plat's lifecycle and the ticket's are different facts; neither implies the other."""
import json
from types import SimpleNamespace

import pytest

from plat import tracker


def cfg(cmd):
    return SimpleNamespace(tracker={"cmd": cmd} if cmd else {})


def test_no_tracker_configured_is_silence_not_an_error():
    assert tracker.lookup(cfg(None), "DEV-1") is None
    assert tracker.configured(cfg(None)) is False


def test_a_good_lookup_returns_the_four_fields():
    payload = json.dumps({"key": "DEV-1", "status": "In Progress",
                          "summary": "s", "url": "u", "extra": "ignored"})
    got = tracker.lookup(cfg(f"echo '{payload}'"), "DEV-1")
    assert got == {"key": "DEV-1", "status": "In Progress", "summary": "s", "url": "u"}


@pytest.mark.parametrize("cmd", [
    "exit 1",                       # tracker failed
    "echo ''",                      # said nothing
    "echo 'not json'",              # said nonsense
    "echo '{\"status\":\"x\"}'",    # no key
    "sleep 99",                     # hung
])
def test_an_unreachable_tracker_is_unknown_never_absent(cmd):
    """'Could not tell' and 'no such ticket' must not be the same answer: the first
    is a reason to stop and look, the second a reason to carry on."""
    assert tracker.lookup(cfg(cmd), "DEV-1", timeout=1) is None


def test_an_open_ticket_collides_and_a_finished_one_does_not():
    """An invented anchor once landed on a live in-progress ticket owned by someone
    else. Plat cannot tell the ticket you mean from one that shares a key."""
    assert tracker.collides({"key": "D-1", "status": "In Progress"}, "t")
    assert tracker.collides({"key": "D-1", "status": "To Do"}, "t")
    for finished in ("Done", "Closed", "Resolved", "Cancelled"):
        assert not tracker.collides({"key": "D-1", "status": finished}, "t")


def test_unknown_never_reads_as_a_collision():
    """A tracker that is down must not block planning."""
    assert tracker.collides(None, "t") is False


def test_plan_warns_before_squatting_on_an_existing_ticket():
    import inspect

    from plat import cli
    src = inspect.getsource(cli.plan)
    assert "_tr.collides(" in src and "already exists" in src
