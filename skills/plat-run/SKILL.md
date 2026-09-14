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

### 1. Check the state before starting

```bash
plat status <TICKET> --all
```

If lots are already `DONE`, say so — `plat start` skips them, it does not redo them. If one is `BLOCKED`, go to **Answering a blocked lot** below instead of starting.

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

Reopen with the reason recorded:

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
