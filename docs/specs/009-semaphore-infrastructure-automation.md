# Specification 009: Semaphore infrastructure automation

Issue: [#4 Semaphore infrastructure automation](https://github.com/supermorphic/homelab-playbook/issues/4)

Status: Draft for operator review. Records the agreed design and implementation
acceptance requirements; it is not evidence of deployment or live recovery.

## Purpose and scope

Deploy Semaphore on NUC #4 as the operator interface for infrastructure
automation. Ansible owns deployment, rootless Podman runs the application and
PostgreSQL, and systemd owns service lifecycle and backup scheduling.

The first delivery includes the application, database, NAS backups, recovery
procedures, an attended restore drill, and one manually triggered `os verify`
repository job. Install a fresh database; no existing database migration is
required. Retained Compose settings are reference material, not desired state.

Workstation execution remains available for every important operation.
Recovering Semaphore or its host must not require Semaphore. Talos upgrade
orchestration remains a separate implementation under issue #6. Do not create
automatic infrastructure maintenance schedules in this delivery.

## Runtime and ownership

Use the dedicated `svc-semaphore` account through the existing Podman foundation.
Declare its numeric identity and subordinate allocations explicitly. Preserve
foundation ownership, lingering user-manager behavior, private state directories,
and root-owned, secret-free Quadlets. Add no login access, sudo permission, or
Podman socket to the service account.

Run Semaphore and PostgreSQL as separate containers under this account, with
separate persistent database storage and application working storage. Use a
private container network; publish no PostgreSQL host port. Publish the
application HTTP backend only on host loopback at an explicitly allocated port.
The account is the shared trust boundary for these application components.

Consume the shared Caddy route and externally managed certificate contracts from
specifications 007 and 008. The application owns its hostname and backend-port
declaration; existing proxy automation owns private HTTPS ingress. Do not add
another proxy, certificate issuer, or backend firewall opening.

Use explicit application, PostgreSQL, and client-image versions and immutable
image references in the implementation. Validate the chosen application against
PostgreSQL 17, and match dump and restore clients to that database major. Resolve
exact image digests and demonstrate compatibility before accepting deployment;
version discovery alone is not compatibility evidence.

Start the application only after database readiness, with bounded startup waits.
Use native systemd restart handling for long-running services and finite timeouts
for one-shot operations. Preserve enforcing platform controls and test volume
ownership and labels on Debian 13 and Rocky Linux 9.

## Configuration and credentials

Public inventory defines non-secret settings and references. Protected inventory
supplies runtime credentials through the established SOPS workflow. Ansible uses
`no_log`, suppresses secret diffs, and delivers private runtime inputs only to the
components that need them. Do not embed secret values in Quadlets, command-line
arguments, image layers, generated reports, or repository job output.

Reuse the current NAS credential inputs through their protected references. Do
not regenerate their values or implicitly load staging inventory from production.
Any required recipient or protected-inventory edit is an operator operation.

Retain Semaphore's stable application key material separately in SOPS-managed
Git history. Recovery requires the settings and key material matching the chosen
database archive. Provisioning an existing installation must not silently replace
these keys or reset its administrator. Bootstrap the initial operator account
using protected inputs and verify subsequent provisioning is idempotent.

## First repository job

Provide one manually triggered job that checks out an explicitly configured
trusted repository revision and executes `os verify` through
`mise run playbook`. Bind the template to a declared inventory and bounded host
selection; do not expose arbitrary playbook actions or free-form shell input.
Record the executed commit and target selection without protected values.

Prepare a reproducible Semaphore execution image with Mise and the tools needed
by the repository's locked bootstrap workflow. Bootstrap the selected checkout
through `mise run bootstrap`, then use its normal gateway and dependency checks.
Do not substitute the stock image's bundled Ansible or bypass a failed dependency
fingerprint. Keep bootstrap and execution serial for a shared checkout, or use
separate job working directories. Start with one concurrent repository job.

Use a dedicated controller SSH credential, strict known-host verification, and a
separate controller age identity. The identity's public recipient covers only the
authorized inventory scope. Never reuse the workstation identity or Forgejo CI
credentials. `os verify` is observational but requires the existing privileged
Ansible access; the template does not make that credential read-only.

Configure `ANSIBLE_SOPS_AGE_KEY_CMD` to an administrator-owned executable that
retrieves the controller identity from protected credential storage directly into
the SOPS input stream. Never persist a plaintext private age identity or fall back
to ambient credentials. Credential delivery must work before the job starts and
must not depend on recovering Semaphore through Semaphore. The implementation
must demonstrate the storage-and-retrieval adapter with disposable identities;
live identity enrollment and recovery remain operator-controlled.

Keep the host's full maintenance workstation-run. The initial job creates no
maintenance schedule and performs no package update, configuration repair,
service restart, or reboot.

## Backup and transfer

Use systemd timers and short-lived client containers. PostgreSQL tools and rclone
run in containers; install neither PostgreSQL clients nor an SMB mount on the
host. Keep helpers limited to archive publication, validation, transfer, and
scoped cleanup. Add no internal cron daemon, custom scheduler, job database, or
per-archive transaction ledger.

| Operation | Schedule | Behavior |
| --- | --- | --- |
| PostgreSQL dump | Daily, `03:00` host-local | Publish a completed local archive |
| NAS transfer | Hourly at `:15` host-local | Copy and verify outstanding archives |

Use `OnCalendar=*-*-* 03:00:00` and `OnCalendar=*-*-* *:15:00`, without a timezone
suffix or random delay. Enable persistent catch-up; this runs a missed activation
when the timer resumes, rather than recreating every missed daily archive.
One-shot services must return to an inactive state so later timer activations
can run. Use finite timeouts and prevent overlapping invocations of each action.

The daily job writes a compressed custom-format `pg_dump` to temporary local
storage. Read the dump through `pg_restore`, produce a SHA-256 checksum, and
publish the pair as one completed archive directory using a same-filesystem
rename. Use unique creation-time names and immutable completed contents. A failed
or interrupted dump never becomes eligible for transfer. Checksums detect damage;
an archive read is not a substitute for a restore drill.

The hourly transfer scans only completed archives. Use rclone copy semantics,
not a mirror that propagates local deletion. Skip unchanged destination data;
successful daily transfer leaves subsequent normal hourly runs doing metadata
checks. Transfer every outstanding archive, including older days after an outage.
An interrupted upload remains retryable without another database dump.

Publish remote completion only after both dump and checksum are present and the
uploaded dump is verified against the local checksum. Use remote content reads
when the SMB backend cannot supply the required hash. Restrict this expensive
verification to new or repaired uploads; hourly success checks must not reread
every historical dump. A completion marker follows verification and is part of
the archive format, not a separate transaction database. Recovery always checks
the retrieved content again.

The transfer service can succeed when no work is needed. NAS failure leaves the
dump service independent and preserves outstanding local archives; the next
hourly activation retries. Systemd records success, failure, and bounded
diagnostics. The timers operate independently: a failed new dump must not prevent
transfer of older completed archives.

## Retention and schedule interaction

Retain archives for seven days locally and 90 days on the NAS, measured from
archive creation time. Preserve local archives without a verified NAS copy even
when older than seven days. After successful transfer, normal age-based cleanup
can remove them. Never silently discard outstanding archives to make space.
Report insufficient space as a failed operation with useful non-secret context.

Prune only this deployment's well-formed completed archives, after successful
transfer verification. Reject unexpected paths and symlinks. Do not delete other
NAS content or apply local retention as a remote mirror. An archive already older
than the NAS retention window after a prolonged outage must not be immediately
uploaded and deleted in the same run: preserve it locally for operator resolution.

The 03:00 dump precedes the source default of 04:00 host security updates and the
Debian 04:30 conditional reboot. These are source defaults, not inspected live
settings. Spacing does not guarantee that a long dump or transfer avoids reboot;
temporary publication and retry behavior must handle interruption. Reuse the
host's configured timezone without introducing a separate UTC policy.

## Recovery and attended restore drill

The workstation initiates recovery using Git, separately retained credentials,
and a selected archive. Preserve existing database storage until the replacement
passes acceptance and the operator accepts cutover. Recreate declared database
roles from configuration; one application database does not need a generic
multi-database catalog or global-role backup framework.

The attended drill follows the homelab-talos single-database recovery pattern:

1. Retrieve an explicitly selected completed archive and checksum from the NAS.
   Validate the exact pair and refuse a corrupt or missing selection without
   silently substituting a different archive.
2. Create run-owned temporary PostgreSQL storage and a fresh database. Restore
   through the matching client container with first-error failure and a single
   transaction. Never restore over the active database.
3. Start an isolated Semaphore instance with the matching runtime settings.
   Block access to managed infrastructure before starting it, and expose no
   production proxy route. Recovered schedules must not execute against real
   targets during the drill.
4. Check expected projects, templates, and history. Exercise a restored synthetic
   credential against a temporary test target to prove application-level use.
   Seed this harmless recovery fixture before the archive used for acceptance.
5. Remove run-owned containers, networks, and storage, and verify their absence.
   Report cleanup failure separately from the original test failure.

The drill ends with cleanup and never cuts over production. Actual recovery has
a separate operator procedure: validate the replacement, review recovered job
settings and target selection before allowing execution, switch the application
to the accepted database, and create and validate a fresh backup. Preserve the
previous database until the operator accepts its retirement.

Application upgrades require a usable pre-upgrade archive and a schema-aware
recovery procedure. Reverting an image alone does not establish database rollback.
PostgreSQL major upgrades require a separate validated migration procedure.

## Operator interface and validation

Replace the retained Compose-only deployment with `semaphore provision` and
observational `semaphore verify` actions through the canonical playbook gateway.
Apply its credential and task-selection guards. Provisioning reconciles declared
state; verification observes service health, private listeners, file metadata,
timer configuration, and backup status without repair or test mutations.

Register restore experiments as `test` workflows under the repository command
lifecycle. Require explicit target and archive selection for attended tests, bind
resources to the run, and repeat live preconditions immediately before mutation.
Document workstation recovery and normal systemd/journald diagnostics in the
Semaphore playbook README and a recovery guide.

Offline validation uses disposable identities, databases, application fixtures,
and SMB storage. Cover effective Quadlet behavior, first provisioning and
idempotence, database readiness, private port publication, image/toolchain
compatibility, and successful repository-gateway execution against fixture hosts.
Test interrupted dumps, corrupt archives, unavailable NAS, interrupted transfer,
three-day backlog recovery, unchanged hourly runs, retention boundaries, and
cleanup scope. Restore real fixture data and prove stored credential use; stub
tests alone cannot establish recoverability.

Run the repository's required validation, including `mise run ci:changed` before
completion. Register container tests in local and CI dispatch together. Separate
offline evidence from operator evidence for actual NAS recovery, controller
enrollment, private ingress, enforcing host controls, and reboot persistence.
No design approval authorizes live playbooks, credential enrollment, or cutover.

## References

- [Podman foundation](006-podman-quadlet-foundation.md).
- [TLS trust](007-off-cluster-tls-trust.md) and
  [shared reverse proxy](008-shared-private-reverse-proxy.md).
- [OS maintenance](003-os-maintenance-security-baseline.md).
- [SOPS controller contract](../guides/sops-secrets.md#automation-controllers).
- [Repository command lifecycle](../reference/repository-command-lifecycle.md).
- [Semaphore configuration](https://semaphoreui.com/docs/admin-guide/configuration).
- [Quadlet reference](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html).
