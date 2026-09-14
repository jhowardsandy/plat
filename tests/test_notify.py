"""Notifications must be impossible to blame for a failed run."""
import pytest
from plat import notify
from plat.notify import Event, send


def ev(kind="blocked"):
    return Event(kind=kind, title="a lot needs you", anchor="DEV-1", lot="api")


def test_only_subscribed_kinds_fire():
    assert send(ev("blocked"), {"on": ["blocked"], "handlers": []}) == []
    calls = []
    notify.HANDLERS["_t"] = lambda e, c: calls.append(e)
    try:
        send(ev("closed"), {"on": ["blocked"], "handlers": ["_t"]})
        assert not calls, "an unsubscribed kind must not fire"
        send(ev("blocked"), {"on": ["blocked"], "handlers": ["_t"]})
        assert len(calls) == 1
    finally:
        notify.HANDLERS.pop("_t")


def test_disabled_means_silent():
    notify.HANDLERS["_t"] = lambda e, c: pytest.fail("fired while disabled")
    try:
        assert send(ev(), {"enabled": False, "handlers": ["_t"]}) == []
    finally:
        notify.HANDLERS.pop("_t")


def test_a_broken_handler_cannot_take_down_a_run():
    """This is the whole contract. A notifier is a courtesy; an exception from one
    must never surface where a lot's result should be."""
    ok = []
    notify.HANDLERS["_boom"] = lambda e, c: (_ for _ in ()).throw(RuntimeError("boom"))
    notify.HANDLERS["_ok"] = lambda e, c: ok.append(1)
    try:
        ran = send(ev(), {"handlers": ["_boom", "_ok"]})
        assert ok == [1], "a broken handler stopped the ones after it"
        assert ran == ["_ok"]
    finally:
        notify.HANDLERS.pop("_boom"); notify.HANDLERS.pop("_ok")


def test_unknown_handler_is_ignored_not_fatal():
    assert send(ev(), {"handlers": ["nope"]}) == []


def test_webhook_without_a_url_does_nothing():
    notify._webhook(ev(), {})          # must not raise or attempt a request


def test_the_runner_notifies_when_a_lot_blocks():
    import inspect
    from plat import runner
    src = inspect.getsource(runner.run_lot)
    assert "notify.send" in src and 'kind="budget" if "budget"' in src


def test_plat_never_announces_to_other_people():
    """Notify is for the operator. Posting to a team channel, opening a PR or
    transitioning a ticket speaks in the user's name and stays deliberate."""
    src = (notify.__doc__ or "") + open(notify.__file__).read()
    for forbidden in ("chat.postMessage", "pulls", "createPullRequest", "transition"):
        assert forbidden not in src, f"notify grew an outward-speaking path: {forbidden}"
