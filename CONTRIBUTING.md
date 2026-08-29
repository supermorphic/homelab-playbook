# Contributing

## Prerequisites

Install [Mise](https://mise.jdx.dev/), Git, and SSH configuration for any hosts
you are responsible for managing.

## Bootstrap

After checkout, install the pinned tools, then install the locked controller and
Galaxy dependencies. Repeat both commands after a tool or dependency change:

```bash
mise install
mise run bootstrap
```

## Focused validation

Run the focused repository-owned validation while iterating:

```bash
mise run validate:fast
mise run validate:ansible
```

Before claiming completion, run the change-directed validation. Use the full
suite when deeper validation is warranted:

```bash
mise run ci:changed
mise run ci
```

`ci:changed` classifies committed and working-tree changes and runs the minimum
required depth. `ci` forces all currently implemented offline validation.

## Pull-request workflow

Work from an issue-backed feature branch; do not commit or push directly to
`main`. Before opening or updating a pull request, run `mise run ci:changed` and
use `mise run ci` when full validation is warranted. Keep the pull request
current with `main`; `merge-gate` must pass before squash merge.

Contributors with repository-administration access can inspect live protection
and preview drift repair with:

```bash
mise run github-protection:check
mise run github-protection:plan
```

These commands are outside offline validation. Follow the
[GitHub main protection guide](docs/guides/github-main-protection.md) for the
desired state, reconciliation, read-back, and recovery procedures.

Molecule is reserved as a future convention for scenarios under
`roles/<role>/molecule/<scenario>/`. No Molecule task, dependency, scenario, or
CI job is currently implemented.

## Running playbooks

Use the repository-owned command interface:

```bash
mise run playbook -- <playbook> <action> <inventory> [ansible-args...]
```

The repository-root alias is an equivalent thin forwarding wrapper:

```bash
./run-playbook <playbook> <action> <inventory> [ansible-args...]
```

Choose the `production`, `staging`, or `frozen/k3s` inventory selector.
Production contains active off-cluster hosts, staging currently contains no
hosts, and frozen K3s is retained for static validation only.

Confirm the playbook, action, inventory selector, and additional arguments
before execution. Run playbooks only against hosts and environments you are
responsible for managing.

## Secrets

Keep the Ansible Vault password or password-retrieval mechanism outside the
repository, such as in a password manager. Do not commit key material or embed
it in Mise configuration, helper scripts, or pull-request CI. Do not expose
decrypted Vault values in logs, command output, issues, or pull requests.

## Validation boundary

Pull-request validation is offline and secret-free. Frozen K3s receives static
validation only. Live verification is performed separately against an
explicitly selected environment and is not CI evidence.

## License

Contributions are made under the repository's [Apache-2.0 license](LICENSE).
