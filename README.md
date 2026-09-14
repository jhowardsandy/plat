# Plat

**One ticket, divided into numbered lots, worked in parallel by agents across repos, reviewed by a different model than wrote it, and recorded as a single instrument.**

Plat is a deterministic orchestrator for coding agents. You plan a ticket into *lots*; Plat runs each one through code → gate → review → fix, escalating models on retry, and stops at a human whenever it should. Every decision — by the system, by an agent, by you — is recorded and queryable long after the branch is gone.

```
$ plat status --watch

 plat  14:22:07   2 agent(s) live   $4.18   1 needs you

 ticket    lot                state      phase   provider/model       att  elapsed  cost    signal
 DEV-2551  api-gateway        CODING     code    claude/sonnet          1    04:12  $0.81   ·
 DEV-2551  web-console        REVIEWING  review  codex/gpt-5.5          2    47:03  $2.40   ! stale 12:40  retry
 DEV-2551  deploy-manifests   PENDING    —       —                      —        —      —   waits on api-gateway
 DEV-2889  billing-service    BLOCKED    —       —                      1        —  $0.60   || needs you
```

## Why it is shaped this way

**Cross-model review is the point.** A model is systematically blind to its own failure modes and will rationalise its own code — it wrote that code because it believed it was right. A reviewer from a different lineage brings different priors. On Plat's first real run a coder reasoned its way into keeping an unsafe default, the tests passed, and the reviewer caught exactly that rationalisation. A same-model reviewer would likely have shared the blind spot.

**Two invariants hold the rest up.**

1. **The control plane is JSON.** Agents write narrative Markdown for humans and a strict JSON verdict for the machine. Nothing routing-relevant is ever decided by reading prose, so a run is deterministic, resumable and auditable.
2. **Never trust self-reported success.** `{"status": "complete"}` is an agent's opinion. The supervisor runs the tests itself and checks the diff is non-empty. An agent that reports passing tests it never ran fails the gate.

**No LLM sits in the routing seat.** The state machine is pure functions, exhaustively tested. What runs next is never a judgement call.

## The vocabulary

A **plat** divides one tract into numbered lots: independently developable, recorded together as a single instrument, and the survey has to *close* before any of it is official.

| Concept | Called | Because |
|---|---|---|
| a run | a **plat** | one ticket, divided and recorded as one instrument |
| a unit of work | a **lot** | independently workable, numbered, part of one whole |
| the master plan | the **plat map** | the drawing every lot is cut from |
| integration review | **closing the traverse** | a survey closes when the boundary calls return to origin; contracts that do not line up across repos are an **error of closure** |
| production readiness | **recording** | preparing the instrument that makes it official |
| the final report | the **abstract** | the compiled record of what happened |

## Install

Requires Python 3.12+, Postgres, Docker (for the dashboard), and at least one agent CLI.

```bash
uv tool install --editable .     # puts `plat` on PATH
plat init                        # schema, views, config.toml, roles.yaml
plat skills                      # links /plat-up and /plat-run into ~/.claude/skills
plat probe                       # does each agent CLI honour the contract, and is it honest?
```

**[QUICKSTART.md](QUICKSTART.md)** takes you from nothing to a finished lot. **[docs/GUIDE.md](docs/GUIDE.md)** is the full reference.

## Commands

| | |
|---|---|
| `plat init` · `plat skills` · `plat probe` | set up and verify |
| `plat plan <spec.yaml>` | cut worktrees, smoke the gate, stage the rows |
| `plat start <anchor>` | run to completion or to a human gate |
| `plat status [--watch]` · `plat top` | the monitor, glanceable or operable |
| `plat show [anchor]` | the decision record |
| `plat history [-m]` | delivered plats (`-m` for markdown ledger rows) |
| `plat pause` · `plat reopen` | the control surface |
| `plat ui` | the Grafana dashboard on :3033 |

## Which models

Roles map to providers in `~/.plat/roles.yaml`, so who does what is configuration:

```yaml
coder:
  provider: claude
  model: sonnet
  resume_session: true            # attempt 2 resumes its own session; attempt 3 starts cold
  escalate:
    attempt_2: {model: opus}      # cheap first, expensive only on proven-hard work

reviewer.correctness:
  provider: codex                 # a different lineage sees what the author cannot
  model: gpt-5.5
  resume_session: false           # fresh eyes, every time
```

Adapters ship for **Claude Code**, **Codex** and **Gemini**. All three are driven headlessly behind one uniform contract, so swapping a role's provider is a config change.

## Status

Working and used in anger, but early. **v0** runs a single lot inline: `PENDING → CODING → GATE → REVIEWING → DONE | BLOCKED`, with session resume, model escalation, oscillation detection and budget ceilings.

Not built yet: the Celery dispatcher and multi-lot DAG, closing the traverse, recording, and the abstract. See [docs/GUIDE.md](docs/GUIDE.md#what-is-not-built-yet).

## Licence

Not yet chosen — see the repository owner.
