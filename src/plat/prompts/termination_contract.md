## TERMINATION CONTRACT — non-negotiable

Before you finish you MUST write two files into `{out_dir}`:

1. **`{phase}.md`** — narrative, for a human and for the next agent's context.
2. **`{phase}.json`** — the control plane. It must match the shape below EXACTLY.

Write each to `<name>.tmp` first, then rename, so a reader never sees a partial file.

The JSON is the only thing the orchestrator reads. If it is missing, unparseable, or
missing a required field, your work is discarded and the lot halts. Do not put
control-plane information only in prose.

### The exact shape of `{phase}.json`

```json
{shape}
```

Every field shown without a comment is REQUIRED. Integers must be integers, never
`null` — if you did not run the tests, write `"ran": false` with `"passed": 0,
"failed": 0`.

### Reporting decisions

Populate `decisions[]` ONLY where a real alternative existed and the choice was
consequential. `alternatives` must be non-empty. If nothing else was on the table it
was not a decision, it was just doing the work — omit it. An empty `decisions[]` is a
valid and common answer.

### Honesty

Report what you actually did. The supervisor independently re-runs the tests and
checks the diff, so an inaccurate claim is caught and fails the gate. An honest
`"partial"` is always better than an optimistic `"complete"`.
