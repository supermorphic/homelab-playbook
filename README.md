# Homelab Playbook

Ansible automation for provisioning and maintaining off-cluster homelab hosts.

## Prerequisites

Install [Mise](https://mise.jdx.dev/), Git, and SSH configuration appropriate
for the hosts you are explicitly authorized to operate.

## Bootstrap

After checkout or a dependency change, install the repository-managed tools and
dependencies:

```bash
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

Execute against production or staging only with explicit operator direction.

## Inventories

Select the environment as the command's inventory argument. Production and
staging inventories are operator inputs; validation does not connect to their
hosts.

## Secrets

Ansible Vault encrypts secret variables. Keep Vault passwords and SSH material
outside the repository; do not decrypt, print, or inspect production Vault
values during development or validation.

## Validation

Use focused checks while iterating, then run change-directed validation before
claiming completion:

```bash
mise run check:fast
mise run check:ansible
mise run ci:changed
```

Use `mise run ci` when deeper validation is warranted. Pull-request validation
is offline and secret-free.

## Frozen K3s

The retained K3s source is frozen. It receives static validation only; live
verification remains operator-run and is not CI evidence.

## License

This repository is licensed under [Apache-2.0](LICENSE).
