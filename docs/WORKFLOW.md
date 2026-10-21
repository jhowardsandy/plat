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

Docs and quality receive the approved master plan, their own lot scope, and
recorded companion-lot states, reported handoffs, and historical gate evidence,
including independent lots without ordering dependencies. These records are not
proof of current revisions, merged PRs, or deployment; remote checkouts are not
read. Required current-scope fixes use `changes_requested`. `pass` requires an
empty findings list. Optional observations and later merge/deployment dependencies
belong in the narrative, explicitly outstanding. Missing required evidence and
real contract conflicts still block acceptance.

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

The Execution tab supports authenticated Start, Pause and Resume requests with
machine acknowledgements and recorded outcomes (see below). Reconcile, reopen,
and deploy remain owner-machine CLI operations. A disconnected board is explicitly
a previous observation and cannot submit new commands until it reconnects.
Service-only runner transport, shared narrative storage and external Jira/PR writes
remain subsequent milestones.


## Authenticated executor controls

Upgrade all local Plat processes and run `plat init` before enabling this feature.
The machine must already have its normal Plat database, repository, role, and
provider configuration. Register it locally with a distinct private credential:

```sh
plat executor register "Justin's laptop" --token-file ~/.plat/team/executor.token
plat executor serve --url http://127.0.0.1:7318 --token-file ~/.plat/team/executor.token
```

Registration is trusted database administration, not public team self-enrollment.
The machine credential cannot authenticate as a team member; a member credential
cannot poll or acknowledge executor commands. Remote service URLs require HTTPS;
redirects are refused to protect bearer credentials. Token files must be private.

In a ticket room, open **Execution**, select the matching online machine, choose
Start/Pause/Resume, inspect the preview, provide a reason, and submit. Engineers
and administrators can issue requests; viewers can inspect their history. All lots
must be assigned to the selected installation and stable host. Assigning an engineer
does not transfer machine ownership. Runs spanning several executors are not yet
supported by these controls.

Start is available for planned work; Resume for paused work. Both revalidate the
workflow version, run status, previewed plan/budget hash, accepted specification, lot states, claims, interactive
writers and role configuration before execution. They use the same independent gates
and serial runner as local execution. Requests never contain a shell command. A CLI
start refuses to race an outstanding browser request. Reopen/reconcile stopped work
locally before requesting a new start.

The visible command lifecycle is:

- **requested**: durable request; no machine acknowledgement yet. It can be cancelled.
- **acknowledged**: owning machine received it; no execution success is implied.
- **running**: the local worker entered the runner.
- **completed**: the control finished. Read its outcome and lot states; this does not
  mean delivery or product readiness. A completed Pause allows in-flight agents to finish.
- **failed/cancelled**: rejected preflight, explicit local reconciliation, or cancellation.
- **uncertain**: a worker crashed or an executor was replaced with unfinished commands.
  Execution is never automatically replayed. A lost heartbeat is also shown explicitly.

A request expires after five minutes before acknowledgement. Only one executor process
holds a machine's 30-second heartbeat lease. HTTP retries retain the original request
identity; a consumed execution cannot run twice. A replacement daemon quarantines
unresolved acknowledgements instead of guessing whether a subprocess started.

Stop the daemon before local command reconciliation and wait for its lease to expire.
Inspect the worker and reconcile any unfinished lot claims first, then:

```sh
plat executor reconcile <command-public-id> --note "Inspected stopped worker and reconciled its claims"
```

To revoke a credential use `plat executor revoke`. This stops future control delivery,
not in-flight agents. To replace a lost or revoked credential, stop the old daemon,
wait for its lease, cancel/reconcile outstanding commands, then register with `--rotate`
and a new token-file path. Historical requests remain associated with the same machine.

The control channel uses authenticated HTTP. The runner still connects directly to the
team database using the installation's existing trusted SQL credentials. This is not
an untrusted multi-tenant executor sandbox or a service-only runner transport. Logs
remain private on the owning machine beside its token file under `logs/`.


Budget ceilings stop further dispatch between phases; an in-flight provider call
can cross the ceiling. A final passing phase may still mark the lot DONE using
its existing gate and verdict evidence, without another agent call. This does not
make an overrun disappear or imply that usage dollars are actual API charges.

### Delivery workspace and external review/CI observations

The ticket Room now has a **Delivery** tab. It shows the current revision for each
lot, revision freshness of integration/package/deployment/validation evidence,
and linked GitHub PR or GitLab MR review and CI observations. The existing
lifecycle readiness checks remain authoritative; external success never advances
stages or replaces independent gates.

Engineers and administrators can save or clear a lot's PR/MR link in Room.
Links must match the lot's canonical repository identity. Updates use optimistic
versions, idempotent requests, and append-only audited artifacts; replacing a link
clears its displayed provider observation while retaining history. Viewers can
inspect but cannot change links.

Refresh status from the owning installation and laptop using its existing `gh`
or `glab` login:

```bash
plat delivery status DEV-1234
plat delivery link DEV-1234 api https://github.com/organization/repository/pull/123 --version 0
plat delivery refresh DEV-1234 api
```

`--version` is the delivery link version returned by `status`, not the workflow
version. Browser updates refresh automatically after an acknowledged link change.
Provider status can be refreshed explicitly through the CLI or requested in the
Delivery tab. Browser polling reads stored observations and does not contact providers. The web server never receives
provider credentials. This slice supports github.com and gitlab.com, including
nested GitLab groups; self-hosted providers remain future work.

Observations include their timestamp and exact PR/MR head. Room warns when the
local worktree is dirty, the observed revision differs, the CI pipeline targets a
different commit, no CI is reported, the current revision cannot be verified, or
the observation is older than 15 minutes. A failed refresh replaces the displayed
success with a failure notice and exits nonzero; prior evidence remains recorded.
Missing approval information is `unknown`, never approval. GitLab displays the
head pipeline aggregate, while GitHub displays reported status checks. Neither
claims completeness of branch protection or merge eligibility.

Only structured status and artifact hashes are shared here. Raw provider payloads,
logs, credentials, stage prose, prompts, and transcripts are not exposed. Refresh
performs read-only provider operations; it does not push, merge, post comments,
or deploy. Explicit shared narratives and publication controls are described below.

In Delivery, choose **Refresh PR / CI status**, review the linked lots and machine,
enter a reason, and select **Request status refresh**. Both the server and executor
must run this release. A registered, online executor must own all lots in the run.
The fixed `refresh_delivery` command reads every linked PR/MR serially on that
machine. It never launches agents or changes the run/workflow state. The request
is bound to the workflow version, run state, plan, budget, and exact link versions.
Changed previews fail at acknowledgement or execution; a link changed during the
provider read cannot be overwritten by the old result.

Room shows requested, acknowledged, running, and completed/failed outcomes with
timestamps, requester, executor, and per-lot results. Completed means the provider
read succeeded, not that CI passed or delivery is ready. Queued requests can be
cancelled; a lost response is retried with the same idempotency key. Disconnection
leaves ambiguous work uncertain and requires local reconciliation; no automatic
replay occurs. Provider failures are sanitized and visible as failed requests.
Existing outstanding execution requests serialize refreshes with other controls.

### Shared delivery drafts and owner-published integration

Delivery now includes explicit shared prose and two additional executor actions:
**Run integration** and **Record package**. Sharing never imports raw narratives,
transcripts, prompts, or logs automatically. All authenticated members can read the
shared content; engineers/admins can edit package drafts and request actions.

Publish an integration recipe on the installation and host that owns every lot.
Publication makes its exact commands visible to the team and authorizes them as
available operations for Room; inspect the file for credentials before publishing.
The browser cannot create or modify these shell commands.

```yaml
checks:
  - name: Producer/consumer contract
    lot: api
    cmd: pytest tests/test_contract.py
  - name: Deployment manifests
    lot: deployments
    cmd: make validate
```

```bash
plat delivery publish-integration DEV-1234 --file integration.yaml --version 0
plat delivery status DEV-1234
```

The recipe is bound to the current clean revisions and accepted specification.
For replacements, use the recipe version returned by `status`. Publication requires
local delivery and a full lifecycle. **Run integration** previews those exact
commands, then the owner executor invokes the existing lifecycle integration
runner with its normal claims, independent checks, and revision validation.
Commands execute with the owner's local permissions, so published checks should
be chosen deliberately. Plat does not invoke the deployment workflow here.
A failed check records failed evidence and does not permit package recording.

Use **Edit and share package draft** to provide summary, rollout, rollback, test
instructions, configuration, and risks. The fields are expandable multiline text;
**Share package draft** explicitly exposes them to the team. Alternatively:

```bash
plat delivery share-package DEV-1234 --file delivery-package.yaml --version 0
```

The CLI imports only those six fields. Both paths preserve immutable prior drafts,
attribute the author, bind the current revisions/specification, and reject stale
edits. Room distinguishes the shared draft from a recorded package source and
shows current/stale/unverified revision status. Editing a draft does not overwrite
an accepted package or advance the workflow.

After successful integration, **Record package** previews the exact draft version
and hash. The owner verifies revisions again and records that draft through the
existing lifecycle acceptance path, advancing to deployment without deploying.
The accepted record includes the requesting member and shared artifact reference.
Both new actions use the same authenticated request/ACK/outcome timeline, queued
cancellation, stale-preview rejection, and no-automatic-replay behavior as the
other executor controls. Shared content changes require reviewing a new preview.

### Recovery and reviewed PR/Jira comments

Room's Delivery tab includes **Recover workflow** for an interrupted integration
or deployment. Inspect the owning machine and, for deployment, the environment.
Enter that inspection in the reason field, review the effect, then choose
**Request inspected recovery**. Recovery records the note and advances an
interrupted `deploying` stage back to `deployment`; `integration` stays at
`integration` for a separately requested rerun. It never kills workers, releases
claims, retries commands, or runs deployment. Active claims and unfinished
attempts must be reconciled on their owner first. An outstanding uncertain
executor command must also be reconciled locally before another request.

**Prepare publication draft** saves explicit comment text for one currently
linked PR/MR or the tracked Jira ticket. Supported destinations are github.com,
gitlab.com, and `<site>.atlassian.net/browse/<key>` matching the run's anchor.
Sync tracker metadata to populate the Jira destination. Arbitrary URLs, new PRs,
review approvals, merges, Jira transitions, and self-hosted providers are outside
this control's scope.

1. Choose the destination and write the exact comment in the expandable editor.
2. **Share publication draft** makes that text visible to authenticated teammates;
   it does not contact the destination.
3. Choose **Review publication**. Inspect the destination, entire comment, draft
   version/hash, and owning machine. Enter the reason, then explicitly choose
   **Publish reviewed comment**.
4. Follow requested → acknowledged → running → completed/failed/uncertain.
   Completed means the provider accepted the comment and it was read back.
   **Open published comment** links to the verified receipt.

The owner must have authenticated `gh`, `glab`, or `twg` available on its PATH.
TWG calls explicitly select the destination site and use plain text; GitHub and
GitLab comments use their native Markdown. CLI text is passed as arguments or a
private JSON file, never interpolated into shell commands. Provider credentials,
raw responses, and errors are not shared with the browser.

Publication requires local delivery, accepted specification, and matching clean
revisions. PR/MR publication additionally checks the provider head against the
reviewed lot revision. Draft, destination, workflow, run, requester, and machine
authorization are rechecked before dispatch. This is a reviewed snapshot; an
external head can still change after the provider read. Publication does not
advance lifecycle readiness or certify the prose as independently verified.

Before posting, Plat durably records a publication attempt for that draft.
A draft with an attempt can never be dispatched again, even after local
reconciliation. There is no automatic POST retry. A timeout, interrupted worker,
malformed response, or failed read-back after dispatch means **uncertain**:
inspect the actual destination before making any further publication decision.
Jira verification scans up to 1,000 comments; an unverified receipt remains
uncertain. If local reconciliation is needed, stop the executor, wait for its
lease to expire, and use:

```bash
plat executor reconcile <command-id> --note "Inspected destination and owning process; observed outcome ..."
```

Only prepare a new draft after inspecting whether the original comment exists.
A new draft is a new publication authorization and can intentionally post the
same text again. Previous drafts, attempts, receipts, and decisions remain in
the audit history. Both team server and executor must reload this release.

### Reviewed test deployments from Room

After recording a current delivery package, publish a deployment recipe from the
installation and host that owns every lot. The recipe names one test environment,
its concrete target (for example cluster/context and namespace), the exact
artifact to deploy, and the artifact to restore. Use immutable image digests or
release identifiers. Supply project-owned deploy, smoke, and rollback commands:

```yaml
environment: dev
target: example-dev-cluster / example-service namespace
lot: deployments
artifact: registry.example/service@sha256:<new-digest>
rollback_artifact: registry.example/service@sha256:<previous-digest>
deploy: ./scripts/deploy-dev.sh <new-digest>
smoke: ./scripts/smoke-dev.sh <new-digest>
rollback: ./scripts/rollback-dev.sh <previous-digest>
```

```bash
plat delivery publish-deployment DEV-1234 --file test-deployment.yaml --version 0
plat delivery status DEV-1234
```

All eight fields are required; extra fields are rejected. Only `local`, `dev`,
`demo`, and `beta` environment labels are accepted. Replacing a recipe requires
its current artifact version from `delivery status`, not the workflow version.
Publishing stores an immutable recipe bound to clean revisions, the accepted
specification, and the exact recorded package hash. It does not deploy.

In Room → Delivery, inspect **Owner-published test deployment**, then choose
**Review test deployment**. Review the owning machine, environment and target,
artifact, rollback artifact, package/recipe hashes, and all three exact commands.
Enter the reason and choose **Deploy reviewed test artifact**. Only an authorized
engineer/admin can request it; the browser cannot author shell commands. Changes
to the recipe, package, revisions, workflow, or authorization invalidate the
request at acknowledgement or before execution.

The owning executor runs the existing claimed lifecycle deployment path. A failed
deploy or smoke invokes the declared rollback while execution remains authorized.
Room displays the deploy/smoke/rollback exit codes without sharing raw command
output. A successful deployment advances to **validation**, where observed
product behavior still needs explicit acceptance. Failed smoke or rollback does
not imply readiness. Command acknowledgements and outcomes remain in the control
timeline, with the requesting member and exact shared recipe recorded in evidence.

An interrupted deployment stays **uncertain** with workflow stage `deploying`.
Inspect the actual environment and owning process, reconcile the command/claims,
then use the inspected recovery control before an explicitly requested rerun.
There is no automatic replay after an uncertain outcome.

The environment and artifact fields describe the owner-authored commands; they
are not a shell sandbox or a registry verification service. The owner must ensure
commands use the stated target and immutable artifact, smoke verifies the deployed
version, and rollback restores the stated prior artifact. Keep secrets in the
owner's local credential setup, not in the shared recipe. Coordinate shared
environment changes across runs; execution ownership is scoped to the lot.
