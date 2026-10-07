# Specification 012: Forgejo to GitHub issue metadata mirror

Issue: [#75](https://forgejo.infra.supermorphic.com/supermorphic/homelab-playbook/issues/75)

Status: implementation prepared; production enrollment and live acceptance remain separate operator actions.

## Purpose and authority

Keep an independently searchable GitHub copy of Forgejo issues, comments,
labels, milestones, and open/closed state. Forgejo remains authoritative.
Run on NUC #4 through workstation-managed Ansible, independently of Semaphore,
Actions, runners, n8n, and Kubernetes. Existing native Git mirroring remains
responsible for commits, branches, and tags.

This is a reconstruction copy. Full Forgejo backups still own PRs, reviews,
attachments, exact application identities, settings, authentication, and Actions
history. [Specification 010](010-forgejo-service.md) owns those backups and native
Git mirroring; issues #58 and #9 own collaboration cutover and broader recovery.
This service neither changes repository authority nor promotes GitHub on failure.

## Selected implementation and alternatives

Use a repository-owned Python standard-library executable and a separate Ansible
role. Install it on Debian as a host-native helper. A system-level oneshot unit
runs as a dedicated non-login account, separate from `svc-forgejo`. A persistent
calendar timer invokes reconciliation approximately every quarter hour and catches
up after downtime. There is no listener, webhook, container, or application database.

Alternatives considered:

- Adopting Gitea Mirror adds a web application, database, and Git replication.
  Its documented GitHub destination supports code replication, while issue
  metadata requires a GitHub source. It does not provide the requested operation.
- Forgesync demonstrates attribution and durable marker discovery and synchronizes
  collaboration in both directions. Its writes back into Forgejo conflict with
  this service's read-only source. Review its patterns without copying implementation
  or licenses.
- Event forwarding needs ingress and reliable event delivery. Periodic comparison
  directly repairs missed runs and edits with fewer operating dependencies.

## Enrollment and protected inputs

Each explicitly enrolled mapping binds one Forgejo HTTPS origin and repository
to one existing GitHub repository. Pin both repositories' API identities as well
as their owner/name paths, expected destination visibility, and the declared
destination actor. Reject duplicate destinations and mismatched identities.
Repository transfers or renames require reviewed configuration changes.
Default enrollment is empty; implementation does not discover or enroll production
repositories automatically.

Enrollment authorizes publication of the selected source issue history into the
declared destination visibility. A private source requires explicit operator
review before publication to a public destination. Stop on visibility drift.
GitHub repository creation, permission changes, and adoption of historical GitHub
objects are separate operator actions.

Use a non-admin Forgejo identity with selected-repository `read:issue` and
`read:repository` token scopes. Use a dedicated GitHub metadata-mirror credential:
a fine-grained token issued by the destination repository owner, restricted to
explicitly selected destination repositories, with Issues write permission and
repository metadata access. Mirrored issues and comments use that owner's identity;
pin it as the destination actor. The token owner must also have repository
Write access: GitHub can silently omit issue labels and milestones when the account
lacks the required repository access, even if the token permits issue operations.
Keep the token limited to Issues and metadata; account membership does not justify
requesting Contents write, Actions, repository administration, or source mutation
permission. Check effective account access during enrollment and each run, and
prove label and milestone behavior during separately authorized live acceptance.

The original design required a separate GitHub automation account. That choice
is superseded for personally owned destinations: GitHub fine-grained tokens do
not support repository collaborators. The owner-issued token preserves the
repository and permission limits while sharing the owner's posting identity.

The operator enrolls durable tokens in a service-owned sibling SOPS inventory file
through [Specification 005](005-sops-age-secrets.md). Ansible handles secret tasks
with `no_log` and installs root-only credential files. systemd `LoadCredential`
delivers read-only runtime copies to the service. Live secret values never appear in
arguments, environment definitions, unit text, output, status files, or test data.
The runtime never obtains a SOPS identity or uses the operator Git credential helper.

Record the GitHub token owner, selected repositories, permissions and expiration
beside its public inventory reference. A non-expiring token records expiration as
none; token renewal is not automatic. To rotate, create a replacement with the
same limits, update the same credential reference in the owning SOPS file,
provision and verify the replacement, then revoke the previous token. Preserve
the mapping identities and service state during rotation.

Use the existing trusted HTTPS path to Forgejo, including the required CA chain,
and normal system trust for GitHub. Do not bypass certificate or hostname checks.
Accept only configured HTTPS origins. Reject redirects and pagination links that
leave the selected API origin or repository boundary; never forward authorization
to an unexpected host. Read-only source URLs from API content are attribution,
not instructions to fetch attachments or arbitrary resources.

## Durable identity and destination ownership

Identity consists of a stable configured source-instance key, numeric Forgejo
repository ID, object kind, and numeric object ID. Issue numbers and titles are
display information, not mapping keys. Preserve the instance key across host
rebuilds. A restored source with changed object identities requires reviewed
re-enrollment rather than guessing correspondence.

Append a versioned machine-readable marker with fixed-order `key=value` fields
to the generated issue/comment body and milestone description. Keep a separate
service namespace: Forgesync identifies repositories by host and path and issues
by their displayed number, so its markers are not interchangeable with these
stable API identities. The runtime defines the exact encoding; keep it compact
enough to preserve complete label identities within GitHub's description limit.
A generated footer identifies the source repository,
original issue number, author and creation time where available, and source URL.
Do not impersonate users or imply that GitHub timestamps are original timestamps.
Parse only the canonical terminal marker, outside source text, so marker-like
content inside a source body cannot define destination ownership.

Labels have no Markdown body. Put a compact source identity marker in their
description, followed by as much original description as the API permits. Use
a single line because GitHub converts label-description newlines to spaces;
retain read compatibility with the original newline-separated format. Use
a deterministic name containing the source label ID and a readable source name.
Bound the readable part to GitHub's name limit without truncating the identity.
Preserve full original names in issue attribution. Label description truncation
is an explicit reconstruction limitation; full backups preserve exact values.
Milestones likewise use a readable title with a source-ID suffix so pre-existing
destination names do not become implicit matches.

Only update objects carrying an unambiguous marker for the enrolled source.
For issues and comments, also require the declared automation actor as creator.
Reserved names alone do not authorize adoption of a label or milestone.
Duplicate, malformed, conflicting, or missing ownership markers on recognizable
service objects stop that mapping with an actionable conflict. Do not silently
take over or merge historical GitHub issues, comments, labels, or milestones.

Edits to owned GitHub fields are replaced by source values on reconciliation.
Unmarked GitHub objects and human-authored comments are preserved. GitHub edits
never become source writes. The source HTTP client supports GET only, and the
destination client permits only the declared issue-metadata endpoints and methods.

An owner-issued token shares its actor with ordinary GitHub history. Actor identity
alone therefore does not make an unmarked issue or comment a service object.
Generated attribution or a damaged terminal service marker identifies a
recognizable shadow. The safety journal also retains the source identity and
destination locator of observed issue/comment shadows. This evidence prevents a
known shadow whose entire body is replaced or whose object is deleted from being
silently recreated. It never authorizes adoption or updates without a valid marker.
Preserve this evidence during attended recovery. If both this evidence and all
service attribution and markers are lost, an object cannot be distinguished from
ordinary owner-authored history; resolve that loss through attended recovery.

## Reconciliation and API boundaries

Every run acquires the service operation lock and validates configuration,
repository identities, destination actor and visibility before any writes.
Build complete paginated inventories of source issues, labels, and milestones,
and destination shadows. Read all states, including closed issues and milestones.
Never treat an incomplete page sequence or an API error as an empty collection.
Bound response sizes, page counts, execution time, and per-run mutation requests,
including rejected requests; reaching
a discovery limit is failure, not a partial inventory accepted as complete.

Request Forgejo issues with `type=issues` and additionally reject records carrying
PR metadata. GitHub also mixes PRs into issue lists; exclude them before marker
discovery or mutation. Fetch source comments only for accepted issues, using the
installed API's issue-comment listing contract. Do not assume every Forgejo list
endpoint has identical pagination parameters. Filter non-issue comment records
and test any repository-wide comment optimization against the accepted issue set.

Reconcile labels and milestones before dependent issues, then issue comments.
Apply title, generated body, source label membership, milestone assignment, and
open/closed state to each owned issue. Apply comment edits as well as additions.
Milestone changes include title, description, state, and due date. Convert due
timestamps to their UTC calendar date before writing; GitHub stores midnight for
the supplied date. Labels include name, color, and the bounded description.
Explicitly clear removed issue labels,
milestone assignments, and milestone due dates where supported by the API.
Compare normalized supported fields before writing. Repeated converged runs make
no effective writes. Unrelated GitHub fields do not cause perpetual updates.
Read back every created or updated object and compare all supported fields with
the intended source projection, including labels, milestone assignment and cleared
values. A successful HTTP response or matching marker alone is insufficient.
Report silently ignored or mismatched fields as failed reconciliation; do not
advance last-converged evidence. That evidence describes the source data observed
by the run, not an atomic snapshot across both services.

Do not delete any destination object because it disappears from the source.
Retain formerly mirrored issues, comments, labels, and milestones. Removing a
label from an existing source issue changes that issue's membership; it does not
delete the label object or unrelated history. No recovery-only inference closes
an issue that is simply missing from the source.

Full inventory comparison is the initial implementation. Avoid relying on issue
update timestamps to discover comment edits or label changes. A bounded run may
leave valid work for the next timer invocation and reports that backlog explicitly.
Use deterministic ordering and skip converged objects so historical backfill makes
progress across runs. Failure in one mapping must not prevent independent mappings
from being examined, but the overall command fails if any mapping fails.

Make API requests serially and pace destination writes. Respect GitHub primary
and secondary rate limits and `Retry-After`. A bounded backoff or deferred retry
must remain visible; it is not successful convergence. Do not log API payloads,
authorization headers, raw error bodies, or source issue text. Report mapping,
operation, sanitized error class, counts, and timestamps instead.

## Interrupted creation and rebuild safety

Markers must be part of the original create request, not added by a later patch.
Before each create, rediscover destination ownership and atomically record a small
pending-create intent in the durable service state document. The intent contains
identity and operation metadata, not credentials or issue content. Flush the file
and its directory before sending the request. Retire the intent when discovery
finds exactly one correctly owned object; read-back of all supported fields must
still pass before reporting convergence. A field mismatch becomes repair work for
that existing object, never another create.

Distinguish confirmed non-creation from an unknown outcome. A validated API
rejection that guarantees no object was created, or a transport failure proven
to occur before sending the request, retires the intent as rejected. A later run
may retry after correcting the cause and respecting any retry deadline. Persist
that transition atomically; a crash before persistence remains conservative.
Do not classify all non-success responses as definite rejection. Timeout, lost
response, malformed success response, ambiguous server error, and an interrupted
request retain the unresolved intent. Offline tests must cover both paths.

After a crash or uncertain HTTP outcome, rediscover the marker. An existing shadow
completes the intent and normal reconciliation continues without another create.
An unresolved intent blocks another POST for that identity. Do not blindly retry
creation after a timeout or ambiguous server error. Other independent work may
continue; report the unresolved operation as failure.

The state document contains both initialization evidence and pending intents, so
loss of the file also removes permission to mutate destinations. Bind initialization
to the enrolled source/destination identities and destination actor. Missing,
corrupt, incompatible, or mismatched state permits read-only discovery but blocks
all destination writes. Provisioning and timer invocations never initialize it
implicitly. An empty pending list in an otherwise valid initialized document is
distinct from a missing document.

An attended bootstrap explicitly selects initial enrollment or recovery. Initial
enrollment requires operator confirmation that no earlier writer or unresolved
create exists for those mappings. Recovery requires stopping previous writers,
preserving any available state, and rediscovering destination markers. Preserve
unresolved intents when state survives. If state is lost, require an explicit
operator decision that earlier create outcomes have been resolved before writing
new initialization evidence. Confirmation binds the exact mappings and operation;
it is not authorization. Record the selected mode and decision reference without
secrets or issue content. Changes to the bound enrollment identities invalidate
initialization until reviewed re-enrollment; ordinary provisioning must preserve
existing state.

This document is a safety journal, not an authoritative mapping database. A fresh
installation reconstructs ordinary mappings from API markers after attended
initialization. REST creation cannot promise exactly-once delivery when both the
response and local uncertainty evidence are lost. Resolve an absent shadow only
after confirming no prior request can still complete; record that decision before
permitting another create. Discovery of an existing shadow can resolve an intent
automatically; clearing an unresolved absent shadow requires an attended decision.

The same principle applies when a marker is removed or ownership becomes ambiguous:
stop and repair the identified object through an explicitly authorized operation.
Do not manufacture a replacement based on a title match.

## Host lifecycle and observation

The service role owns a locked non-login account with an explicit inventory UID/GID,
private state, root-owned executable/configuration, systemd units, and credentials.
Do not allocate subordinate IDs, Podman storage, linger, or access to Forgejo data.
Validate existing identity ownership and refuse incompatible accounts instead of
renumbering them. The account receives no SSH keys, sudo authority, or unrelated
supplementary groups.

systemd owns timeout, persistent scheduling, process termination, and observable
exit status. The single oneshot unit plus a nonblocking filesystem lock prevents
overlap with manual invocation. Use filesystem protection, no new privileges,
private temporary storage, and bounded memory/tasks. Network access remains
necessary for the two HTTPS APIs; do not claim account separation alone restricts
egress. Persist retry deadlines by credential so restarting the process cannot bypass
throttling and independently credentialed mappings can still progress. Publish
sanitized per-mapping status atomically, including last attempted
run, last converged run, backlog, and unresolved creates. A partial run does not
advance last-converged evidence. Verification checks freshness separately from
the last exit status.

Expose provision and observational verify through `mise run playbook`, preserving
its dependency, inventory, connection, and task-selection guards. Support production
only until staging has separately approved inputs. Provision installs
the service definitions; starting an enrolled timer is an external publication action requiring
explicit authorization for its mappings. Verification observes installed definitions,
ownership, timer state, and existing status without synchronizing metadata.
Manual reconciliation uses the same executable and lock as the timer.

Recovery prerequisites are a GitHub checkout of this repository, the workstation
toolchain, independent SOPS identity recovery, restored enrolled credentials,
source availability, and the preserved source-instance key. Reinstall through the
canonical playbook path after separately authorizing the target. Stop the timer
before investigating ownership or pending-create failures. Preserve its safety
journal, inspect the identified GitHub object, resolve uncertain outcomes, and
restart only after an authorized repair. Missing state requires the attended
recovery bootstrap; copying configuration or rerunning provision cannot enable
writes. No separate mapping database restore is needed for normal marker discovery.

## Attended operation and recovery

The `forgejo_metadata` inventory group and its UID/GID, protected tokens and
repository mappings require a separately authorized enrollment change. The
playbooks do not add a production host or select repositories themselves. All
commands below require the operator's direction for the exact host and action;
replace `<host>` with that selected target. A confirmation fingerprint is an
execution-intent guard, not permission to publish.

1. After authorizing installation, run
   `mise run playbook -- forgejo-metadata provision production --limit <host>`.
   Keep the timer disabled during initial enrollment or recovery. Provisioning
   preserves existing initialization and pending creates; it does not initialize
   replication. Use `verify` to observe current definitions and replication
   evidence. A missing initialization or convergence status is pending acceptance
   and returns failure.
2. Run `mise run playbook -- forgejo-metadata plan production --limit <host>`.
   It reports the installed public enrollment and its fingerprint without API
   writes or changes to safety state. Review the destination visibility, repository
   identities and automation actor before forming an attended request. Exact
   request syntax is defined by the installed runtime's `control --help`.
   Supply the request as `forgejo_metadata_request` in a private, uncommitted
   JSON extra-vars file under `.tmp/`; it contains no tokens or issue content.
3. For initial enrollment, select `bootstrap-initial` only after establishing
   that no earlier writer or uncertain create exists. For recovery, stop previous
   writers, preserve available safety state and resolve earlier outcomes before
   selecting `bootstrap-recover`. Record a retrievable non-secret decision
   reference. Bind the operation and confirmation to the reported fingerprint.
   Submit with `mise run playbook -- forgejo-metadata bootstrap production
   --limit <host> -e @.tmp/metadata-control.json`. The control unit discovers all
   destination markers before changing initialization. It preserves surviving
   unresolved intents and archives invalid prior state for investigation.
4. After separately authorizing backfill, select `manual-apply` in a bound request
   and run `mise run playbook -- forgejo-metadata apply production --limit <host>
   -e @.tmp/metadata-control.json`. The root submission helper holds its own lock
   until the fixed control unit finishes and then removes its transient request.
   The control unit and timer use the same service operation lock. A successful
   bootstrap alone is not replication acceptance.
5. Run `mise run playbook -- forgejo-metadata verify production --limit <host>`
   to observe the installed definitions, isolated account, timer selection and
   existing convergence status. It does not start a unit or repair state. Backlog,
   read-back errors, unresolved creates or stale convergence require investigation.
   Enable the declared timer through a separately authorized provision action
   only after the exact mappings and supported live behavior have been accepted.

For an unresolved create, keep previous writers stopped and inspect the identified
shadow. Ordinary complete discovery resolves an existing correctly owned marker.
If no shadow exists, establish that the earlier request cannot still complete
before authorizing `resolve-absent` for that exact source key. Record the decision
and submit with `mise run playbook -- forgejo-metadata resolve production
--limit <host> -e @.tmp/metadata-control.json`; the control repeats complete marker
discovery before clearing the selected intent. It does not itself create a shadow.
Invalid or missing safety state requires recovery bootstrap, even if the last
status file reports earlier convergence. Ownership conflicts require a separately
authorized repair of the identified object; a title match never permits adoption.

## Acceptance and delivery gates

The design was reviewed before implementation. Keep implementation plans uncommitted
under `.tmp/`. Production credentials, identity allocation, repository enrollment,
deployment, initial backfill, and live acceptance require separate operator actions.

Offline behavior tests use API doubles and disposable HTTP fixtures with synthetic
identities and credentials. An independent expected-data oracle must demonstrate:

- Historical issues, comments, labels, milestones and state converge once.
- Source edits, added/edited comments, assignments, closure and reopening converge.
- PR records and review content never enter the reconciliation path.
- The source receives no mutation, including after destination-only edits.
- Existing unmarked GitHub history and human comments remain intact.
- Unchanged runs make no writes; a clean restart rediscovers all existing shadows.
- Accepted creates followed by lost responses or interruption recover by marker;
  unknown outcomes block duplicate POSTs until resolved.
- Confirmed non-creation can retry after repair; ambiguous errors retain intents.
- Missing, corrupt or identity-mismatched state prevents writes until attended
  initialization; provisioning preserves existing initialization and intents.
- Successful responses with dropped labels, milestones or other supported fields
  fail read-back and do not advance last-converged evidence.
- Missing source objects retain their recovery shadows; incomplete pagination,
  duplicate markers, permission failures and visibility drift fail safely.
- Rate limits, destination outage, time budgets and backlog remain observable.
- Tokens and source content remain absent from diagnostics and status artifacts.

A disposable Debian systemd scenario verifies provisioning twice, protected
credential delivery, account isolation, locking, timeout, timer persistence, and
observational verification. Register the new bounded test workflow and impact
selection with the repository classifier. Run the required `mise run ci:changed`
before implementation completion. Prose is reviewed and mechanically linted,
not tested for prescribed phrases.

Separately authorized live acceptance uses exact source/destination repositories
and confirms initial backfill, repeat-run behavior, supported edits, PR exclusion,
read-only source permissions, timer execution, and independent GitHub search.
Offline evidence never substitutes for live acceptance or repository authority
cutover. Record detailed evidence in established evidence stores rather than this
specification.

## Upstream references

- [Forgejo token scope and selected-repository access](https://forgejo.org/docs/v15.0/user/authentication/token-scope/)
- [GitHub issue API and PR distinction](https://docs.github.com/en/rest/issues/issues)
- [GitHub issue comments](https://docs.github.com/en/rest/issues/comments)
- [GitHub labels and description limits](https://docs.github.com/en/rest/issues/labels)
- [GitHub milestones](https://docs.github.com/en/rest/issues/milestones)
- [GitHub API pacing, pagination, and rate-limit handling](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
- [systemd credential delivery](https://systemd.io/CREDENTIALS/)
- [systemd calendar and persistent timer behavior](https://github.com/systemd/systemd/blob/v257/man/systemd.timer.xml)
- [Gitea Mirror supported directions and runtime](https://github.com/RayLabsHQ/gitea-mirror)
- [Forgesync marker-based inbound collaboration](https://github.com/eleboucher/forgesync)
