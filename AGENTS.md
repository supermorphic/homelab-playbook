# Repository instructions

## Scope and safety

- This repository manages off-cluster hosts with Ansible.
- Never execute a playbook against production or staging without explicit operator direction.
- Never decrypt, print, or inspect production Vault values.
- Preserve unrelated user changes and stop if an in-scope edit would overwrite them.

## Development lifecycle

- Use one issue and isolated branch/worktree per initiative.
- Durable design specifications live under `docs/specs/` with three-digit identifiers.
- Transient implementation plans live uncommitted under `.tmp/plans/`.
- Reconcile an active specification with material implementation changes before merge.

## Commands

- Run `mise run bootstrap` explicitly after checkout or dependency changes.
- Use `mise run playbook -- <playbook> <action> <inventory> [ansible-args...]` for operator execution.
- Use focused `mise run validate:fast` and `mise run validate:ansible` validation while iterating.
- Before claiming completion, run `mise run ci:changed`; use `mise run ci` to escalate.
- The repository classifier chooses the minimum required depth. Agents may escalate but never de-escalate it.

## Validation boundaries

- Pull-request validation is offline and secret-free.
- Frozen K3s receives static validation only.
- Live verification is operator-run and is not CI evidence.
- A validation assertion must protect a current invariant and have one canonical owner.
- Remove obsolete executable checks instead of adding permanent forbidden-reference checks.
