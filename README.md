# Plat

One ticket is divided into numbered **lots**, worked in parallel by agents across
repos, reviewed across models, and recorded as a single instrument. The survey has
to **close** before any of it is official.

| System concept | We call it |
|---|---|
| a run | a **plat** |
| a unit of work | a **lot** |
| master plan | the **plat map** |
| per-repo plan | a **lot description** |
| integration review | **closing the traverse** (a failure is an *error of closure*) |
| prod readiness | **recording** |
| final report | the **abstract** |

## Two invariants

1. **The control plane is JSON.** Narrative is Markdown, for humans and for the
   next agent's context. Nothing routing-relevant is ever decided by reading prose.
2. **Never trust self-reported success.** The supervisor runs tests, lint and the
   diff check itself. `{"status":"complete"}` is an opinion.

## What Plat does and does not own

Plat owns **orchestration state**. The workspace owns **checkouts**.
`lots.worktree_path` points into `<workspace>/.worktrees/<TICKET>/<repo>/`,
created by the existing worktree convention. Plat never relocates a worktree.

## Quick start

    docker compose -f ../../docker-compose.infra.yml up -d postgres redis
    uv run plat init          # create db, run migrations, install views
    uv run plat seed          # fake plats in interesting states, for the monitor
    uv run plat status
