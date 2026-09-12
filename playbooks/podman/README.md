# Podman foundation playbooks

These playbooks establish reusable rootless Podman capability on Debian 13 hosts
that already have the OS baseline. They target
`podman` through the canonical `mise run playbook` gateway.

- `provision.yml` installs official distribution prerequisites, reconciles
  explicitly declared service identities and directories, and verifies them.
- `verify.yml` observes capability, identity mappings, permissions, password
  locks, sudo denial, and user-manager state without repairs or container runs.

Both actions require the existing key-only `ansible` account with passwordless
sudo. Provisioning includes verification; a second verify run is optional and
useful for later drift checks. Neither action deploys application containers.

Production declares `svc-semaphore` in the `nuc4` host variables. Service
initiatives declare their own accounts and add secret-free, root-owned Quadlets.
No service uses the administrative `ansible` account as its runtime identity.

Use the [root README command reference](../../README.md#podman-foundation-commands)
for operator commands. See
[specification 006](../../docs/specs/006-podman-quadlet-foundation.md) for the
account, filesystem, and Quadlet design.

## Account inputs and ownership

`podman_foundation_accounts` defaults to `[]`. Each entry declares a stable
UID and GID plus unique subordinate UID and GID ranges. For example, these
synthetic values illustrate the schema; they are not production allocations:

```yaml
podman_foundation_accounts:
  - name: svc-example
    uid: 2001
    gid: 2001
    subuid_start: 200000
    subuid_count: 65536
    subgid_start: 200000
    subgid_count: 65536
```

Names begin with `svc-` or `ci-`; each account has a matching private group.
Before assigning production IDs, inspect existing host identities and mappings
through an authorized read-only workflow. Each subordinate range needs at
least 65536 IDs and must not overlap other allocations or host identities in
its namespace. Only file-based subordinate allocation is supported. Related
application and database containers may share one service account; a runner
uses its own account.

| Path | Owner:group | Mode |
| --- | --- | --- |
| `/etc/containers/systemd/users/<UID>/` | `root:<account>` | `0750` |
| Quadlet definitions and `.foundation-account.json` allocation record | `root:<account>` | `0640` |
| `/var/lib/<account>/` | `<account>:<account>` | `0700` |

Accounts have locked passwords, a non-login shell, no SSH keys or sudo, and
persistent user managers through logind lingering. The allocation record
identifies foundation-owned accounts; matching names alone do not permit
adoption. The foundation preserves unrelated subordinate mappings.

Service roles own their application directories, container-user mappings,
image pins, and secret-free Quadlets. They validate definitions with the
installed generator, reload the owning user manager, and start their services.
Boot-started Quadlets use `[Install]` with `WantedBy=default.target`. Generated
service files are transient; do not edit them or enable them manually.

The foundation does not initialize container storage. Preserve subordinate
numeric ownership within application data rather than recursively changing it
to the host account UID. Quadlet mode `0640` is defense in depth, not secret
storage. Root-owned definitions do not prevent the account from controlling
its own user manager; cross-account isolation remains a separate boundary.

## Verification and recovery

Verification checks installed prerequisites and cgroup v2, then each declared
account's mappings, directories, definition metadata, password lock, sudo
denial, and active lingering manager. It stops at the first failed assertion
without repairs, image pulls, or container execution. With no declared accounts,
it checks only host capability and shared boundaries.

After partial account creation, inspect the account, group, subordinate entries,
home, and allocation record before retrying. Incomplete mappings or a missing
record on an existing account require operator migration review. Do not delete
data or renumber identities to bypass that check.

Changing established IDs or ranges requires a reviewed migration with stopped
workloads and backups that preserve numeric ownership. Removing a declaration
does not remove its account, mappings, or data; retirement and ID reuse are
separate actions. Keep the external controller, Git checkout, SOPS identity access, and
console or rescue path available independently of services on this host.

## Development evidence

The registered `system_maintenance/baseline` Molecule scenario exercises the
foundation with two synthetic accounts on Debian 13. It
checks idempotence, permission separation, the installed Quadlet generator,
and drift detection. These checks do not prove a rootless application container
can run on production, survive physical boot, or enforce runner network and
resource policy.
