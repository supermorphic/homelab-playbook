# Managed host onboarding

Use this guide to add an installed Debian 13 or Rocky Linux 9 host to the
repository's `os_managed` inventory and operating-system baseline. The host
must already have a trusted initial administration path. Ansible does not
install the operating system or create the authority that lets it become root.

The commands use `nuc4` and the production inventory as the current example.
For another host, replace the SSH alias, inventory, host limit, hostname, and
inventory paths with that deployment's approved values.

Do not execute a live command unless the operator has authorized that exact
action, inventory, host limit, and arguments.

## Before you start

Prepare the controller after checkout:

```bash
mise install
mise run bootstrap
```

The target needs all of these conditions before Ansible runs:

- a supported operating system that boots normally;
- usable official package repositories;
- an `ansible` account with a home directory and interactive shell;
- an approved public key installed for the `ansible` account;
- non-interactive, passwordless `sudo` for that account;
- a locked `ansible` account password;
- SSH reachability from the authorized controller; and
- trusted console or rescue access that does not depend on SSH.

The provision playbook's bootstrap stage can install missing Python capability
through the existing passwordless `sudo` path. It never falls back to root
login, a password prompt, or a broader credential.

## 1. Establish manual authority

Use the operating-system installer, the physical console, or an already
authorized administrator to establish the prerequisites. Retain suitable
rescue or installation media and confirm that you can reach the machine's boot
menu. This recovery path lets an operator repair the system if SSH access is
lost.

For the current `nuc4` example, Debian 13 installation media and the NUC boot
menu provide that recovery path. Do not retain a root SSH login or unlock the
`ansible` password as a fallback.

## 2. Configure workstation SSH

Create a dedicated Ed25519 key when the operator needs a new key:

```bash
ssh-keygen \
  -t ed25519 \
  -f ~/.ssh/id_ed25519_homelab_ansible \
  -C "homelab ansible operator"
```

Protect the private key according to workstation policy. The repository does
not store the private key or define passphrase caching.

Create a named entry in `~/.ssh/config`. This example uses `nuc4`:

```sshconfig
Host nuc4
    HostName nuc4
    User ansible
    IdentityFile ~/.ssh/id_ed25519_homelab_ansible
    IdentitiesOnly yes
```

For another host, use its chosen alias after `Host` and its resolvable private
hostname or address after `HostName`. Never commit a live address to this
repository. Use the named SSH connection for each command; do not add
command-line authentication overrides.

Install the public key through the installer-created or otherwise authorized
initial access path. For `nuc4`:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519_homelab_ansible.pub nuc4
```

## 3. Configure and verify initial access

Open the named connection, create the passwordless-sudo drop-in, validate the
complete sudoers configuration, and prove non-interactive sudo. For `nuc4`:

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

The first command must print `ansible`. The second must print `0`. Repeat these
checks with the named SSH connection for any host being onboarded.

## 4. Add the host to inventory

Place active hosts under `os_managed` in the selected environment's
`hosts.yml`. Put public connection defaults in
`group_vars/os_managed/vars.yml`, and put the host's public desired hostname in
`host_vars/<host>/vars.yml`.

The repository registers each encrypted inventory path explicitly with its
secret-free validation boundary. Before adding the first managed host to a new
environment or using a new Vault path, land a reviewed repository change that
adds the public inventory contracts and registers the exact encrypted path with
the Vault guard and YAML lint exclusion. Do not create an unregistered Vault
path as an operator-only step.

The current production example resolves to this shape:

```yaml
---
all:
  children:
    os_managed:
      hosts:
        nuc4:
```

The public host variables contain only non-sensitive metadata:

```yaml
---
host_identity_hostname: nuc4
```

The public `os_managed` group variables select the automation account:

```yaml
---
ansible_user: ansible
```

Do not put a live address, SSH key, management CIDR, credential, or exact
deployment timezone in public inventory.

## 5. Create the protected inventory input

The complete desired SSH public-key set is required because the existing security baseline authoritatively manages the `ansible` account's `authorized_keys`.
The complete private management-source set is also required because the
baseline authoritatively limits inbound SSH through the firewall. Include all
approved keys and sources; partial sets can remove valid access during
provisioning.

Create the encrypted `os_managed` Vault only when it does not exist. For the
current production example:

```bash
mise exec -- ansible-vault create inventory/production/group_vars/os_managed/vault.yml
```

When the Vault already exists, change it with the corresponding interactive
command. For the current production example:

```bash
mise exec -- ansible-vault edit inventory/production/group_vars/os_managed/vault.yml
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

Treat the Vault as opaque during repository development and validation. Do not
decrypt, print, parse, or inspect its protected values. Do not use a Vault
password file or automated password retrieval for these local commands.

When rotating controller access, use an add-verify-remove sequence:

1. Add the replacement public key while the current key remains present.
2. Establish a new connection with the replacement key and verify the
   `ansible` login and passwordless-sudo path.
3. Remove the old key in a later authorized reconciliation.

Each controller or operator uses a distinct private key. Never share a private
key between the operator workstation and an automation controller.

## 6. Inspect and provision

Use the canonical repository gateway. Substitute the approved inventory and
host limit for a different deployment.

An authorized operator can first collect a read-only snapshot. For `nuc4`:

```bash
mise run playbook -- os inspect production --limit nuc4 --ask-vault-pass
```

Inspection reports allowlisted operating-system facts without privilege
escalation or mutation. These facts cover the platform, kernel, package
manager, Python and service-manager versions, and virtualization context. An
inspection does not establish that the baseline is healthy.

After review and fresh explicit authorization for the exact live mutation,
provision the host:

```bash
mise run playbook -- os provision production --limit nuc4 --ask-vault-pass
```

Provisioning runs one host at a time. It performs the initial full update,
reconciles host identity and the security baseline, configures native security
updates, reboots when required, reconnects, and verifies effective state before
the batch advances.

The repository gateway rejects Ansible password prompts, password files,
`--start-at-task`, tag selection, and `--step` for mutating OS operations.
These controls could bypass the required key-only access or safety checks.

## 7. Operate the managed host

Use each operation according to the host's lifecycle:

| When | Operation | Result |
| --- | --- | --- |
| Before a change, when a snapshot is useful | `os inspect` | Reports allowlisted OS facts without mutation. |
| After installation satisfies the manual prerequisites | `os provision` | Updates, reconciles, and verifies the complete baseline. |
| Every day after provisioning | Native security updater | Applies security-only updates in its configured window. |
| Periodically after provisioning | `os maintain` | Performs a full package update and verifies without reapplying baseline configuration. |
| After incomplete provisioning or suspected drift | `os provision` | Repeats complete reconciliation and verification. |

A successful `os provision` already includes the initial full package update
and verification. Do not immediately follow it with `os maintain`.

For a later periodic full update of `nuc4`:

```bash
mise run playbook -- os maintain production --limit nuc4 --ask-vault-pass
```

Maintenance verifies hostname, timezone, access, and the rest of the baseline,
but it does not reconcile identity or baseline drift. If it reports drift,
rerun `os provision` after authorization. If onboarding stops, correct the
reported cause and rerun `os provision`; do not use `os maintain` to finish an
incomplete onboarding.

Native security updates and explicit full maintenance are separate. Debian
uses `unattended-upgrades` for Debian Security origins. Rocky Linux uses
`dnf-automatic` with `upgrade_type = security`. Native security-update reboots
are independent of the explicit playbook reboot path. The repository defines
no host-local recurring full-update timer or cron job.

Unless inventory changes the defaults, native security updates run daily at
`04:00` with no random delay. Debian reboots at `04:30` when
`/var/run/reboot-required` exists, including when a user remains logged in.
Rocky Linux uses the native `when-needed` reboot behavior.

A separate automation controller can schedule later full maintenance. The
controller host remains subject to its own native security-update policy, so it
can reboot even when it did not start that reboot. A future controller design
must define how it updates its own host without allowing a task to reboot the
controller that is running it.

## 8. Confirm live state

After an authorized successful provisioning run, observe the effective state.
For `nuc4`:

```bash
ssh nuc4 'hostnamectl --static'
ssh nuc4 'timedatectl show --property=Timezone --value'
ssh nuc4 'sudo -n id -u'
```

The commands must report `nuc4`, the operator-approved deployment-local IANA
timezone, and `0`, respectively. These live observations complement the
playbook verifier but are not pull-request CI evidence.

## Recovery and evidence limits

When the normal SSH path is not usable, repair access through the retained
console or rescue path before rerunning provisioning. Do not add root SSH,
password-based SSH, or an unlocked automation-account password as a fallback.

The complete baseline verifier is read-only. It checks effective access, SSH,
firewall, mandatory access control, native time service, logging, updater
configuration, package-manager health, reboot-required state, and failed
systemd units. It repairs nothing.

Failure evidence remains in systemd failed-unit state, persistent journald,
auditd, and APT or DNF history. A future automation-controller task can add its
own result, but a host that does not return cannot report its own failure. This
repository does not add a host-local health daemon, aggregate result file,
dead-man monitor, remote log forwarding, or notification transport.

Offline Molecule validation proves configuration, task decisions, idempotence,
and container-observable verification. It does not prove a physical reboot,
boot persistence, host-kernel enforcement, real firewall reachability, host
clock synchronization, physical recovery access, wall-clock timer execution,
or automation-controller scheduling and notification delivery. Those behaviors
need separate, explicitly authorized live verification.
