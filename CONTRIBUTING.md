# Contributing

## Prerequisites

Install [Mise](https://mise.jdx.dev/), Git, and SSH configuration suitable for
the hosts you are explicitly authorized to operate.

## Bootstrap

After checkout or a dependency change, install the repository-managed tools and
dependencies:

```bash
mise run bootstrap
```

## Focused validation

Run the focused repository-owned checks while iterating:

```bash
mise run check:fast
mise run check:ansible
```

Before claiming completion, run the change-directed validation. Use the full
suite when deeper validation is warranted:

```bash
mise run ci:changed
mise run ci
```

## Running playbooks

Use the repository-owned command interface:

```bash
mise run playbook -- <playbook> <action> <inventory> [ansible-args...]
```

Never execute a playbook against production or staging without explicit
operator direction. Never decrypt, print, or inspect production Vault values.
