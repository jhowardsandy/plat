# Contributing

## Running it

```bash
docker compose -f docker-compose.plat.yml --profile db up -d
uv tool install --editable .
plat setup && plat init
```

## Before opening a pull request

```bash
uvx ruff check src tests
uv run pytest -q
```

CI runs both against a real Postgres, and then checks that the **installed binary**
starts. That last step exists because `uv run` uses the project venv while a user
runs the tool `uv tool install` built: a dependency added without reaching the
installed environment once shipped `plat top` broken with a green suite.

## What the tests are for

The suite is unusually opinionated about one failure mode, because it has happened
seven times: **something that looks fine while measuring nothing.** A view that
silently did not install, an attempt row invisible outside its own transaction, a
heartbeat that never beat, Grafana panels with no id, a test count parsed from the
wrong line, a log pane full of protocol. Each has a test that would catch it again.

So when you fix a bug here, prefer a test that asserts the *behaviour* over one
that greps the source — and if the bug was "this reported success while doing
nothing", say that in the test's docstring. Several of them explain what they cost
to learn, and that is on purpose.

## The rules that are not up for negotiation

1. **The control plane is JSON.** `fsm.py` stays pure: no I/O, no LLM, no parsing
   prose to make a routing decision.
2. **Never trust self-reported success.** The supervisor runs the tests; the agent's
   claim is evidence, not a verdict.
3. **`decisions.record()` is the only mutation path.** No bare `UPDATE`.
4. **Plat notifies you and never speaks to anyone else.** No posting to channels,
   opening pull requests or transitioning tickets as a side effect of a run.

## Style

`ruff` config lives in `pyproject.toml`. Three E-rules are suppressed deliberately
and each says why — they are conventions in this codebase, not oversights.
