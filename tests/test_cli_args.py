"""Bare invocations should do the obvious thing, and a miss should explain itself."""
import inspect
import pytest
from plat import cli


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


@pytest.mark.parametrize("cmd", ["start", "reopen", "show"])
def test_commands_resolve_through_the_shared_resolver(cmd):
    """_resolve lists what exists on a miss instead of raising NoResultFound at
    the user, and `.one()` anywhere would bring that traceback back."""
    src = inspect.getsource(
        next(c.callback for c in cli.app.registered_commands
             if (c.name or c.callback.__name__) == cmd))
    assert "_resolve(" in src
    assert ").one()" not in src, "a raw .one() surfaces a traceback instead of a message"
