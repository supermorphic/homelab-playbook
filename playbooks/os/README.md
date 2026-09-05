# Operating-system baseline

This guide describes the operator interface for inspecting, provisioning, and
maintaining off-cluster operating systems. The playbooks are Ansible
operations; they do not create a new management authority or a host-local
full-update scheduler.

## Supported platforms

Debian 13 and Rocky Linux 9 are the supported complete-baseline platforms.

| Platform | Complete provisioning | Explicit full maintenance | Native security updater | Native time provider |
| --- | --- | --- | --- | --- |
| Debian 13 | Yes | Yes | `unattended-upgrades`, Debian Security origins | `systemd-timesyncd` |
| Rocky Linux 9 | Yes | Yes | `dnf-automatic`, `upgrade_type = security` | chrony (`chronyd`) |

The baseline preserves each distribution's repository and DHCP-provided time
sources. Issue #13 does not provide a time-source override. Complete-baseline
operations reject unsupported platforms before mutation.

## Required inputs

Run the pinned controller setup after checkout:

```bash
mise install
mise run bootstrap
```

Before complete provisioning, the target must already have:

- an `ansible` account with a working operator or installer public key;
- a locked account password and non-interactive, passwordless `sudo`;
- a usable official package-manager configuration; and
- SSH reachability from the authorized controller.

The bootstrap stage may install missing Python capability through that existing
`sudo` path. It never falls back to root login, a password prompt, or a broader
credential. If the account or `sudo` contract is missing, use the installer or
console path that owns initial authority and then retry the playbook.

Complete provisioning and complete-platform maintenance require non-empty
operator inputs for:

- `security_baseline_authorized_keys`, the authoritative public keys for the
  `ansible` account; and
- `security_baseline_management_sources`, the private management sources that
  may reach SSH through the host firewall.

Keep these values in the operator's approved inventory or Vault input. Do not
put private keys, public key material, live addresses, or credentials in this
guide, public inventory, fixtures, or CI output. The repository stores no
private controller key. The operator workstation owns its private key. Issue #4
will add a separate Semaphore private key and its public counterpart; do
not share a private key between those controllers.

Rotate controller keys with an add-verify-remove sequence:

1. Add the replacement public key while the current key remains present.
2. Run a read-only inspection or establish a new connection with the
   replacement key and verify the `ansible` path.
3. Remove the old key in a later authorized reconciliation after the new key
   works.

The authorized-key list is authoritative and non-empty. A failed scoped key
or privilege check does not trigger a fallback credential.

## Managed-host onboarding

Use this procedure to establish the initial manual authority and onboard
`nuc4`. Do not execute a live command unless the operator has authorized that
exact action, inventory, host limit, and arguments.

### 1. Establish manual authority

Before Ansible runs, use the Debian installer, the physical console, or an
already authorized administrator to ensure that:

- Debian 13 is installed and boots normally;
- official Debian package repositories are usable;
- the `ansible` account has a home directory and an interactive shell;
- the operator public key is installed for the `ansible` account;
- the account can run `sudo -n` successfully;
- the `ansible` account password is locked;
- Debian rescue or installation media remains available; and
- access to the NUC boot menu is confirmed.

Retain the media and boot-menu path so that an operator can repair the system
through trusted console or rescue access if SSH is lost. Do not retain a root
SSH login or unlock the `ansible` password as a fallback.

### 2. Configure workstation SSH

Create a dedicated Ed25519 key when the operator needs a new key:

```bash
ssh-keygen \
  -t ed25519 \
  -f ~/.ssh/id_ed25519_homelab_ansible \
  -C "homelab ansible operator"
```

Protect the private key according to workstation policy. The repository does
not store the private key or define passphrase caching. Configure the named
connection in `~/.ssh/config`:

```sshconfig
Host nuc4
    HostName nuc4
    User ansible
    IdentityFile ~/.ssh/id_ed25519_homelab_ansible
    IdentitiesOnly yes
```

`HostName` can instead be the operator's resolvable private hostname or
address. Never commit a live address to this repository. Use the named SSH
configuration for each command; do not add command-line authentication
overrides.

Install the public key through the installer-created or otherwise authorized
initial access path:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519_homelab_ansible.pub nuc4
```

### 3. Configure and verify initial access

Open the named connection, create the passwordless-sudo drop-in, validate the
complete sudoers configuration, and prove non-interactive sudo:

```bash
ssh nuc4
sudo visudo -f /etc/sudoers.d/ansible
sudo visudo -c
sudo -n true
```

The drop-in contains exactly:

```sudoers
ansible ALL=(ALL:ALL) NOPASSWD: ALL
```

Lock the account password, inspect its status, and leave the session:

```bash
sudo passwd --lock ansible
sudo passwd --status ansible
exit
```

The status output must identify the `ansible` password as locked. Then confirm
the login identity and non-interactive root path:

```bash
ssh nuc4 'id -un'
ssh nuc4 'sudo -n id -u'
```

The first command must print `ansible`. The second must print `0`.

### 4. Create the protected inventory input

The complete desired SSH public-key set is required because the existing security baseline authoritatively manages the `ansible` account's `authorized_keys`.
The complete private management-source set is also required because the
baseline authoritatively limits inbound SSH through the firewall. Include all
approved keys and sources; partial sets can remove valid access during
provisioning.

Create the encrypted sibling of the public `os_managed` variables. Enter the
Vault password interactively:

```bash
mise exec -- ansible-vault create inventory/production/group_vars/os_managed/vault.yml
```

Enter only operator-approved values in place of these marked placeholders:

```yaml
---
host_identity_timezone: "<deployment-local IANA timezone>"
security_baseline_authorized_keys:
  - "<complete desired SSH public key>"
security_baseline_management_sources:
  - "<private management CIDR>"
```

Treat this Vault as opaque. Do not decrypt, print, parse, or inspect its
protected values during repository development or validation. Do not use a
Vault password file or automated password retrieval for these local commands.

### 5. Inspect and provision

After the manual access checks pass, an authorized operator can collect a
read-only snapshot:

```bash
mise run playbook -- os inspect production --limit nuc4 --ask-vault-pass
```

Inspection does not establish that the baseline is healthy. After review and
fresh explicit authorization for the exact live mutation, provision the host:

```bash
mise run playbook -- os provision production --limit nuc4 --ask-vault-pass
```

Provisioning performs the initial full update, reconciles host identity and the
security baseline, configures native security updates, reboots when required,
and verifies the resulting state.

### 6. Maintain, rerun, and recover

Use maintenance only for later periodic full package updates:

```bash
mise run playbook -- os maintain production --limit nuc4 --ask-vault-pass
```

Maintenance verifies hostname and timezone identity, but it does not reconcile
identity drift or reapply baseline configuration. If maintenance reports
identity drift, rerun `os provision` after authorization. If onboarding stops
at any point, correct the reported cause and rerun `os provision`; do not use
`os maintain` to finish an incomplete onboarding. When the normal SSH path is
not usable, repair access through the retained console or rescue path before
rerunning provisioning.

### 7. Confirm live state

After an authorized successful provisioning run, observe the effective state:

```bash
ssh nuc4 'hostnamectl --static'
ssh nuc4 'timedatectl show --property=Timezone --value'
ssh nuc4 'sudo -n id -u'
```

The commands must report `nuc4`, the operator-approved deployment-local IANA
timezone, and `0`, respectively. These are live operator observations. They
complement the playbook verifier but are not pull-request CI evidence.

## Operating lifecycle

Use the OS operations according to the host's lifecycle:

| When | Operation | Result |
| --- | --- | --- |
| Before a change, when an OS snapshot is useful | `os inspect` | Reports allowlisted OS facts without privilege escalation or mutation. |
| After a fresh OS installation satisfies the access prerequisites | `os provision` | Performs a full update, reconciles and verifies the complete baseline, configures native security updates, and reboots when required. |
| Every day after provisioning | Native Debian or Rocky security updater | Applies security-only updates in its configured window and performs a required native security-update reboot. |
| Periodically after provisioning | `os maintain` | Performs a later full package update, reboots when required, and verifies the complete baseline without reapplying baseline configuration. |
| After incomplete provisioning or when baseline policy, authoritative inputs, or suspected drift must be reconciled | `os provision` | Repeats the complete provisioning reconciliation and verification path. |

A successful `os provision` run already includes the initial full package
update and complete verification. Do not immediately follow it with `os
maintain`. If provisioning stops before completion, correct the reported cause
and rerun `os provision`; do not use `os maintain` to finish a partially
configured baseline.

Provisioning is rerunnable, but it is not the routine full-update scheduler.
Rerun it deliberately when baseline state must be reconciled. Use `os maintain`
for subsequent periodic full package updates, and allow the native updater to
handle daily security updates between those runs.

## Operator actions

Use the canonical repository gateway. Replace `<inventory>` with `production`,
`staging`, or `frozen/k3s`, and replace `<host>` with an inventory host
selector. The operator must be authorized for the exact playbook, action,
inventory, host selector, and extra variables before running a mutating action.

### Inspect

```bash
mise run playbook -- os inspect <inventory> --limit <host>
```

`os inspect` is read-only and does not escalate privilege. It reports an
allowlisted snapshot: architecture, distribution and release, kernel,
Ansible OS family and package manager, Python and service-manager versions,
and virtualization type and role. It does not claim that the host is
reachable later or that the baseline is healthy.

### Provision

```bash
mise run playbook -- os provision <inventory> --limit <host>
```

Provisioning is a mutating, complete-baseline operation for Debian 13 or Rocky
Linux 9. It runs one host at a time, performs the full update and baseline
reconciliation, configures native security updates, and verifies effective
state. When an operating-system or MAC transition requires a reboot, the
playbook reboots without a suppression input. It then resets the connection,
gathers facts, and verifies the host before the batch advances. This behavior
does not change the independent native security-updater policy.

### Maintain

```bash
mise run playbook -- os maintain <inventory> --limit <host>
```

Maintenance is a mutating full operating-system update for Debian 13 and Rocky
Linux 9. Before privileged fact gathering, it rejects password inputs and
proves the key-only `ansible` login and passwordless sudo path. It then checks
repository trust and package-manager consistency before package work, waits for
bounded package-manager activity, performs the distribution-supported full
update, verifies effective state, and processes a multi-host selection
sequentially.

For mutating OS actions, the repository gateway rejects Ansible password
prompts and password files. It also rejects `--start-at-task`, tag selection,
and `--step`, because those controls could skip a required safety check.

If the full update reports a reboot requirement, the playbook reboots without
a suppression input. It reconnects and verifies before it advances to the next
host. Never use a live production or staging command as CI evidence.

## Update and reboot boundaries

Native security updates and explicit full maintenance are separate operations.
The native updater runs daily in an inventory-configurable, bounded window;
it is not a full system update and does not perform broad package cleanup.
Unless inventory changes the defaults, the timer is `*-*-* 04:00:00` with no
random delay.

Debian's `unattended-upgrades` is restricted to Debian Security origins. When
native reboot is enabled, it reboots at `04:30` when
`/var/run/reboot-required` exists, including when a user remains logged in.
Rocky's `dnf-automatic` applies security updates and uses the native
`when-needed` reboot behavior. These native security-update reboots are
independent of the explicit playbook reboot path and do not use a repository
reboot coordinator.

Routine full updates have no host-local recurring systemd timer or cron job.
The maintenance playbook is scheduler-neutral and remains directly runnable
from an operator workstation. Issue #4 will define the Semaphore task and
schedule that invokes this playbook, including credentials and any notification
integration. This issue does not claim that notifications are configured or
delivered.

The NUC that will host Semaphore is subject to the same native security-update
policy. A native security reboot may therefore restart it even though
Semaphore did not initiate that reboot. Issue #4 must define how the Semaphore
host receives a full update without allowing Semaphore to reboot its own host.

## Health and evidence

The complete baseline's verification task set is read-only. Provisioning runs
it after reconciliation, and maintenance runs it after each Ansible-controlled
reboot. It checks effective access, SSH, firewall, MAC, native time client,
logging, updater configuration, package-manager health, reboot-required state,
and failed systemd units. It repairs nothing.

Failure evidence remains in the existing mechanisms: systemd failed-unit state,
persistent journald, auditd, APT or DNF history, and the result of a Semaphore
task when Issue #4 schedules one. A host that does not return cannot report its
own failure. This repository does not add a host-local health daemon,
aggregate result file, dead-man monitor, remote log forwarding, or notification
transport.

Molecule is offline, secret-free, and runs rootless, unprivileged Podman
containers. It proves configuration, task decisions, and the verification
contract that a container can observe. It does not prove a physical reboot,
boot persistence, host-kernel SELinux or AppArmor enforcement, real firewall
reachability, host clock synchronization or external NTP reachability,
audit-kernel event collection, physical recovery access, timer execution at a
wall-clock time, or Semaphore scheduling and notification delivery. Those
behaviors need separate, explicitly authorized live verification.

## Local and CI scenario selectors

Run either registered Molecule scenario with its exact selector:

```bash
mise run test:molecule -- system_maintenance/default
mise run test:molecule -- system_maintenance/baseline
```

`system_maintenance/baseline` covers complete Debian 13 and Rocky Linux 9
composition. Both scenarios cover Debian 13 and Rocky Linux 9, and CI registers
the four exact selector-and-platform combinations. The top-level workflow jobs
are `classify`, `fast`, `ansible`, `molecule`, and `merge-gate`; all CI
validation remains offline and secret-free.
