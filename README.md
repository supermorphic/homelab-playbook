# Homelab Playbook

Ansible automation for provisioning and maintaining off-cluster homelab hosts.

## Prerequisites

Install [Mise](https://mise.jdx.dev/), Git, and SSH configuration appropriate
for the hosts you are explicitly authorized to operate.

## Bootstrap

After checkout, install the pinned tools, then install the locked controller and
Galaxy dependencies. Repeat both commands after a tool or dependency change:

```bash
mise install
mise run bootstrap
```

## Repository layout

`playbooks/` contains host automation, `roles/` contains reusable Ansible roles,
and `inventory/` contains environment inventories. Durable design specifications
live in `docs/specs/`; transient implementation plans belong in `.tmp/plans/`.

## Running playbooks

Run playbooks through the repository interface:

```bash
mise run playbook -- <playbook> <action> <inventory> [ansible-args...]
```

The repository-root alias is an equivalent thin forwarding wrapper:

```bash
./run-playbook <playbook> <action> <inventory> [ansible-args...]
```

Execute against production or staging only with explicit operator direction.

### OS baseline operations

The [OS baseline guide](playbooks/os/README.md) documents the supported
platforms, required operator inputs, reboot guard, native update policy, and
evidence limits. The three operator actions are:

```bash
mise run playbook -- os inspect <inventory> --limit <host>
mise run playbook -- os provision <inventory> --limit <host>
mise run playbook -- os maintain <inventory> --limit <host>
```

`inspect` is read-only. `provision` supports complete Debian 13 and Rocky
Linux 9 provisioning. `maintain` performs an explicit full update on the same
two platforms. Provisioning and maintenance default to
`os_reboot_enabled=false`. A reboot requires an explicitly authorized
invocation with `-e os_reboot_enabled=true`.

Routine full updates have no host-local timer or cron job. Issue #4 will use
Semaphore as the scheduler for the maintenance playbook. Native Debian and
Rocky security updaters remain independent and may perform their own required
reboots. The NUC/Semaphore full-update and self-reboot policy is deferred to
Issue #4.

## Inventories

Select one of these inventory arguments:

- `production` contains the active off-cluster hosts.
- `staging` contains no hosts; it retains non-active Semaphore deployment and
  backup inputs for future work.
- `frozen/k3s` retains the non-active K3s inventory.

Production and staging are operator inputs. Validation parses public-only
inventory mirrors and does not connect to their hosts.
Each inventory directory stores its static host and group topology in
`hosts.yml`; public variables remain under `group_vars/`.

## Secrets

Ansible Vault encrypts secret variables. Operators own the Vault password or
password-retrieval mechanism outside the repository, such as in a password
manager. Do not commit key material or embed it in Mise configuration, helper
scripts, or pull-request CI. Do not decrypt, print, or inspect production Vault
values during development or validation.

Public group variables live in `vars.yml`; version pins in `versions.yml` are
public as well. Encrypted variables live only in sibling `vault.yml` files. The
active boundary is `inventory/production/group_vars/pihole/`. Retained
Semaphore inputs are under `inventory/staging/group_vars/semaphore/`, and
retained K3s variables are under `inventory/frozen/k3s/group_vars/`. Inventory
parsing and Ansible semantic validation use the public files and never receive
encrypted `vault.yml` input. Broad redacted Gitleaks scans inspect repository
bytes and history, including encrypted file bytes, without decryption or
plaintext output.

## Validation

Use focused validation while iterating, then run change-directed validation before
claiming completion:

```bash
mise run validate:fast
mise run validate:ansible
mise run ci:changed
```

`ci:changed` classifies committed and working-tree changes and runs the minimum
required depth. Use `mise run ci` to force all currently implemented offline
validation. Pull-request validation is offline and secret-free.

### Molecule tests

`mise run test:molecule -- system_maintenance/default` runs the repository's
rootless Podman scenario for Debian 13 and Rocky Linux 9. The same platform set
runs locally and in GitHub's native AMD64 matrix.

`mise run test:molecule -- system_maintenance/baseline` runs complete Debian
and Rocky composition. Local ARM64 skips Arch because that platform is only in
the default maintenance scenario; CI runs the two baseline platforms and the
three default platforms as five native AMD64 matrix jobs. Container results do
not prove physical reboot, host-kernel enforcement, real network reachability,
or Semaphore scheduling and notification delivery.

## GitHub main protection

Changes reach `main` through a feature branch, a current pull request, the
required `merge-gate`, and a squash merge. Repository-owned commands can inspect
or preview the live protection state:

```bash
mise run github-protection:check
mise run github-protection:plan
```

Live checks require authenticated repository-administration access and remain
outside CI. Applying a plan is a separately authorized, repository-bound action;
the plan and its confirmation value do not grant that authority. See the
[GitHub main protection guide](docs/guides/github-main-protection.md) for the
exact Ruleset, guarded apply procedure, UI inspection, and recovery steps.

## Frozen K3s

The retained K3s source is frozen. It receives static validation only; live
verification remains operator-run and is not CI evidence.

## License

This repository is licensed under [Apache-2.0](LICENSE).
