# Specification 010: Forgejo service

Issue: [#7](https://github.com/supermorphic/homelab-playbook/issues/7)

## Purpose and authority boundary

Provide recoverable, Ansible-managed Forgejo on the existing Debian host using
rootless Quadlet/systemd/Podman. The workstation remains the deployment and
recovery interface. Existing Caddy/TLS provides private HTTPS; Git uses HTTPS
only. Dedicated PostgreSQL, nightly backup downtime and native nightly GitHub
recovery mirroring are deliberate operating choices.

Service implementation and offline evidence do not establish live acceptance.
[#57](https://github.com/supermorphic/homelab-playbook/issues/57) owns isolated
runners; [#58](https://github.com/supermorphic/homelab-playbook/issues/58) owns CI
parity, collaboration migration and repository authority cutover. Existing
repositories remain GitHub-authoritative until their protections, validation,
collaboration-history decision and recovery are accepted. This service neither
moves Flux sources nor enables deployment jobs. Broader reconstruction/off-site
application backups belong to [#9](https://github.com/supermorphic/homelab-playbook/issues/9).
Backup and mirroring cannot depend on notification-service availability.

## Runtime, storage and credentials

Use maintained upstream rootless Forgejo LTS and a dedicated PostgreSQL container
under the service's [foundation allocation](006-podman-quadlet-foundation.md).
[Role defaults](../../roles/forgejo/defaults/main.yml) own immutable image pins,
paths and operational settings. PostgreSQL reuses the existing operating pattern
and accommodates concurrent work, at the cost of another lifecycle/restore step;
it does not improve backup frequency. Do not share Semaphore's database or add
an application image build, queue, object store or automatic image updates.

The application database role controls only its database/schema; initialization
authority remains separate. Match dump/restore clients to the selected database
major. Validate upstream container-user and mount compatibility. The foundation
owns account creation and mappings; Forgejo never renumbers them or recursively
resets application ownership. Keep root-owned secret-free Quadlets, a lingering
user manager, database-before-application startup, bounded readiness and systemd
restart/deadline handling.

Separate private database, application, configuration, credential and backup
storage. Live application and database storage stays local, never NAS-mounted.
The backup boundary includes repositories, attachments, avatars, local
LFS objects and stable application keys; container image storage is disposable.
Protect runtime settings through [SOPS](005-sops-age-secrets.md). The application
gets no runtime socket, runner identity, age identity, TLS issuer credential or
unrelated administrative authority.

Use local application accounts with closed registration and lock managed
installation. First-administrator bootstrap is idempotent: the pinned
CLI produces a private random password and an authenticated API update installs
the protected initial password. Never pass passwords in process arguments.
Ordinary provisioning neither resets the administrator nor regenerates stable
keys. Interrupted bootstrap retains evidence and requires operator recovery when
an existing account lacks completion evidence. Independent administrator recovery
without SMTP or an external identity provider remains an acceptance requirement;
the repository does not currently expose a dedicated recovery command.

Keep protected inputs matching each archive independently recoverable after later
key/credential rotation. Enable ordinary repositories, issues, PRs, attachments
and HTTPS Git/LFS; Actions execution, SSH and packages remain disabled. New durable
features must extend backup and restore acceptance together.

## Deployment and private HTTPS

The [playbooks](../../playbooks/forgejo/) use the canonical gateway and its
credential/task-selection guards. Provision compatible declared state, then
verify; staging needs separate activation and frozen inventory is unsupported.
Deploy in this order, with separate authorization for each live action:

1. Inspect identity allocations and the effective forwarding source; enroll
   foundation, group membership and protected application/NAS/proxy inputs.
2. Provision and verify the foundation. Prepare the shared certificate and Caddy
   route declaration, following [TLS bootstrap](007-off-cluster-tls-trust.md#host-bootstrap-and-diagnosis).
3. Provision Forgejo, activate its route through proxy provisioning, then verify
   Forgejo, proxy and TLS and perform client acceptance.

Publish application HTTP only on host loopback, with no PostgreSQL or SSH host
port. The external root URL matches the chosen private HTTPS hostname. Preserve
unrelated proxy routes; [the proxy](008-shared-private-reverse-proxy.md) owns TLS
keys and route activation. Use application authentication, disable proxy
authentication, and trust only the observed forwarding source rather than a
wildcard. Loopback health does not prove trusted client HTTPS.

Reject changes to established identity, stable keys/database credentials, unknown
storage, database major or runtime pins that require migration. Record pending
activation before changing files, retain it across interruption, and clear it
only after required reloads, restarts and verification. Retry the same authorized
provisioning action to complete activation; capture and independent verification
refuse pending state. Do not remove guards to force provisioning.

## Consistent backup and independent transfer

Daily capture permits bounded Forgejo downtime while PostgreSQL remains running.
Persistent timers own capture and independent backlog transfer; successful capture
and service recovery also request transfer. One-shots return inactive for later
activations. [Unit templates](../../roles/forgejo/templates/) own exact calendars
and budgets. No host database client, SMB mount or custom scheduler is required.

Capture and provisioning serialize on the service-operation lock:

1. Repeat path, storage, compatibility and running-state preconditions.
2. Stop Forgejo and confirm all application Git subprocesses stopped. Never accept
   a capture made while the application can change database or file state.
3. Produce a readable custom-format database dump and archive all durable
   application/configuration state with numeric ownership and permissions.
4. Validate archive structure, non-secret compatibility metadata and checksums;
   atomically publish one complete generation from a private same-filesystem stage.
5. Restore intended application availability and verify health before releasing
   the lock; only then request transfer.

A database-only archive is insufficient. Independent systemd-supervised recovery
handles capture timeout, interruption and boot, not just shell exit traps. A
backup timer must not start a deliberately stopped application. Keep primary
capture and recovery failures distinct; failed restart retains recovery intent
and blocks further active-state changes. Completed-backup transfer stays available
without rereading payloads during availability recovery.

Rclone copies into the dedicated NAS prefix; it never mirrors local deletions.
Verify new/repaired uploaded payloads before publishing remote completion metadata.
Unchanged runs inspect completion metadata without downloading every historical
payload. NAS failure preserves local archives and independent retries, without
blocking the next capture. Transfer budgets do not extend application downtime.

Measure retention from creation. Prune local archives only after verified remote
copy and only within declared service boundaries. Preserve untransferred data and
report insufficient capacity. Backlog older than remote retention requires
operator resolution, not upload followed by immediate deletion. Reject unexpected
paths, links and incomplete archives. Archives contain private application state.
Nightly capture permits roughly a day's loss when healthy; outages can extend it.
GitHub mirroring supplies off-site code, not a database/collaboration backup.

## Native nightly GitHub mirrors

The application owns HTTPS push mirrors and destination-scoped credentials;
neither runners nor Semaphore are prerequisites. Use one native nightly scan,
with push/startup synchronization disabled. Keep eligibility shorter than the scan
interval so completion rounding does not skip alternate nights. The exact schedule
is configuration-owned and must be tested with the pinned application, including
restart and failure behavior. Manual/initial synchronization needs separate direction.

Keep destinations credential-free. A private read-only askpass helper supplies
scoped credentials without process-argument exposure or deleting declared inputs
after failed authentication. Retain this configuration in the backup boundary.
Mirrored LFS authentication and object recovery need separate acceptance.

Production targets start unconfigured. Enrollment must identify source authority,
exact source/destination, visibility and protection; disable competing writes.
Native mirrors can force-update branches and delete destination refs. Do not relax
canonical repository protection to make a demonstration succeed. Observe native
attempt/error/last-success state and staleness; compare explicit expected branch
and tag revisions, not moving targets. Failed or missed runs remain visible.

Mirroring permits nightly code lag and does not replicate issues, reviews,
settings, Actions history or release assets. Never promote GitHub automatically
when Forgejo is unavailable. Authority cutover remains separate.

## Independent restore and diagnosis

Keep the GitHub checkout, workstation tools, NAS access and archive-matching
protected inputs available independently of Forgejo, runners, Semaphore and the
managed host. Existing attended restore help is available with:

```sh
mise run test:forgejo -- restore --help
```

The [restore workflow](../../scripts/forgejo/restore.py) owns arguments and
processing bounds. Select an exact completed archive, fresh run-owned destination,
matching protected settings and independently recorded expected application state.
Authorization is required for that target and experiment; confirmation is only
an intent guard. The restore must:

1. Verify remote completion, checksums, compatibility, dump readability and safe
   extraction. Reject escaping paths, unsafe links and excessive archive resources;
   check bytes and inodes. Never fall back to another or the latest archive.
2. Establish isolated networking before starting restored software. Restore into
   new database/application storage, with compatible images and correct ownership,
   using first-error handling and one database transaction. Never target active
   volumes or install a production route.
3. Block outbound production/GitHub access even when restored state contains
   mirrors or webhooks; disabling schedules alone is insufficient isolation.
4. Authenticate with the restored original credential, without replacing it.
   Compare issues, PRs, comments, attachments, exact Git refs and LFS hashes against
   the independent oracle, then prove new fixture Git work succeeds.
5. Clean up only run-owned resources and verify absence. Cleanup failure fails the
   experiment but remains distinct from its original result; preserve the archive.

An archive listing or successful database import does not prove application
recovery. Actual cutover is separately authorized: preserve old storage, validate
the replacement in isolation, review recovered mirrors/webhooks before enabling
egress, and retain prior state until acceptance. Upgrades require a usable
pre-upgrade archive; image rollback cannot undo database migrations. Database
major changes need a validated migration procedure.

Use `forgejo verify` and bounded service-user journals to distinguish capture,
availability recovery, transfer and mirror status. Keep protected settings and
upstream errors out of public logs. On failed recovery, preserve intent/storage,
resolve its failed precondition, then authorize recovery. On NAS failure, restore
declared access/capacity and observe independent retry. Verification observes
installed state and evidence without repairs, image pulls, backups, mirror sync,
secret rotation or service restart. Missing initial evidence stays pending.

## Evidence and acceptance

The shared classifier selects the registered Forgejo Molecule and runtime tests;
CI/framework/dependency changes retain full validation. Offline fixtures cover
corruption, interruption, NAS backlog, retention, timer reactivation, recovery
supervision, original authentication and exact restored data, using disposable
resources and independent expected values. Stock-container tests prove runtime
compatibility and native mirror trigger/failure behavior. Molecule proves Debian
preparation, idempotence, generated units and supervision; nested namespace limits
prevent product container startup there. Neither proves physical host boot.

Live acceptance requires separately authorized host provision/verify, trusted
private HTTPS, authenticated web administration and Git, restart/physical reboot
persistence, an exact real NAS restore and a representative GitHub mirror with
explicit source authority. Implementation, deployment and native acceptance are
separate outcomes; issue #7 alone establishes no repository cutover or runner CI
parity. Design approval authorizes none of these live operations.

## References

- [Command lifecycle](001-agentic-development-modernization.md)
- [Forgejo LTS](https://forgejo.org/releases/15.x/)
- [Rootless deployment](https://forgejo.org/docs/v15.0/admin/installation/docker/)
- [Backup and upgrade guidance](https://forgejo.org/docs/v15.0/admin/upgrade/#backup)
- [Native mirror configuration](https://forgejo.org/docs/v15.0/admin/config-cheat-sheet/)
- [Repository mirroring](https://forgejo.org/docs/v15.0/user/repo-mirror/)
