# Quickstart

From nothing to a finished lot. Budget about twenty minutes, most of it waiting on agents.

## 1. Prerequisites

- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/)
- **Postgres** — bring your own, or let Plat run one (step 2)
- **Docker** — only for the dashboard; everything else works without it
- **At least one agent CLI**, logged in and working:
  [Claude Code](https://claude.com/claude-code) (`claude`), [Codex](https://developers.openai.com/codex/cli) (`codex`), or Gemini CLI (`gemini`)

Plat shells out to these, so whatever account you are already logged into is what it uses. There are no API keys to configure.

## 2. A database

Already have Postgres? Create a database and skip ahead:

```bash
createdb plat
```

Otherwise let Plat run one:

```bash
docker compose -f docker-compose.plat.yml --profile db up -d
```

That publishes **5432**. If something already has that port — you probably have a
Postgres — pick another and tell Plat about it:

```bash
PLAT_DB_PORT=5433 docker compose -f docker-compose.plat.yml --profile db up -d
export PLAT_DATABASE_URL="postgresql+psycopg://plat:plat@localhost:5433/plat"
```

The same command also brings up Grafana on :3033, which `plat ui` uses later.

## 3. Install

```bash
uv tool install --editable .
plat setup
```

`setup` walks you through it and **checks as it goes**: which agent CLIs are
installed, which models your account can actually reach (one trivial call each —
installed is not the same as permitted), who codes and who reviews, an effort
preset, which phases run, and where your repositories are. It prints what it will
write before writing it, and keeps a `.bak`.

Then create the schema:

```bash
plat init
```

Both are re-runnable. If you would rather configure by hand, `~/.plat/config.toml`
and `~/.plat/roles.yaml` are plain files — `workspace_root` is the one that matters
most, since worktrees resolve to `<workspace_root>/.worktrees/`.

## 4. Check your agents honour the contract

```bash
plat probe
```

This builds a throwaway repo whose tests **cannot pass**, asks each CLI for a change, and checks two things: did it write a valid JSON verdict, and did it report the failure or claim success.

```
claude/sonnet ...
  exit=0  36s  $0.134  session=yes
  contract ok - both files written, schema valid
  honesty: reported ran=True passed=1 failed=1 -> HONEST
```

Fix anything that fails here before going further. An agent that cannot honour the contract cannot run a lot, and one that lies about tests will waste a whole plat.

## 5. Describe a lot

Copy `example.plat.yaml` and edit. Start with something small and real in one repo:

```yaml
anchor: DEV-1234                 # or fix_some-thing for unticketed work
title: One malformed word box must not lose a document's highlights
slug: wordbox
budget_usd: 12
phases: [code, review]

plat_map: |
  ## Problem
  <what is wrong, and why it matters>
  ## Out of scope
  <what must not be touched>

lots:
  - key: my-service
    repo: services/my-service     # relative to workspace_root
    gate:
      setup: "uv pip install -q -e '.[dev]'"   # a fresh worktree has no venv
      test:  "uv run pytest -q tests/unit"
    plan: |
      ## Scope in this repo
      ## Out of scope — do not touch

criteria:
  - id: AC1
    statement: "A malformed entry is skipped rather than fatal."
    verify: "uv run pytest -q tests/unit/test_thing.py"
```

Two rules that decide whether this works:

- **An acceptance criterion that cannot be checked cannot gate anything.** Give every one a `verify` command if a command can exist.
- **The gate must already be green on the base commit.** If your full suite is red on main — integration tests wanting a broker, a database, a VPN — narrow the gate to what genuinely passes and say so in `plan`.

## 5b. Or let it propose the shape

Rather than writing the spec yourself:

```bash
plat shape DEV-1234 --ticket ticket.md
```

Cheap reconnaissance — which repos are really involved, is the ticket specified
enough to decompose, does the code already exist — then a menu of ways to divide
the work with cost and time estimates drawn from what runs here have actually
cost. Pick one and it plans that shape in detail.

Those dollar figures are **API-equivalent cost** — what the work would cost if you
were paying per token rather than through a subscription. On a plan nothing is
billed per token, but it is still the number worth having: it says which shape is
genuinely cheaper, whether the economics hold at volume, and what a run is worth
against the time it saves.

## 6. Plan it

```bash
plat plan my.plat.yaml
```

Cuts the worktree, runs `setup`, then runs the gate **before any agent starts**:

```
my-service  /Users/you/src/.worktrees/DEV-1234/my-service
  gate smoke ok 799 passed
```

A failure here is the single most valuable thing Plat tells you. A gate that cannot run fails every attempt and blocks the lot for reasons no agent can fix. Fix it, re-run, then commit the plan:

```bash
plat plan my.plat.yaml --commit
```

## 7. Run it, and watch

In one pane:

```bash
plat status --watch      # or `plat top` for drill-in and a live agent log
```

In another:

```bash
plat start DEV-1234
```

```
  [PENDING a0] -> agent: dependencies satisfied
  code: claude/sonnet ...
    exit=0 282s $0.88
  [CODING a1] -> gate: coder finished; verifying independently
  gate: uv run pytest -q tests/unit
  [GATE a1] -> agent: gate passed
  review: codex/gpt-5.5 ...
    exit=0 118s $0.52~
  [REVIEWING a1] -> agent: changes_requested
  code: claude/opus (resuming session) ...
```

The coder resumes **its own session** on attempt 2, so it keeps the context for why it made those choices rather than being handed a complaint cold — and escalates to a stronger model, because the work has proven hard once.

## 8. Read what it decided

```bash
plat show DEV-1234
```

```
agent   14:02  codex/gpt-5.5   changes_requested · 1 finding(s)
          the rightmost XFF hop is the load balancer's own VIP, identical for every caller
system  14:02  fsm.retry       code · attempt 2 · escalated to opus · resuming session
agent   14:11  claude/opus     take the caller from the verified suffix, not the last hop
          alternatives: keep attempt 1's rightmost-hop reading
system  14:34  fsm.close       lot DONE
```

Blue is observed by the supervisor. Amber is self-reported by an agent and weaker evidence. Red is you.

## 8b. Reviewing something you wrote yourself

You do not need a whole plat to get cross-model review. On a branch you wrote:

```bash
plat review --test "pytest -q"
```

One reviewer, one diff, one verdict — no plan, no worktree, no coder, about a
dollar. It reads past findings for that repo first, so it will not re-raise
something already settled. On its first real use it found that a merged-looking
change was **inert in every deployed environment**, which the plat that wrote the
code could not see because its scope was one file.

## 9. Then what

Plat stops at the wall. It does not push, open a pull request, transition a ticket, or touch any environment. Review the diff and decide:

```bash
git -C <worktree> diff origin/main..HEAD
```

**Two models agreeing is not verification.** Read it yourself before it goes anywhere.

## When something goes wrong

| | |
|---|---|
| `gate smoke failed` | your setup/test command does not work on a clean worktree — fix before spending anything |
| `BLOCKED — termination contract not honoured` | the agent never wrote valid JSON. Check `permission_denials` first; it is usually environmental |
| `BLOCKED — oscillation` | the same finding twice: coder and reviewer are talking past each other. Rule on it yourself, then `plat reopen --note "<the ruling>"` |
| `0 agents live` while one is clearly running | you are on an old install — `uv tool install --editable . --force` |
| the database will not start | port 5432 is taken; see the `PLAT_DB_PORT` note above |
| `plat ui` shows an empty dashboard | nothing is running, which is correct — `plat demo` seeds a board to look at |
| costs shown with `~` | estimated from tokens; Codex and Gemini do not report dollars |
