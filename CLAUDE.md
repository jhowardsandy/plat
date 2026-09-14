# Plat — notes for agents working ON Plat

Read `README.md` first for the vocabulary (plat / lot / plat map / closing the traverse).

## The two invariants — do not weaken these

1. **The control plane is JSON.** `fsm.py` must stay pure: no I/O, no LLM, no prose
   parsing. If a routing decision seems to need judgement, it belongs in a `router`
   role answering one narrow enum question — not in the state machine.
2. **Never trust self-reported success.** `gates.py` runs independently of the agent
   and the FSM reads the gate, not the claim.

## Rules of the codebase

- **`decisions.record()` is the only mutation path.** No bare `UPDATE`. Every change
  writes a decision row and applies the change in one transaction.
- `views.sql` holds the derived signals (stale, retrying, needs-human). Renderers stay
  thin; never compute a signal in a renderer.
- `dispatch/` is behind an interface so v0's inline runner and v1's Celery tasks call
  identical logic. Keep `runner.py` free of dispatch concerns.
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
