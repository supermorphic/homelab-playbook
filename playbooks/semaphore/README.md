# Semaphore playbooks

These playbooks manage Semaphore and PostgreSQL 17 as rootless containers under
`svc-semaphore` on Debian 13. They use pinned upstream images;
the application image is not built in this repository.

`provision.yml` reconciles application storage, protected configuration,
Quadlets, timers, and the optional initial controller job. `verify.yml` observes
existing state without pulling images, repairing files, or restarting services.
Use the canonical gateway with a separately authorized target and action:

```bash
mise run playbook -- semaphore provision production --limit <host>
mise run playbook -- semaphore verify production --limit <host>
```

Both commands require the existing key-only `ansible` account and passwordless
sudo. No deployment or migration of an existing database is implicit.

## Prerequisites and inputs

Provision the [Podman foundation](../podman/README.md) first. Declare
`semaphore_foundation_account` with the exact existing `svc-semaphore` allocation:
`name`, `uid`, `gid`, `subuid_start`, `subuid_count`, `subgid_start`, and
`subgid_count`. The application role verifies this record and does not allocate
or change identities.

Set `semaphore_hostname`, `semaphore_backend_port`, and
`semaphore_certificate_name`. The shared `reverse_proxy_routes` list must contain
that exact hostname, backend port, and certificate name. Provision the existing
proxy and certificate contracts first. Semaphore publishes HTTP only on
`127.0.0.1`; PostgreSQL publishes no host port. Private HTTPS remains the shared
proxy's responsibility.

Supply these runtime values through the repository's SOPS inventory workflow:

- `semaphore_database_password`;
- `semaphore_admin_login`, `semaphore_admin_password`, `semaphore_admin_name`,
  and `semaphore_admin_email`;
- `semaphore_cookie_hash`, `semaphore_cookie_encryption`, and
  `semaphore_access_key_encryption`;
- `semaphore_rclone_config`, containing the SMB remote configuration.

Set the public `semaphore_rclone_remote` to a deployment-specific directory
below that remote, such as `backup:archives/semaphore-example`. Use the existing
protected NAS credentials as inputs; do not print or copy decrypted inventory
into issues or documentation. Retain the application settings independently
for recovery. Reprovisioning must use the same stable application keys.

The [role defaults](../../roles/semaphore/defaults/main.yml) define image pins,
paths, timeouts, and retention. Retained inventory is a baseline to evaluate,
not a substitute for these required contracts.

## Initial repository job

Enable `semaphore_controller_enabled` and
`semaphore_controller_identity_enabled` after preparing the dedicated identity
through the [controller SOPS procedure](../../docs/guides/sops-secrets.md).
Configure a trusted HTTPS repository URL, full commit in
`semaphore_controller_revision`, matching repository ref, inventory name, and
explicit host selection. Supply the dedicated controller SSH private key and
independently verified `semaphore_ssh_known_hosts` content.

Provisioning declares one manual OS verification template using the native
Semaphore API. It creates no schedule. The fixed wrapper checks the checkout's
origin and commit, rejects tracked modifications, and holds a bounded lock
across bootstrap and the canonical `os verify` command. It rejects arbitrary
arguments and uses strict SSH host-key checking. The SSH credential's authority
must be evaluated separately from this template's observational behavior.

Tools persist under the service's `tools` directory. Provisioning copies only
repository dependency inputs into a bootstrap workspace and prepares them in a
short-lived stock Semaphore container. Jobs use their checked-out revision's
normal bootstrap. Requirements changes select a new verified Galaxy generation;
unchanged requirements reuse the existing generation without contacting Galaxy.
Override-only changes reuse the downloaded base. Python dependencies follow the
frozen uv lock independently.

If preparation fails, the previous verified Galaxy generation stays available,
but the changed job fails instead of silently using stale dependencies. Correct
the failed input or dependency source and rerun the authorized job. Do not delete
all persistent tools as a routine repair or force Galaxy installation on each run.

## Backups and transfer

The user manager runs two persistent timers in the host's local time:

| Timer | Schedule | Work |
| --- | --- | --- |
| `semaphore-backup.timer` | Daily at 03:00 | PostgreSQL client creates a local archive |
| `semaphore-transfer.timer` | 00:15, 04:15, 08:15, 12:15, 16:15, 20:15 | rclone transfers outstanding archives |

A successful dump also starts transfer. Each job has a finite timeout and exits
when its work finishes. The transfer schedule remains independent of the latest
dump, so an older backlog can transfer even when today's dump fails.

Archives contain `database.dump` and `SHA256SUMS`. A temporary directory becomes
a completed local archive only after dump readability and checksum checks pass.
The transfer verifies newly uploaded content before publishing the remote
`COMPLETE` marker. Later runs check metadata and completion records without
reading or writing unchanged dump payloads. After a NAS outage, the next
successful run transfers all outstanding completed archives.

Local retention is seven days and NAS retention is 90 days, measured from archive
creation. Local cleanup requires a verified remote copy. Untransferred archives
are preserved; an outstanding archive already beyond NAS retention requires
operator review. Unknown paths and incomplete local staging directories are not
treated as completed archives.

## Diagnosis and recovery

On the selected host, inspect user units through the service account's manager:

```bash
sudo systemctl --user --machine=svc-semaphore@.host status semaphore.service
sudo systemctl --user --machine=svc-semaphore@.host list-timers
sudo journalctl _UID="$(id -u svc-semaphore)" _SYSTEMD_USER_UNIT=semaphore-backup.service
sudo journalctl _UID="$(id -u svc-semaphore)" _SYSTEMD_USER_UNIT=semaphore-transfer.service
```

Do not print protected environment files or rclone configuration while diagnosing
failures. Follow the [recovery guide](../../docs/guides/semaphore-recovery.md)
to select and test an exact archive. Recovery tests use replacement storage and
an isolated application; production cutover is a separate operator action.

Offline tests use synthetic credentials and run-owned resources. The supported-OS
Molecule fixtures check preparation idempotence, filesystem metadata, generated
container commands, and timer properties. Their nested user-namespace limits
prevent starting the application containers. Separate stock-container tests
exercise PostgreSQL compatibility, backups, transfer, and recovery. Full host
provision and verify, consecutive one-shot activations, physical reboot, host
security enforcement, private HTTPS reachability, and real NAS recovery need
separate operator evidence on the intended hosts.
