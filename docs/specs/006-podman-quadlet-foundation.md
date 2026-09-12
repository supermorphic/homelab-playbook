# Specification 006: Podman and Quadlet foundation

Issue: [#3 Establish NUC #4 Podman and Quadlet foundation](https://github.com/supermorphic/homelab-playbook/issues/3)

## Purpose and scope

Establish the reusable host capability and conventions for
GitHub → Ansible → Quadlet → systemd → Podman. Ansible installs Podman and
manages deployment definitions. systemd supervises the resulting services.

Issue #3 implements the reusable foundation, not the future applications.
Its production account list is empty. It does not create `svc-forgejo`,
`svc-semaphore`, `ci-runner`, application data directories, application
Quadlets, or application credentials. Later service issues instantiate the
foundation with explicit account declarations and service-specific files.
Synthetic accounts and Quadlets may exist in bounded disposable tests only.

The foundation remains separate from the OS baseline defined by
[Specification 003](003-os-maintenance-security-baseline.md). Debian 13 is the
supported platform. Podman prerequisites use
official distribution packages; unsupported capabilities fail explicitly.
No additional Galaxy collection is required merely to write Quadlet files.

Tailscale's deployment model remains a separate service decision. The
foundation does not assume that a host networking daemon is a rootless app.

## Account and authority model

Each domain service has its own unprivileged account and private primary
group. Related containers, such as an application and its dedicated database,
may share that service account. There is no shared `svc-containers` account.
Build workloads use a separate rootless runner account when the runner issue
activates them.

The existing `ansible` account remains the administrative and human login
path. Ansible administers service accounts through its existing sudo authority.
Application containers never run under `ansible`.

Service accounts have locked passwords, a non-login shell, no SSH keys, no
sudo grants, and no supplementary membership granting access to another
service's state or management credentials. The existing SSH policy continues
to admit only `ansible`. Root retains administrative access to all state.

Account separation is a filesystem and process ownership boundary on a shared
kernel. It is not a virtual-machine boundary and does not by itself prevent
network access or resource exhaustion. Later runner work must define workload
trust, resource limits, network access, and allowed mounts before deployment.

## Declarative identity allocation

The reusable account input defaults to an empty list. Each entry must declare:

- account name;
- stable numeric UID;
- stable numeric GID for a private primary group with the same name;
- subordinate UID range start and count; and
- subordinate GID range start and count.

The account's primary UID and GID identify it on the host. Its subordinate
ranges reserve additional numeric IDs that rootless Podman can map to users
and groups inside its containers. A new domain service may need these IDs
for an application user and a database user in its related containers.
Reserving a range does not create a host login account or group for every ID.
The range start is the first available ID; the count is the number reserved.
For example, a start of `200000` and count of `65536` reserve IDs
`200000–265535`. These numbers are illustrative, not production allocations.

When a later service issue instantiates an account, it declares unused ranges
alongside the stable primary UID and GID. The service role selects container
user mappings and data ownership appropriate to its application images.
Related containers may use the same account's allocation; a separate range
is not required for every container. Different service accounts must have
non-overlapping allocations so their container identities cannot map to the
same host file-owner IDs.

UIDs and GIDs are explicit inventory inputs, never allocated from declaration
order or the next available host number. A service's declaration is the
durable allocation record. Later service issues assign actual production
values after inspecting existing allocations; this specification reserves no
future application identities.

Each account receives one contiguous range of at least 65536 subordinate UIDs
and one contiguous range of at least 65536 subordinate GIDs. Ranges must be
unique and non-overlapping within their respective UID or GID namespace,
including allocations for unmanaged accounts. Subordinate ranges must also
exclude assigned host identities in their respective namespace. An account's
subuid and subgid ranges may use the same numbers because UID and GID are
separate namespaces.

Before any account or mapping mutation, validate the complete declaration
set and compare it with effective host account/group lookup and existing
`/etc/subuid` and `/etc/subgid` allocations. Reject duplicate names, IDs,
overlapping ranges, invalid bounds, ambiguous mappings, and ownership
conflicts. Resolve both named and numeric subordinate-file owners. Repeat
these safety preconditions immediately before the relevant mutation.

An existing account with different UID, GID, home, or subordinate mappings
causes a failure with an operator-directed migration requirement. Automation
does not renumber accounts, change existing storage mappings, recursively
change application ownership, or take over an unrelated account. Missing
mappings on a pre-existing account require explicit migration review rather
than an assumption that it has no container storage.

For newly created accounts, suppress implicit subordinate allocation and
install exactly the declared mappings. Preserve unrelated entries. Confirm
the effective account and mapping state after creation. A partial failure
stops the host; recovery instructions identify the completed steps and required
inspection before rerunning. Removing an entry from desired inputs does not
delete its account, storage, or mappings. Retirement is a separate operation;
IDs and ranges are not automatically recycled.

## Filesystem ownership

For each explicitly declared service account, use this layout:

| Path | Owner:group | Mode | Purpose |
| --- | --- | --- | --- |
| `/etc/containers/systemd/users/` | `root:root` | `0755` | Shared administrator-owned parent |
| `/etc/containers/systemd/users/<UID>/` | `root:<service-group>` | `0750` | Account-scoped Quadlet definitions |
| Quadlet files below that directory | `root:<service-group>` | `0640` | Secret-free deployment definitions |
| `/var/lib/<account>/` | `<account>:<service-group>` | `0700` | Private account home and runtime state |

Parent directories must not be writable by service accounts. Reject unexpected
symlinks and conflicting ownership at managed roots before mutation. Create
account-specific directories only for declared accounts. Application issues
own any `data/` subdirectories and container-specific ownership below them.
The foundation never recursively resets ownership of existing application data.

Podman's default rootless storage lives below the account's home at
`.local/share/containers/storage`. Container users may map to subordinate
host IDs within application storage. Backup and restore procedures must
preserve those numeric ownership relationships as well as the primary UID.

The foundation does not pre-create container storage by running Podman under
future service identities. It does not create shared writable application
directories or grant one service access to another service's home.

## Quadlet and systemd conventions

Each foundation-owned account has a root-owned `.foundation-account.json`
record in its UID-scoped directory, mode `0640` and the service's private group.
The record contains only its declared identity allocation. Existing accounts
without this record require operator migration review before adoption.

Root installs secret-free Quadlet definitions into the UID-scoped directory.
Do not put per-service units directly in the shared
`/etc/containers/systemd/users/` directory, where they can apply to all users.
Rootless Quadlets run through the owning account's user systemd manager;
setting `User=` on a rootful Quadlet is not the rootless mechanism.

For instantiated accounts, enable lingering so the user manager can start at
boot and remain available without an interactive login. Manage the user
manager and runtime directory through systemd/logind, not manual directory
creation under `/run/user`. Reload the relevant user manager after successful
definition validation. Generated service files are transient outputs and are
never edited or committed. Future long-running application Quadlets use
`[Install]` with `WantedBy=default.target` for user-manager startup.

Each future service owns its Quadlets, explicit dependencies, image pins,
restart behavior, health checks, mounts, and resource policy. Application and
image versions are pinned in Git; floating references such as `latest` and
automatic image updates are excluded. The foundation does not enable Podman
API sockets, automatic-update timers, image pruning, application listeners,
or host-wide privileged-port exceptions.

Quadlet files contain no secrets. Mode `0640` is defense in depth, not a
secret-storage mechanism. Future service work chooses its credential delivery
mechanism under the repository's current SOPS and age boundary. This issue
does not introduce secret payloads, change encryption systems, or inspect
protected inventory values.

Root ownership protects the installed definitions from ordinary writes by the
service identity. It does not claim to prevent a compromised service account
from controlling its own user manager or writing higher-precedence user
configuration. Cross-account isolation must not depend on that claim.

## Proposed implementation and operator interface

Expose standalone `os verify` using the existing `os_baseline_verify` role.
It gathers fresh facts and loads policy defaults independently of provisioning
or maintenance, while preserving inventory overrides. It checks the same OS
baseline without package updates, repairs, service restarts, or reboots.
Existing OS provisioning and maintenance retain their included verification;
a second verify command is not required after either succeeds. The standalone
action permits later drift checks and reports the first failed assertion rather
than promising an exhaustive drift report. `os inspect` remains the basic
OS-fact snapshot. Update the OS design records and guide with this interface.

Add a narrow `podman_foundation` role for prerequisite packages, account input
validation, conflict checks, account and mapping creation, private directories,
and user-manager setup. Add a separate observational verifier for the resulting
foundation state. Avoid a general-purpose application deployment schema in this
issue; later roles own their concrete Quadlet templates.

Expose `podman provision` and `podman verify` through the existing
`mise run playbook -- <playbook> <action> <inventory> [ansible-args...]` gateway.
Use an explicit `podman` group containing `nuc4`. Group defaults declare no
accounts; host variables declare application allocations, including
`svc-semaphore` on `nuc4`. Adding inventory membership does not execute against
the host.

`provision` reconciles the foundation after the existing OS baseline and
includes verification. It processes one host at a time and repeats platform,
connection, identity-conflict, and prerequisite checks before mutation. Extend
the gateway's applicable credential and task-selection guards to this action.
Do not make OS maintenance install or reconfigure application infrastructure.

`verify` only observes installed prerequisites, cgroup v2 availability,
declarative account and mapping state, filesystem metadata, and applicable
systemd state. It does not pull images, initialize storage, start services,
reload managers, or run disposable containers. Functional experiments belong
in a registered `test` workflow, not an observational command.

## Verification and recovery

Offline validation covers schema errors, identity/range collisions, existing
account mismatches, preservation of unrelated mappings, filesystem ownership,
empty-list behavior, gateway guards, and verifier drift detection. Assertions
use effective state or explicit invariants rather than reproducing template
text as their only oracle.

Disposable tests instantiate synthetic accounts to prove idempotence and
cross-account filesystem denial, render synthetic secret-free Quadlets with
the installed generator, and verify generated user-service definitions.
The synthetic Quadlet uses a pinned image reference but is not started or
pulled. Runtime application execution is separate live evidence for later
service work; any future container experiments require bounded test cleanup.
Nested-container limitations must be reported explicitly; they do not prove
physical boot persistence, host-kernel enforcement, network isolation, or
production readiness. Do not weaken the host baseline to make nested tests pass.

Run `mise run bootstrap`, focused `validate:fast` and `validate:ansible`, and
the depth selected by `mise run ci:changed`. Extend the registered test and
classifier surfaces when necessary to cover the new subsystem.

Recovery remains possible from an external controller and trusted console.
Reconstruct the host foundation from Git and the declared identity mappings;
restore application data and secrets from independently available backups
under later service procedures. Nothing deployed on NUC #4 may be the sole
mechanism needed to recover NUC #4 or Talos. This foundation does not implement
application backups or claim that an untested restore is proven.

Live production execution remains a separate operator-authorized action with
the exact playbook, action, inventory, host limit, and arguments reconfirmed
immediately before execution.

## References

- [Podman Quadlet documentation](https://docs.podman.io/en/stable/markdown/podman-systemd.unit.5.html)
  documents UID-scoped definitions, generator behavior, and user-unit startup.
- [Podman documentation](https://docs.podman.io/en/stable/markdown/podman.1.html)
  documents rootless storage and subordinate identity requirements.
- [Specification 003](003-os-maintenance-security-baseline.md) defines the OS
  baseline and administrative authority that this foundation builds on.
- [Repository command lifecycle](../reference/repository-command-lifecycle.md)
  distinguishes provisioning, observation, and bounded experiments.
