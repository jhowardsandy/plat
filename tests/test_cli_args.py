"""Bare invocations should do the obvious thing, and a miss should explain itself."""
import inspect
import pytest
from plat import cli


def _default(param):
    """Unwrap typer's OptionInfo/ArgumentInfo to the value a bare call would get."""
    d = param.default
    return getattr(d, "default", d)


def _params(name):
    return inspect.signature(
        next(c.callback for c in cli.app.registered_commands
             if (c.name or c.callback.__name__) == name)).parameters


@pytest.mark.parametrize("cmd", ["status", "history", "show"])
def test_read_only_commands_run_with_no_arguments(cmd):
    """`plat show` erroring bare while `plat status` worked was just inconsistent."""
    for p in _params(cmd).values():
        assert p.default is not inspect.Parameter.empty, \
            f"{cmd}({p.name}) has no default, so a bare `plat {cmd}` fails"
        assert _default(p) is not ..., f"{cmd}({p.name}) is a required typer argument"


@pytest.mark.parametrize("cmd", ["start", "reopen", "show"])
def test_commands_resolve_through_the_shared_resolver(cmd):
    """_resolve lists what exists on a miss instead of raising NoResultFound at
    the user, and `.one()` anywhere would bring that traceback back."""
    src = inspect.getsource(
        next(c.callback for c in cli.app.registered_commands
             if (c.name or c.callback.__name__) == cmd))
    assert "_resolve(" in src
    assert ").one()" not in src, "a raw .one() surfaces a traceback instead of a message"


def test_status_watch_is_opt_in_and_reuses_the_same_query():
    """--watch must not become a second implementation of the table. A divergent
    live view is worse than none: it would disagree with `plat status` and with
    Grafana, and all three read the same view precisely so they cannot."""
    params = _params("status")
    assert _default(params["watch"]) is False, "watching must be opt-in"
    assert _default(params["interval"]) > 0
    import inspect
    from plat import cli
    watch_src = inspect.getsource(cli._watch)
    assert "_live_rows(" in watch_src and "_lots_table(" in watch_src
    status_src = inspect.getsource(cli.status)
    assert "_live_rows(" in status_src and "_lots_table(" in status_src


def test_in_flight_attempts_are_committed_before_the_agent_spawns():
    """flush() keeps the INSERT inside the transaction, so no other connection can
    see it until commit — which is AFTER the agent finishes. The monitor then shows
    no phase, no provider and no elapsed for the whole run, which is exactly the
    window it exists to cover."""
    import inspect
    from plat import runner
    for fn in (runner._run_agent, runner._run_gate):
        src = inspect.getsource(fn)
        add = src.index("db.add(a)")
        spawn = src.index("adapters.run(" if fn is runner._run_agent else "gates.run(")
        between = src[add:spawn]
        assert "db.commit()" in between, (
            f"{fn.__name__} must commit the attempt row before spawning, "
            "or the run is invisible to every other process")
        assert "db.flush()" not in between, "flush is not enough; it does not commit"
