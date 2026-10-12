# Plat — notes for agents working ON Plat

Read `README.md` first for the vocabulary (plat / lot / plat map / closing the traverse).

## The two invariants — do not weaken these

1. **The control plane is JSON.** `fsm.py` must stay pure: no I/O, no LLM, no prose
   parsing. If a routing decision seems to need judgement, it belongs in a `router`
   role answering one narrow enum question — not in the state machine.
2. **Never trust self-reported success.** `gates.py` runs independently of the agent
   and the FSM reads the gate, not the claim.

## Rules of the codebase

- **`decisions.record()` is the domain mutation path.** Plan/state/ownership and
  outcome changes require a decision in the same transaction. Phase evidence is
  written in that transaction. Only heartbeat timestamps, PIDs and live log chunks
  may use independent telemetry writes; they cannot change workflow state. Demo
  seed/clear is fixture maintenance.
- `views.sql` holds the derived signals (stale, retrying, needs-human). Renderers stay
  thin; never compute a signal in a renderer.
- `dispatch/` owns inline scheduling and host-routed Celery execution. Both call
  the same claimed runner. Do not bypass ownership fencing or inspect remote PIDs.
- `lifecycle.py` records acceptance and independently executed delivery checks.
  Revision drift invalidates readiness. `team.py` never executes client-supplied shell commands.
- Add schema changes as frozen Alembic revisions under `migrations/versions`.
  Identity administration is recorded with a null plat scope. Never delete audit history.
- Findings carry `(origin_id, anchor, repo, fingerprint)` so two installs' corpora can
  be unioned later. Never reference a finding by bare autoincrement id across installs.

## Testing ladder

    uv run pytest              # L1 fsm properties + L2 contract/gate/splitter/tui
    uv run plat probe          # L3 does each real CLI honour the contract, and is it honest

**After changing dependencies, reinstall the tool and smoke the real binary.**
`uv run` uses the project venv; the user runs the installed tool, and a new
dependency does NOT reach an already-installed one. `plat top` shipped broken
this way with a green suite.

    uv tool install --editable . --force && plat top --help

L1 is the layer where a bug costs money rather than an error message — the FSM is
walked exhaustively. Keep it that way.

## Known sharp edges

- `codex exec` appends anything on stdin as a `<stdin>` block → adapters use
  `stdin=DEVNULL`.
- Codex reports tokens, never dollars → `cost_estimated=True`. Never enforce a budget
  against a number that only looks authoritative.
- Codex may exit 0 on a failed turn → parse `turn.failed`, do not trust the exit code.
- Which models an account can reach varies. `model: default` omits `-m`.
- Claude's result JSON has `permission_denials` — a non-empty list means the agent was
  *blocked*, not merely unproductive. Surface it.
- A fresh worktree has no `.venv`; `gate.setup` runs once at plan time via `gates.smoke`.
