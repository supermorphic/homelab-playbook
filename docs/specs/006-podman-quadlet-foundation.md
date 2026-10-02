# Specification 006: Podman and Quadlet foundation

Issue: [#3](https://github.com/supermorphic/homelab-playbook/issues/3)

## Purpose and ownership

Provide reusable rootless Podman capability on the
[Debian OS baseline](003-os-maintenance-security-baseline.md). Ansible owns
installation and deployment definitions; Quadlet generates systemd services;
systemd supervises Podman. Use distribution prerequisites and fail on unsupported
capabilities. Application roles own their concrete containers and credentials;
the foundation does not define a generic application schema.

[Role inputs](../../roles/podman_foundation/defaults/main.yml) and
[validation](../../roles/podman_foundation/tasks/main.yml) own the declaration
schema. Service accounts exist only when explicitly declared. Service work, not
this spec, selects actual allocations after authorized host inspection. Host
networking daemons and build runners need their own deployment/trust decisions.

## Account and allocation guarantees

Each domain service has an unprivileged account and private primary group.
Related application/database containers may share it; unrelated services and
build workloads do not. The administrative `ansible` account remains the human
and Ansible access path and never runs application containers.

Service accounts have locked passwords, non-login shells, no SSH keys, no sudo,
and no supplementary access to other services or management credentials. This
is shared-kernel filesystem/process separation, not VM isolation, network denial
or a resource-limit guarantee. Root retains administrative access.

Declarations own stable host UID/GID and contiguous subordinate UID/GID ranges.
Subordinate IDs map container users onto distinct host file owners; they do not
create additional host login accounts. Different services' ranges cannot overlap
one another or assigned host identities in the respective namespace, including
unmanaged allocations. UID and GID are separate namespaces and may reuse numbers.
Do not derive allocations from declaration order or the next available number.

Before mutation, validate the complete desired set against effective account/group
lookup and existing file-based subordinate allocations. Resolve named and numeric
owners; reject collisions, ambiguous mappings, invalid bounds and ownership
conflicts. Repeat relevant preconditions immediately before mutation. New accounts
suppress implicit subordinate allocation and receive exactly their declarations;
unrelated entries remain intact.

An existing account with different identity, home or mappings requires reviewed
migration. Missing mappings or a missing foundation allocation record do not
prove an account has no storage. Never adopt an unrelated account, renumber IDs,
or recursively reset application ownership to bypass a mismatch. Removing a
declaration retires nothing; account deletion and ID reuse are separate actions.

## Filesystem and service-manager boundary

Root owns the shared Quadlet parent and each UID-scoped definition directory
under `/etc/containers/systemd/users/<UID>/`. The service can read its own
secret-free definitions and `.foundation-account.json` allocation record but
cannot write their parent. Its private home under `/var/lib/<account>/` contains
runtime state; service roles own application data and container-user mappings.
Reject unexpected links and ownership before mutation. Do not initialize future
container storage or recursively change existing data owners during foundation
provisioning. Backups/restores preserve subordinate numeric ownership as well as
the primary account identity.

Use the owning account's user systemd manager, with logind lingering for startup
without login. Systemd/logind creates runtime directories; do not construct them
manually under `/run/user`. Account-specific Quadlets belong under their UID,
not the shared all-users directory. Setting `User=` in a rootful unit is not
rootless deployment. Validate definitions before manager reload; generated units
are transient and never edited. Boot-started application Quadlets declare their
user-manager startup target.

Service roles own explicit dependencies, pinned images, readiness, restart and
resource policy. The foundation enables no API socket, automatic image update,
pruning, application listener or privileged-port exception. Quadlet permissions
are defense in depth, not a credential delivery method; use the
[SOPS boundary](005-sops-age-secrets.md). Root-owned definitions do not prevent a
compromised service identity from controlling its own user manager or writing
higher-precedence user configuration. Cross-account isolation cannot rely on that.

## Reconciliation, recovery and evidence

[Podman playbooks](../../playbooks/podman/) use the canonical gateway after the OS
baseline. Provisioning reconciles one host at a time, repeats prerequisites and
identity checks, then verifies. OS maintenance does not reconfigure application
infrastructure. Observational verification checks capability, mappings, ownership,
password locks, sudo denial and user-manager state; it stops at the first failed
assertion without repairs, image pulls, manager reloads or container execution.
No declarations means host/shared-boundary checks only.

After partial creation, inspect the account, group, subordinate entries, home
and allocation record before retrying. Missing records or incomplete mappings
on existing accounts require operator migration review. Preserve storage and
completed state rather than deleting data to force another attempt. Changing
established mappings requires stopped workloads and independently recoverable
backups preserving numeric ownership.

Reconstruct from an independent workstation/Git checkout and declared identity
mappings, then restore application data and matching protected inputs through
the service's recovery procedure. Retain console/rescue access and SOPS identity
recovery outside the managed host. No service may be the sole mechanism needed
to recover its host or the cluster.

Disposable validation proves idempotence, account conflict handling, unrelated
mapping preservation, permission separation, the installed Quadlet generator and
verifier drift detection. Synthetic definitions do not prove production application
startup, physical boot persistence, host-kernel isolation or runner network/resource
policy. Those require separate native acceptance; do not weaken the host baseline
to make nested containers pass.

## References

- [Command lifecycle](001-agentic-development-modernization.md)
- [Podman Quadlet](https://docs.podman.io/en/stable/markdown/podman-systemd.unit.5.html)
- [Podman rootless storage](https://docs.podman.io/en/stable/markdown/podman.1.html)
