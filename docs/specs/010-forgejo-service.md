# Specification 010: Forgejo service

Issue: [#7 Forgejo self-hosted Git](https://github.com/supermorphic/homelab-playbook/issues/7)

Status: approved design, implemented locally. Offline validation and live service
acceptance are separate evidence; live acceptance remains pending.

The guarded application upgrade design for
[#118](https://forgejo.infra.supermorphic.com/supermorphic/homelab-playbook/issues/118)
is implemented locally. Production execution and live acceptance require separate
operator authorization.

## Purpose and delivery boundary

Deploy a recoverable, Ansible-managed Forgejo service on the existing Debian 13
host. The workstation remains the authoritative deployment and recovery surface:

```text
operator workstation -> mise run playbook -> Ansible
    -> rootless Quadlet / systemd / Podman -> Forgejo + PostgreSQL
private HTTPS client -> existing Caddy / TLS -> loopback Forgejo HTTP
```

The operator selected HTTPS-only Git access, a dedicated PostgreSQL database,
nightly backup downtime and once-nightly GitHub recovery mirroring. Forgejo is
intended eventually to own Git, issues, PRs, reviews and CI. This delivery makes
the service usable and proves recovery; repository authority changes and runner
adoption are separate work.

Issue #7 includes deployment, persistent state, protected configuration, ordinary
web and Git use, backups, an isolated restore experiment and a representative
mirror. It does not deploy runners, migrate existing issues or PRs, move all
repositories, switch Flux sources, configure application deployment jobs, or
perform the broader host rebuild exercise.

Follow-up ownership:

- [#57](https://github.com/supermorphic/homelab-playbook/issues/57) owns isolated
  Forgejo Runner infrastructure.
- [#58](https://github.com/supermorphic/homelab-playbook/issues/58) owns this
  repository's CI parity, collaboration migration and authority cutover.
- Downstream Git source changes require separate review and acceptance.
- [#8](https://github.com/supermorphic/homelab-playbook/issues/8) owns reviewed
  dependency discovery; [#9](https://github.com/supermorphic/homelab-playbook/issues/9)
  owns broader reconstruction and off-site application-backup recovery.
- [#33](https://github.com/supermorphic/homelab-playbook/issues/33) owns shared
  off-cluster notification delivery. Backup and mirror operations remain
  independent of notification availability.

Issue metadata reconstruction on GitHub is owned by
[Specification 012](012-forgejo-github-metadata-mirror.md). Native Git mirroring
and full Forgejo backups remain the recovery boundaries described here.

## Runtime and database decision

Use the maintained upstream rootless Forgejo image and a dedicated PostgreSQL 17
container under `svc-forgejo`. The original deployment selected the supported
Forgejo LTS line. Research on
2026-09-30 selected Forgejo 15.0.9-rootless, PostgreSQL 17.11 and rclone 1.75.1.
Reviewed immutable multi-architecture image digests are recorded in role defaults.
Stock-container compatibility checks cover protected initialization, database
privileges, persistence and native HTTPS mirroring. No floating image updates or
custom application image is introduced.

Issue #118 selects the latest published, supported stable release at implementation.
Review the release index, intervening release notes and maintenance window again
before each subsequent immutable rootless image change.
A scheduled major release is not a published stable release. Moving from LTS to
the stable release line requires recurring attended upgrades within its shorter
support window; dependency discovery remains a proposal mechanism, not deployment
authority. Preserve the original LTS generation as a reviewed recovery target.

SQLite is supported and recommended upstream for low to moderate activity. The
operator chose PostgreSQL to reuse the existing database operating pattern and
provide capacity for concurrent work. It adds a database lifecycle and restore
step; it does not itself improve the frequency of backups. Use neither a shared
Semaphore database nor a new database cluster, queue service or object store.

The PostgreSQL container belongs to the Forgejo service boundary. Its data and
credentials are separate from Semaphore. Give the application a database role
limited to its own database/schema, with no cluster-administration authority.
Keep database initialization authority separate from the application's runtime
credential. Match dump and restore clients to PostgreSQL major 17.

Consume specification 006's foundation account and allocation record. The service
role verifies the existing declaration; the Podman foundation owns account and
subordinate-ID creation. Select real allocations only after an authorized host
inspection, and repeat collision checks during foundation provisioning. Do not
renumber existing identities or recursively reset existing application ownership.

Use root-owned, secret-free UID-scoped Quadlets and a lingering user manager.
Validate generated definitions before reloading that manager. Start PostgreSQL
before Forgejo, with bounded database and HTTP readiness waits. Use systemd restart
handling and finite start/stop timeouts. Validate the upstream rootless image's
container user and mount ownership on the supported host.

The application receives no container runtime socket, runner identity, SOPS
private identity, TLS issuer credential or unrelated management authority.
Future build workers are separate consumers of the runner design in #57.

## Storage and configuration

Use `/var/lib/svc-forgejo/forgejo` as the private service state root, with separate
database, application data, configuration, credential and local-backup directories.
Mount application data at the upstream rootless image's `/var/lib/gitea` and
configuration at `/etc/gitea`. Keep repositories, attachments, avatars and enabled
Git LFS objects on local service-owned storage, within the backup boundary.
Do not mount live application or database storage from the NAS.

SOPS inventory supplies durable application key material, database credentials,
initial administrator inputs and NAS access. Provision them through private files
with secret diffs and logs suppressed. Keep secrets out of Quadlets, command-line
arguments, generated evidence and container images. Forgejo does not decrypt SOPS
inventory itself.

Use local application accounts and closed registration. Disable the interactive
installation page after managed initialization. Bootstrap the first administrator
idempotently; ordinary provisioning must neither reset an existing administrator
nor regenerate stable application keys. The pinned CLI creates a random initial
password captured privately; an authenticated API update installs the protected
initial password. Retain interrupted bootstrap state, and require operator
recovery when an existing account lacks completion evidence. Never interpolate
passwords into process arguments.
Document independent administrator recovery without requiring an external identity
provider or SMTP service.

Enable normal repository, issue, PR and attachment behavior. Support HTTPS Git
with application tokens and local LFS storage. Disable Actions execution, SSH
service and package-registry hosting in this delivery; enabling additional durable
storage features later must extend their backup and restore acceptance together.

Record paths and effective version metadata needed to associate a selected backup
with its matching protected recovery inputs. Preserve those inputs independently
through the established SOPS recovery process. A backup generation must remain
recoverable after later credential or application-key rotation.

## Private HTTPS integration

Reuse the existing private DNS, Caddy route and `infra` certificate contracts.
The hostname is an operator input under the existing wildcard namespace; examples
use `forgejo.infra.example.com`. Select loopback backend port `18081`, subject to
an immediate availability check before first binding. Keep live deployment
identifiers in protected inventory.

Publish only `127.0.0.1:<backend-port>:3000`. Publish no PostgreSQL or Git SSH
host port. Set Forgejo's external root URL to the selected HTTPS hostname so web
redirects, clone URLs and authentication use the same private endpoint. Append
the exact hostname/backend/certificate route to the authoritative protected proxy
list while preserving unrelated routes. The existing proxy workflow owns route
activation and certificate access; Forgejo mounts neither TLS private keys nor
the Caddy administration socket.

Use application authentication and disable reverse-proxy authentication. Set
`REVERSE_PROXY_LIMIT = 0` to disable processing of forwarded client identity.
The configured HTTPS root URL supplies the public endpoint. Caddy owns TLS,
routing and ingress client restrictions; Forgejo uses the connection peer for
client identity, which can be a Podman forwarding address. Per-client IP auditing
or policy inside Forgejo is outside this deployment's contract.

There is no forwarding-address enrollment, temporary discovery listener, or
startup dependency on address measurement. Offline runtime evidence must show
that forwarded IP and authentication headers cannot replace the connection peer
or sign a user in, and that generated clone URLs use the configured HTTPS root.
Prove browser password login, secure session cookies and cross-origin write
rejection at the HTTP backend with the public HTTPS host and origin.
Exercise both the published port and container loopback, including an
upstream-default trusted source, so ignored headers are proved independently of
the container subnet. Ordinary application verification observes local HTTP
health; a trusted HTTPS check from the intended client network is separate live
acceptance.

## Nightly backup and NAS transfer

Use the established systemd and container-client pattern. The daily backup runs
at 03:30 host-local, after Semaphore's source-default 03:00 backup. NAS transfer
runs independently every four hours at :45, and successful local publication
also triggers transfer. Timers are persistent and one-shot services return to
inactive state so later activations can run. Do not install a host PostgreSQL
client, mount SMB on the host or add a custom scheduler.

The backup operation has a 30-minute budget while Forgejo is stopped. A separate
bounded recovery phase restarts and checks Forgejo, including after failure or
interruption. The two-hour NAS transfer budget does not extend application
downtime. These are execution limits, not promises about normal backup duration.

The local sequence is:

1. Obtain the service-operation lock and verify selected paths, available storage,
   compatible image versions and running database/application prerequisites.
   Provisioning and backup serialize changes to the same service state.
2. Gracefully stop Forgejo and confirm it has stopped, including application Git
   subprocesses. Keep PostgreSQL running. No successful archive may contain a copy
   made while Forgejo can change either database or application files.
3. Produce a compressed custom-format `pg_dump` and a file archive of the complete
   durable application/configuration state. Preserve required numeric ownership
   and permissions. Exclude container image storage, disposable caches and backup
   directories themselves; do not exclude unique user data or encryption material.
4. Record non-secret format/version metadata, check dump readability and archive
   structure, compute checksums, and atomically publish one completed directory.
5. Restart Forgejo and verify health before releasing the service-operation lock.
   Request transfer of the new archive after local publication and recovery.

Use a private temporary directory and same-filesystem rename for publication.
An archive contains `database.dump`, `files.tar.gz`, `manifest.json` and
`SHA256SUMS`; a remote `COMPLETE` marker records the verified content set. The
manifest binds the format, creation time, image pins and storage layout. No
database-only archive satisfies Forgejo recovery acceptance.

Systemd-supervised recovery must restore intended application availability after
timeout, failed dump, archive failure or interrupted work; do not depend solely
on a shell exit trap. Boot ordering must also recover from an interrupted backup.
If restart fails, retain a distinct recovery failure and stop further changes to
active application state. Independent transfer of completed backups remains
available. Preserve the original backup failure even if service recovery succeeds.
Do not start a deliberately stopped application merely because a backup timer ran.

Transfer uses rclone copy semantics into a dedicated Forgejo NAS prefix. Verify
new uploaded payloads before publishing `COMPLETE`; subsequent unchanged runs
check completion metadata without downloading every historical payload. NAS
failure preserves local archives and does not prevent the next backup. Retry
outstanding completed archives independently of whether today's backup succeeded.

Retain seven days locally only when a verified remote copy exists, and 90 days
on the NAS, measured from archive creation. Preserve untransferred archives and
report insufficient space. An outstanding archive already older than remote
retention requires operator resolution rather than immediate upload and deletion.
Prune only well-formed, completed archives inside this service's declared prefix;
reject unexpected links and paths. Never propagate local deletions as a NAS mirror.

Backup archives contain protected application state and remain private. GitHub
mirroring is an independent off-site code copy, not an off-site copy of the
PostgreSQL database or issue/PR state. Broader off-site backup storage is owned by
issue #9. Nightly backups allow roughly a day's loss under healthy operation; outages
can extend that interval and must remain visible.

## Representative nightly GitHub mirror

Use native Forgejo push mirroring over HTTPS. The application owns the repository
mirror configuration and its narrowly scoped destination credential. A runner or
Semaphore job must not be required to maintain the mirror.

Use Forgejo's native mirror cron schedule once nightly at 02:00 in the host's
timezone. Disable synchronization on push and startup-triggered synchronization.
Set mirror eligibility intervals to eight hours, below the daily scan interval,
so completion-time rounding cannot make a healthy mirror skip alternate nights.
This does not trigger extra runs because the scheduler scans only once per night.
Test the combination against the pinned version, including restart and failure behavior.
An attended initial/manual synchronization is separate from routine scheduling.
Do not silently substitute an every-few-minutes schedule or a drifting 24-hour
timer for the selected nightly behavior.

Native Git uses credential-free HTTPS destination URLs and a private read-only
`core.askPass` program. Pinned Forgejo disables Git credential helpers; askpass
reads destination-scoped protected credentials without exposing them in process
arguments or erasing declared inputs after failed authentication. Keep that
configuration inside the archive boundary. LFS authentication and transfer need
separate acceptance.

The installed service starts with no declared production mirror targets. A
separately authorized representative repository demonstrates initial enrollment,
scheduled synchronization, authentication failure and ref equality against an
explicit GitHub destination. Define an exact source/destination and source
authority; disable competing writes and preserve intended visibility. Native push
mirrors can force-update destination refs, so destination protection and deletion
behavior require explicit review. This delivery does not alter protection on an
existing canonical repository to make the demonstration succeed.

Observe native last-success and error state and verify expected branch/tag refs.
Flag a failed attempt or a last successful synchronization older than 36 hours;
do not claim exact ref equality while new commits are arriving. Offline tests use
fixed fixture revisions and disposable mirror targets. Live acceptance records
the intended revision and compares it explicitly.

Normal nightly mirroring allows roughly a day's code lag. Missed or failed runs
must remain visible; the operator can authorize manual synchronization. Never
promote GitHub automatically when Forgejo is unavailable. No replication of issue
threads, PR reviews, settings, Actions history or release assets is implied by a
Git mirror. Required LFS objects need explicit transfer and recovery evidence.

The actual mirrors and collaboration cutovers belong to the follow-up migration
issues. Existing repositories remain GitHub-authoritative until their equivalent
protections, validation, collaboration-history decision and recovery are accepted.

## Isolated restore and service recovery

Register `mise run test:forgejo` modes for local unit tests, upstream compatibility,
the synthetic backup/transfer/restore fixture, and an attended restore experiment.
Use the existing command lifecycle and bounded-resource conventions. An attended
NAS restore requires explicit archive, source and destination selection, operator
authorization and repeated preconditions immediately before temporary mutation.
Confirmation arguments bind intent but do not grant authority.

The fixture seeds a user and credential, commits on multiple refs, a tag, issue,
PR, comment, attachment and LFS object before creating the backup. Expected values
and object hashes are recorded separately as the acceptance oracle. Test database
role privileges, native HTTPS-capable Git behavior and application persistence
against the selected upstream images, not only rendered templates.

Restore acceptance must:

1. Retrieve one exact completed archive without a latest/fallback selection.
   Verify completion metadata, all checksums, manifest compatibility, dump
   readability and safe file extraction. Reject absolute paths, path traversal
   and links that escape the new restore root.
2. Establish isolated networking before starting restored software. Use fresh
   run-owned database and application storage, with matching protected keys and
   compatible pinned images. No active service volume or proxy route is a target.
3. Restore the database using `pg_restore` with first-error handling, one
   transaction and controlled ownership/privileges. Restore the matching file
   state with correct ownership before starting Forgejo.
4. Prove the restored instance cannot contact production, GitHub or other live
   targets, even if its database contains mirrors or webhooks. Isolate outbound
   networking as well as overriding restored scheduling for the experiment.
5. Authenticate using the restored synthetic credential without replacing it;
   check expected issues, PRs, comments and attachment contents. Clone and compare
   exact Git refs and LFS content, then fetch/push new fixture work successfully.
6. Clean up only resources created and owned by that run, on success and failure.
   Report cleanup failure separately, preserve the selected source archive and
   fail the experiment if cleanup cannot be verified.

Bound archive processing to 100,000 filesystem entries (including implicit parent
directories), 256 GiB of file contents, 257 GiB of decompressed tar data and
32 MiB of retained path names. Reject oversized extended headers and paths deeper
than 64 components. Estimate extraction capacity with rounded file blocks and
8 KiB of overhead per entry, and check available inodes before extraction.
These conservative limits require a reviewed controller change for larger data.

Test corruption, interrupted publication/upload, NAS outage and backlog recovery,
unchanged transfer efficiency, retention limits, consecutive timer activations
and recovery after a failed backup. An archive listing or successful `pg_restore`
alone is insufficient; the application must use the restored state successfully.

Actual service recovery is a separate operator-controlled operation. Preserve old
storage, validate a replacement in isolation, review recovered mirrors and
webhooks before permitting egress, then authorize cutover. Retain the previous
state until the replacement is accepted. Application upgrades require a usable
pre-upgrade archive; reverting an image does not reverse a database migration.
PostgreSQL major upgrades need their own validated migration procedure.

## Guarded application upgrade

The workstation remains the upgrade authority through
`mise run playbook -- forgejo upgrade <inventory> [ansible-args...]`.
Ordinary provisioning continues to reject application or database image changes.
An upgrade changes only the reviewed application pin; keep the PostgreSQL major
and immutable pin, foundation identity, storage boundary, private ingress and
runner registration scope. Do not combine unrelated configuration or credential
changes with migration. Keep staging inactive.

Use an archive-backed upgrade of the existing application storage. This preserves
a reconstructable source generation, rather than intact original source volumes.
It avoids introducing a second production database and a general cutover system
for each application upgrade. Actual recovery still preserves failed or migrated
storage while reconstructing a replacement separately, as required above.

### Preparation and recovery evidence

Keep reviewed exact recovery profiles independently of the active deployment
default. A profile identifies the immutable runtime and matching database clients,
compatible controller and helpers, and the source controls needed to reconstruct
that generation. Preserve prior definitions and protected configuration/key
generation references with the selected archive. An archive must match a reviewed
profile; its contents cannot select arbitrary executable images. Runtime and
archive validators must support the reviewed target and retained source profiles
without weakening digest, integrity, ownership or storage checks.

Preserve the repository/controller revision, recovery tools and source images
outside dependence on the working Forgejo service or runners. Recover old archives
using their reviewed source profile, rather than substituting current role
defaults. Keep protected values within the existing SOPS and private runtime
boundaries; evidence and public logs contain only non-secret references.

Require a completed synthetic migration and independent source recovery rehearsal
for the exact proposed transition. Bind its evidence to the runtime pins, relevant
controller and configuration content, and the independent fixture/oracle.
Require successful cleanup as part of completion. Reject missing, incomplete or
changed-candidate evidence. A synthetic rehearsal establishes procedure behavior;
it does not prove that a particular production archive has been restored.

Under the operation lock, repeat installed source, allocation, storage capacity,
candidate identity and pending-operation checks immediately before mutation.
Capture a fresh archive while source writers remain stopped. Require matching
source/profile/key-generation references, complete payload and checksum
validation, readable database dump, and durable publication before migration.
Checksum validation alone is not proof of successful application recovery.

Protect the transaction's recovery archive from both local and remote retention.
Coordinate protection with the transfer lock as well as the service operation
lock, using a fixed lock order. Keep transfers available. Preserve the archive and
source controls through acceptance and until explicit release; normal retention
must not make an interrupted upgrade unrecoverable.

### Transaction and admission boundaries

Stage helpers and definitions privately. Reject incompatible ordinary provisioning
and pending transactions before replacing active helpers. Activate a complete,
compatible helper/definition generation under the operation lock. Safely install
upgrade-aware guards for the source generation before starting the transaction;
their installation must not rely on replacing an executing recovery helper or
bypassing the existing image-change guard.

Use a durable administrator-owned transaction record. All application start,
provisioning, capture, backup recovery, boot recovery and upgrade resume paths
must consult it. Preserve it through process death, timeout and reboot. Malformed
or inconsistent state blocks ordinary activation and requires operator diagnosis.

| State | Required boundary |
| --- | --- |
| Source ready | Installed source generation is consistent; no pending service operation exists. |
| Prepared | Exact transition and rehearsal match; independent recovery material and compatible guards are available. |
| Quiesced | Transaction is durable; new work is blocked; existing workers and queues are handled; application writers are stopped. |
| Archive sealed | Fresh source archive and controls are durable, validated and protected from retention. |
| Migration started | Irreversible boundary is durable before any target process opens writable source storage. |
| Target verified, gated | Bounded target checks pass while normal client admission and external background effects remain blocked. |
| Target accepted | Temporary writers have stopped; target definitions are published; the durable decision permits normal target activation. |
| Target committed | Declared and running target agree; intended client access and application schedules resume. |

Prevent new work before draining existing workers and application work. Use the
established runner drain boundary where authorized; do not implicitly cancel
jobs or change registration scope. Establish queue compatibility before shutdown.
Verify Forgejo and its child writers have stopped before capture, and do not
restart the source between archive capture and migration.

The existing backup availability-recovery mechanism must not restart an
application during an upgrade merely because a capture process died or the host
rebooted. Give the authorized upgrade process narrowly bound permission to start
the exact target. A recorded migration phase alone must not permit an ordinary
restart. If the controller dies while the target is running, preserve the journal
and admission restrictions; do not infer acceptance from HTTP health.

Before migration starts, a defined, explicitly authorized abort may restore
verified source availability and prior admission settings. At or after migration
starts, failure or interruption before acceptance keeps the service gated and requires an explicit
resume or recovery action. Never automatically start the old image against
possibly migrated state. Repeating a completed upgrade with the same candidate
is observational and does not recapture or migrate; compatible provisioning
after completion remains idempotent.

Run bounded technical acceptance while normal ingress, mirror/webhook egress and
worker activity remain gated. Persist the target acceptance decision only after
runtime, database, configuration and continuity checks pass and temporary writers
have stopped. Activation then restores declared schedules and access; confirmed
native readiness completes the transaction. Interruption after acceptance resumes
target activation and cannot authorize an old-image restart. This durable decision
precedes access, so a crash after reopening admission cannot imply lossless rollback.
Detailed private Actions API acceptance
requires separate authority, or explicit inclusion in the same attended window;
it is not implied by technical readiness.

Once normal writes or external effects resume, the pre-upgrade archive is a
historical recovery point. Restoring it can lose subsequent work or repeat
external effects. It is no longer a lossless rollback. Recover into fresh storage
with external effects isolated, retain the failed generation, and require separate
authority before replacing the active service or enabling recovered egress.

### Attended preparation and interruption recovery

Admission control uses Caddy's existing protected Unix administration socket.
Save the exact single-host route and replace only its declared loopback proxy
handler with a maintenance response. Preserve client access restrictions and other
services. Conditional updates reject concurrent configuration changes; restoration
requires the exact saved gate or already-restored original. Caddy restart can reload
the original route. Repeat preparation and fresh queue prerequisites after that
event; reapplying the gate advances its recorded time and invalidates earlier
queue attestations, even within their freshness window. The application startup
guard independently blocks pending transactions.

Migration runs the database without networking and the application inside only
that database's loopback namespace. Preserve their respective user mappings and
storage ownership. Neither temporary container publishes ports or restarts on
reboot. Disable application cron. The journal blocks ordinary application starts
until explicit pre-migration abort or durable target acceptance.

Use this sequence in an explicitly authorized maintenance window:

1. Retain this checkout and the reviewed source images independently of Forgejo.
   Run `mise run test:forgejo -- upgrade`. Select its completed evidence file under
   `.tmp/forgejo/` through `forgejo_upgrade_evidence_path`. Evidence hashes the helper
   cohort, fixture/oracle, templates and defaults before execution and rejects
   changed files or incomplete cleanup.
2. Drain the authorized runners using their owning workflow. Verify running jobs
   finish and polling stays stopped. Invoke
   `mise run playbook -- forgejo upgrade-prepare <inventory> [ansible-args...]`
   with typed `forgejo_upgrade_runners_drained: true` and the selected evidence.
   The source remains running behind maintenance ingress. Save the returned
   transaction identity and its source generation from the private journal.
3. The attended operator handles outstanding mirrors and flushes source queues.
   In the existing `svc-forgejo` rootless session, upstream's command is
   `podman exec forgejo forgejo --config /etc/gitea/app.ini manager flush-queues`.
   Require successful completion; resolve a timeout before continuing. This
   server-side command is an operator prerequisite, outside agent authority.
4. Supply `forgejo_upgrade_prerequisites` with the transaction identity, source
   generation, current UTC observation time and successful runner/queue/mirror
   observations. Use the field contract in
   [the prerequisite validator](../../roles/forgejo/files/upgrade_host.py).
   Invoke the canonical `forgejo upgrade` action within the freshness window.
   It checks the gate and absence of running Actions tasks again before stopping
   the source. An attestation is an execution guard, not authorization.
5. After interruption, retain the journal and storage. Use `forgejo upgrade-resume`
   with the same candidate and completed evidence. A pre-migration retry needs
   fresh prerequisites; incomplete capture bytes are quarantined and a complete
   archive is reused. Use explicitly authorized `forgejo upgrade-abort` only before
   migration. After migration begins, never manually start the old image on that
   storage. Accepted target activation is resumable even if readiness failed.
6. Verify the target, then separately authorize resuming the declared runners
   through their [owning workflow](011-forgejo-runners.md). The service upgrade
   leaves drained runner polling stopped; runner provisioning restores its declared
   timers. Confirm post-upgrade backup and normal nightly mirrors before live
   acceptance. Do not force synchronization to manufacture acceptance evidence.

Private recovery inputs live beneath the helper root's `recovery/<transaction>/`:
source controls, complete compatible helpers, protected settings and generation.
A final checksum record seals all required inputs; resume rejects damaged sealed
material. The journal identifies the protected archive. Preserve these inputs
and source images off-host before relying on them for disaster recovery. Discover
the independent archive restore arguments with
`mise run test:forgejo -- restore --help`; reconstruct into fresh isolated storage
using the matching reviewed profile and protected generation. Authorize cutover
and egress separately after acceptance. These steps require this checkout and the
archive, not working Forgejo, runners or Semaphore.

The root-owned retained-archive references survive transaction completion. Release
one only through an explicitly authorized operator change serialized with the
service operation lock followed by the transfer lock, after reviewing remaining
recovery coverage. Never delete the journal to bypass a pending migration.

### Upgrade acceptance

Exercise populated synthetic Git, issues/PRs, attachments, authentication and
database state through the actual migration. Compare independent expected refs,
content and credentials. Then restore the pre-upgrade archive into fresh
source-version storage independently of the migrated service. Demonstrate network
isolation and absence of outbound effects with actual boundaries and sentinels.

Synthetic files in Actions storage prove file preservation only. Use actual
synthetic workflow runs to prove known logs, artifact hashes, correlated
run/job/attempt identities and observable cancellation. Check the selected API's
rerun and same-owner custom-fork PR behavior without assuming support or replacing
the configured CLI. Keep consumer CI and runner corrections within their owning
issues.

The reviewed target API exposes no rerun operation. The populated synthetic probe
creates a same-owner fork with a custom name and a branch present only in that fork;
creating its PR through `head: owner:branch` returns HTTP 404. These are consumer
limitations, not upgrade failures. Report the observed capability results with
the rehearsal evidence. These API checks do not select a CLI. Client selection
requires the shortlist assessment and native comparison specified in issue #118;
retain the configured metadata client until that comparison supports a change.

Test interruption at each publication/start boundary, including controller death
while the target runs, reboot, invalid journals, stale evidence, concurrent
capture/transfer and retention protection. Verify ordinary provision rejection
leaves the source helper generation intact. Run the selected offline CI and
existing real-runtime compatibility, backup and restore workflows before live
execution. After separate exact authorization, check native service verification,
post-upgrade backup/recovery evidence, normal nightly Git/metadata mirror results
and consumer acceptance. Live results remain separate from CI evidence.

## Operator interface and observational verification

Expose `forgejo provision` and `forgejo verify` through the canonical playbook
gateway and its existing credential/task-selection guards. Target the explicit
`forgejo` inventory group. Keep staging inactive until it has independently
approved host and storage inputs; frozen inventory is not a Forgejo target.

Provision reconciles compatible declared configuration, application/database units,
backup schedules and readiness, then verifies. It does not allocate foundation
identities, edit protected inventory, create GitHub repositories, migrate consumer
repositories or enable runners. Refuse incompatible existing database/storage
state and direct the operator to a migration instead of overwriting it.
Record pending activation before publishing changed files. Preserve it through
interruption and clear it only after required reloads, restarts and verification.
Refuse capture or independent verification while activation remains pending.

Verify observes installed image pins, account allocation, private file metadata,
effective generated definitions, listeners, database/application readiness,
timer state and existing backup/mirror status. It does not repair, pull images,
create fixtures, produce backups, synchronize mirrors, rotate secrets or restart
services. Missing first-backup evidence is reported as incomplete acceptance;
verification does not manufacture that evidence.

This spec owns independent recovery prerequisites. Discover exact attended
restore inputs with `mise run test:forgejo -- restore --help` and
[the restore workflow](../../scripts/forgejo/restore.py). Use bounded service-user
journals through authorized access to distinguish capture, recovery, transfer and
mirror failures; keep protected settings and upstream errors out of public logs.
Resolve the failed precondition before authorizing reconciliation.
Keep all important procedures usable with this repository from GitHub, independent
of Forgejo, its runners, Semaphore and the managed cluster.

## Change-directed validation

Register a `forgejo/default` Debian 13 Molecule scenario and explicit impact rules.
Local `ci:changed` and hosted CI consume the same selection; runtime compatibility
and backup/restore experiments run when the Forgejo scenario is selected.

| Change | Required Forgejo coverage |
| --- | --- |
| Forgejo role, playbooks, helper scripts or focused tests | Forgejo scenario plus runtime experiments |
| Unrelated mapped service changes | No Forgejo scenario |
| Documentation-only changes | Existing fast validation |
| Shared contracts consumed by the Forgejo fixture | Forgejo and other affected consumers |
| Dependency, CI or Molecule framework changes | Existing full-suite policy |
| Unknown impact or invalid classification | Existing full-suite fallback |
| Scheduled/manual full validation | Full suite including Forgejo |

Map the new helper scripts and focused tests explicitly; do not leave them under
the unknown-path fallback. Preserve global full-depth rules and do not change
unrelated subsystem selection merely to make this delivery selective. The initial
implementation touches the registry/classifier/workflow and therefore requires
full validation; later ordinary Forgejo changes can be selective.

Molecule proves role preparation, idempotence, installed generator output,
permissions and timers. Where the existing nested user-namespace limit prevents
application startup, use separate stock-container experiments for actual runtime
and recovery evidence. State that boundary explicitly; do not label generated
unit inspection as proof of production boot persistence or host isolation.

Add classification tests for positive Forgejo paths, unrelated service paths,
documentation, shared dependencies and full-suite fallback, plus matching local
and hosted runtime dispatch. Assertions use effective state or an independent
oracle, not template text as their only source of truth.

Run bootstrap after checkout/dependency changes, focused fast/Ansible validation
during implementation, and `mise run ci:changed` before claiming completion.
Offline PR validation uses synthetic inputs and no production credentials.

## Acceptance and evidence boundaries

Registered stock-container experiments validate consistent database/files capture,
SMB backlog transfer, exact isolated recovery and native HTTPS nightly mirroring.
The mirror experiment retains the shipped schedule and epoch clock, using a
disposable named TZif zone to exercise local 02:00 without waiting overnight.
It checks eligibility, disabled startup/push triggers, completion rounding,
authentication failure/retry and native branch force-update/tag deletion.
Molecule checks installed definitions and actual recovery supervision with a
separate disposable account. These results do not prove physical host boot or
live destination access. Execution logs remain uncommitted under `.tmp/`.

Repository implementation acceptance requires the selected offline validation and
real fixture backup/restore evidence. Service acceptance additionally requires
separately authorized provision/verify on the intended host, trusted private HTTPS,
authenticated clone/fetch/push and web administration, restart and physical reboot
persistence, an exact real NAS archive restore, and the representative GitHub
mirror with explicit source authority.

No design approval authorizes protected-input enrollment, live playbooks,
repository migration, production restart/reboot or mirror-target mutation. Record
those outcomes separately from CI. Issue #7 completion does not declare any
consumer repository Forgejo-authoritative or claim CI parity for future runners.

## References

- [Podman foundation](006-podman-quadlet-foundation.md)
- [Shared private reverse proxy](008-shared-private-reverse-proxy.md)
- [Semaphore service and recovery pattern](009-semaphore-infrastructure-automation.md)
- [Repository command lifecycle](001-agentic-development-modernization.md#repository-command-lifecycle)
- [Forgejo LTS releases](https://forgejo.org/releases/15.x/)
- [Forgejo release index](https://forgejo.org/releases/)
- [Forgejo release schedule](https://forgejo.org/docs/latest/admin/release-schedule/)
- [Stable-release upgrade and maintenance policy](https://forgejo.org/docs/latest/admin/upgrade/)
- [Forgejo 16 changes](https://forgejo.org/2026-07-release-v16-0/)
- [Upstream rootless container deployment](https://forgejo.org/docs/v15.0/admin/installation/docker/)
- [Database recommendations](https://forgejo.org/docs/v15.0/admin/setup/recommendations/#databasedb_type)
- [Database preparation](https://forgejo.org/docs/v15.0/admin/installation/database-preparation/)
- [Backup and upgrade guidance](https://forgejo.org/docs/v15.0/admin/upgrade/#backup)
- [Configuration and native mirror scheduling](https://forgejo.org/docs/v15.0/admin/config-cheat-sheet/)
- [Repository mirroring](https://forgejo.org/docs/v15.0/user/repo-mirror/)
- [Branch and tag protection](https://forgejo.org/docs/v15.0/user/repository/protection/)
