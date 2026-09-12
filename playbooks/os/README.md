# Operating-system playbooks

This directory contains the Ansible entry points for inspecting, provisioning,
verifying, and maintaining off-cluster operating systems. The playbooks target
the `os_managed` inventory group. They do not install an operating system,
create initial administrative authority, or schedule recurring full updates.

Follow the [managed host onboarding guide](../../docs/guides/managed-host-onboarding.md)
for workstation SSH setup, manual host prerequisites, inventory preparation,
protected input, live commands, verification, and recovery. The
[SOPS secrets guide](../../docs/guides/sops-secrets.md) owns age identity setup,
recovery, editing, and recipient management.

## Supported platforms

Debian 13 is the supported complete-baseline platform.

| Platform | Complete provisioning | Full maintenance | Native security updater | Native time provider |
| --- | --- | --- | --- | --- |
| Debian 13 | Yes | Yes | `unattended-upgrades` | `systemd-timesyncd` |

Complete-baseline operations reject unsupported platforms before mutation.
The baseline preserves Debian's official repository configuration and existing
time sources.

## Playbook surface

- `inspect.yml` reads an allowlisted operating-system snapshot without
  privilege escalation or mutation.
- `provision.yml` bootstraps Python when necessary, performs the initial full
  update, reconciles host identity and the complete security baseline,
  configures native security updates, reboots when required, and verifies the
  resulting state.
- `maintain.yml` performs later full package updates, reboots when required,
  and verifies effective baseline state without reapplying baseline
  configuration.
- `verify.yml` performs connection and privilege preflight, gathers fresh facts,
  and checks the complete effective baseline without updating packages,
  changing configuration, restarting services, or rebooting.

Provisioning, maintenance, and standalone verification process selected hosts
one at a time. Provisioning and maintenance already include verification. Use
`verify.yml` for a later drift check; it stops at the first failed assertion and
does not produce an exhaustive drift report. Use provisioning again after an
incomplete run or when authoritative baseline inputs or reported drift require
reconciliation.

A successful provisioning run also includes a full update; do not run
maintenance immediately afterward just to repeat that update.

## Required state and inputs

Before provisioning, the host must already provide a key-only `ansible`
account with a locked password and non-interactive passwordless `sudo`. The
operator must also retain a trusted console or rescue path.

The complete baseline requires these protected inventory values:

- `host_identity_timezone`;
- `security_baseline_authorized_keys`, the complete authoritative public-key
  set for the `ansible` account; and
- `security_baseline_management_sources`, the complete private source set
  allowed to reach SSH.

The public host variable `host_identity_hostname` supplies the desired static
hostname. Production protected values use SOPS and age. Agents and CI do not
decrypt or inspect them; static validation checks only structure and public
recipient metadata.

## Safety and evidence boundaries

The canonical repository gateway rejects password-based Ansible access and
execution controls that could skip required safety tasks. Provisioning and
maintenance have no reboot-suppression input. Native daily security updates
and their reboot behavior remain independent of explicit full maintenance.
Standalone verification loads the security and maintenance policy defaults
without running either role's mutating tasks, and inventory values continue to
override those defaults.

The baseline verifier observes effective host state and repairs nothing.
Molecule validation is offline and secret-free. It cannot prove physical
reboots, boot persistence, host-kernel enforcement, real firewall or NTP
reachability, recovery-media access, or external scheduling and notifications.

## Development validation

The registered Molecule selectors are:

```bash
mise run test:molecule -- system_maintenance/default
mise run test:molecule -- system_maintenance/baseline
```

Both scenarios cover Debian 13. The baseline scenario covers
complete provisioning composition, idempotence, and independent verification.
