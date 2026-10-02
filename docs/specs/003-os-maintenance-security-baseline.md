# Specification 003: OS maintenance and security baseline

Issues: [#13](https://github.com/supermorphic/homelab-playbook/issues/13)
and [#40](https://github.com/supermorphic/homelab-playbook/issues/40).

## Purpose and ownership

Provide a reusable Debian 13 baseline for off-cluster hosts: minimum management
capability, administrative access, package maintenance, host security, native
updates, local failure evidence, and read-only effective-state verification.
Unsupported distributions or releases fail before mutation. Native ARM64 and
AMD64 are supported without claiming Raspberry Pi-specific behavior.

Repository roles own policy; focused dependencies implement mechanisms without
making their defaults the contract. [OS playbooks](../../playbooks/os/) compose
bootstrap, [host identity](../../roles/host_identity/),
[maintenance](../../roles/system_maintenance/),
[security policy](../../roles/security_baseline/), and
[independent verification](../../roles/os_baseline_verify/).
[Specification 004](004-managed-host-onboarding.md) owns manual access and
onboarding. Applications own their packages, ports, users, credentials, health,
and additional repositories. This baseline adds no public SSH, Tailscale access,
broad compliance profile, remote logging, notification transport, or health daemon.

## Provisioning and maintenance lifecycle

A target already supplies a key-only `ansible` account, locked password, and
passwordless `sudo -n`. Raw bootstrap connects without become or fact gathering,
reads the platform, proves the expected login and sudo authority, and checks
package-manager prerequisites before installing only missing Python capability.
It resets the connection, gathers facts, and repeats platform/privilege checks
with normal modules. Failure identifies the required installer or console action;
there is no root-login, password, or broader-credential fallback.

Complete provisioning validates protected inputs, reconciles hostname/timezone,
proves repository trust and establishes time synchronization before the full
update, applies access/security policy, configures native security updates,
reboots when required, reconnects, and verifies the resulting baseline. It
processes one host at a time and validates access-affecting candidates before
activation. The gateway rejects password and task-selection controls that could
skip these safety checks. Provisioning and maintenance have no reboot-suppression
input.

Provisioning is rerunnable reconciliation. Successful provisioning already
includes a full update and verification. After an incomplete run, correct the
reported cause and rerun provisioning; maintenance cannot finish a partially
configured baseline. Later maintenance proves current connection, privilege,
repository trust, and package-manager consistency immediately before a full
update, waits only boundedly for native package activity, reboots if required,
and reconnects and verifies before advancing. It does not reconcile host identity
or security configuration. Detected drift requires authorized provisioning.

## Administrative access

`ansible` is the sole repository-managed administrative login, with the shell
and home needed by Ansible, a locked password, and an authoritative non-empty
key set. Every operator/controller has its own private key; keys are never
shared. Protected inventory owns the complete desired public-key and management-
source sets. Rotation adds the replacement, proves a new login and sudo path,
then removes the old key in a later reconciliation. Candidate authorized-key
content is privately validated before exclusive replacement.

Passwordless sudo grants all commands because Ansible transfers changing module
payloads into temporary paths; a command allowlist would be brittle. A dedicated
root-owned drop-in is validated with `visudo` before activation and `sudo -n`
is proved afterward. The human operator uses their own key through this same
account rather than a second shared administrative credential.

OpenSSH accepts public keys for `ansible` and retains Ansible's SFTP path. It
rejects passwords, keyboard-interactive and empty-password authentication, and
direct root login; disables agent/TCP/X11 forwarding and tunnels; uses port 22;
and leaves cryptographic algorithms to Debian policy. It does not depend on
Tailscale addressing. `UseDNS no` keeps connection-specific host/address checks
bound to the authenticated peer address.

The focused SSH dependency checks a candidate before reload and does not own
firewall policy. Verification reads effective `sshd -T -C` policy for the actual
connection. The active session remains available through access changes; a new
connection must prove the intended path. Invalid candidates never reload.

## Repository, package, and update policy

Only signature-verified official Debian repositories belong to the baseline.
Preserve selected suites/components while binding both supported APT source
formats to the package-owned Debian archive keyring. Legacy one-line sources
missing `Signed-By` receive that restriction before trust validation. Unpinned
sources, third-party repositories, and authentication bypasses fail preflight.
Later roles must justify and own additional trust.

Packages are limited to the management, firewall, mandatory access control,
time, logging, security-update, and platform-support mechanisms. Convenience,
development, virtualization, and application packages are outside this role.
An already installed package is not removed merely because the baseline no
longer owns it. Explicit provisioning/maintenance may clean up packages after
successful updates; daily security jobs do not perform broad removal, and vendor
kernel-retention policy preserves rollback options.

Native `unattended-upgrades` applies Debian Security updates daily in an explicit
bounded maintenance window. Hosts may stagger schedules without a reboot
coordinator. A reboot-required marker authorizes the configured native reboot,
even with logged-in users. This policy also applies to an automation-controller
host independently of controller jobs.

Full updates remain an explicit Ansible operation. No host-local recurring
full-update timer or cron job exists, and recurring Semaphore maintenance is
deferred by [specification 009](009-semaphore-infrastructure-automation.md).
Any future schedule must address updates to its own controller host without
making recovery depend on that controller.

## Host security and durable failure signals

firewalld owns the nftables-backed baseline: deny unsolicited inbound traffic
and forwarding, permit outbound and established return traffic, and allow SSH
only from a non-empty explicit private management-source set. IPv4 and IPv6
receive equivalent policy when enabled. Applications receive no implicit ports
or forwarding; later service roles own explicit extensions.

Before firewall activation or interface movement, prove that the active SSH
peer belongs to the desired management sources. Establish the new allowance
before removing the old one. Reject unsupported existing bindings, direct
openings, or policy objects for operator reconciliation. Independently read back
runtime and permanent state, including ICMP blocks and inversion, before claiming
exact reconciliation. Do not add fail2ban under private-source, key-only SSH;
public exposure would require reevaluating that decision.

AppArmor must be enabled with vendor profiles enforcing and next-boot enablement.
Application roles own extra profiles and must not disable enforcement to avoid a
denial. `systemd-timesyncd` must be healthy with effective clock synchronization;
preserve distribution/DHCP time sources without making an internal service a
bootstrap dependency or reimplementing source-provenance parsing.

Persistent journald retains bounded update, service, and boot records. Auditd
uses vendor-default rules; speculative broad syscall/watch profiles are excluded.
Systemd failed-unit state, journald, auditd, and APT history retain local failure
evidence. A host that never returns cannot report its own failure: independent
availability or dead-man monitoring remains a separate requirement.

## Verification and evidence limits

Provisioning and maintenance use the same read-only baseline verifier after
completion and every Ansible-controlled reboot. Standalone verification proves
connection/privilege, gathers fresh facts, and resolves policy defaults without
running mutating roles; inventory overrides remain effective. Producer inputs
are passed explicitly so verification does not rely on producer execution or
facts from an earlier play. It stops at the first failed assertion and promises
no exhaustive drift report.

Verification observes platform and identity; account, key, sudo, and effective
SSH policy; matching firewall states; AppArmor; clock synchronization; persistent
logging/audit service health; native updater/timer state; package-manager health;
reboot-required state; and unexpected failed units. It never updates, repairs,
restarts services, or reboots.

[Specification 002](002-multi-os-molecule-validation.md) owns container evidence.
Complete-baseline tests cover convergence, deterministic idempotence, independent
verification, candidate rejection, access safeguards, and minimum package policy.
Disposable key material remains temporary and private. Container controls may
suppress unavailable firewall, clock, audit-kernel, MAC, and reboot execution
while preserving configuration/decision coverage and production defaults.
Physical reboot/reconnection, boot persistence, host-kernel enforcement, real
management/NTP reachability, console recovery, wall-clock timers, scheduling,
and notifications require separate operator-authorized live evidence.
