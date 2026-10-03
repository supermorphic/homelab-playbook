# Homelab Playbook

Ansible automation for provisioning and maintaining off-cluster homelab hosts.
Production contains active hosts; staging is reserved for future deployments.

## Start

Install [Mise](https://mise.jdx.dev/), Git, and SSH configuration for the hosts
you are authorized to operate. After checkout or dependency changes:

```bash
mise install
mise run bootstrap
```

Discover workflows with `mise tasks`. The canonical gateway lists playbooks
without arguments; add a playbook to list actions, then an action to list
inventories:

```bash
mise run playbook
mise run playbook -- <playbook>
mise run playbook -- <playbook> <action>
```

Live operations require explicit operator authorization for the exact target and
action. Validate local changes with `mise run ci:changed`. Execution and public
data rules are in [AGENTS.md](AGENTS.md); tool pins and tasks are in
[.mise.toml](.mise.toml).

## Design topics

Read only the owning topic needed for a task; source, configuration, and command
help own implementation details.

- [Development, command lifecycle, and CI](docs/specs/001-agentic-development-modernization.md),
  [container validation](docs/specs/002-multi-os-molecule-validation.md).
- [OS baseline](docs/specs/003-os-maintenance-security-baseline.md),
  [host onboarding and recovery](docs/specs/004-managed-host-onboarding.md),
  [secrets and identity recovery](docs/specs/005-sops-age-secrets.md).
- [Podman foundation](docs/specs/006-podman-quadlet-foundation.md),
  [TLS, UniFi, and trust recovery](docs/specs/007-off-cluster-tls-trust.md),
  [private reverse proxy](docs/specs/008-shared-private-reverse-proxy.md).
- [Semaphore](docs/specs/009-semaphore-infrastructure-automation.md),
  [Forgejo](docs/specs/010-forgejo-service.md),
  [Forgejo runners](docs/specs/011-forgejo-runners.md).
