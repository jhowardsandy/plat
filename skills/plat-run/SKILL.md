---
name: plat-run
description: Run a planned Plat to completion or to a human gate, then report what happened. Use when the user says "run DEV-1234", "/plat-run DEV-1234", "start the plat", or wants to execute work already planned by /plat-up. Also handles answering a blocked lot and reopening one. Deliberately has no judgement of its own — the state machine decides, and this skill reports.
---

# plat-run

Execute a plat that `/plat-up` has already planned, and report honestly on what it did.

> This half must have **no judgement**. The FSM decides what runs next; your job is to start it, surface what happened, and never paper over a failure.

## Inputs

- **`<TICKET>`** (required) — the anchor, e.g. `DEV-1234` or `fix_some-thing`.

## Steps

### 1. Verify registration before starting

Postgres is the authoritative record. `plat top` and the team web board read it;
neither registers a plan. Files in Obsidian and worktrees are not proof of registration.

```bash
plat config
plat doctor
plat workflow status <TICKET>
plat status <TICKET> --all
```

Verify the intended workspace and database (never print credentials), then resolve
the exact approved spec, normally `<workspace>/.worktrees/<TICKET>.plat.yaml`.
Compare its anchor, lot keys, repositories, phases and dependencies with the recorded
plan in `plat show <TICKET>` and the lot status. Do not treat an empty status table
as success. Stop on failed health checks or an ambiguous/mismatched destination.

If the plan is missing or has no lots:

- Recover the approved spec and its context/definition evidence from the planning
  handoff. Do not invent a plan, acceptance, owner, or gate result.
- For full-lifecycle work, run `plat workflow begin` if absent, then record the
  already-accepted context and definition using the current workflow version.
- Run `plat plan <spec> --commit` only for the exact scope already authorized by
  the user. This runs the gate smoke checks and registers the lots, criteria and
  plan snapshot. If scope has not been approved, return to the planning handoff.
- Accept the resulting specification with the existing approval rationale and
  current version. Re-read workflow and lot status before executing.

Do not recommit an existing matching plan, revise started work, or convert a legacy
run to full lifecycle automatically. A mismatch needs reconciliation against the
approved scope, not duplicate records. Existing user authorization is sufficient;
do not ask again merely because registration was missed.

For a configured team service, read `plat team board --token-file <private-token-file>
--url <configured-service-url>` and verify the same run public ID and lot keys as
the database record. `cached: true` is not a live verification. If the board cannot
be verified, report the visibility problem before claiming team tracking is ready;
do not create another run, member, or database to make it appear. Never guess a
service URL, display a token, or seed demo records for real work.

Report the anchor, registered lots, workflow stage and verified destination before
starting. If lots are already `DONE`, say so — `plat start` skips them. If one is
`BLOCKED`, go to **Answering a blocked lot** below instead of starting.

### 2. Run it

```bash
plat start <TICKET>
```

This blocks until every lot reaches a terminal state or a human gate. Each phase prints its transition, provider/model, duration and cost. Expect minutes per phase.

### 3. Report what actually happened

Read the record rather than the terminal scrollback:

```bash
plat status <TICKET> --all
plat show <TICKET>            # the decision record: system / agent / human, interleaved
```

Tell the user:

- **Final state per lot**, and for anything not `DONE`, why — quoted from the decision record, not paraphrased.
- **What the reviewer found**, if anything. A finding the coder then fixed is the system working, and worth showing.
- **Cost**, flagging `~` as an estimate — Codex reports tokens, not dollars, so a budget is approximate on that side.
- **Any permission denials.** Non-zero means the agent was *blocked*, not merely unproductive. Never report that as "it didn't find much to do."
- **The diff**, per lot: `git -C <worktree> diff origin/<default>..HEAD --stat`

Report failures with their output. If the gate failed, say so and show it. If a lot halted on a contract error, the transcript is in the `transcripts` table for that attempt.

### 4. Hand back

Plat stops at the wall. It does **not** push, open an MR, transition a ticket, or touch any environment. Tell the user what is ready and let them decide.

If the work is worth keeping: review the diff yourself first — two models agreeing is not verification — then push the branch and open a pull request through whatever the project uses.

## Answering a blocked lot

A lot reaches `BLOCKED` for one of four reasons, and they need different responses:

| Reason | What it means | What to do |
|---|---|---|
| reviewer returned `blocked` | the agent hit a question only a human can answer | read `blocking_question` in the verdict, put the answer to the **user**, then reopen with it |
| oscillation | the same finding fingerprint twice — coder and reviewer are talking past each other | read both sides and rule on it yourself with the user; a third attempt will not fix it |
| attempts exhausted | three tries, still failing | look at what actually changed each time before reopening |
| contract not honoured | the agent never wrote valid verdict JSON | check `permission_denials` and the transcript first; this is usually environmental |

Reopen with the reason recorded. This queues a fresh coding attempt (the `CODING`
option remains a compatibility alias for `PENDING`), resets its retry allowance,
and invalidates dependent lots and obsolete delivery claims:

```bash
plat reopen <TICKET> <lot> --state CODING --note "<the ruling, and why>"
plat start <TICKET>
```

The note goes into the decision record as **your** decision. Write it for someone reading it in six months.

## Watching a long run

```bash
plat ui        # Plat Room on :3033 — live lots, decision record, event tail
```

`stale` in the monitor is relative to how long that phase usually takes in that repo, not a flat timeout. A stale lot with a cold heartbeat is wedged; a long-running one that is still beating is just slow.

## Never

- Never re-run a lot to get a different answer without recording why.
- Never report a lot as done when the gate failed.
- Never transition a Jira ticket or post to Slack as part of a run — those are separate, deliberate acts.

## Full lifecycle and team work

Check `plat workflow status <TICKET>` as well as the lot board. When a full lifecycle
is enabled, accepted context, definition and specification are prerequisites for
`start`; local delivery alone is not readiness. After development, follow the
versioned `workflow integrate`, `accept recording`, `deploy`, and `accept validation`
steps in `docs/WORKFLOW.md`. Record only evidence that was actually observed.
Deployment requires the user's chosen target and authorization to execute its
concrete deploy/smoke/rollback commands. Respect authorization already given in the
conversation; do not add an extra permission loop.

`plat sessions` lists or explicitly attaches Claude and Codex sessions. An attached
interactive writer must be stopped and reconciled before automated execution.
`plat team` provides the shared board, assignments, review requests and questions.
For an expired execution claim, reconcile on the owner installation before reopen.
Never inspect or signal a PID belonging to another host. External messages and
production deployment remain separate actions.
