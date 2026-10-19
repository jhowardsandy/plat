# Documentation package for {anchor}/{lot}

Inspect {worktree} read-only. Write a self-contained documentation artifact in docs.md
in your output directory: behavior changes, public contracts, configuration/migrations,
operational instructions, test instructions, rollout and rollback notes, and known limitations.
Use concrete evidence from the code. Flag missing required documentation as findings.
Do not edit the repository. Repository documentation edits belong to the coder and receive
fresh verification on retry. Do not claim a deployment or approval happened.

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
