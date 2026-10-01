# Specification 010: Forgejo service

Issue: [#7 Forgejo self-hosted Git](https://github.com/supermorphic/homelab-playbook/issues/7)

Status: approved design, implemented locally. Offline validation and live service
acceptance are separate evidence; live acceptance remains pending.

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

## Runtime and database decision

Use the maintained upstream rootless Forgejo image and a dedicated PostgreSQL 17
container under `svc-forgejo`. Select the supported Forgejo LTS line. Research on
2026-09-30 selected Forgejo 15.0.9-rootless, PostgreSQL 17.11 and rclone 1.75.1.
Reviewed immutable multi-architecture image digests are recorded in role defaults.
Stock-container compatibility checks cover protected initialization, database
privileges, persistence and native HTTPS mirroring. No floating image updates or
custom application image is introduced.

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

Use application authentication. Disable reverse-proxy authentication and explicitly
restrict trusted proxy sources to the effective local proxy path. Confirm the
source seen through rootless port forwarding rather than using a wildcard trust
setting. Ordinary application verification observes local HTTP health; a trusted
HTTPS check from the intended client network is separate live acceptance.

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

Verify observes installed image pins, account allocation, private file metadata,
effective generated definitions, listeners, database/application readiness,
timer state and existing backup/mirror status. It does not repair, pull images,
create fixtures, produce backups, synchronize mirrors, rotate secrets or restart
services. Missing first-backup evidence is reported as incomplete acceptance;
verification does not manufacture that evidence.

Document prerequisites and diagnosis in `playbooks/forgejo/README.md`, and exact
archive selection and attended recovery in `docs/guides/forgejo-recovery.md`.
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
- [Repository command lifecycle](../reference/repository-command-lifecycle.md)
- [Forgejo LTS releases](https://forgejo.org/releases/15.x/)
- [Upstream rootless container deployment](https://forgejo.org/docs/v15.0/admin/installation/docker/)
- [Database recommendations](https://forgejo.org/docs/v15.0/admin/setup/recommendations/#databasedb_type)
- [Database preparation](https://forgejo.org/docs/v15.0/admin/installation/database-preparation/)
- [Backup and upgrade guidance](https://forgejo.org/docs/v15.0/admin/upgrade/#backup)
- [Configuration and native mirror scheduling](https://forgejo.org/docs/v15.0/admin/config-cheat-sheet/)
- [Repository mirroring](https://forgejo.org/docs/v15.0/user/repo-mirror/)
- [Branch and tag protection](https://forgejo.org/docs/v15.0/user/repository/protection/)
