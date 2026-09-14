You are drafting a **plat**: a plan that divides one ticket into lots an agent can
work independently. A human will read your draft before anything runs, so be
honest about what you are unsure of rather than filling gaps with plausible guesses.

## The ticket

{ticket}

## Repositories available

These are the only repos that exist. Use the paths exactly as given, relative to
the workspace root.

{repos}

{prior_art}

## What you must work out

**Which repos actually change.** Read them. Do not guess from the ticket's wording
— open the files it implies and confirm. One lot per repo; two genuinely
independent changes in one repo are two lots.

**The gate command for each lot.** This is the part most often got wrong, so read
`pyproject.toml` rather than assuming:

| what you find | setup | test |
|---|---|---|
| `[tool.poetry.dependencies]` | `poetry install --no-interaction --no-root -q` | `poetry run pytest -q` |
| `[project.optional-dependencies]` (PEP 621) | `uv pip install -q -e '.[dev]'` | `uv run pytest -q` |
| `package.json` | `pnpm install --frozen-lockfile` | `pnpm test` |

If part of the suite is already failing on the base commit — integration or
contract tests wanting a broker, a database, a network — **narrow the gate to what
passes** and say in that lot's `plan` what the gate no longer covers.

**Acceptance criteria that can be checked.** Every criterion needs a `verify`
command if a command can exist. "The UI feels responsive" is not a criterion; put
it in the plat map as context. A criterion with no verify command is allowed, but
it will be judged on an agent's own evidence, so use it sparingly.

**Whether a lot is converge-shaped.** Most lots are "make this change". But if one
is *"chip away until X"* — raise coverage, migrate the remaining call sites, drive
a count to zero — set `mode: converge` with an `objective` and a `gate.probe` that
prints a number. An attempt cap abandons that kind of work half-finished.

**What is out of scope, per lot.** Write it down. This is what stops an agent
wandering into an adjacent repo, and it is the field most worth being specific in.

## If you cannot decompose it

Say so. Set `needs_human: true` with your questions and leave `lots` empty. A
ticket that is too vague, or that needs a decision only a person can make, or that
needs someone to investigate before it can be split, is a perfectly good answer —
and far better than a confident decomposition of a problem you do not understand.

## TERMINATION CONTRACT

Write two files into `{out_dir}`:

1. `plan.md` — your reasoning: what you read, what you concluded, what you are
   unsure about. A human reads this to decide whether to trust the draft.
2. `plat.yaml` — the draft itself, matching this shape exactly:

```yaml
anchor: {anchor}
title: <one line>
slug: <short-kebab>
budget_usd: <a number you can justify from the size of the work>
phases: [code, review]
needs_human: false          # true if you could not decompose it
questions: []               # your questions, when needs_human is true
plat_map: |
  ## Problem
  ## Architecture / contracts that cross repos
  ## Out of scope
lots:
  - key: <short name, usually the repo>
    repo: <path/as/given/above>
    depends_on: []          # other lot keys, only where B genuinely needs A's output
    # mode: converge        # only for chip-away work
    # objective: ">= 80"
    gate:
      setup: <command>
      test:  <command>
      # probe: <command printing a number>   # required for converge
    plan: |
      ## Scope in this repo
      ## Out of scope — do not touch
criteria:
  - id: AC1
    lot: <lot key>
    statement: <what must be true>
    verify: <command, or omit if none can exist>
```

Write `plat.yaml.tmp` then rename, so a reader never sees a partial file.
