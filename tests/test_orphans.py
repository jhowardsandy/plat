"""An agent outlives the session that started it.

Agents are spawned with start_new_session=True, which puts them in their own
process group -- so closing the terminal does not kill them. That is deliberate
(a SIGINT in your shell should not leave half-written files in a worktree), but
it has a consequence nobody designed for: a later session can pick up a lot whose
heartbeat went cold while an agent is still writing to it. Gating a moving target
produces a verdict about a tree that no longer exists.

So: record the pid, refuse to act on a lot that still has a live one, and give a
person one command to stop it.
"""
import inspect
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plat.adapters import base


@pytest.fixture
def child():
    """A real process in its own group, like an agent."""
    procs = []

    def start(script="import time; time.sleep(30)"):
        p = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
        procs.append(p)
        for _ in range(50):                      # wait for it to actually exist
            if base.process_alive(p.pid):
                break
            time.sleep(0.02)
        return p

    yield start
    for p in procs:
        base.terminate(p.pid, grace=0.5)
        p.wait(timeout=5)


def test_a_running_process_reads_as_alive(child):
    p = child()
    assert base.process_alive(p.pid) is True


def test_liveness_checks_identity_not_just_the_number(child):
    """PIDs get reused. Liveness alone would eventually point `plat abort` at a
    stranger's process group."""
    p = child()
    assert base.process_alive(p.pid, "definitely-not-this-binary") is False


def test_a_process_older_than_the_attempt_is_ours_whatever_ps_calls_it(child):
    """The name check alone is not enough: ps reports the RESOLVED executable, so
    a venv shim or wrapper reports a path that never contains the invoked name.
    Age is the corroboration -- and it is the half pid reuse cannot fake."""
    p = child()
    started = datetime.now(timezone.utc)
    time.sleep(1.1)
    assert base.process_alive(p.pid, "definitely-not-this-binary", since=started) is True


def test_a_process_younger_than_the_attempt_is_not_ours(child):
    """What a recycled pid looks like: our agent died, the number came back around,
    and the process wearing it now started AFTER the attempt row did."""
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    p = child()
    assert base.process_alive(p.pid, since=started) is False


def test_terminate_refuses_to_signal_a_group_it_cannot_identify(child):
    """The one place Plat reaches outside its own data. It does not get to be
    approximately right."""
    p = child()
    stranger = datetime.now(timezone.utc) - timedelta(hours=2)
    assert base.terminate(p.pid, since=stranger) is False
    assert base.process_alive(p.pid) is True          # untouched


@pytest.mark.parametrize("raw,secs", [
    ("05:07", 307), ("01:00:00", 3600), ("2-03:04:05", 183845), ("       12:00", 720)])
def test_ps_elapsed_time_parses(raw, secs):
    assert base._etime_seconds(raw) == secs


def test_unparseable_elapsed_time_does_not_answer_yes():
    """An unreadable ps falls back to the name check rather than inventing an age."""
    assert base._etime_seconds("nonsense") is None


def test_a_dead_process_reads_as_dead():
    assert base.process_alive(2 ** 22 - 1) is False
    assert base.process_alive(None) is False


def test_a_defunct_child_reads_as_dead(child):
    """ps still lists a zombie with its full command line. Counting one as alive
    would hang terminate()'s grace loop until it gave up and SIGKILLed a corpse."""
    p = child()
    base.terminate(p.pid, grace=0.5)
    time.sleep(0.2)                              # signalled, deliberately not reaped
    assert base.process_alive(p.pid) is False
    p.wait(timeout=5)


def test_terminate_stops_the_process(child):
    p = child()
    assert base.terminate(p.pid) is True
    p.wait(timeout=5)
    assert base.process_alive(p.pid) is False


def test_terminate_takes_the_whole_group_not_just_the_agent(child):
    """An agent starts test runners and package managers of its own. Killing only
    the parent leaves those running -- the exact problem, one level down."""
    src = inspect.getsource(base.terminate)
    assert "killpg" in src and "getpgid" in src
    assert "os.kill(" not in src


def test_terminate_escalates_to_sigkill():
    src = inspect.getsource(base.terminate)
    assert "SIGTERM" in src and "SIGKILL" in src
    assert src.index("SIGTERM") < src.index("SIGKILL")


def test_terminate_on_nothing_is_not_an_error():
    assert base.terminate(None) is False
    assert base.terminate(2 ** 22 - 1) is False


def test_spawn_reports_the_pid_before_it_reports_anything_else():
    """A session that dies mid-run still has to leave something to find the agent
    by, so the pid is handed back at spawn time -- not on completion."""
    seen = []
    rc, out, _, _ = base.spawn(
        [sys.executable, "-c", "print('hi')"], cwd=Path.cwd(), timeout_s=30,
        on_pid=lambda pid, cmd: seen.append((pid, cmd)))
    assert rc == 0
    assert len(seen) == 1
    pid, cmd = seen[0]
    assert isinstance(pid, int) and pid > 0
    assert cmd == sys.executable          # the identity the liveness check uses


def test_a_failing_on_pid_never_takes_the_run_down():
    """Bookkeeping is not worth an agent run."""
    def boom(pid, cmd):
        raise RuntimeError("no db")

    rc, out, _, _ = base.spawn([sys.executable, "-c", "print('ok')"], cwd=Path.cwd(),
                               timeout_s=30, on_pid=boom)
    assert rc == 0 and "ok" in out


def test_every_adapter_can_report_a_pid():
    """Missed on one provider, orphan detection silently does nothing there."""
    from plat import adapters
    for name, mod in adapters.PROVIDERS.items():
        assert "on_pid" in inspect.signature(mod.run).parameters, name


# --- the runner and the CLI ------------------------------------------------

def test_the_runner_records_the_pid_on_its_own_transaction():
    """The runner's session holds an open transaction for the whole agent run.
    A pid nobody else can read until the agent finishes tells nobody anything --
    which is the same bug that once made in-flight attempts invisible."""
    from plat import runner
    src = inspect.getsource(runner._run_agent)
    assert "on_pid=_pid" in src
    pid_fn = src[src.index("def _pid"):src.index("def _chunk")]
    assert "from .db import session" in pid_fn and "s2.commit()" in pid_fn


def test_a_finished_attempt_holds_no_pid():
    """Otherwise every completed lot looks like it has an orphan on it."""
    from plat import runner
    src = inspect.getsource(runner._run_agent)
    assert "a.pid = None" in src
    assert src.index("a.finished_at") < src.index("a.pid = None")


def test_the_runner_refuses_to_act_on_a_lot_with_a_live_agent():
    """The point of the whole exercise: do not gate a tree that is still moving."""
    from plat import runner
    src = inspect.getsource(runner.run_lot)
    head = src[:src.index("while True:")]
    assert "live_agent(db, lot)" in head
    assert "return lot.state" in head            # refuses, without changing state
    assert "plat abort" in head                  # and says what to do about it


def test_detection_looks_only_at_unfinished_attempts():
    from plat import runner
    src = inspect.getsource(runner.live_agent)
    assert "Attempt.finished_at.is_(None)" in src
    assert "Attempt.pid.isnot(None)" in src


def test_a_pid_that_is_no_longer_alive_gets_cleared():
    """An agent that was killed never got to clear its own pid. Leaving it set
    makes every later run re-check a dead process forever."""
    from plat import runner
    src = inspect.getsource(runner.live_agent)
    assert "a.pid = None" in src and "db.commit()" in src


def test_abort_is_recorded_as_the_humans_decision():
    from plat import cli
    src = inspect.getsource(cli.abort)
    assert 'actor="human"' in src and 'kind="override"' in src
    assert "LotState.ABORTED.value" in src
    assert "alternatives=" in src


def test_abort_kills_before_it_marks():
    """Marking a lot ABORTED while its agent keeps writing is the worst of both."""
    from plat import cli
    src = inspect.getsource(cli.abort)
    assert src.index("terminate(pid") < src.index('setattr(L, "state"')


def test_abort_asks_first():
    from plat import cli
    src = inspect.getsource(cli.abort)
    assert "Confirm.ask" in src and "default=False" in src


def test_abort_leaves_finished_lots_alone():
    from plat import cli
    src = inspect.getsource(cli.abort)
    assert "L.state not in done" in src
    assert "LotState.DONE.value" in src


def test_aborted_is_terminal_to_the_fsm():
    """The state existed and nothing ever set it. Now that something does, decide()
    has to already treat it as an end -- otherwise abort just gets undone."""
    from plat import fsm
    nxt = fsm.decide(fsm.LotState.ABORTED, 1, fsm.Ctx(deps_met=True))
    assert nxt.kind in ("noop", "wait")
