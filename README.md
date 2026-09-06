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
and `inventory/` contains environment inventories. The
[documentation index](docs/README.md) links current guides, references, and
durable specifications. Transient implementation plans belong in `.tmp/plans/`.

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

The OS playbooks inspect, provision, and maintain Debian 13 and Rocky Linux 9
hosts in `os_managed`. The source-adjacent
[OS playbook README](playbooks/os/README.md) summarizes their composition and
development boundaries. Follow the
[managed host onboarding guide](docs/guides/managed-host-onboarding.md) for
manual prerequisites, SSH setup, inventory and Vault preparation, exact live
commands, lifecycle decisions, verification, and recovery.

The active production inventory contains `nuc4` as the first managed-host
example. Native daily security updates remain separate from explicit full
maintenance, and no host-local recurring full-update scheduler exists.

## Inventories

Select one of these inventory arguments:

- `production` contains the active `nuc4` host in `os_managed`.
- `staging` contains no hosts; it retains non-active Semaphore deployment and
  backup inputs for future work.
- `frozen/k3s` retains the non-active K3s inventory.

Production and staging are operator inputs. Validation parses public-only
inventory mirrors and does not connect to their hosts.
Each inventory directory stores its static host and group topology in
`hosts.yml`; public variables remain under `group_vars/`.

## Secrets

Ansible Vault encrypts secret variables. Operators own Vault passwords outside
the repository, such as in a password manager. Supply the active production OS
Vault password interactively with `--ask-vault-pass`. Do not commit key
material or embed it in Mise configuration, helper scripts, or pull-request CI.
Do not decrypt, print, or inspect production Vault values during development or
validation.

Public group variables live in `vars.yml`; version pins in `versions.yml` are
public as well. Encrypted variables live only in sibling `vault.yml` files. The
active boundary is `inventory/production/group_vars/os_managed/`.
`inventory/production/host_vars/nuc4/vars.yml` contains public hostname
metadata. The sibling `os_managed/vault.yml` contains protected identity and
access inputs. Validation treats it as opaque and never decrypts, parses, or
inspects its protected values. Retained Semaphore inputs are under
`inventory/staging/group_vars/semaphore/`, and retained K3s variables are under
`inventory/frozen/k3s/group_vars/`. Inventory parsing and Ansible semantic
validation use the public files and never receive encrypted `vault.yml` input.
Broad redacted Gitleaks scans inspect repository bytes and history, including
encrypted file bytes, without decryption or plaintext output.

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
and Rocky composition. CI runs both platforms for both scenarios as four exact
selector-and-platform matrix jobs. Container results do not prove physical
reboot, host-kernel enforcement, real network reachability, or Semaphore
scheduling and notification delivery.

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
