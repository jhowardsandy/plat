---
name: plat-up
description: Plan a ticket into a Plat — divide it into lots, write the plat map and lot descriptions into Obsidian, force every acceptance criterion into a checkable form, cut the worktrees and smoke the gate. Use when the user says "plan DEV-1234 as a plat", "/plat-up DEV-1234", "set up a plat for X", or otherwise wants to start orchestrated work on a ticket. Interactive and human-in-the-loop by design — it ends by asking for confirmation and never starts agents itself.
---

# plat-up

Turn a ticket into a runnable **plat**. This is the half of Plat that needs judgement; `/plat-run` is the half that must have none.

Read `~/.plat/config.toml` for `workspace_root` — that is `$ROOT` below, and worktrees resolve to `$ROOT/.worktrees/`. Never hardcode a path.

> **The plan is the product.** A weak plat map buys six hours of confident, well-reviewed, thoroughly documented work on the wrong problem, and no amount of review rescues it. Spend the time here.

## Inputs

- **`<TICKET>`** (required) — e.g. `DEV-1234`. If missing, ask once.
- **`[slug]`** (optional) — short kebab-case, e.g. `wordbox`. Ask if omitted.

## Steps

### 1. Read the ticket

Pull it from whatever issue tracker is in use — a CLI if one is configured, otherwise ask the user to paste it. Prefer a CLI that can select fields: an unfiltered issue payload is often tens of kilobytes and reading it back costs more than the call it replaced.

Identify the **roster**: the anchor, member tickets worked as one effort, and related tickets that are context only. Related tickets are never transitioned.

### 2. Read the prior art

Plat remembers. Before proposing anything, look at what reviewers have already found in the repos likely in scope:

```bash
plat show                      # the most recent plat's decision record
psql "$(python3 -c 'from plat.config import load; print(load().database_url.replace("+psycopg",""))')" -c \
  "SELECT anchor, repo, severity, fingerprint, claim, resolved_in_attempt_id
     FROM findings WHERE repo = '<repo>' ORDER BY id DESC LIMIT 15"
```

Surface anything relevant to the user. A finding previously **dismissed** with a reason is the most valuable row there — it stops a reviewer re-raising something already settled.

### 3. Scope it, with the user

This is a conversation, not a form. Establish:

- **What changes in which repo.** One lot per repo. If a repo needs two genuinely independent changes, that is two lots.
- **Which shape each lot is.** Most are ordinary: make a change, review it, done. But if a lot is *"chip away until X"* — raise coverage, migrate the remaining call sites, drive a count to zero — it is a **converge** lot and needs `mode: converge`, an `objective`, and a `gate.probe` that prints a number. An attempt cap abandons that kind of work half-finished.
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

**Do not infer the packaging from the presence of `pyproject.toml`.** Open it and read how dependencies are actually declared — the workspace inventory is not reliable here, and getting this wrong costs a whole run.

| What you find in `pyproject.toml` | setup | test |
|---|---|---|
| `[tool.poetry.dependencies]` | `poetry install --no-interaction --no-root -q` | `poetry run pytest -q` |
| `[project]` + `[project.optional-dependencies]` (PEP 621 / hatchling) | `uv pip install -q -e '.[dev]'` | `uv run pytest -q` |
| `package.json` instead | `pnpm install --frozen-lockfile` | `pnpm test` |

A real example: a service documented as "Python / Poetry" was actually PEP 621 + hatchling. `poetry install --no-root` installed no test dependencies at all and the gate died with `Command not found: pytest`.

Two more rules, both learned the same way:

- **A fresh worktree has no `.venv`.** `setup` is never optional.
- **The gate must be a subset that is already green on the base commit.** Run it yourself before accepting it. If the full suite is red on master — integration or contract tests needing a broker, a database, a VPN — narrow the gate to what genuinely passes and say so in the lot's `plan`. One repo here is 12-red on main in `tests/contract` (it wants a pact broker); gating on the whole suite would fail every attempt for a reason no agent can fix, burning all three and blocking the lot. Scoped to `tests/unit` it is 799 green.

A narrowed gate is a real reduction in assurance, so name it: record in the plat map what the gate does *not* cover, so the reviewer knows to look there rather than assuming the tests did.

### 6. Write the plan somewhere durable

If the user keeps working notes outside their repos — a vault, a wiki, a docs folder — write the plan there and follow their existing layout. Otherwise put it next to the spec. What matters is that it is **not** committed into a code repo it describes.

```
<notes>/<TICKET>/plans/
  master.md          # the plat map
  <repo>.md          # one lot description per repo
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
  - key: my-service
    repo: services/my-service
    depends_on: []
    gate:
      setup: "poetry install --no-interaction --no-root -q"
      test:  "poetry run pytest -q"
    plan: |
      ## Scope in this repo
      ## Out of scope — do not touch
criteria:
  - id: AC1
    lot: my-service
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
- Never commit planning notes into a code repo they describe.
- If the repo root is dirty or behind `origin`, fix that first — a worktree cut from a stale base is the quiet start of cross-repo drift.
