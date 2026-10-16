# Local work through a team handoff

Plat keeps agent execution, observed verification, human acceptance, deployment,
and shipment as separate facts. Use the full lifecycle for new work; existing
`plan → start → close` records retain their local-development semantics.

## Install and qualify

```sh
uv tool install --editable . --force
plat init
plat doctor
plat skills --provider all
plat probe
```

`init` runs transactional Alembic upgrades and refreshes projections. Back up an
existing database before an upgrade. Duplicate lot keys or duplicate normal-run
anchors within an installation stop migration without deleting history.
`doctor` checks binaries, role configuration, schema version, and expired claims;
it does not prove account/model access. `probe` performs real provider calls.

Skills are packaged in wheels as well as editable installs. Claude links go in
`~/.claude/skills`; Codex links go in `~/.agents/skills`, following the
[official local skill convention](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).
Existing unrelated directories or links are preserved. Use `--provider claude`
or `--provider codex` to select one installation target.

## Context, definition, planning, specification

Start before writing code:

```sh
plat workflow begin DEV-123 --owner justin --title 'The requested change'
plat workflow accept DEV-123 context --file context.yaml --version 0
plat workflow accept DEV-123 definition --file definition.yaml --version 1
plat plan plat.yaml --commit
plat workflow accept DEV-123 specification --file acceptance.yaml --version 2
plat start DEV-123
```

Working files remain in your notes location. Accepted input is snapshotted in
Postgres. Versions are optimistic concurrency checks, not stage numbers: use
`plat workflow status DEV-123` after any revision or recovery to obtain the current
version. A stale submission fails instead of overwriting someone else's decision.

`context.yaml`:

```yaml
summary: Explain the ticket, affected repositories, and existing behavior.
sources:
  - reference: 'ticket:DEV-123'
    status: collected
    snapshot: The relevant ticket contents, including requirements and constraints.
  - reference: 'design:optional-reference'
    status: unavailable
    reason: Explain the missing source and why it does not block this scope.
blockers: []
```

`definition.yaml`:

```yaml
problem: Describe the observed problem.
outcome: Describe the behavior we need.
scope: List the included and excluded changes.
success_measures: [A measurable result tied to the acceptance criteria]
open_questions: []
```

`acceptance.yaml` contains `note: <why this scope is accepted>`. Specification
acceptance binds the current plan hash and accepted context/definition hashes.
Starting is blocked until these inputs are accepted. Revising an unstarted plan
invalidates its prior specification acceptance. Started scope remains immutable;
use a new anchor for scope changes.

## Execution and sessions

The implemented phase order is `code → gate → review → docs → quality`; select
phases in the spec. `docs` creates a documentation artifact in the attempt output
directory, covering behavior, configuration, operation, testing, and rollout.
It does not edit repository documentation. Missing repository documentation is a
finding for the coder to fix and verify on another attempt. `quality` independently
assesses maintainability, security boundaries, recovery and coverage. Both phases
have their own prompts, contracts, provider-failure handling and worktree-mutation
checks. Configure the `documenter` and `reviewer.quality` roles before enabling them.

```sh
plat start DEV-123 --workers 3
plat start DEV-123 --dispatcher celery --workers 3
```

Parallel execution schedules only eligible independent lots. Bounded-dollar runs
remain serial: concurrency requires `budget_usd: 0`. Cost figures remain
API-equivalent estimates and do not enforce a provider billing limit. Celery uses
JSON messages on `plat.<installation-id>`; start a worker with the same Plat config:

```sh
uv run celery -A plat.dispatch.celery_app:app worker --concurrency 1
```

A job addressed to a different installation is rejected. Redis/broker credentials
are administrative credentials; keep the broker private. Duplicate starts cannot
acquire a second active claim. Completed lots are skipped on job replay. A crash
leaves an interrupted attempt requiring explicit reconciliation, not automatic
re-execution against a possibly moving worktree.

Claude and Codex launched sessions record owner, installation, host, phase,
provider session ID, activity and outcome. Resume checks provider, installation and
host; reviewers start fresh. Attach an existing interactive session explicitly:

```sh
plat sessions attach DEV-123 api codex '<real-session-id>' --note 'Working interactively'
plat sessions list DEV-123
plat sessions attach DEV-123 api codex '<real-session-id>' --state completed --note 'Committed and stopped'
```

Attachment is an observation, not proof of code completion. It grants no remote
process control. An unresolved interactive writer blocks automated execution.
Manual attachment does not scrape desktop chats or install provider hooks.

## Execution ownership and recovery

An active database claim covers the lot and host-local worktree, with a generation
used to reject stale results. Expiry alone never permits takeover. Host-local PIDs
are never inspected or terminated from another host. Recover on the owner machine:

```sh
plat doctor
plat workflow reconcile DEV-123 api --note 'Supervisor stopped; inspected worktree and confirmed child exit'
plat reopen DEV-123 api --note 'Recovery decision and the answer to the blocker'
plat start DEV-123
```

The supervisor must have stopped, the claim must have expired, and its subprocesses
must be gone. Reopening invalidates dependent lots and delivery evidence. A paused
run uses `plat pause DEV-123 --resume` instead.

For a never-started pending lot assigned to another installation, clone its
canonical repository locally and explicitly bind it:

```sh
plat workflow localize DEV-123 api services/api --note 'Taking execution on this machine'
```

Repository identity must match, and the original base commit must be available.
Started work is not silently moved between machines. This is a trusted executor
operation using the central database; see the team trust boundary below.

## Integration, delivery package, test deployment and validation

When all lots are DONE, run integration checks against committed worktrees:

```sh
plat workflow integrate DEV-123 --file integration.yaml --version 3
```

```yaml
checks:
  - lot: api
    cmd: ./scripts/integration-test.sh
```

Each check runs independently and records command, exit code, output, host and
execution ownership. Failed checks cannot advance. The revision set includes
canonical origin, commit and content fingerprint. Downstream evidence is stale
when that set changes.

Record the delivery package with `plat workflow accept DEV-123 recording --file
package.yaml --version 5`. Required fields are `summary`, `rollout`, `rollback`,
`test_instructions`, `configuration`, and `risks`. Say `none` with a reason when a
field does not apply. The package also pins the accepted spec and integration
record; include PR references and deployment artifact identifiers in the prose.

Deployment is explicit. Preview the file first, then pass `--execute` to authorize
its commands, including its rollback command:

```sh
plat workflow deploy DEV-123 --file deployment.yaml --version 6
plat workflow deploy DEV-123 --file deployment.yaml --version 6 --execute
```

```yaml
environment: dev                  # local, dev, demo, beta only
lot: api
artifact: 'registry/service@sha256:<digest>'
deploy: './scripts/deploy-dev.sh <digest>'
smoke: './scripts/smoke-dev.sh'
rollback: './scripts/rollback-dev.sh <previous-digest>'
```

These are project-supplied commands run on the owning executor. Plat does not
invent cluster contexts, credentials, artifacts or environment targets. Commands
should be bounded and idempotent. A failed deploy or smoke triggers the declared
rollback; failure and rollback results remain visible. Production execution is
not exposed. If the supervisor dies during deployment, the stage remains
`deploying`; inspect the actual environment, reconcile ownership, then use
`workflow recover-stage --note ... --version ...` before retrying. No automatic
success or rollback is inferred after a crash.

Pausing or aborting stops subsequent commands, including an automatic rollback.
Inspect the environment and explicitly perform any needed recovery before retrying.

After observing the product behavior, accept validation:

```sh
plat workflow accept DEV-123 validation --file validation.yaml --version 8
plat workflow status DEV-123
plat workflow abstract DEV-123 --out delivery-record.json
```

```yaml
accepted: true
note: Who observed which environment and what behavior was confirmed.
checks: [Concrete validation steps and observed results]
```

Readiness requires current integration, package, deployment and validation
evidence, completed lots, and no open questions or unresolved/stale requested
reviews. `plat close DEV-123 --note '<actual shipment evidence>'` is still a
separate human action. Deleting worktrees does not delete the recorded package or
abstract; current readiness checks report unavailable live revisions honestly.

## Team collaboration

One control-service database represents **one trusted team**. Administrators
provision per-person bearer tokens into private files; the service stores only
hashes. Public self-registration is disabled. A database administrator bootstraps
the first admin; that admin can issue named, single-use invitations over the team
service. Viewers cannot mutate. Engineers create assignments, questions, notes and
review requests. Only the assignee or an administrator resolves an item. Review
outcomes pin revisions, and changed revisions invalidate approval.

```sh
plat team member-add justin --role admin --token-file /private/path/justin.token
plat team serve                      # http://127.0.0.1:7318
plat team board --token-file /private/path/justin.token
plat team events --after 0 --token-file /private/path/justin.token
```

After upgrading with `plat init`, invite the second laptop or a teammate:

```sh
# Administrator's laptop; use the real HTTPS team URL for remote access.
plat team invite justin-laptop-two --role engineer --hours 24 \
  --token-file /private/path/justin.token \
  --invitation-file /private/path/laptop-two-invitation.json --url https://plat.example.com

# Recipient's laptop, after privately transferring the invitation file.
plat team join --invitation-file /private/path/laptop-two-invitation.json \
  --token-file /private/path/laptop-two.token --url https://plat.example.com
plat team board --token-file /private/path/laptop-two.token --url https://plat.example.com
```

The invitation is a 256-bit secret bound to the name and viewer/engineer role
selected by the admin. It expires after 24 hours by default (maximum seven days),
and cannot create another member after redemption. It is stored hashed on the
server and never included in audit events. Treat the file as a temporary credential:
anyone holding it can claim that identity before its intended recipient does.
There is no email verification or device binding. Share it privately, never in a
repository. Each team-service database is a separate workspace; invites grant
access to that entire team's board, not selected repositories.

`join` writes a private member token before making the request. If the response
is lost, retain that file and repeat the same command with `--retry`; only the
same token can recover the result. It never creates a second account or replaces
an existing token file. Revoke an invitation using `plat team invite-revoke <id>
--token-file <admin-token-file> --url <service-url>`. For someone who already
joined, a database administrator uses `plat team member-revoke <name>` to revoke
their access. Invite revocation alone does not remove an admitted member.

Invitations grant collaboration-service access only. They never distribute SQL,
Redis or provider credentials. Executor access is separately provisioned to
trusted laptops. `member-add` remains a database-administrator operation; there
is no unauthenticated member-provisioning endpoint.

Open the board and enter the token; it stays in memory for the page session. Filter
by ticket, person or repository, see work and sessions, assign responsibility,
request a review, ask a question and record responses. The board shares summaries,
not prompts, transcripts, local PIDs or local paths. CLI board snapshots are cached
with a visibly stale/disconnected label; mutation success is never fabricated
offline. Events have durable replay cursors, and command retries use idempotency
keys plus expected item versions.

For remote access, put the service behind an HTTPS reverse proxy and bind with
`--host <private-interface> --behind-tls`. Tokens are provisioned through invitations
or database administration; SSO and multiple team tenants are not implemented. Team clients need only
the service URL and their own token. Trusted executor machines use the central
Postgres connection and local provider credentials; they are administrators of
that execution plane, not untrusted web clients. Do not give SQL or Redis
credentials to viewers. A service-only remote executor transport is not implemented.

The service issues collaboration actions, never remote shell commands. To submit
one from the CLI, save JSON and use `plat team command command.json --token-file
... --url https://...`. For example:

```json
{"key":"unique-retry-stable-id","action":"review","plat":"<public-plat-id>",
 "owner":"teammate","body":{"text":"Review the changed API contract"}}
```

Actions: `assign`, `question`, `review`, `note`, `reassign`, `answer`, `review_result`.
Updates also need `item`, its current `version`, and a `note`. Review results need
`outcome: approved|changes_requested`. Reuse the entire command and key after an
unknown network outcome. No Slack/email/ticket message is sent by these actions.

## Backups and release qualification

Use PostgreSQL's standard `pg_dump -Fc` and `pg_restore --exit-on-error` against an
empty replacement database. Keep credentials in your normal private connection
configuration, not shell history. Include database records and accepted artifacts;
worktrees are not a substitute for the record. Verify restoration and then run
`plat init` and `plat doctor` against the restored database before switching over.
There are no destructive Alembic downgrades: restoration is the rollback path.

The regression suite uses isolated schemas and disposable repositories. Live
provider probes are separate because they use account access. Before a team demo,
run a small real ticket locally, inspect the decision record, exercise recovery,
and verify the actual project deployment/smoke/rollback commands. A synthetic
fixture proves orchestration; it does not establish product acceptance for a real
service or a two-machine operational pilot.

### Evidence-only investigations

Set `kind: investigation` on a lot when the output is a researched answer rather
than a code change. The agent must leave the worktree unchanged and write its
report as `code.md` with a typed criterion verdict. Supervisor checks and review
still run; only the mandatory code-diff requirement is waived. Use verifiable
criteria for the facts being investigated and select `review` for assessment of
the report. Investigation is distinct from a failed developer producing no diff.

## Plat Room workspace

The authenticated web board now provides **Needs attention, My work, Tickets,
Products, Engineers, Services, Repositories, Reviews, and Delivery** views. Each
view groups the same recorded lots. Product/service groups may overlap; the header
counts distinct tickets and lots once. Shipped work is hidden by default and can
be included. Grouping, sorting, product scope and search filters are carried in the
URL fragment, so a teammate can open the same view after authenticating.

Open a ticket to inspect configured phases, budget and recorded spend, readiness,
acceptance history, criteria, findings, phase attempts, tracked sessions, and the
latest 200 decisions. Artifact hashes and names identify evidence; raw artifact
bodies, prompts and transcripts are not exposed by these endpoints. This is an
artifact manifest, not a shared transcript archive.

Use **Overview → Organize repository** to set product/service labels. Multiple
labels are supported. Mappings use the recorded repository identity, with
installation-scoped fallback for legacy work; unmapped repositories appear under
Unclassified. Updates use an expected version, so conflicting edits fail rather
than overwrite another person's classification. This classification organizes work;
it is not a change to the accepted execution scope.

**Coordinate** supports assignments, questions, review requests, notes, responses,
and reassignment. Select an active team member and ticket/lot scope, then write a
multiline brief. The editor grows with its contents and can be expanded manually.
Reassignment preserves its new brief and records the decision. An assignment does
not transfer execution ownership. Browser polling preserves an unsent room draft;
leaving a dirty form asks before discarding it.

Saved views are persisted in Postgres. Everyone can save personal views; engineers
and administrators can share views with the team. Only the owner (or an administrator
for shared views) can delete them. Viewers cannot classify repositories or issue
collaboration commands. Unknown mutation outcomes retain the same retry key in
session storage, scoped to the credential hash; the token itself is kept in memory.

This workspace release does not issue remote execution or deployment commands.
Start, pause, reconcile, reopen and deploy remain owner-machine CLI operations.
A disconnected board is explicitly a previous observation, and cannot submit new
commands until it reconnects. Private executor transport, stable machine identity
recovery, shared narrative storage and external Jira/PR writes remain subsequent
milestones.
