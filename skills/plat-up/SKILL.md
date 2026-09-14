---
name: plat-up
description: Plan a ticket into a Plat — divide it into lots, write the plat map and lot descriptions into Obsidian, force every acceptance criterion into a checkable form, cut the worktrees and smoke the gate. Use when the user says "plan DEV-1234 as a plat", "/plat-up DEV-1234", "set up a plat for X", or otherwise wants to start orchestrated work on a ticket. Interactive and human-in-the-loop by design — it ends by asking for confirmation and never starts agents itself.
---

# plat-up

Turn a ticket into a runnable **plat**. This is the half of Plat that needs judgement; `/plat-run` is the half that must have none.

Workspace root is `/Users/jhoward/development/mediciland/source` (`$ROOT`). The vault is `~/Documents/obsidian/MLG`.

> **The plan is the product.** A weak plat map buys six hours of confident, well-reviewed, thoroughly documented work on the wrong problem, and no amount of review rescues it. Spend the time here.

## Inputs

- **`<TICKET>`** (required) — e.g. `DEV-1234`. If missing, ask once.
- **`[slug]`** (optional) — short kebab-case, e.g. `wordbox`. Ask if omitted.

## Steps

### 1. Read the ticket

Use `twg`, never the Atlassian MCP, and **always** `--select` — an unfiltered single-issue payload is ~90KB:

```bash
F=$(twg jira workitem get <TICKET> --output json \
      --select "data.key,data.summary,data.description,data.status.name,data.issuelinks" \
    2>/dev/null | grep -oE '"/[^"]*stdout\.json"' | tr -d '"')
python3 -c "import json;d=json.load(open('$F'))['data'][0];print(d['key'],'|',d['status']['name'],'|',d['summary'])"
```

Identify the **roster**: the anchor, member tickets worked as one effort, and related tickets that are context only. Related tickets are never transitioned.

### 2. Read the prior art

Plat remembers. Before proposing anything, look at what reviewers have already found in the repos likely in scope:

```bash
psql "postgresql://postgres:mlgdev@localhost:5432/plat" -c \
  "SELECT anchor, repo, severity, fingerprint, claim FROM findings
   WHERE repo IN ('core/docai-core') ORDER BY id DESC LIMIT 15"
```

Surface anything relevant to the user. A finding previously **dismissed** with a reason is the most valuable row there — it stops a reviewer re-raising something already settled.

### 3. Scope it, with the user

This is a conversation, not a form. Establish:

- **What changes in which repo.** One lot per repo. If a repo needs two genuinely independent changes, that is two lots.
- **Dependencies between lots.** Lot B needs A's contract? Record it in `depends_on`. Be sparse — a false dependency serialises work for no reason.
- **The integration contracts** — Kafka topics, API shapes, env vars, feature flags, deployment pins. Anything crossing a repo boundary goes in the plat map where every lot can see it.
- **What is explicitly out of scope**, per lot. Write it down. This is what stops an agent wandering into an adjacent repo.

Ask about anything genuinely ambiguous. Do not invent requirements the ticket does not contain — if something is underspecified, say so and ask.

### 4. Force every acceptance criterion into a checkable form

**An AC that cannot be checked cannot gate a state machine.** For each one, write a `verify` command if one can exist:

- `poetry run pytest -q -k some_test` — good
- `kubectl diff ...` — good
- "the UI feels responsive" — not an AC. Either make it measurable or move it to the plat map as context.

An AC with no verify command is allowed, but flag it to the user: it will be judged on an agent's evidence rather than a fact.

### 5. Determine the gate command per lot

Inspect each repo and pick the real command:

| Shape | setup | test |
|---|---|---|
| `pyproject.toml` + `tests/` | `poetry install --no-interaction --no-root -q` | `poetry run pytest -q` |
| `Makefile` with a test target | as above | `make test` |
| `package.json` | `pnpm install --frozen-lockfile` | `pnpm test` |

A fresh worktree has **no `.venv`**, so `setup` is not optional. `plat plan` runs this as a smoke test before any agent starts — a gate that cannot run fails every attempt and blocks the lot for reasons that have nothing to do with the code.

### 6. Write the plan into Obsidian

Plans live in the vault, never in a code repo:

```
~/Documents/obsidian/MLG/work-items/<TICKET>/plans/
  master.md          # the plat map
  <repo>.md          # one lot description per repo
  INDEX.md           # links + as-built status
```

`master.md` carries the roster in frontmatter so it never has to be reconstructed:

```yaml
anchor: DEV-2551
tickets: [DEV-2474, DEV-2651]     # members — an audit must mark ALL of these
related: [DEV-2650, DEV-1691]     # context only — never transition these
```

Each lot description starts with `**Master:** [[master]]` and contains: scope in this repo, the AC subset this repo owns, an explicit "out of scope — handled by [[other-plan]]" section, and the integration contract notes connecting it to siblings.

### 7. Write plat.yaml

Write to `$ROOT/.worktrees/<TICKET>.plat.yaml`. The `plan` and `plat_map` fields are **snapshots** — `plat plan` copies them into the lot's context box, so an agent never reads the vault (and macOS TCC never bites a headless subprocess).

```yaml
anchor: DEV-1234
title: ...
slug: <slug>
budget_usd: 15
tickets: []
related: []
phases: [code, review]        # v0 ships these two; docs/quality need their providers
plat_map: |
  <the problem, the architecture, the contracts, what is out of scope>
lots:
  - key: docai-core
    repo: core/docai-core
    depends_on: []
    gate:
      setup: "poetry install --no-interaction --no-root -q"
      test:  "poetry run pytest -q"
    plan: |
      ## Scope in this repo
      ## Out of scope — do not touch
criteria:
  - id: AC1
    lot: docai-core
    statement: "..."
    verify: "poetry run pytest -q -k ..."
```

### 8. Dry run, then STOP

```bash
plat plan $ROOT/.worktrees/<TICKET>.plat.yaml
```

This cuts the worktrees, runs the gate smoke test, and stages the rows without committing them. Report to the user:

- the lot DAG and which role/model each phase will use (`~/.plat/roles.yaml`)
- **the gate smoke result per lot** — a failure here must be fixed before anything else
- the budget

Then **stop and ask for confirmation.** Do not proceed on your own judgement; this is the gate that catches a wrong decomposition before it costs anything.

### 9. Commit the plan

Only after an explicit yes:

```bash
plat plan $ROOT/.worktrees/<TICKET>.plat.yaml --commit
```

Tell the user it is ready and that `/plat-run <TICKET>` starts it. **Do not start it yourself** — starting is a separate, deliberate act.

## Notes

- Unticketed work uses a `<prefix>_<name>` anchor (`fix_`, `chore_`, `spike_`, `docs_`, `infra_`); Plat keeps that as the branch name verbatim. If it turns out to be worth tracking, open a ticket and re-anchor rather than merging a `prefix_` branch carrying real work.
- Never commit vault content into a code repo.
- If the repo root is dirty or behind `origin`, fix that first — a worktree cut from a stale base is the quiet start of cross-repo drift.
