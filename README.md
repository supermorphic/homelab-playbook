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
Commands apply to every host in the playbook's target group within the selected
inventory. Add `--limit <host-or-pattern>` when you need a narrower target.

### OS baseline commands

These commands target Debian 13 and Rocky Linux 9 hosts in `os_managed`.
The examples use the production inventory. Complete the
[managed host onboarding guide](docs/guides/managed-host-onboarding.md) first
for manual host preparation, SSH access, inventory, and secret setup.

| Command | Purpose |
| --- | --- |
| `mise run playbook -- os inspect production` | Read a basic OS fact snapshot. |
| `mise run playbook -- os provision production` | Perform a full update, reconcile the complete baseline, reboot if needed, and verify. |
| `mise run playbook -- os maintain production` | Perform a later full package update, reboot if needed, and verify without reapplying configuration. |
| `mise run playbook -- os verify production` | Check the complete effective baseline without changes. |

Provisioning and maintenance include verification. Use standalone verification
at any time to check for drift; use provisioning to reconcile it. A successful
provisioning run includes a full update, so do not immediately follow it with
maintenance. Native daily security updates remain separate from explicit full
maintenance; no host-local recurring full-update scheduler exists.

See the [OS playbook README](playbooks/os/README.md) for inputs, composition,
and validation boundaries.

### Podman foundation commands

Run these after establishing the OS baseline. They target `podman_hosts`;
`nuc4` is the current production member. Ansible loads encrypted inventory
through SOPS and the configured macOS Keychain helper; no extra secret flag is
needed. Complete the [SOPS setup](docs/guides/sops-secrets.md) on the operator
workstation before running playbooks.

| Command | Purpose |
| --- | --- |
| `mise run playbook -- podman provision production` | Install Podman prerequisites, reconcile declared service accounts and directories, and verify. |
| `mise run playbook -- podman verify production` | Check installed capability, declared identities, permissions, and user-manager state without changes. |

Provisioning includes verification. Standalone verification is useful for later
drift checks, including after OS maintenance. Both verifiers stop at the first
failed assertion and do not repair drift.

The production service-account list is empty, so these commands currently
establish and check host capability and shared directories. They deploy no
applications. See the [Podman playbook README](playbooks/podman/README.md) for
account inputs, ownership, failure recovery, and validation boundaries.

### Shared private HTTPS commands

Caddy runs directly on the host under systemd. Separately managed Podman
applications publish HTTP backends on host loopback; the shared proxy owns
private TCP/443 and consumes external certificates supplied under issue #5.

| Command | Purpose |
| --- | --- |
| `mise run playbook -- reverse-proxy provision production` | Install the distribution package, reconcile declared routes, and verify. |
| `mise run playbook -- reverse-proxy verify production` | Observe the installed proxy, TLS, and private firewall policy without repairs. |

The production route list is empty. Initial provisioning creates an admin-only
service with no HTTPS listener. Activating routes requires operator-supplied
private inputs and externally deployed certificates. Caddy is an unpinned system
package upgraded through existing OS maintenance; package updates may restart
the shared service. See the [proxy README](playbooks/reverse-proxy/README.md)
for inputs, prerequisites, and troubleshooting.

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

SOPS encrypts inventory secrets to public age recipients. The operator's
dedicated repository identity lives in the macOS login Keychain; its encrypted
backup and backup passphrase remain outside every checkout. Future automation
controllers use separate identities. See the [SOPS secrets guide](docs/guides/sops-secrets.md)
for workstation setup, recovery, editing, and recipient changes.

Public group variables live in `vars.yml`; version pins in `versions.yml` are
public as well. Encrypted variables use sibling `secrets.sops.yml` files. The
active boundary is `inventory/production/group_vars/os_managed/`.
`inventory/production/host_vars/nuc4/vars.yml` contains public hostname
metadata. The sibling protected file contains identity and access inputs.
Retained Semaphore inputs are under
`inventory/staging/group_vars/semaphore/`, and retained K3s variables are under
`inventory/frozen/k3s/group_vars/`.

The operator completed the one-way conversion of production, staging, and
frozen inventory. SOPS is the only current inventory encryption format.
Agents and CI never decrypt or inspect protected inventory.

## Validation

Use focused validation while iterating, then run change-directed validation before
claiming completion:

```bash
mise run validate:fast
mise run validate:ansible
mise run validate:secrets
mise run test:secrets
mise run ci:changed
```

`ci:changed` classifies committed and working-tree changes and runs the minimum
required depth. Use `mise run ci` to force all currently implemented offline
validation. `validate:secrets` checks ciphertext structure and public recipient
metadata. `test:secrets` uses only ephemeral identities and fixtures. Pull-request
validation is offline and receives no live identity.

### Molecule tests

`mise run test:molecule -- system_maintenance/default` runs the repository's
rootless Podman scenario for Debian 13 and Rocky Linux 9. The same platform set
runs locally and in GitHub's native AMD64 matrix.

`mise run test:molecule -- system_maintenance/baseline` runs complete Debian
and Rocky composition. `mise run test:molecule -- reverse_proxy/default` exercises
the private proxy using disposable certificates and HTTP/WebSocket backends.
Change-directed validation selects affected scenarios on both platforms using
[the checked-in impact map](scripts/ci/molecule-impact.json). Proxy and TLS role
changes also select the baseline scenario, which exercises those integrations.
TLS runtime tests and the three TLS-specific Ansible test files select the same
two scenarios. Other Ansible validation tests retain full-suite selection.
Shared framework changes and uncertain impact select all eight rows. Documentation
changes under `docs/` or subsystem READMEs select no Molecule rows.

Use `mise run ci:changed -- --dry-run` to inspect selected scenarios and reasons.
GitHub Actions consumes the same plan and writes exact base/head SHAs and row
reasons to its job summary. Scheduled and manual CI run the complete suite;
`mise run ci` remains the explicit complete local validation command. Container results do not prove physical
reboot, host-kernel enforcement, real network reachability, private ingress, or recovery from the real NAS.

`mise run test:molecule -- semaphore/default` prepares Semaphore storage and
configuration and checks generated Quadlets and timers on both distributions.
Nested user-namespace limits prevent application startup in these OS fixtures.
Separate `test:semaphore` modes exercise the pinned runtime and recovery with
disposable containers whenever the Semaphore scenario is selected.

See the [Semaphore playbooks](playbooks/semaphore/README.md) for prerequisites,
controller configuration, and backup operation, and the
[recovery guide](docs/guides/semaphore-recovery.md) for isolated restore tests
and replacement-host recovery.

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
