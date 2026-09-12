# Specification 003: OS maintenance and security baseline

Issues: [#13 Establish a maintainable OS maintenance and security baseline](https://github.com/supermorphic/homelab-playbook/issues/13)
and [#40 Remove Rocky Linux support](https://github.com/supermorphic/homelab-playbook/issues/40)

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

Issue #40 narrows the current managed-host contract to Debian 13. It removes the
unused Rocky Linux implementation and its validation paths while preserving the
existing Debian behavior. Related contracts remain in [Specification 002](002-multi-os-molecule-validation.md),
[Specification 004](004-managed-host-onboarding.md), [Specification 006](006-podman-quadlet-foundation.md),
[Specification 007](007-off-cluster-tls-trust.md), [Specification 008](008-shared-private-reverse-proxy.md),
and [Specification 009](009-semaphore-infrastructure-automation.md).

## Governing decisions

1. Debian 13 is the only complete-provisioning platform.
2. A target must already provide a key-only `ansible` account with working
   passwordless sudo. Automation never falls back to root login, a password
   prompt, or broader credentials.
3. The repository owns security policy. Focused dependencies may implement a
   mechanism, but their defaults do not define the policy.
4. `willshersystems.sshd` implements OpenSSH configuration. Repository roles
   own accounts, sudo, firewall, mandatory access control, time, logging, and
   package-maintenance policy.
5. firewalld provides the host firewall mechanism. Time synchronization uses
   Debian's `systemd-timesyncd` client.
6. AppArmor remains enforcing.
7. Native operating-system tools install security updates and perform required
   security-update reboots. The repository does not add a reboot coordinator.
8. Routine full system updates run through an Ansible maintenance playbook.
   Recurring Semaphore maintenance scheduling remains deferred; no host-local
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
- firewalld, AppArmor, platform-native time synchronization,
  persistent journald, and auditd;
- read-only effective-state verification reusable after provisioning, after an
  Ansible-controlled reboot, through a standalone operator action, and from the
  manually triggered Semaphore verification job;
- complete-composition Molecule coverage for Debian 13.

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

| Capability | Debian 13 |
| --- | --- |
| Complete provisioning | yes |
| Explicit full package update | yes |
| Native automatic security updates | yes |
| Complete access and hardening policy | yes |
| Complete-composition Molecule coverage | yes |
| Maintenance-role Molecule coverage | yes |

Complete-baseline playbooks reject every unsupported distribution or release
before mutation.

The Debian-only transition removes the unused Red Hat-family bootstrap,
maintenance, repository-trust, updater, time-service, firewall-policy,
mandatory-access-control, and verification branches. It also removes their
task files, templates, variables, helper exports, and result references when no
Debian consumer remains. The transition retains Debian APT trust,
`unattended-upgrades`, reboot scheduling, AppArmor, firewalld, SSH policy,
auditd, `systemd-timesyncd`, and the exact Caddy ownership marker used by later
proxy operations. It does not add Raspberry Pi behavior or change the generic
native ARM64 and AMD64 test architecture support.

## Provisioning lifecycle

Complete provisioning uses one host-sized batch and the following order:

1. Connect as the existing `ansible` account without fact gathering or
   privilege escalation.
2. Read `/etc/os-release` and verify that the target is Debian 13.
3. Verify the expected account, an available `sudo` command, non-interactive
   `sudo -n` success, and a usable package-manager configuration.
4. Install only the minimum Python runtime through the Debian raw bootstrap
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
11. Reboot at playbook level when the provisioning transaction requires it.
12. Reconnect and run the reusable read-only verification task set.

The initial raw stage has no root-login alternative. If the account, key,
`sudo`, or passwordless policy is missing, the playbook stops and identifies the
installer or console action required. It does not attempt to create its own
authority.

Complete provisioning is the initial operating-system lifecycle transition and
a rerunnable baseline reconciliation. A successful run already performs the
initial full package update, any required reboot, and complete verification; it
is not followed immediately by the maintenance playbook. If provisioning stops
before completion, the operator corrects the reported cause and reruns complete
provisioning rather than using maintenance to finish a partially configured
baseline. The operator also reruns provisioning deliberately when baseline
policy, authoritative inputs, or suspected drift must be reconciled.

## Administrative access

### Account and keys

The only repository-managed administrative login is `ansible`. The account has
a home directory, an interactive shell needed for Ansible operation, and a
locked password. SSH is its normal access path.

Authorized public keys are an explicit, non-empty SOPS-encrypted or
operator-supplied inventory list. Key values do not appear in public inventory,
fixtures, logs, or documentation. Each controller has a separate key:

- the operator workstation private key remains on the workstation; and
- Semaphore uses a separate controller key under
  [Specification 009](009-semaphore-infrastructure-automation.md).

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
SELinux integration through the explicit `sshd_manage_selinux: false` dependency
input.

The effective policy:

- permits only the `ansible` account;
- permits public-key authentication and retains SFTP required by Ansible;
- disables password, keyboard-interactive, empty-password, and direct root
  authentication;
- disables client-hostname lookup with `UseDNS no`, so the connection-specific
  `host` and `addr` inputs both use the authenticated peer address;
- disables agent forwarding, TCP forwarding, X11 forwarding, and tunnels;
- explicitly renders and verifies TCP port 22, rejects a management connection
  on any other port before access mutation, and does not bind to a
  Tailscale-specific address;
- leaves algorithm selection to Debian's distribution policy;
  and
- uses platform service management without replacing vendor unit files.

The role renders and syntax-checks a candidate before reload. Repository
verification then checks the complete effective configuration with
`sshd -T -C` for the actual administrative connection user and endpoints,
independently confirms `usedns no`, keeps the current connection open through
activation, establishes a new connection, and confirms the authorized
administrative path. An invalid candidate never triggers a reload.

## Repository and package policy

Only enabled, signature-verified official Debian repositories are part of the
baseline. The implementation preserves APT signature verification and does not
import third-party signing keys. Later roles must own and justify any additional
repository.

Debian sources select the package-owned Debian archive keyring explicitly.
Before trust validation, provisioning adds that restriction to official Debian
repositories in legacy one-line source files that omit `Signed-By`. It
preserves their selected suites and components. The trust preflight then
rejects an unpinned source, a third-party source, or an authentication bypass.
This supports both Debian source formats without relying on APT's broader
global trusted-key store.

The baseline package set contains only packages required for:

- the Python and sudo management path;
- OpenSSH;
- firewalld;
- the platform-native time synchronization client;
- AppArmor;
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

Debian uses `unattended-upgrades` with Debian Security origins only. Its native
timer runs daily at an inventory-configurable maintenance time.
Timer configuration is explicit and bounded so vendor random delay cannot move
work outside the intended window.

Automatic reboot is enabled when `/var/run/reboot-required` exists, including
when a user remains logged in. A host-specific schedule can stagger hosts
without adding a reboot coordinator.

This policy also applies to the NUC that will run Semaphore. A native
security-update reboot is acceptable because Semaphore did not initiate it.

### Full updates

Provisioning performs one full system update as part of its explicitly
authorized transaction. A separate maintenance playbook provides the same full
update, reboot, reconnection, and verification path for routine operation.

The maintenance playbook:

- remains directly runnable through `mise run playbook` from an operator
  workstation;
- rejects SSH and become passwords and proves the `ansible` login and
  passwordless `sudo -n` path inside each one-host batch immediately before
  privileged fact gathering;
- rejects Ansible task-selection controls for mutating OS actions so the
  invocation cannot skip connection, trust, or package-manager preflight;
- verifies distribution repository trust and package-manager consistency
  without mutation before it starts package work;
- validates the target, action, inventory, and package-manager state before
  mutation;
- waits for bounded native package-manager activity and fails clearly when the
  lock does not clear;
- processes one host at a time;
- uses the distribution's supported full-upgrade operation;
- reboots when the operating system reports that a reboot is required, without
  a suppression input; and
- reconnects and runs effective-state verification before advancing.

Maintenance is the periodic full-update path after successful provisioning. It
does not reconcile baseline configuration and is not a completion stage for a
failed provisioning run. Native security-only updates continue independently
between scheduled maintenance runs.

Recurring Semaphore maintenance schedules remain deferred.
[Specification 009](009-semaphore-infrastructure-automation.md) provides a
manually triggered verification job and excludes automatic infrastructure
maintenance schedules. A future scheduling design must also address full updates
to the Semaphore host without making its recovery depend on Semaphore. This
specification adds no recurring full-update systemd timer or cron entry.

## Firewall policy

firewalld is installed and enabled on Debian. It uses nftables through the
distribution packages and owns the baseline host firewall.

The baseline:

- denies unsolicited inbound traffic by default;
- permits outbound traffic and established return traffic;
- denies packet forwarding by default;
- permits SSH on TCP port 22 only from a non-empty list of explicit private
  management sources;
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
permanent configuration are read back independently. Exact reconciliation also
removes ICMP blocks and ICMP-block inversion in both states.

fail2ban is not installed. Under a private-source, key-only SSH policy it adds
little protection while introducing another privileged daemon, dynamic ban
state, and possible lockout. A later public exposure design must reassess both
source restrictions and rate-limiting controls.

## Mandatory access control

Debian requires AppArmor enabled with distribution-supplied profiles loaded in
enforce mode. The baseline does not create speculative application profiles.
Later application roles own any profiles required by their resources and must
not disable platform enforcement to resolve a denial.

Container tests assert packages, configuration, and task decisions but do not
claim that an unprivileged container proves host-kernel enforcement.

## Time synchronization and local logging

Time synchronization uses Debian's default `systemd-timesyncd` client. The
baseline ensures that the selected package and service are present and enabled,
but it does not
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
A separate notification design may use Semaphore's supported task notifications
or an external availability monitor.

## Verification and failure signals

The repository provides a reusable, read-only Ansible verification task set. It
runs after complete provisioning and every Ansible-controlled reboot. A
standalone `os verify` action performs connection and privilege preflight,
gathers fresh facts, and invokes the same task set without package updates,
repairs, service restarts, or reboots. The manual Semaphore verification job
uses the same task set under Specification 009.

The verifier owns its expected-policy interface. Provisioning and maintenance
pass the required authorized-key and management-source inputs explicitly.
Optional firewall-service and journal-size expectations use low-precedence
verifier defaults that follow the security-baseline policy defaults and retain
inventory overrides. The verifier does not depend on the security-baseline role
having run earlier in the play.

Standalone verification loads the security and maintenance role defaults into
private namespaces without invoking either role's tasks. It resolves direct
verifier overrides first, then producer inventory overrides, then producer
defaults, and passes the effective values explicitly to the verifier. It stops
at the first failed assertion and does not promise an exhaustive drift report.

Verification checks:

- the expected operating-system release;
- the `ansible` account, authoritative public keys, locked password, and
  non-interactive sudo path;
- connection-specific effective `sshd -T -C` authentication, port, account,
  and forwarding policy;
- firewalld service enablement, default policy, private port-22 SSH allowance,
  and matching runtime and permanent state, including ICMP block surfaces;
- AppArmor effective enforcement and next-boot enablement;
- platform-native time-client health and effective clock synchronization;
- persistent journal availability and auditd active and next-boot service
  health;
- native security-update configuration and enabled timer;
- package-manager consistency;
- reboot-required state; and
- unexpected failed systemd units.

Checks are observational and never repair a failed host. Existing sources keep
failure evidence durable:

- systemd retains failed unit state;
- journald retains service, update, and boot detail;
- auditd retains structured security records;
- APT retains update results and history; and
- Semaphore retains the result of tasks it starts.

A host that never returns cannot report its own failure. External availability
or dead-man monitoring remains a follow-on requirement and is not replaced by
Semaphore running on that same host.

## Dependency decisions

| Dependency | Current repository pin | Decision | Reason |
| --- | --- | --- | --- |
| `robertdebock.bootstrap` | absent | keep repository-owned bootstrap | The repository needs a narrow Python bootstrap with an explicit pre-existing sudo contract. |
| `geerlingguy.security` | absent | keep repository-owned security policy | The baseline separates SSH, sudo, firewall, and automatic-update mechanisms under one explicit host policy. |
| `willshersystems.sshd` | `v0.34.0` | retain | It supports Debian 13, validates configuration before reload, and handles platform OpenSSH service behavior without owning repository policy. |
| `ansible.posix` | `2.2.2` | retain | It supplies focused maintained modules for POSIX platform mechanisms such as firewalld and supports the pinned controller. |
| `devsec.hardening` | absent | do not add | Its OS and SSH roles apply a much broader policy surface than this baseline has evaluated. |

Repository-owned replacement behavior has executable coverage in the complete
composition. Dependency removal requires that coverage to remain intact.

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
post-update verification for direct workstation execution. Recurring Semaphore
maintenance remains deferred.
`playbooks/os/verify.yml` exposes the same effective-state checks independently
without invoking either mutating lifecycle.
`mise run playbook` remains the only repository playbook execution interface.

## Testing and evidence

The baseline validates complete Debian composition using the rootless,
unprivileged Podman worker model defined by [Specification 002](002-multi-os-molecule-validation.md).

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
- AppArmor kernel enforcement;
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
6. Compose and validate complete Debian provisioning.
7. Remove `robertdebock.bootstrap` and `geerlingguy.security` only after the
   complete composition proves their selected replacement behavior.
8. Update operator documentation and run all change-directed validation.

This order keeps the repository executable during migration and prevents a
dependency removal from creating an untested access or update gap.

## Acceptance criteria

The current baseline contract is satisfied when:

1. complete provisioning rejects every platform except Debian 13 before
   mutation, including from the raw bootstrap path before Python is available;
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
6. AppArmor is configured enforcing, with container evidence limited to what
   the container can prove;
7. `systemd-timesyncd` is enabled and effective clock synchronization is
   verifiable;
8. persistent journald and vendor-default auditd retain local update, boot,
   service, and security failure evidence;
9. Debian installs security updates daily through native tools and performs
   required reboots in an explicit maintenance window;
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
14. complete Debian Molecule composition passes converge,
    deterministic-task idempotence, and independent verification;
15. no test contacts inventory hosts, reads protected inventory values, performs a real
    reboot, or overstates container evidence;
16. repository-required change-directed validation passes before publication;
17. no obsolete Red Hat-family role, scenario, package, template, helper, or
    task-result branch remains, and no permanent forbidden-reference test is
    added;
18. the four Debian 13 Molecule rows defined by Specification 002 cover the
    maintenance default, complete baseline, reverse proxy, and Semaphore
    scenarios.

### Issue #40 acceptance mapping

| Requirement | Current contract |
| --- | --- |
| Support one managed-host OS | Debian 13 is the sole complete-provisioning and maintenance platform. |
| Reject unsupported hosts safely | Raw bootstrap and fact-based preflight reject non-Debian or non-version-13 targets before package or configuration mutation. |
| Remove obsolete implementation | Red Hat-family role tasks, templates, variables, helper branches, and result references have no retained contract. |
| Preserve Debian behavior | APT trust, unattended security updates, conditional reboot, AppArmor, firewalld, SSH, auditd, time synchronization, Podman identities, Caddy safeguards, and Semaphore credential permissions remain required. |
| Narrow application roles | Podman, TLS, reverse proxy, and Semaphore preflights require Debian 13; Caddy uses Debian packages and Semaphore has no platform-specific labeling path. |
| Retain required dependencies | `ansible.posix` remains for Debian firewalld and authorized keys; `community.general` remains for host timezone configuration; its explicit inventory-filtering dependency and all SSH, Podman, SOPS, TLS, and application dependencies remain. |
| Preserve useful generic behavior | Native ARM64 and AMD64 selection and generic lifecycle, cleanup, timing, and impact behavior remain supported. |
| Use the complete current test matrix | Specification 002 defines exactly four Debian rows: maintenance default, complete baseline, reverse proxy default, and Semaphore default. |
| Keep documentation stable | Existing specification identifiers and paths remain; README files, guides, and subsystem specifications state the Debian 13 contract and link to this platform contract. |
| Test current behavior | Molecule fixtures and Ansible/CI contracts cover Debian 13 and the exact four-row registry; tests assert current invariants without a permanent forbidden-reference scan. |
| Update VM examples | Remove Rocky and CentOS entries from `infra/manifest.csv` and replace Rocky examples in `infra/orbs.sh` with Debian 13. Other generic VM examples do not declare managed-host support. |
| Keep platform scope narrow | No Raspberry Pi behavior is added; generic native ARM64 and AMD64 selection remains. |
