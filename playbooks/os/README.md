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
state. Its `os_reboot_enabled` default is `false`, so an operating-system or
MAC transition that needs a reboot is reported without an Ansible-controlled
reboot.

When the maintenance window and operator authorization allow the playbook to
reboot, pass the explicit input:

```bash
mise run playbook -- os provision <inventory> --limit <host> -e os_reboot_enabled=true
```

An authorized Ansible-controlled reboot is followed by connection reset,
fact gathering, and verification before the batch advances. A provisioning
reboot input does not change the independent native security-updater policy.

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

The default `os_reboot_enabled` value is `false`. If the full update reports a
reboot requirement, the playbook explains the required input and leaves the
host running. Re-run only after confirming the window and authorization:

```bash
mise run playbook -- os maintain <inventory> --limit <host> -e os_reboot_enabled=true
```

This input authorizes an Ansible-controlled reboot after the explicit full
update. It does not authorize a native security updater or make a failed host
healthy. The playbook reconnects and verifies before it advances to the next
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
independent of `os_reboot_enabled` and do not use a repository reboot
coordinator.

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
composition. The Molecule matrix has five platform/scenario executions: three
for the inherited default scenario and two for the complete baseline scenario.
The top-level workflow jobs are `classify`, `fast`, `ansible`, `molecule`, and
`merge-gate`; all CI validation remains offline and secret-free.
