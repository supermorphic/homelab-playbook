# Specification 003: OS maintenance and security baseline

Issue: [#13 Establish a maintainable OS maintenance and security baseline](https://github.com/supermorphic/homelab-playbook/issues/13)

## Purpose

Establish a reusable, least-privilege operating-system baseline for off-cluster
hosts. The baseline owns bootstrap expectations, package maintenance,
administrative access, SSH, firewall policy, mandatory access control, time
synchronization, local security logging, reboot behavior, and effective-state
verification.

The baseline prepares hosts for later application roles without deploying or
configuring those applications. It replaces the current composition of broad
third-party bootstrap and security roles with narrow repository-owned policy
and focused maintained mechanisms.

## Governing decisions

1. Debian 13 and Rocky Linux 9 are the complete-provisioning platforms. Debian
   13 is the primary production platform.
2. A target must already provide a key-only `ansible` account with working
   passwordless sudo. Automation never falls back to root login, a password
   prompt, or broader credentials.
3. The repository owns security policy. Focused dependencies may implement a
   mechanism, but their defaults do not define the policy.
4. `willshersystems.sshd` implements OpenSSH configuration. Repository roles
   own accounts, sudo, firewall, mandatory access control, time, logging, and
   package-maintenance policy.
5. firewalld provides one firewall mechanism across Debian and Rocky. Time
   synchronization uses each platform's default client: `systemd-timesyncd` on
   Debian and chrony on Rocky.
6. SELinux remains enforcing on Rocky. AppArmor remains enforcing on Debian.
7. Native operating-system tools install security updates and perform required
   security-update reboots. The repository does not add a reboot coordinator.
8. Routine full system updates run through an Ansible maintenance playbook.
   Issue #4 will schedule that playbook through Semaphore UI; no host-local
   recurring full-update timer or cron job is added here.
9. Persistent journald, auditd, native updater records, and systemd unit state
    are the durable failure signals. Verification remains an Ansible operation;
    the baseline does not add a host-local aggregate health daemon or result
    format.
10. Complete provisioning and disruptive maintenance process one host at a
    time.
11. CI remains offline and secret-free. Container evidence does not prove
    physical reboot, boot persistence, recovery access, network reachability,
    or kernel enforcement.

## Scope

### Included

- pre-mutation platform, connection, interpreter, privilege, and repository
  preflight;
- minimum Python bootstrap through an established passwordless-sudo path;
- authoritative management of the `ansible` account's public keys and sudoers
  policy after bootstrap;
- official distribution repository trust and minimum baseline packages;
- complete and security-only package-update behavior;
- native automatic-update schedules and required security-update reboots;
- a scheduler-neutral full-maintenance playbook;
- repository-owned SSH policy implemented through `willshersystems.sshd`;
- firewalld, SELinux or AppArmor, platform-native time synchronization,
  persistent journald, and auditd;
- read-only effective-state verification reusable after provisioning, after an
  Ansible-controlled reboot, and from a future Semaphore schedule;
- complete-composition Molecule coverage for Debian 13 and Rocky Linux 9.

### Excluded

- application deployment or application-specific packages, ports, sysctls,
  users, credentials, and health checks;
- Tailscale installation, Tailscale SSH, or automatic access through
  `tailscale0`;
- fail2ban while SSH remains private-network-only and key-only;
- public SSH exposure;
- remote log forwarding or selection of a notification transport;
- a custom reboot coordinator, notification daemon, or post-boot status file;
- a host-local recurring full-update timer or cron job;
- Semaphore task templates, schedules, credentials, notifications, and its
  self-update or self-reboot behavior;
- broad CIS, STIG, DevSec, or similar compliance profiles;
- custom SSH cryptographic algorithm policy;
- automatic removal of packages that may now be owned by an operator or later
  application role;
- live production or staging execution; and
- formal compliance certification.

## Supported platform contract

| Capability | Debian 13 | Rocky Linux 9 |
| --- | --- | --- |
| Complete provisioning | yes | yes |
| Explicit full package update | yes | yes |
| Native automatic security updates | yes | yes |
| Complete access and hardening policy | yes | yes |
| Complete-composition Molecule coverage | yes | yes |
| Maintenance-role Molecule coverage | yes | yes |

Complete-baseline playbooks reject every unsupported family before mutation.

## Provisioning lifecycle

Complete provisioning uses one host-sized batch and the following order:

1. Connect as the existing `ansible` account without fact gathering or
   privilege escalation.
2. Read `/etc/os-release` and verify that the target is Debian 13 or Rocky
   Linux 9.
3. Verify the expected account, an available `sudo` command, non-interactive
   `sudo -n` success, and a usable package-manager configuration.
4. Install only the minimum Python runtime through a platform-specific raw
   command when Python is absent.
5. Reset the Ansible connection, gather facts, and repeat the platform and
   privilege assertions with normal modules.
6. Verify official repository trust and configure the platform-native time
   synchronization client.
7. Perform the explicitly authorized full operating-system update and install
   only packages required by this baseline.
8. Reconcile the administrative account, sudo, SSH, firewall, mandatory access
   control, journald, and audit policy.
9. Configure the native security-only updater and its maintenance window.
10. Validate every access-affecting candidate before activation.
11. Reboot at playbook level when the provisioning transaction requires it and
    the invocation authorizes reboot execution.
12. Reconnect and run the reusable read-only verification task set.

The initial raw stage has no root-login alternative. If the account, key,
`sudo`, or passwordless policy is missing, the playbook stops and identifies the
installer or console action required. It does not attempt to create its own
authority.

## Administrative access

### Account and keys

The only repository-managed administrative login is `ansible`. The account has
a home directory, an interactive shell needed for Ansible operation, and a
locked password. SSH is its normal access path.

Authorized public keys are an explicit, non-empty Vault-backed or
operator-supplied inventory list. Key values do not appear in public inventory,
fixtures, logs, or documentation. Each controller has a separate key:

- the operator workstation private key remains on the workstation; and
- Issue #4 adds a different Semaphore private key and its public counterpart.

The repository never stores private key material and stores no plaintext public
key material. Rotation uses an add-verify-remove sequence so the active key is
not removed in the same unverified change that introduces its replacement.
Before exclusive replacement, the complete candidate `authorized_keys` content
is written to a private temporary file and validated with OpenSSH tooling.
OpenSSH logs the accepted public-key fingerprint on the managed host for
attribution and revocation.

### Sudo

The `ansible` account receives passwordless sudo for all commands. Complete
provisioning needs broad root authority, and a command allowlist would be brittle
because Ansible transfers and executes versioned module payloads from temporary
paths.

The sudo policy is a dedicated root-owned `0440` drop-in. A candidate is checked
with `visudo` before activation. The playbook proves `sudo -n` operation after
activation. It never requests or stores an account password.

The single account also supports a human operator's exceptional one-off command
from an authorized workstation. That operation uses the operator's own SSH key
and non-interactive sudo; it does not require a second human account or a shared
private key.

### OpenSSH

`willshersystems.sshd` is pinned to `v0.34.0`. The repository supplies the
complete selected policy through a drop-in and disables the role's firewall and
SELinux integration.

The effective policy:

- permits only the `ansible` account;
- permits public-key authentication and retains SFTP required by Ansible;
- disables password, keyboard-interactive, empty-password, and direct root
  authentication;
- disables client-hostname lookup with `UseDNS no`, so the connection-specific
  `host` and `addr` inputs both use the authenticated peer address;
- disables agent forwarding, TCP forwarding, X11 forwarding, and tunnels;
- uses the standard port and does not bind to a Tailscale-specific address;
- leaves algorithm selection to the distribution and RHEL system crypto policy;
  and
- uses platform service management without replacing vendor unit files.

The role renders and syntax-checks a candidate before reload. Repository
verification then checks the complete effective configuration with
`sshd -T -C` for the actual administrative connection user and endpoints,
independently confirms `usedns no`, keeps the current connection open through
activation, establishes a new connection, and confirms the authorized
administrative path. An invalid candidate never triggers a reload.

## Repository and package policy

Only enabled, signature-verified Debian or Rocky distribution repositories are
part of the baseline. The implementation preserves APT signature verification
and DNF repository GPG checks. It does not import third-party signing keys or
enable EPEL by default. Later roles must own and justify any additional
repository.

The baseline package set contains only packages required for:

- the Python and sudo management path;
- OpenSSH;
- firewalld;
- the platform-native time synchronization client;
- SELinux or AppArmor;
- auditd and persistent system logging;
- native automatic security updates; and
- certificates and platform support required by those mechanisms.

The baseline stops installing Git, Vim, tree, xterm, Python development tools,
`netaddr`, HTTP utilities, `qemu-guest-agent`, fail2ban, and other convenience,
development, virtualization, or application packages. It does not uninstall an
already present package merely because the new minimum no longer owns it. A
later application or platform-specific role installs and owns its exact
dependencies.

Package cleanup occurs only during an explicit full-maintenance or provisioning
run, after the update succeeds. Daily security-update jobs do not perform broad
automatic package removal. Vendor kernel-retention defaults remain in effect so
the baseline does not reduce the available rollback set.

## Update and reboot policy

### Native security updates

Debian uses `unattended-upgrades` with Debian Security origins only. Rocky uses
`dnf-automatic` with `upgrade_type = security` and `reboot = when-needed`.
Both native timers run daily at an inventory-configurable maintenance time.
Timer configuration is explicit and bounded so vendor random delay cannot move
work outside the intended window.

Debian automatic reboot is enabled when `/var/run/reboot-required` exists,
including when a user remains logged in. Rocky delegates the reboot decision to
DNF's native `when-needed` behavior. A host-specific schedule can stagger hosts
without adding a cross-platform reboot coordinator.

This policy also applies to the NUC that will run Semaphore. A native
security-update reboot is acceptable because Semaphore did not initiate it.

### Full updates

Provisioning performs one full system update as part of its explicitly
authorized transaction. A separate maintenance playbook provides the same full
update, reboot, reconnection, and verification path for routine operation.

The maintenance playbook:

- remains directly runnable through `mise run playbook` from an operator
  workstation;
- validates the target, action, inventory, and package-manager state before
  mutation;
- waits for bounded native package-manager activity and fails clearly when the
  lock does not clear;
- processes one host at a time;
- uses the distribution's supported full-upgrade operation;
- reboots only when the operating system reports that a reboot is required and
  reboot execution is enabled for that invocation; and
- reconnects and runs effective-state verification before advancing.

Issue #4 will create the recurring Semaphore schedule and task template. It
will also decide how the Semaphore host itself receives a full update without
allowing Semaphore to reboot its own host. This specification adds no recurring
full-update systemd timer or cron entry.

## Firewall policy

firewalld is installed and enabled on Debian and Rocky. It uses nftables through
the distribution packages and owns the baseline host firewall on both systems.

The baseline:

- denies unsolicited inbound traffic by default;
- permits outbound traffic and established return traffic;
- denies packet forwarding by default;
- permits SSH only from a non-empty list of explicit private management
  sources;
- applies equivalent IPv4 and IPv6 policy when those families are enabled;
- opens no application, DNS, HTTP, Podman, forwarded, public, or Tailscale
  access; and
- exposes an empty structured extension list for later service roles.

The SSH dependency does not manage firewall state. Firewall changes establish
the new management allowance before removing an old allowance, preserve the
active connection, validate runtime state, and then persist the proven policy.
Before any firewall activation or interface move, the baseline proves that the
active SSH peer belongs to the desired management sources. It also fails closed
when existing zone bindings, direct openings, or policy objects are outside the
supported platform state and require operator reconciliation. Runtime and
permanent configuration are read back independently.

fail2ban is not installed. Under a private-source, key-only SSH policy it adds
little protection while introducing another privileged daemon, dynamic ban
state, possible lockout, and an EPEL dependency on Rocky. A later public
exposure design must reassess both source restrictions and rate-limiting
controls.

## Mandatory access control

Rocky requires SELinux targeted policy in enforcing mode. Debian requires
AppArmor enabled with distribution-supplied profiles loaded in enforce mode.
The baseline does not layer both systems on one platform and does not create
speculative application profiles.

A transition from SELinux disabled state can require boot configuration,
filesystem relabeling, and reboot. Provisioning detects that state and performs
only a controlled, explicit transition whose reboot remains subject to the
playbook reboot control. It never changes directly from disabled to an
unverified enforcing runtime state. Later application roles own any labels or
profiles required by their resources and must not disable platform enforcement
to resolve a denial.

Container tests assert packages, configuration, and task decisions but do not
claim that an unprivileged container proves host-kernel enforcement.

## Time synchronization and local logging

Time synchronization uses the operating system's default client:
`systemd-timesyncd` on Debian and chrony on Rocky. The baseline ensures that
the selected package and service are present and enabled, but it does not
replace distribution or DHCP-provided time sources.

The repository does not expose a time-source override in this initiative and
does not make an internal time service a bootstrap dependency. A later design
may add provider-neutral explicit sources when the environment has an internal
NTP requirement. Verification checks the selected provider, service health,
and effective clock synchronization. It does not reimplement either provider's
configuration parser or attempt to prove the provenance of every dynamic time
source.

Journald uses persistent storage and its bounded rotation behavior so updater,
service, boot, and verification records survive a reboot without unbounded disk
growth. Auditd is installed and enabled with vendor-default rules. This
initiative does not add a broad syscall or filesystem-watch profile that could
create excessive logging or interfere with later container workloads.

Remote forwarding and notification credentials are outside this initiative.
Issue #4 may use Semaphore's supported task notifications, and a later external
monitor may deliver availability failures to the operator's selected service.

## Verification and failure signals

The repository provides a reusable, read-only Ansible verification task set. It
runs after complete provisioning and every Ansible-controlled reboot. Issue #4
can schedule the same task set after native maintenance windows.

The verifier owns its expected-policy interface. Provisioning and maintenance
pass the required authorized-key and management-source inputs explicitly.
Optional firewall-service and journal-size expectations use low-precedence
verifier defaults that follow the security-baseline policy defaults and retain
inventory overrides. The verifier does not depend on the security-baseline role
having run earlier in the play.

Verification checks:

- the expected operating-system release;
- the `ansible` account, authoritative public keys, locked password, and
  non-interactive sudo path;
- connection-specific effective `sshd -T -C` authentication, account, and
  forwarding policy;
- firewalld service, default policy, private SSH allowance, and matching runtime
  and permanent state;
- SELinux or AppArmor effective enforcement;
- platform-native time-client health and effective clock synchronization;
- persistent journal availability and auditd service health;
- native security-update configuration and enabled timer;
- package-manager consistency;
- reboot-required state; and
- unexpected failed systemd units.

Checks are observational and never repair a failed host. Existing sources keep
failure evidence durable:

- systemd retains failed unit state;
- journald retains service, update, and boot detail;
- auditd retains structured security records;
- APT and DNF retain update results and history; and
- Semaphore will retain the result of tasks it starts.

A host that never returns cannot report its own failure. External availability
or dead-man monitoring remains a follow-on requirement and is not replaced by
Semaphore running on that same host.

## Dependency decisions

| Dependency | Current repository pin | Selected decision | Reason |
| --- | --- | --- | --- |
| `robertdebock.bootstrap` | `7.1.5` | remove; do not upgrade to `7.1.7` | The repository needs a narrow Python bootstrap with an explicit pre-existing sudo contract. The newer role assumes root execution and does not remove that authority boundary. |
| `geerlingguy.security` | `3.0.0` | remove; do not upgrade to `3.0.2` | It couples SSH, sudo, fail2ban, and automatic updates while not implementing the complete selected host policy. |
| `willshersystems.sshd` | absent | add at `v0.34.0` | It supports Debian 13 and EL 9, validates configuration before reload, and handles platform OpenSSH service behavior without owning repository policy. |
| `ansible.posix` | absent | add at `2.2.2` | It supplies focused maintained modules for POSIX platform mechanisms such as firewalld and SELinux and supports the pinned controller. |
| `devsec.hardening` | absent | do not add at `10.6.0` | Its OS and SSH roles apply a much broader policy surface than this baseline has evaluated. |

Dependency removal occurs only after replacement behavior has executable
coverage in the complete composition. No dependency is upgraded solely to be
removed in the same initiative.

## Role and playbook boundaries

The implementation keeps four clear responsibilities:

1. A repository bootstrap task set performs only raw platform detection,
   prerequisite checks, minimum Python installation, connection reset, and fact
   gathering.
2. `system_maintenance` owns explicit full updates, cleanup, minimum
   maintenance packages, native security-update configuration, and reboot-state
   detection. It no longer owns unrelated convenience or application packages.
3. A repository security-baseline role owns the administrative account, sudo,
   SSH policy input, firewalld, mandatory access control, platform-native time
   synchronization, journald, and auditd.
4. A reusable verification task set observes effective state without mutation.

`playbooks/os/provision.yml` composes these responsibilities in the lifecycle
defined above. A separate OS maintenance playbook exposes full update and
post-update verification for direct workstation and future Semaphore use.
`mise run playbook` remains the only repository playbook execution interface.

## Testing and evidence

Issue #13 validates complete Debian and Rocky composition using the same
rootless, unprivileged Podman worker model.

Executable evidence includes:

- raw preflight acceptance and unsupported-platform rejection;
- minimum bootstrap behavior with and without Python already present;
- converge, deterministic-task idempotence, and independent verification;
- authoritative account, key, sudoers, and effective SSH policy;
- invalid sudo and SSH candidate rejection before activation;
- rendered firewalld policy and guarded runtime-application decisions with
  private-source SSH only;
- native updater configuration, timer enablement, and reboot suppression inside
  containers;
- platform-native time synchronization, journald, auditd, and platform MAC
  packages, configuration, and guarded service decisions;
- minimum package presence and absence from the managed package set;
- dependency pins and removal of replaced roles; and
- scheduler-neutral full-maintenance playbook structure and sequential batch
  control.

Tests generate disposable SSH key material at runtime in bounded temporary
state. No private or public test key is committed or emitted to CI logs.

Safety controls suppress operations that require unavailable host-kernel
authority, such as firewall application, clock adjustment, audit-kernel
attachment, MAC enforcement, and reboot. Their production defaults remain
enabled. Scenario values disable only execution and retain configuration,
decision, and verification-contract coverage.

Where a container cannot safely expose the real kernel or boot behavior,
contract tests prove the configuration and decision boundary instead. CI does
not claim evidence for:

- an actual reboot and reconnection;
- persistence across a physical boot;
- SELinux or AppArmor kernel enforcement;
- firewall runtime enforcement or reachability from a real management network;
- host clock synchronization or external NTP reachability;
- audit-kernel event collection;
- physical or out-of-band recovery access;
- native timer execution at wall-clock time; or
- Semaphore scheduling and notification delivery.

Those behaviors require later explicitly authorized live verification. No live
verification result becomes pull-request CI evidence.

## Migration sequence

1. Add the focused dependency pins and dependency contract tests.
2. Add bootstrap preflight and minimum Python handling.
3. Add the security-baseline role and its focused tests.
4. Narrow `system_maintenance` to maintenance behavior and stop installing
   non-essential packages.
5. Add the reusable verification task set and scheduler-neutral maintenance
   playbook.
6. Compose and validate complete Debian and Rocky provisioning.
7. Remove `robertdebock.bootstrap` and `geerlingguy.security` only after the
   complete composition proves their selected replacement behavior.
8. Update operator documentation and run all change-directed validation.

This order keeps the repository executable during migration and prevents a
dependency removal from creating an untested access or update gap.

## Acceptance criteria

Issue #13 is complete when:

1. complete provisioning rejects every platform except Debian 13 and Rocky
   Linux 9 before mutation;
2. bootstrap succeeds through an existing key-only `ansible` account with
   passwordless sudo, installs only missing Python capability, and has no root
   or password fallback;
3. the `ansible` account has a locked password, an authoritative non-empty key
   list, validated passwordless sudo, and independently rotatable controller
   keys;
4. effective SSH state permits only the approved key-based administrative path,
   retains Ansible file transfer, disables the selected forwarding features,
   and follows platform crypto policy;
5. firewalld denies unsolicited inbound and forwarded traffic and permits SSH
   only from explicit private management sources in matching runtime and
   permanent state;
6. SELinux is configured enforcing on Rocky and AppArmor enforcing on Debian,
   with container evidence limited to what the container can prove;
7. `systemd-timesyncd` is enabled on Debian, chrony is enabled on Rocky, and
   effective clock synchronization is verifiable on both platforms;
8. persistent journald and vendor-default auditd retain local update, boot,
   service, and security failure evidence;
9. Debian and Rocky install security updates daily through native tools and
   perform native required reboots in an explicit maintenance window;
10. no host-local recurring full-update schedule exists, and the full
    maintenance playbook remains directly runnable and suitable for a future
    Semaphore schedule;
11. complete provisioning and full maintenance process one host at a time and
    reconnect and verify after every authorized Ansible-controlled reboot;
12. the baseline manages no convenience, development, virtualization, or
    application package without a current requirement;
13. `robertdebock.bootstrap` and `geerlingguy.security` are absent only after
    replacement coverage passes, while `willshersystems.sshd` and
    `ansible.posix` are exactly pinned;
14. complete Debian and Rocky Molecule composition passes converge,
    deterministic-task idempotence, and independent verification;
15. no test contacts inventory hosts, reads Vault material, performs a real
    reboot, or overstates container evidence; and
16. repository-required change-directed validation passes before publication.
