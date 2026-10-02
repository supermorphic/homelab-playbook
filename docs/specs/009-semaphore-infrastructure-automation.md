# Specification 009: Semaphore infrastructure automation

Issue: [#4](https://github.com/supermorphic/homelab-playbook/issues/4)

## Purpose and scope

Provide the operator's off-cluster infrastructure automation interface through
Ansible-managed, rootless Semaphore and PostgreSQL. Systemd owns lifecycle and
backup scheduling. This boundary includes a fresh database, NAS backups, attended
restore and one manually triggered `os verify` repository job. Talos upgrades
and automatic maintenance schedules are separate work.

Every important operation remains workstation-accessible. Recovering Semaphore,
its credentials or its host must not require Semaphore, Forgejo or the cluster.

## Runtime and credentials

Consume the existing [Podman foundation](006-podman-quadlet-foundation.md)
allocation for the dedicated service account; the application role does not
allocate or renumber identities. Keep a lingering user manager, administrator-owned
secret-free Quadlets and private persistent application/database storage. Add no
login, sudo or runtime socket access. Separate containers share this account's
trust boundary and a private network. Publish application HTTP only on loopback;
PostgreSQL has no host port. Existing [Caddy](008-shared-private-reverse-proxy.md)
and [TLS](007-off-cluster-tls-trust.md) own private HTTPS.

Use immutable upstream images and the repository's pinned execution tools.
[Role defaults](../../roles/semaphore/defaults/main.yml) own exact image pins,
paths and operational settings. Match database dump/restore clients to the
selected database major and demonstrate runtime compatibility, readiness and
mount ownership on the [supported host](003-os-maintenance-security-baseline.md).
Retain enforcing platform controls, bounded waits and systemd restart handling.

Protected inventory supplies runtime inputs through the
[SOPS boundary](005-sops-age-secrets.md), with private delivery, suppressed logs
and diffs. Secrets never enter Quadlets, process arguments, images or reports.
Reuse protected NAS references without regenerating values or crossing inventory
boundaries. Retain stable application keys independently with the protected inputs
matching each archive; current keys alone may not recover an older archive.
Ordinary provisioning must not replace those keys or reset an existing administrator.

## First repository job

The manual template binds a trusted HTTPS repository, exact revision, inventory
and bounded host selection to the canonical `mise run playbook` gateway's
observational `os verify`. Reject arbitrary actions and shell arguments; record
commit and targets without secrets. Its SSH credential still has privileged
Ansible authority: an observational template does not make the credential read-only.

Use a dedicated controller SSH key, strict known-host verification and a separate
controller age identity covering only its approved inventory scope. Never reuse
workstation or CI credentials. An administrator-owned `ANSIBLE_SOPS_AGE_KEY_CMD`
retrieves the identity directly from protected storage into SOPS; no plaintext
identity or ambient fallback is permitted. The credential adapter must function
before jobs start and remain recoverable without Semaphore.

The selected adapter uses a root-owned systemd encrypted credential and a
service-UID-scoped socket provider. For loss or restart, restore or separately
re-enroll the independently retained encrypted credential, verify its private
ownership, then restart `semaphore-age.socket` through authorized host access.
Do not create an ambient identity file. Adapter implementation belongs to the
[controller tasks](../../roles/semaphore/tasks/controller.yml) and
[unit templates](../../roles/semaphore/templates/).

Use the pinned upstream application image without an implicit custom build or
per-job host package installation. Persist execution tools and dependency storage
across disposable checkouts and container restarts. Bootstrap the selected checkout
normally, preserving the combined dependency verification before execution.
Unchanged verified Galaxy dependencies must work without Galaxy or dependency Git
access. Override-only or Python-lock changes must not force Galaxy downloads.

Serialize shared dependency preparation and execution. Stage changed dependencies
separately and publish only after installation, overrides and verification pass.
Failure preserves the last verified set but fails the changed job; it never runs
changed requirements against stale dependencies. Initial preparation and genuine
dependency changes still need their sources. Full host maintenance remains
workstation-run, with no maintenance schedule created by this template.

## Backup, transfer and retention

Use persistent host-local systemd timers and bounded, non-overlapping container
clients. [Templates](../../roles/semaphore/templates/) own exact calendars and
[defaults](../../roles/semaphore/defaults/main.yml) own retention and deadlines.
Keep daily capture and independent backlog transfer; a successful capture also
requests transfer. Persistent catch-up runs a missed activation, not every missed
historical backup. One-shots return inactive so later activations can run.
Install neither host PostgreSQL clients nor an SMB mount or custom scheduler.

Capture a compressed custom-format database dump, prove readability and checksum
it, then atomically publish an immutable completed directory on one filesystem.
Failed or interrupted capture never becomes transfer-eligible. Backups contain
protected application state and remain private.

Use rclone copy semantics, never deletion-propagating mirroring. Verify new or
repaired remote payloads before publishing completion metadata; unchanged runs
check metadata without rereading every historical dump. Retry all outstanding
completed archives independently of today's capture. NAS outage preserves local
archives and does not block another dump; interrupted upload remains retryable.
Checksums and readable dumps do not establish application recoverability.

Measure retention from archive creation. Local pruning requires a verified remote
copy and is restricted to this service's well-formed completed archives. Reject
unexpected paths and symlinks. Never discard untransferred archives to make room;
report insufficient capacity. Archives already beyond remote retention after an
outage require operator resolution, not immediate upload followed by deletion.
Schedule spacing around host maintenance does not guarantee freedom from reboot;
publication and retries must tolerate interruption.

## Independent recovery and attended restore

Use the independently accessible Git checkout, exact selected NAS archive and
matching protected settings from the workstation. Keep prior database storage
until replacement acceptance and explicit cutover. Recreate declared database
roles from configuration; this service does not require a global database catalog.

The existing attended workflow is discoverable without Semaphore:

```sh
mise run test:semaphore -- restore --help
```

Command help and the [restore implementation](../../scripts/semaphore/restore.py)
own exact arguments. Prepare the selected archive, private runtime settings and
NAS configuration, a fresh destination, a bounded disposable credential test
target and independently recorded expected application state. Attended mutation
requires explicit target authorization; confirmation arguments bind intent only.

1. Retrieve that exact completed archive. Verify remote completion, checksum and
   dump readability; reject symlinked or escaping paths, corruption and missing
   selection without falling back to another archive.
2. Restore into run-owned PostgreSQL storage with matching clients, first-error
   handling and a single transaction. Never target the active database.
3. Before starting the restored application, establish networking that permits
   only its temporary database, bounded test target and test client. Disable
   recovered schedules, block managed infrastructure and create no production route.
4. Compare restored projects, templates and history with an independent oracle.
   Execute the seeded synthetic job using its restored credential without replacing
   it. Prove the target rejects absent/incorrect credentials and accepts the restored
   credential; retain the result without revealing its value.
5. Remove only run-owned containers, networks and storage on success or failure.
   Verify absence and report cleanup failure separately from the original failure.

A drill never performs cutover. Actual recovery reviews restored jobs and target
selection before allowing execution, separately authorizes switching to the
accepted database, then creates and validates a fresh backup. Preserve old storage
until retirement is accepted. Application upgrades need a usable pre-upgrade
archive and schema-aware recovery; reverting an image does not roll back a
migration. Database major upgrades need a separately validated procedure.

## Verification and acceptance boundaries

The [playbooks](../../playbooks/semaphore/) reconcile compatible declared state
and observe installed health, private listeners, permissions, timers and backup
status through the canonical gateway. Verification never repairs, pulls images,
restarts services, produces backups or runs fixture jobs. Use the service account's
user manager and bounded journals for diagnosis; do not print protected environment
or rclone configuration. Failed dependency preparation is corrected and retried
without deleting all persistent tools or forcing downloads on every job.

Offline validation uses disposable identities, application/database state and SMB
storage. It covers idempotent preparation, dependency reuse and failed publication,
actual gateway execution, corruption, interruption, NAS outage/backlog, retention
and restored stored-credential use. Local and hosted classification must dispatch
the same registered tests. The supported-OS nested namespace cannot start the
application containers: Molecule proves preparation, generated units and metadata;
separate stock-container experiments provide runtime and recovery evidence.
Neither is complete native host acceptance.

Actual controller enrollment, host provision/verify, consecutive timer activations,
private HTTPS, enforcing host controls, physical reboot and exact real NAS recovery
require separately authorized operator evidence. Design approval grants no live
execution, credential enrollment or cutover authority.

## References

- [Command lifecycle](001-agentic-development-modernization.md)
- [Semaphore configuration](https://semaphoreui.com/docs/admin-guide/configuration)
- [Quadlet reference](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
