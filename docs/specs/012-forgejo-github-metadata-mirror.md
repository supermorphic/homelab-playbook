# Specification 012: Forgejo to GitHub issue metadata mirror

Issues: [#75](https://forgejo.infra.supermorphic.com/supermorphic/homelab-playbook/issues/75),
[#89](https://forgejo.infra.supermorphic.com/supermorphic/homelab-playbook/issues/89)

## Purpose and authority

Keep an independently searchable GitHub copy of Forgejo issues, comments,
labels, milestones, and open/closed state. Forgejo remains authoritative.
Run on NUC #4 through workstation-managed Ansible, independently of Semaphore,
Actions, runners, n8n, and Kubernetes. Native Git mirroring owns commits, branches,
and tags.

This is a reconstruction copy. Full Forgejo backups own PRs, reviews, attachments,
exact application identities, settings, authentication, and Actions history.
[Specification 010](010-forgejo-service.md) owns backups and native Git mirroring;
issues #58 and #9 own collaboration cutover and broader recovery. This service
neither changes repository authority nor promotes GitHub on failure.

## Architecture and alternatives

Use a repository-owned host service and a separate Ansible role on Debian.
A systemd oneshot service runs as a dedicated non-login account,
separate from Forgejo. A persistent nightly timer catches up after downtime.
There is no listener, webhook, container, or application database. Periodic
comparison repairs missed runs and edits without an event-delivery dependency.

Alternatives considered:

- Gitea Mirror adds an application, database, and Git replication. Its GitHub
  destination supports code replication; issue metadata requires a GitHub source.
- Forgesync demonstrates attribution and marker discovery, but its writes back
  into Forgejo conflict with the read-only source boundary. Its marker identities
  are not interchangeable with this service's stable API identities.
- Event forwarding requires ingress and reliable delivery without removing the
  need to repair missed events.

## Enrollment and protected inputs

Each explicitly enrolled mapping binds one Forgejo HTTPS repository to one existing
GitHub repository. Pin repository API identities, owner/name paths, destination
visibility, and destination actor. Reject duplicate destinations, identity
mismatches, and visibility drift. Repository transfers, renames, and changed source
identities require reviewed re-enrollment; repositories are never enrolled by
discovery.

Enrollment authorizes publication of the selected source history to the declared
visibility. A private source needs explicit review before publication to a public
destination. Repository creation, permission changes, and adoption of historical
GitHub objects are separate operator actions.

Use a non-admin Forgejo identity with read-only issue and repository access to the
selected repositories. The GitHub credential is an owner-issued fine-grained token
limited to the selected destinations, Issues write permission, and repository
metadata access. Pin the owner as destination actor. The owner also needs repository
Write access: token permissions alone do not prove that label and milestone
assignments will be accepted. Check effective access and prove those assignments
during live acceptance. Do not request Contents, Actions, administration, or source
mutation permissions.

The original separate GitHub automation-account design is historical: fine-grained
tokens do not support repository collaborators for personally owned destinations.
The owner-issued token preserves the repository and permission limits while
sharing the owner's posting identity.

[Specification 005](005-sops-age-secrets.md) owns token encryption and identity
recovery. Tokens remain in service-owned protected inventory and reach the isolated
runtime through systemd credentials. They never enter arguments, environment
definitions, output, or status. The runtime has no SOPS identity or operator Git
credentials. Record public token ownership, scope, and expiration metadata. Renewal
is attended. Rotation preserves scope, mapping identities, and safety state; verify
the replacement before revoking its predecessor.

Use trusted HTTPS with certificate and hostname checks. Confine requests and
pagination to the configured API origin and repository; never forward authorization
through redirects or to another host. Source URLs are attribution, not instructions
to fetch attachments or arbitrary resources.

## Durable identity and destination ownership

Identity is the stable source-instance key, numeric Forgejo repository ID, object
kind, and numeric object ID. Issue numbers, titles, and names are display values.
Preserve the instance key across rebuilds; changed source identities require
reviewed re-enrollment rather than guessed correspondence.

Versioned markers identify destination objects. Generated attribution preserves
the source repository, issue number, author, creation time, and URL where available
without impersonating users or presenting GitHub timestamps as original timestamps.
Only the canonical service marker outside source text can establish ownership;
marker-like content in a source body cannot do so.

Labels preserve original Forgejo names and colors. Their descriptions carry the
complete identity marker and as much source description as the destination permits.
Reject names that cannot be represented exactly rather than substituting or
truncating them. Description truncation remains a reconstruction limitation covered
by full backups. Milestones retain a readable title with a source-ID suffix to avoid
implicit matches with pre-existing destination objects.

Ordinary synchronization updates only objects with an unambiguous marker for the
enrolled source. Issues and comments also require the declared actor as creator.
Actor identity or a matching name alone never authorizes adoption. Preserve
unmarked GitHub history and human comments. Replace edits to owned fields with
source values; GitHub edits never become source writes.

The safety journal retains evidence of observed issue/comment shadows and numeric
GitHub label IDs, so label ownership survives renames. Missing or damaged markers,
duplicate ownership, or an absent recorded shadow stop the mapping rather than permit
recreation or takeover. Recorded labels also reject replacement by another numeric
ID even when the name matches. Malformed label markers are conflicts regardless of
name. Preserve surviving evidence through upgrades and recovery alongside
initialization and unresolved creates. Evidence alone never authorizes a write
without a valid marker. If all ownership evidence and recognizable service attribution
are lost, ordinary owner-authored history cannot be distinguished from a shadow;
attended recovery must resolve that uncertainty.

## Reconciliation and API boundaries

Before writes, validate enrollment, repository identities, actor, visibility, and
safety state. Build complete inventories, including closed objects; exclude PRs and
review content even where APIs mix them with issues or comments. Incomplete
pagination, API errors, or discovery limits must not become empty or partial
inventories accepted as complete. Bound discovery, execution, and mutation requests.

Reconcile labels and milestones before dependent issues, then comments. Create
missing labels under their original names; ordinary synchronization never adopts
unmarked collisions. Rename marked labels in place, retaining their numeric GitHub
IDs. An occupied desired name stops the mapping before changing that label.

Copy supported source fields and assignments, including closure, reopening,
comment edits, and removed optional values. Preserve milestone due dates as UTC
calendar dates. Compare normalized supported values so converged objects need no
writes and unrelated destination fields do not cause repeated updates. Read back
all supported values after each write: a successful response or matching marker
does not prove convergence when an API silently drops an assignment or other field.

Never delete a destination object because it disappears from Forgejo, or infer
closure solely from absence. Removing a source issue's label changes its membership,
not the retained label or unrelated history.

Use full comparison rather than relying on issue timestamps to discover comment
or label changes. Deterministic progress and skipped converged objects let bounded
backfill continue across runs. Independent mappings can progress after another
fails, but any failure makes the overall run fail. Partial work and backlog never
advance last-converged evidence, which describes observed source data rather than
an atomic snapshot across both services.

Pace serial requests and respect primary and secondary rate limits. Persist retry
deadlines by credential so restarts cannot bypass throttling and independent
credentials can progress. Report sanitized operation results and convergence
freshness; never log credentials, API payloads, raw error bodies, or source content.
Freshness must account for the nightly schedule, clock changes, and bounded run time.

## Interrupted creation and rebuild safety

Creation includes the ownership marker and requires a durable pending intent
recorded before sending the request. The journal contains identity and operation
evidence, never credentials or issue content. Journal transitions preserve uncertainty
across interruption. Rediscovery of exactly one correctly owned object resolves its
intent; mismatched supported fields become repair work on that object, never another
create.

Proven non-creation can retire an intent and permit a later retry after repair and
any retry deadline. Lost responses, timeouts, malformed success, ambiguous errors,
and interrupted requests retain uncertainty. An unresolved intent blocks another
create for that identity while independent work can continue. Resolve an absent
shadow only after establishing that the prior request cannot still complete and
recording an attended decision.

Initialization binds the enrolled identities and actor. Missing, corrupt,
incompatible, or mismatched state permits observation but blocks all destination
writes. Provisioning and timer invocations never initialize it implicitly or
discard surviving ownership and pending-create evidence.

Initial enrollment requires confirmation that no previous writer or unresolved
create exists. Recovery requires stopped writers, preserved available state, and
complete marker discovery. Lost state additionally requires an explicit decision
that earlier create outcomes are resolved. Bind attended decisions to the exact
enrollment and operation; a confirmation value records execution intent, not
authorization.

The journal is safety evidence, not an authoritative mapping database. After
attended initialization, markers reconstruct ordinary mappings without a database
restore. Exactly-once REST creation cannot be guaranteed when both response and
uncertainty evidence are lost. Ownership conflicts require repair of the identified
object, never a replacement inferred from its title.

## Host lifecycle and observation

Isolate the service from Forgejo data and unrelated privileges. Keep executable,
configuration, and credentials administrator-owned and state private to the service.
Refuse incompatible account ownership rather than renumbering existing identities.
The service needs no SSH access, sudo, container identity allocation, or unrelated
groups. Bound resources and filesystem access; account separation alone does not
restrict the HTTPS network access the APIs require.

Scheduled and attended operations share mutual exclusion, time bounds, and observable
exit status. Verification observes installed definitions, isolation, scheduling,
and existing convergence evidence without starting synchronization or repairing
state. Provisioning, observation, and mutation remain distinct; use the canonical
`mise run playbook` interface and its target guards. Production enrollment does not
authorize staging.

Recovery must remain usable independently of the running service. Prerequisites
are a repository checkout, workstation toolchain, independent SOPS identity
recovery, restored scoped credentials, source availability, and the preserved
instance key. Keep synchronization stopped while preserving the journal,
investigating ownership and unknown outcomes, and performing authorized repairs.
Reinstallation cannot substitute for attended initialization or acceptance.

## Attended operation and recovery

### Original-name label cutover

Exact source and destination name, color, and description equality proposes an
adoption pair; it grants no ownership. An observational plan identifies the enrolled
repositories, source identities, destination numeric IDs, intended values, and
conflicts without changing destinations, safety state, scheduling, or convergence
evidence. Case variants, ambiguous names, conflicting markers or journal evidence,
unsupported names, and uncertain creates at proposed candidates require resolution.

Explicit authorization binds the reviewed set and its observed fields and IDs.
Recheck that set immediately before each write and reject changes since review.
Adoption adds generated markers to existing labels, verifies their fields and
unchanged IDs, and records ownership. It creates no labels, merges no objects, and
preserves historical issue assignments. Subsequent source changes affect those
labels wherever historical GitHub issues use them.

Keep synchronization paused and previous writers stopped through label preparation,
deployment, and adoption. Deployment alone grants no adoption authority, and manual
label preparation is a separate operator action.

After interruption, preserve the journal and review a fresh plan. Valid owned labels
need no repeat adoption; remaining pairs need fresh authorization. Missing or invalid
initialization still requires attended recovery. Separately authorize reconciliation
to restore mirrored issue membership, verify original names and convergence, then
authorize scheduling to resume.

### Enrollment and recovery

Installation, identity allocation, scoped credentials, repository enrollment,
initialization, backfill, and scheduling each require the appropriate operator
authorization. Keep scheduling disabled while reviewing enrollment and performing
initialization or recovery. Review repository identities, actor, and visibility
before any publication; initialization alone does not establish replication
acceptance.

Stop previous writers before resolving ownership or creation conflicts. Earlier
convergence cannot authorize writes when safety state is missing or invalid.
Resume scheduling only after uncertain outcomes and conflicts are resolved and
convergence is verified.

## Acceptance requirements

Offline validation must demonstrate:

- Historical metadata and supported edits converge; unchanged runs make no writes.
- PRs and reviews are excluded, the source remains read-only, and unrelated GitHub
  history survives destination-only edits and missing source objects.
- Markers rediscover objects after restart; original-name labels retain numeric
  identity through rename and adoption, and collisions or stale adoption plans fail.
- Accepted creates with lost responses recover without duplicates; proven
  non-creation can retry, while unknown outcomes remain blocked until resolved.
- Missing or invalid initialization blocks writes, and provisioning and recovery
  preserve surviving ownership and uncertainty evidence.
- Incomplete discovery, dropped fields, identity or visibility drift, permission
  failures, outages, rate limits, and exhausted budgets remain observable failures.
- Credential isolation, mutual exclusion, bounded execution, persistent scheduling,
  and observational verification hold in the installed host service; diagnostics
  expose neither tokens nor source content.

Separately authorized live acceptance for exact enrolled repositories must confirm
backfill, repeated runs, supported edits and assignments, PR exclusion, read-only
source access, scheduled execution, and independent GitHub search. Offline evidence
does not establish live acceptance or change repository authority. Keep execution
evidence outside this design record.

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
