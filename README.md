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

## Inventories

Select one of these inventory arguments:

- `production` contains the active off-cluster hosts.
- `staging` is an intentionally empty environment boundary.
- `frozen/k3s` retains the non-active K3s inventory.

Production and staging are operator inputs. Validation parses public-only
inventory mirrors and does not connect to their hosts.

## Secrets

Ansible Vault encrypts secret variables. Operators own the Vault password or
password-retrieval mechanism outside the repository, such as in a password
manager. Do not commit key material or embed it in Mise configuration, helper
scripts, or pull-request CI. Do not decrypt, print, or inspect production Vault
values during development or validation.

## Validation

Use focused checks while iterating, then run change-directed validation before
claiming completion:

```bash
mise run check:fast
mise run check:ansible
mise run ci:changed
```

`ci:changed` classifies committed and working-tree changes and runs the minimum
required depth. Use `mise run ci` to force all currently implemented offline
validation. Pull-request validation is offline and secret-free.

Molecule is reserved as a future convention for scenarios under
`roles/<role>/molecule/<scenario>/`. This repository currently has no Molecule
task, dependency, scenario, or CI job.

## Frozen K3s

The retained K3s source is frozen. It receives static validation only; live
verification remains operator-run and is not CI evidence.

## License

This repository is licensed under [Apache-2.0](LICENSE).
