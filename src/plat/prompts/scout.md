Reconnaissance, not planning. Someone is deciding **how to shape** a piece of work
before paying to plan it in detail, and they need four facts cheaply.

Be fast. Read enough to answer honestly and stop — a thorough answer here costs
more than the decision is worth, and the detailed pass happens afterwards.

## The ticket

{ticket}

## Repositories available

{repos}

## What to determine

1. **Which repos actually change.** Grep for the symbols, files and terms the
   ticket names. Only list a repo if you found something in it — a guess here
   sends the whole run into the wrong tree.

2. **Is this specified enough to decompose?** Could a competent engineer start
   from this ticket without asking a question first? If the answer is no, say so:
   an investigation is the right next step and a confident decomposition of a
   problem nobody understands is the expensive mistake.

3. **Does the change already exist?** Check whether the current branch, or a
   branch named for this ticket, already carries it. If it does, the work is a
   review rather than a build.

4. **Is the objective measurable?** Some work is "chip away until X" — a coverage
   number, a count of remaining call sites, a type-error total. If so, give the
   objective as a comparison and a shell command that prints the number.

## TERMINATION CONTRACT

Write `{out_dir}/scout.json` — write `scout.json.tmp` and rename — matching:

```json
{
  "repos": ["path/as/given/above"],
  "specified": true,
  "reason": "one sentence: why it is or is not ready to decompose",
  "code_exists": false,
  "measurable": null,
  "notes": "anything the person choosing should know; keep it to two sentences"
}
```

`measurable` is either `null` or `{"objective": ">= 80", "probe": "<command>"}`.

Claiming a repo you did not actually find the work in is worse than returning an
empty list, and saying `specified: true` about a ticket you do not understand is
worse still. Both send a person into an expensive pass in the wrong direction.
