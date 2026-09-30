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
overrides. This intermediate change establishes preflight only; runtime
reconciliation and verification follow in the service implementation task.

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

Application keys and administrator credentials remain stable on ordinary
provisioning. Retain the protected inputs matching each backup generation
independently. Agents do not read or enroll protected inventory values.

See [specification 010](../../docs/specs/010-forgejo-service.md) and the
[command lifecycle](../../docs/reference/repository-command-lifecycle.md).
