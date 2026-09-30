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

See [specification 010](../../docs/specs/010-forgejo-service.md) and the
[command lifecycle](../../docs/reference/repository-command-lifecycle.md).
