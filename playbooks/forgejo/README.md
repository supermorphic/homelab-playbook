# Forgejo playbooks

Forgejo uses dedicated PostgreSQL 17 and upstream rootless containers under
`svc-forgejo` on Debian 13. The existing Podman foundation owns that account;
the existing Caddy and TLS workflows own private HTTPS.

Run separately authorized host operations through the canonical gateway:

```sh
mise run playbook -- forgejo provision production --limit <host>
mise run playbook -- forgejo verify production --limit <host>
```

Both actions require key-only SSH and passwordless sudo for `ansible`. The
playbooks target the `forgejo` inventory group. Staging is inactive and frozen
inventory is unsupported. The interface rejects password and task-selection
overrides. Provisioning reconciles private configuration and rootless services
under one host operation lock. Verification observes installed state without
pulling images, restarting services, or repairing configuration.

Declare `forgejo_foundation_account` with the exact existing foundation fields:
`name`, `uid`, `gid`, `subuid_start`, `subuid_count`, `subgid_start`, and
`subgid_count`. The role does not create or renumber identities. Real allocations
require authorized host inspection and separate foundation provisioning.

Set `forgejo_hostname`, `forgejo_backend_port`, `forgejo_certificate_name`, and
the exact host address seen through rootless forwarding in `forgejo_proxy_source`
as a single `/32` IPv4 address or `::1/128`. Use synthetic examples such as
`forgejo.infra.example.com` and backend port `18081` in public documentation.
The protected `reverse_proxy_routes` list must contain that exact unique route;
preserve unrelated routes. TLS files must exist before Forgejo provisioning.

Protected inventory supplies these inputs through the existing SOPS workflow:

- `forgejo_database_admin_password` and `forgejo_database_password`;
- `forgejo_admin_login`, `forgejo_admin_password`, `forgejo_admin_email`;
- `forgejo_secret_key`, `forgejo_internal_token`, `forgejo_lfs_jwt_secret`,
  `forgejo_oauth2_jwt_secret`; and
- `forgejo_rclone_config`, with separately selected `forgejo_rclone_remote`.

Optional `forgejo_mirror_credentials` supplies a protected Git credential-store
file. Leave it empty until separately authorized mirror enrollment. Entries use
HTTPS credential URLs scoped to one destination repository. The role installs a
read-only `core.askPass` lookup, so failed authentication cannot erase declared inputs.
The complete private configuration directory remains inside the backup boundary.

Enroll each native push mirror with a credential-free HTTPS destination URL,
`interval: 8h` and `sync_on_commit: false`. The shipped native scan runs once at
02:00 in the host timezone, with startup synchronization disabled. Initial/manual
synchronization requires separate operator direction. Verification reads native
attempt/error metadata without requesting synchronization. A failed attempt is
reported as failed; without a first success the status remains pending. Successful
evidence older than 36 hours is stale. No configured targets means unconfigured.

Native Git mirroring can force-update branches and delete destination refs. Review
the exact destination, visibility and protections before enrollment. Issues, PRs,
reviews and other collaboration data remain in the database backup boundary.
Mirrored LFS authentication and object transfer need separate acceptance evidence.

Application keys and administrator credentials remain stable on ordinary
provisioning. Retain the protected inputs matching each backup generation
independently. Agents do not read or enroll protected inventory values.

## Deployment order

Each live command needs direction for its exact host, action and arguments.
Follow the existing [foundation](../podman/README.md), [TLS](../tls/README.md)
and [proxy](../reverse-proxy/README.md) procedures:

1. Inspect approved allocations and the forwarding source. Enroll foundation
   declarations, Forgejo group membership and protected application, NAS and
   proxy inputs. Preserve unrelated accounts and routes.
2. Provision and verify the Podman foundation on that host.
3. Prepare the TLS issuer and shared certificate. Initial issuance needs separate
   authorization. Prepare Caddy and its matching route declaration.
4. Provision Forgejo, then activate its route through proxy provisioning.
5. Verify Forgejo, proxy and TLS, then perform the attended checks in
   [specification 010](../../docs/specs/010-forgejo-service.md#acceptance-and-evidence-boundaries).

Provisioning rejects identity changes, stable key/database credential changes,
unknown existing storage, PostgreSQL major changes and runtime pin changes.
Those changes need a reviewed recovery or migration procedure.

## Backup operation and diagnosis

| Operation | Host-local schedule | Bound |
| --- | --- | --- |
| Native push-mirror scan | Once nightly at 02:00 | Eight-hour eligibility; no startup or push trigger |
| Local backup | Daily at 03:30 | 30-minute capture; separate five-minute recovery |
| NAS transfer | Every four hours at :45, plus successful backup recovery | Two hours, independent of downtime |

Persistent backup/transfer timers catch missed activations. Backup stops Forgejo
while PostgreSQL stays running, captures the database and complete application
and configuration files, then restores intended availability. Persistent intent
and a separate recovery service handle interruption and boot. A timer does not
start an application that was deliberately stopped.

`/var/lib/svc-forgejo/forgejo/backups` holds completed local generations. Retain
local archives for seven days from creation, deleting them only after remote
verification. Retain NAS archives for 90 days from creation. Failed transfers
preserve the backlog. Generations older than remote
retention, unexpected files and unresolved recovery state need operator review;
the helper fails instead of broadening deletion or overwriting another archive.

`forgejo verify` reports readiness, backup/transfer evidence and native mirror
status. Missing first evidence stays pending; `acceptance_complete` stays false
because live acceptance is independent. Review capture, recovery and transfer
unit results separately in the `svc-forgejo` user manager. Use bounded journal
reads and keep private configuration and upstream errors out of public logs.

For failed capture, establish whether recovery restored availability. For failed
recovery, preserve intent and storage, identify the failed precondition, and
authorize recovery separately. Do not remove the recovery guard to force
provisioning. For NAS failure, restore declared access and capacity, then observe
the independent retry. Prove recoverability with an exact restore drill.

Interrupted provisioning retains pending activation. Retry the same authorized
provisioning action to finish reloads and required restarts; verification and
capture refuse unfinished activation. Verify also observes loaded systemd
deadlines, recovery hooks, startup guards, dependencies and timer calendars.
Availability recovery does not reread archive payloads; capture validates before
publication and transfer validates independently within its own budget.

## Offline validation

```sh
mise run test:forgejo -- unit
mise run test:forgejo -- compatibility
mise run test:forgejo -- fixture
mise run test:molecule -- forgejo/default
mise run ci:changed
```

Compatibility checks the pinned upstream images and disposable HTTPS mirrors.
Fixture mode checks consistent capture, SMB outage/backlog transfer and recovery
with original authentication, refs, collaboration objects, attachments and LFS.
Both modes report owned cleanup separately from operation failure.

Molecule checks Debian installation, idempotence, generated units, permissions
and actual systemd recovery supervision with a separate disposable service
account. Nested namespaces prevent product container startup there; stock
container experiments supply runtime evidence. Physical reboot, trusted HTTPS,
real NAS access and real GitHub mirrors remain attended checks.
Ordinary Forgejo changes select its scenario and both runtime modes. Shared
consumer contracts add affected scenarios; CI/dependency/framework changes
require the full suite. Unrelated mapped services do not select Forgejo.

See [specification 010](../../docs/specs/010-forgejo-service.md) and the
[command lifecycle](../../docs/guides/repository-command-lifecycle.md).
