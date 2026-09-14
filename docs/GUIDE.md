# Plat — the guide

[Quickstart](../QUICKSTART.md) gets you running. This explains why it works the way it does, so you can tell a bug from a decision.

---

## 1. What Plat is

A **deterministic supervisor** for coding agents. It is not an agent framework and there is no LLM in the routing seat. The state machine is pure functions; agents are subprocesses that produce evidence, and the supervisor decides what happens next from that evidence.

The closest formal relatives are LangGraph (stateful graph with checkpointing) and Temporal (durable execution). Plat is a lighter approximation of the same idea, specialised for one job.

## 2. The two invariants

Everything else is negotiable. These are not.

### The control plane is JSON

Every agent terminates by writing **two** files: `<phase>.md` for humans and the next agent's context, and `<phase>.json` for the machine, validated against a schema on arrival.

The tempting alternative — let an orchestrator agent read the review and decide — is an LLM parsing prose to drive control flow. It is non-deterministic, non-resumable and unauditable. You would never be able to answer *"why did it loop four times on Tuesday."*

Invalid or missing JSON is a hard failure with one cheap repair attempt. Never a guess.

### Never trust self-reported success

`{"status": "complete"}` is an opinion. Between the coder and the reviewer the supervisor runs the repo's own test command itself and checks `git diff` is non-empty. An agent that reports passing tests it never ran fails the gate; so does one that reports success having changed nothing.

This is the single highest-value guardrail in the system, and it is why `gate` is a state you can see rather than something an agent performs.

## 3. Vocabulary

A plat divides one tract into numbered lots — independently developable, recorded together as one instrument, and the survey must *close*.

- **plat** — one run, one ticket
- **lot** — one unit of work: one agent, one worktree, one session at a time
- **phase** — a step within a lot: `code`, `review`, `docs`, `quality`
- **verdict** — the JSON an agent must write to terminate
- **gate** — an objective check the *supervisor* runs. Never delegated
- **plat map** — the master plan; **lot description** — the per-repo plan
- **closing the traverse** — integration review. A contract that does not line up is an **error of closure**
- **abstract** — the compiled record of what happened

## 4. The state machine

```
PENDING ──deps met──▶ CODING ──▶ GATE ──▶ REVIEWING ──▶ DOCS ──▶ QUALITY ──▶ DONE
                        ▲          │          │
                        │          │          ├── blocked ──────▶ HUMAN ⏸
                        └──────────┴──────────┘
                     changes_requested / gate failed
                        attempt++, max 3
```

`decide(state, attempt, ctx)` in `fsm.py` is pure — no I/O, no LLM, no prose. The transition predicate:

```python
advance = verdict == "pass"
          and all criteria met
          and gate.tests_failed == 0
          and gate.diff_files > 0
```

Where genuine nuance is needed, a `router` role answers **one** narrow question returning an enum. Never "read this and decide what to do."

### Retry, and why attempt 3 differs

Attempt 2 **resumes the coder's own provider session** — it keeps the context for why it made those choices instead of being handed a diff and a list of complaints by a stranger. It is also cheaper, because the prompt cache is warm.

Attempt 3 **starts cold**. By then the session is carrying a wrong mental model, and escaping it is the entire reason a third attempt exists.

Reviewers are **never** resumed. Fresh eyes are the point.

### Oscillation

Every finding carries a `fingerprint`: a deterministic slug of file + rule + symbol. If the same fingerprint appears on a later attempt, the coder and reviewer are talking past each other and a third attempt will not fix it — the lot stops and asks for a human. Unbounded ping-pong is the most reliable way to spend several hundred dollars overnight and produce nothing.

### Converge mode

Some work is not "make this change" but "chip away until X": raise coverage,
migrate the remaining call sites, drive a type-error count to zero. A three-attempt
cap abandons that half-done, and no cap grinds forever on a stuck problem. So a
converge lot stops on a **measurement**:

```yaml
mode: converge
objective: ">= 80"        # >= <= > < ==  — direction decides what counts as progress
patience: 2               # iterations without improvement before it asks for help
max_iterations: 8         # hard ceiling regardless
gate:
  test:  "pytest -q"
  probe: "pytest -q --cov=app/quota | awk '/TOTAL/{print $NF}'"
```

The probe runs as part of the gate and the **last number it prints** is the reading.
Each iteration's coder sees the whole trail — `42 → 55 → 63` — so it continues the
work rather than repeating the cheapest move it already made. It resumes its own
session, because continuity is exactly what this shape needs.

It stops when: the objective is met (then it is reviewed), progress stalls for
`patience` iterations, `max_iterations` is reached, or the budget is exhausted.
A *regression* counts as no improvement, in whichever direction the objective points.

> **The failure mode a converge loop invites is gaming.** An agent told to raise
> coverage can do it by deleting the tests that fail: the number goes up, the suite
> stays green, and the metric is a lie. The supervisor records the passing-test
> count before the lot starts and stops the lot if it ever falls. Guard the metric
> you optimise — this is the general lesson, not a detail about coverage.

## 5. Roles, providers and cost

`~/.plat/roles.yaml` maps a role to a provider, model and permissions. The FSM knows only role *names*.

```yaml
coder:
  provider: claude
  model: sonnet
  permission_mode: acceptEdits
  allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, TodoWrite]
  resume_session: true
  escalate:
    attempt_2: {model: opus}

reviewer.correctness:
  provider: codex
  model: gpt-5.5
  sandbox: workspace-write     # NOT read-only — see below
  resume_session: false
```

**Why the reviewer is not read-only.** It must write its own verdict, and a read-only sandbox cannot write at all — it fails *silently*, producing no verdict file and no error. Writes are confined to the attempt's output directory, and the supervisor separately verifies the worktree fingerprint is unchanged across a review. Observed, not trusted.

**Cost.** Claude reports `total_cost_usd` exactly. Codex and Gemini report only tokens, so their cost is estimated and marked `cost_estimated`, shown as `~`. Budget ceilings are therefore approximate on those providers — set them conservatively.

## 6. Gates

The gate is per-lot configuration, not inference:

```yaml
gate:
  setup: "uv pip install -q -e '.[dev]'"
  test:  "uv run pytest -q tests/unit"
```

Two rules, both learned the hard way:

**A fresh worktree has no virtualenv.** `setup` is never optional.

**The gate must already be green on the base commit.** `plat plan` runs it as a smoke test before any agent starts — a gate that cannot run fails every attempt and blocks the lot for a reason no agent can fix. If your full suite is red on main, narrow the gate to what passes and record in the plat map what it no longer covers, so the reviewer knows to look there rather than assuming the tests did.

Do not infer packaging from the presence of `pyproject.toml`. Read it: `[tool.poetry.dependencies]` and `[project.optional-dependencies]` need completely different commands.

## 7. Persistence

Agents write files because they must. The moment a phase completes, everything is ingested into Postgres and **disk becomes a cache you may delete** — which matters, because tearing down a worktree will.

| table | holds |
|---|---|
| `plats`, `lots`, `attempts` | the run |
| `sessions` | provider session ids, so attempt 2 can resume |
| `verdicts`, `findings`, `criteria` | evidence |
| `artifacts`, `transcripts`, `log_chunks` | narrative, full transcripts, live output |
| `decisions` | why it went this way |
| `events` | append-only, with a `NOTIFY` trigger for push consumers |

Findings carry `(origin_id, anchor, repo, fingerprint)` — a stable identity across installs, so two people's corpora can be merged later without collisions.

### The decision record

One table, three actors, a tree via `parent_id`, read as narrative:

```
14:02  reviewer  codex/gpt-5.5   changes_requested · 2 findings
14:02   └ system fsm.retry       attempt 2 · escalate sonnet→opus · resume session
14:11      └ coder claude/opus   rewrote the consumer rather than patching it
                                 alternatives: [extend existing handler]
14:33         └ JH               dismissed f2 as wontfix — "by design"
```

**System decisions are observed.** The supervisor knows them for certain. **Agent decisions are self-reported** and epistemically weaker — a model asked to record its reasoning will happily produce post-hoc rationalisation. Two mitigations: `alternatives` is mandatory and non-empty (if nothing else was on the table it was not a decision, it was just doing the work), and the two are rendered differently everywhere.

`record()` is the **only** mutation path. No command writes a bare `UPDATE`; every change writes a decision and applies the change in one transaction, so it is structurally impossible to change a plat without leaving a record.

## 8. Watching it

Three renderers, one source. `v_live_lots` computes the derived signals — `stale`, `retrying`, `needs_human`, burn rate — in SQL, so nothing recomputes them and they cannot disagree.

| | for |
|---|---|
| `plat status [--watch]` | glanceable, in the terminal you are already in |
| `plat top` | drill-in, live agent log, keys that act |
| `plat ui` → Grafana :3033 | reading: history, the decision record at width, the archive |

**`stale` is relative, not a timeout.** A 95-minute indexing run is healthy; a 12-minute review is probably wedged. The threshold is three times the median duration of that phase in that repo, floored at ten minutes.

## 8b. Being told

Plat's most expensive moment is a lot that stopped for a human while nobody was
looking: block at minute 20 of an hour-long run and the remaining forty minutes
are idle. `[notify]` in `~/.plat/config.toml` fixes the latency, not the gate.

```toml
[notify]
handlers = ["desktop"]              # desktop | webhook | exec
on       = ["blocked", "budget"]    # blocked | closed | budget | review_findings

# [notify.webhook]
# url = "https://hooks.slack.com/services/..."   # a DM or your own channel

# [notify.exec]
# cmd = "my-notifier"               # the event arrives on stdin as JSON
```

**Plat notifies you. It never announces to anyone else.** A team-channel post, a
pull request, a ticket transition — those speak in your name, and they stay a
deliberate act you take, not a side effect of a run. Plat will happily draft one;
a human sends it. There is a test asserting the notifier has not grown an
outward-speaking path.

A notification is a courtesy, so it can never be blamed for a failed run: a
handler that raises is swallowed, the handlers after it still fire, and an
unreachable webhook times out and is forgotten.

Slack specifically: an **incoming webhook** posts outward only and needs nothing
listening, which is why it suits a local tool. A full Slack *app* — one that asks
a blocked lot's question with a reply box and unblocks it from your phone — needs
a process listening, and that is a real architectural change for something that
is deliberately local-only. Worth doing; not the same size of job.

## 9. Testing ladder

```bash
uv run pytest      # L1 fsm properties · L2 contract, gates, views, TUI
plat probe         # L3 do the real CLIs honour the contract, and are they honest
```

**L1** walks the whole reachable state space and asserts four properties: every path terminates, no path exceeds `max_attempts`, oscillation always reaches a human, and a failed gate never advances. This is where a bug costs money rather than an error message.

**L3** is the honesty probe: a repo whose tests cannot pass, and a check on whether the agent says so. Learning that an agent lies costs fifty cents there and a whole plat otherwise.

**L4**, unimplemented and worth doing: a *shadow run* against a ticket you have already delivered. Check out the parent commit, run Plat, and diff against what actually shipped. It is the only test that vets the output rather than the machinery. (One caveat: the real fix is still in `git log --all`, so an agent that greps history can cheat.)

## 10. What is not built yet

- **Converge mode has never run against real agents.** The state machine is
  exhaustively tested — objective direction, stalling, ceilings, gaming — but no
  live lot has iterated yet.
- **The Celery dispatcher and multi-lot DAG.** The design is a queue-backed graph with lots running in parallel; v0 runs one lot inline. `dispatch/` is behind an interface so the swap is contained.
- **Closing the traverse, recording, the abstract.** The whole run-level join, including the cross-repo contract check.
- **`docs` and `quality` phases.** Wired into the FSM and disabled by default; they need their providers.
- **Prior-art retrieval.** `findings` is the corpus; a `pgvector` column would let past findings sharpen review prompts.
- **Alembic.** `init` applies additive `ALTER … IF NOT EXISTS` as a stopgap; that handles added columns and nothing else.

## 11. Failure modes worth knowing

| | what it looks like | why |
|---|---|---|
| **Weak plat map** | hours of confident, well-reviewed work on the wrong problem | Nothing downstream rescues a bad plan. This is why planning stays a conversation that ends at a confirm. |
| **Ping-pong** | coder and reviewer circling one finding | capped at 3 attempts, fingerprint oscillation detection, USD ceilings |
| **Phantom success** | `complete`, empty diff, tests never run | the gate, run by the supervisor |
| **Silent blanks** | a panel, view or table that is simply empty | The most common bug class here by far. A missing view, an uncommitted row, a swallowed log — all look identical to "nothing is happening." Most guard tests exist for this. |
| **Permission stalls** | agent exits 0 having done nothing | `permission_denials` is persisted and surfaced; non-zero means *blocked*, not unproductive |

## 12. Conventions it expects

Plat **points at** worktrees; it never owns them. `lots.worktree_path` resolves to `<workspace_root>/.worktrees/<ANCHOR>/<lot>/`, created from a freshly fetched default branch. A worktree belongs to the repo it was cut from.

Anchors that look like `ABC-123` get branch `dev-abc-123-<slug>`. Anything else — `fix_thing`, `spike_thing` — is used as the branch name verbatim, so unticketed work keeps its own convention.
