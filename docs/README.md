# Documentation

Use the [root README command reference](../README.md#running-playbooks) for
routine playbook operations and subsystem READMEs for automation usage and
configuration. Guides cover operator-executed setup and recovery procedures;
specifications record design decisions and integration contracts.

## Guides

- [GitHub main protection](guides/github-main-protection.md) — Inspect, verify,
  and recover the repository's protected-branch settings.
- [Managed host onboarding](guides/managed-host-onboarding.md) — Prepare,
  provision, verify, maintain, and recover an off-cluster Ansible-managed host.
- [SOPS secrets](guides/sops-secrets.md) — Set up and recover the operator age
  identity, edit protected inventory, and manage recipients.
- [UniFi TLS certificates](guides/unifi-tls.md) — Configure native UniFi OS
  certificate issuance and renewal with Cloudflare DNS and direct local access.
- [Private device proxy](guides/device-proxy.md) — Prepare private device browser
  access through Caddy for authenticated HTTPS and HTTP-only backends.

## Reference

- [Off-cluster TLS automation](../playbooks/tls/README.md) — Install the host
  issuer, configure its disabled timer, and prepare the fixed Caddy integration.
- [Repository command lifecycle](reference/repository-command-lifecycle.md) —
  Classify command behavior, safeguards, execution authority, and evidence.

## Specifications

- [001 — Agentic development modernization](specs/001-agentic-development-modernization.md)
  — Defines the repository's agentic workflow, safety controls, and validation
  architecture.
- [002 — Debian Molecule validation](specs/002-multi-os-molecule-validation.md)
  — Defines deterministic Debian 13 container validation.
- [003 — OS maintenance and security baseline](specs/003-os-maintenance-security-baseline.md)
  — Defines supported host maintenance, security policy, verification, and
  reboot behavior.
- [004 — Managed host onboarding](specs/004-managed-host-onboarding.md) —
  Defines the reusable onboarding design and the first active `os_managed`
  production host.
- [005 — SOPS and age inventory secrets](specs/005-sops-age-secrets.md) —
  Defines the one-way migration from Ansible Vault and the current secret
  loading, identity, recovery, and validation contract.
- [006 — Podman and Quadlet foundation](specs/006-podman-quadlet-foundation.md) —
  Defines separate service-account ownership, stable identity allocations, and
  reusable rootless container conventions without deploying applications.
- [007 — Off-cluster TLS trust](specs/007-off-cluster-tls-trust.md) — Records the
  selected hybrid topology and NUC #4 certificate automation design.
- [008 — Shared private reverse proxy](specs/008-shared-private-reverse-proxy.md) —
  Defines host-level Caddy, private HTTPS routes, external certificate delivery,
  configuration recovery, and service integration contracts.
