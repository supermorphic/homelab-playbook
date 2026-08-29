# Contributing

## Prerequisites

Install [Mise](https://mise.jdx.dev/), Git, and SSH configuration suitable for
the hosts you are explicitly authorized to operate.

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
`main`. Fetch and inspect both the remote feature branch and `origin/main`
before each push. Open a pull request, keep it current, wait for `merge-gate`,
and squash merge only with explicit operator authority for that merge or
auto-merge action.

Use the read-only live protection commands when repository-administration
access is available:

```bash
mise run github-protection:check
mise run github-protection:plan
```

These commands are outside offline validation. A plan and the apply confirmation
do not authorize a change. Follow the
[GitHub main protection guide](docs/guides/github-main-protection.md) for the
exact desired state, guarded apply, read-back, and recovery procedure.

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
Production contains active off-cluster hosts, staging is intentionally empty,
and frozen K3s is retained for static validation only.

Never execute a playbook against production or staging without explicit
operator direction.

## Secrets

Operators own the Ansible Vault password or password-retrieval mechanism
outside the repository, such as in a password manager. Do not commit key
material or embed it in Mise configuration, helper scripts, or pull-request CI.
Never decrypt, print, or inspect production Vault values.

## Validation boundary

Pull-request validation is offline and secret-free. Frozen K3s receives static
validation only; live verification is operator-run and is not CI evidence.

## License

Contributions are made under the repository's [Apache-2.0 license](LICENSE).
