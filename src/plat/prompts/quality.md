# Quality assessment for {anchor}/{lot}

Inspect {worktree} read-only. Review maintainability, security boundaries, error recovery,
resource cleanup, observability, test coverage of failures, and compatibility of contracts.
Do not duplicate unsupported stylistic preferences as blocking findings. Identify concrete
failures with file references and required fixes. Write quality.md and quality.json only
in your output directory. Never modify the worktree. Missing evidence is not passing evidence.

{context}

## Scope and rollout boundaries
Assess required changes in this lot against its approved scope. Do not demand that
a companion lot already be merged or deployed unless an acceptance criterion
explicitly requires it at this stage. Record later integration, merge and rollout
dependencies in the narrative with their evidence and unknowns; never claim them
as complete. Missing required evidence or a real contract conflict still blocks
acceptance. Optional improvements belong in narrative, not findings.

## Acceptance scope
{criteria}

## Independently observed checks
{gate}

{termination}
