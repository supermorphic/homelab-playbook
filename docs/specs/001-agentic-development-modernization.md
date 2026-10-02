# Specification 001: Agentic development modernization

Issue: [#1](https://github.com/supermorphic/homelab-playbook/issues/1)

## Purpose and boundaries

Keep `homelab-playbook` a deterministic, workstation-operable Ansible source
for off-cluster hosts. Preserve useful automation while separating development
validation, live operations, and deployment evidence. Root [AGENTS.md](../../AGENTS.md)
owns execution policy; necessary tool adapters import or adapt that policy.

Active inventories are production and staging. Staging reserves a future
boundary rather than claiming an available test platform. Frozen K3s remains
versioned for static validation, with no CI execution or implied deployment
support. Its retained Kube-VIP design provides an ARP-advertised, leader-elected
control-plane endpoint, separate from Kubernetes Service load balancing; the
[manifest template](../../roles/kube_vip/templates/30-kube-vip-daemonset.yaml)
owns its configuration. Restoring that deployment requires a separate design
and authorization. Retained Pi-hole automation likewise has no active inventory
target; removing inventory membership does not change former hosts.

[Specification 003](003-os-maintenance-security-baseline.md) owns the Debian
managed-host contract, [004](004-managed-host-onboarding.md) owns onboarding,
and [005](005-sops-age-secrets.md) owns SOPS and age. Later service specs own
application topology. This repository does not make Kubernetes, Semaphore,
Forgejo, or any managed host a prerequisite for its own bootstrap or recovery.
GitHub and an independent workstation path remain available.

## Reproducible controller and operator interface

Mise owns pinned tools and workflow entry points; uv owns locked Python
dependencies; [requirements.yml](../../requirements.yml) owns exact Galaxy
requirements. Bootstrap explicitly establishes those dependencies. Live playbook
execution verifies them without downloading or installing replacements and
fails with an actionable bootstrap instruction when they are absent or stale.
Repository overrides remain part of the dependency freshness contract.

Galaxy generations may be reused only after verifying the complete requirements
and selected overrides. A failed candidate never replaces the previous verified
selection. [The dependency manager](../../scripts/galaxy_dependencies.py)
owns cache selection, repair options, and freshness checks; task help owns usage.
Credentials and identities never belong in tool configuration or cache evidence.

`mise run playbook` is the canonical interface. It validates the requested
playbook, action, inventory, dependency state, and applicable safety controls
before invoking Ansible. Additional Ansible arguments retain their intended
forwarding semantics. [run-playbook](../../run-playbook) is only a thin Mise
forwarder; it must not implement another resolver, install dependencies, or
change credential policy. The [gateway](../../scripts/playbook.sh) owns exact
argument handling. A valid command supplies no authority to execute it against
a live inventory.

## Command lifecycle

Classify new or renamed commands by the result requested and their actual
effects, then choose proportionate safeguards. Do not add ceremony for naming
symmetry or keep deprecated aliases after an approved semantic rename.

| Term | Contract |
| --- | --- |
| `validate` | Prove local source, configuration, policy, or evidence correctness without live credentials or managed-host contact. Explicit local outputs are allowed. |
| `verify`, specialized `check` | Observe a live or external target without intentionally changing it. |
| `apply`, or a precise action | Reconcile an existing target, validate current prerequisites, mutate under the required authority, and read back the result. |
| `bootstrap` | Establish missing local or target capability and verify it. |
| `test` | Conduct a bounded experiment with exactly owned temporary state, evidence, and cleanup. |
| `cleanup` | Delete only an exactly identified owned target and verify absence. |

Purpose-specific terms such as `install`, `renew`, `restart`, or `render` remain
valid when they describe the result more precisely. An aggregate inherits the
effects of its components: a validation aggregate may run a registered bounded
test. Creating containers for positive evidence follows the test contract even
when execution is local.

A plan describes later work; a dry-run exercises the relevant validation path
without persisting the intended mutation. Neither grants authorization. Expose
a standalone preflight or plan only when it has independent value; otherwise
embed preflight to avoid stale evidence. Consequential live mutations repeat
safety-critical checks immediately before execution and use target-bound
confirmation where proportionate. Confirmation records intent, while AGENTS.md
and the operator supply authority. Observational commands need no confirmation
merely to resemble mutating commands.

Validate public arguments and registered targets before operations. Invalid
usage or unsupported dispatch returns status `2`; failed runtime prerequisites
or operations return `1`, preserving signal statuses when practical. Expected
operator errors produce concise, actionable stderr without a traceback. Keep
primary assertion, dependency, recovery, and cleanup outcomes distinct. Cleanup
failure fails a test without hiding its primary outcome. Failed scoped checks
never justify broader credentials, targets, privilege, or deletion scope.

## Validation architecture

The repository selects minimum required depth: `fast < ansible < molecule < full`.
`ci:changed` uses committed changes from the merge base, staged and unstaged
changes, untracked non-ignored files, and both sides of renames. Missing bases,
unknown paths, invalid mappings, or Git discovery failures fail closed to full
validation. Authors may escalate, never de-escalate. The
[classifier](../../scripts/ci/classify.py) owns path rules; workflows do not
copy those rules. [Specification 002](002-multi-os-molecule-validation.md)
owns executable scenario selection.

Keep each invariant with one validator. General syntax/style checks, Ansible
semantics, inventory resolution, wrapper contracts, secret metadata/integration,
redacted secret scanning, and workflow security checks provide complementary
rather than repeated evidence. Ansible source discovery includes candidate
tracked and untracked non-ignored files, excludes exact protected inputs,
rejects symlink aliases and empty manifests, and propagates discovery failures.
CI never decrypts protected inventory or contacts an inventory host.

Pull-request workflows always start, without top-level path filters. A stable
`merge-gate` reconciles classifier output and every selected job. A selected job
that fails, is cancelled, or is skipped fails the gate; intentionally unselected
jobs may be skipped. Full validation includes every implemented offline check.
Read-only permissions, pinned actions, bounded timeouts, and cancellation of
superseded candidates constrain CI. Caches accelerate setup; they never supply
a previously passing result as current evidence.

The ordinary static pull-request gate targets approximately two minutes p95;
executable scenarios have separate measured costs. Preserve concise selection,
reason, and duration evidence. When retained checks become slow, first remove
obsolete or duplicated work and reduce setup costs, then improve implementation
and use safe bounded parallelism. A generalized dependency planner or reporting
platform needs a measured consumer. Detailed run comparisons belong in existing
evidence stores or ignored task state, not this spec.

## GitHub integration protection

The control objective is that `refs/heads/main` changes only through GitHub
completing a current successful pull-request merge. Workflow success and live
GitHub enforcement are separate controls. Squash-only repository merge settings
and one active repository-owned `Protect main` Ruleset work together. The rule
has no bypass actors, requires a current candidate's GitHub Actions
`merge-gate`, requires linear history, and blocks deletion and force pushes.
Zero required approvals preserves the single-operator workflow. Do not add an
update restriction with no bypass: it would prevent valid GitHub merges too.
The [desired-state implementation](../../scripts/repository/github_protection.py)
owns exact API fields and normalization.

Ruleset enforcement requires a supported account-plan and visibility
combination. Under the adopted public-repository plan, making the repository
private without a supporting plan breaks this protection prerequisite. Treat
an inaccessible Rulesets API as failed protection, not advisory enforcement.

The protection checker reads complete paginated state and derives the Actions
integration identifier from a successful `merge-gate` in the check suite of a
successful [ci.yml](../../.github/workflows/ci.yml) run. An unrelated same-commit
check is insufficient. It refuses ambiguous ownership, duplicate managed
Rulesets, malformed responses, or unexpected effective rules. Administration
read access is needed to see the full bypass definition. Live checks remain
outside offline CI.

To recover drift, inspect **Settings → General → Pull Requests**, **Settings →
Rules → Rulesets → Protect main**, and **Settings → Branches**. The checker does
not query legacy branch-protection rules; independently confirm that no legacy
rule layers additional requirements onto `main`. Use `github-protection:check`
and the read-only `github-protection:plan` to inspect the result. Resolve
visibility/plan limitations, duplicate rules, incomplete ownership, or unmanaged
protection before authorizing repair. Workflow recovery must first restore the
stable gate and obtain a successful normal PR run so its source can be resolved.

`github-protection:apply` requires authorization for that exact administration
operation and the repository-scoped confirmation shown by command help. It
recollects state immediately before writes and requires complete API read-back.
Partial writes also trigger read-back; failure never implies rollback. Verify
functional enforcement through a normal PR and a new candidate revision, not
through attempted direct pushes, branch deletion, or force pushes. A passing API
check grants no merge or auto-merge authority.
