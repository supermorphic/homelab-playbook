# Specification 004: Managed host onboarding

Issue: [#2](https://github.com/supermorphic/homelab-playbook/issues/2)

## Purpose and boundaries

Connect an installer-prepared Debian host to the reusable
[OS baseline](003-os-maintenance-security-baseline.md), using existing OS
playbooks rather than a machine-specific bootstrap path. Active managed hosts
belong to `os_managed`; service membership is added only when an owning design
activates that function. [Inventory source](../../inventory/production/hosts.yml)
owns current topology. Inventory removal does not authorize changes to former
hosts; retained Pi-hole source has no active production target.

A focused host-identity role manages static hostname and deployment-local IANA
timezone independently of hardware or applications. Inventory alias and desired
hostname are explicit separate inputs, so changing an alias does not silently
rename a machine. Maintenance verifies identity without reconciling it.
Onboarding installs no Git, container runtime, application, or additional
baseline package. Host boot, SSH access, updates, and baseline verification
must remain independent of Kubernetes, Plex, managed DNS, and Tailscale.

## Manual authority and independent recovery

Ansible must not create the authority that lets Ansible become root. The
installer, console, or an already authorized administrator establishes:

- a bootable supported OS with usable official repositories;
- an `ansible` account with a home, interactive shell, approved key, locked
  password, and working non-interactive passwordless sudo;
- SSH reachability from an authorized controller; and
- trusted console/rescue access independent of SSH and managed services.

Retain trusted Debian installation/rescue media and prove access to the machine's
boot menu. Lost SSH access is repaired through that path; it never justifies
root SSH, password authentication, or unlocking the automation-account password.
The external controller needs its checkout, SSH private key, and
[recoverable SOPS identity](005-sops-age-secrets.md) without services on the host.

Use a dedicated Ed25519 workstation key and named `~/.ssh/config` entry selecting
`User ansible`, the private key, and `IdentitiesOnly yes`. Resolve the host through
operator-owned private configuration without publishing its address. Install the
public key through the authorized initial access path; the repository owns no
private key or passphrase-caching mechanism. Additional operators/controllers
receive distinct keys, never the same private key.

Before locking the account password, establish and validate the sudo drop-in
through the installer/console authority. Its policy is
`ansible ALL=(ALL:ALL) NOPASSWD: ALL`; validate the complete configuration with
`visudo -c`. Lock the password and inspect `passwd --status ansible`. From a new
named SSH connection, `id -un` must report `ansible` and `sudo -n id -u` must
report `0`. Keep the independent recovery path available while proving access.

## Inventory and protected-input preparation

Add active hosts to the selected environment's `os_managed` group. Public
inventory supplies the connection user and explicit desired hostname; protect
the exact timezone, complete authorized public-key set, and complete private
management-source set. Partial desired sets can remove valid access. Source
roles own input schemas; do not duplicate their field catalogs here.

Before using a new environment or protected path, land a reviewed change to
public inventory contracts, [.sops.yaml](../../.sops.yaml), and the exact
[static-validation scope](../../scripts/secrets/validate.py). Creating an
unregistered protected path is not an operator-only setup shortcut. The current
baseline protected document is
`inventory/production/group_vars/os_managed/secrets.sops.yml`.

The operator creates or edits only the selected registered document through
`mise run secrets:sops -- <path>`. [Specification 005](005-sops-age-secrets.md)
owns identity setup and recipient changes. Normal playbooks need no additional
secret flag. Agents and CI treat protected values as opaque; static checks prove
structure/public metadata, never authenticated integrity. Controllers use their
own identity retrieval command rather than the workstation identity.

## Minimum onboarding sequence

After controller bootstrap and identity setup, obtain authorization for each
exact live action, inventory, host limit, and extra arguments. These examples
use the existing `nuc4` alias; substitute the approved target for another host.

1. Prove manual key-only access and `sudo -n` as above. Optionally collect the
   allowlisted non-privileged snapshot with
   `mise run playbook -- os inspect production --limit nuc4`. Inspection is not
   a baseline-health assertion.
2. Reconfirm the exact mutation and run
   `mise run playbook -- os provision production --limit nuc4`. Provisioning
   processes one host at a time, updates, reconciles identity/security,
   configures native updates, reboots if needed, reconnects, and verifies.
3. After success, observe `hostnamectl --static`,
   `timedatectl show --property=Timezone --value`, and `sudo -n id -u` through
   the named SSH connection. Results must match the approved hostname/timezone
   and root UID. These observations are live evidence, never PR CI evidence.

Provisioning already includes full update and verification. Later
`mise run playbook -- os verify production --limit nuc4` observes effective
baseline drift without repair, while `os maintain` performs the periodic full
package update without reconciling identity/security policy. Native daily
security updates remain independent. Exact options and gateway rejection rules
belong to [the playbook gateway](../../scripts/playbook.sh).

## Failure, reconciliation, and evidence

Invalid identity values fail before identity mutation. Missing identity access
fails before protected inputs can be used. Failed access, trust, package,
identity, security, reboot, or verification steps stop the one-host batch. Correct
the cause and rerun authorized provisioning after an incomplete run or baseline
drift; maintenance is not an onboarding completion stage.

Rotate SSH access using add-verify-remove: retain the current key, add its
replacement, prove a new connection and sudo with the replacement, then remove
the old key in a later authorized reconciliation. Never remove a working access
path on the strength of an untested replacement.

Local failure evidence remains in systemd, persistent journald, auditd, and APT
history. The verifier repairs nothing and stops at its first failed assertion.
Offline tests use synthetic identity and temporary keys, prove current inventory
and composition contracts, and detect host-identity drift. They neither contact
production nor prove physical reboot, boot persistence, recovery-media access,
real firewall/NTP reachability, timers, or controller scheduling. Those require
separate operator evidence under [specification 003](003-os-maintenance-security-baseline.md).
