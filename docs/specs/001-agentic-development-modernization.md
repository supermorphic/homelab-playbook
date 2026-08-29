# Specification 001: Agentic development modernization

Issue: [#1 Modernize homelab-playbook for agentic development](https://github.com/supermorphic/homelab-playbook/issues/1)

## Purpose

Modernize `homelab-playbook` into the safe, deterministic Ansible source for
off-cluster host provisioning and configuration management. The repository must
remain directly operable from a workstation and be understandable to both human
operators and coding agents without rewriting useful Ansible automation.

This initiative establishes repository boundaries, lifecycle decisions, an
operator interface, a reproducible development toolchain, Ansible-native secret
handling, agent instructions, and fast offline validation. It does not deploy
services or change managed hosts.

## Context

The only active managed service is Pi-hole on Raspberry Pi 1. The nodes previously
intended for K3s are not currently running. The repository therefore needs an
audit based on current consumers rather than preservation of every historical
executable surface. Future service and infrastructure designs remain with their
own issues and specifications.

## Governing principles

1. Preserve active and valuable automation, not obsolete execution surfaces.
2. Keep Ansible the repository's native operational and secret-management model.
3. Keep production execution deliberate, workstation-friendly, and separate from
   validation.
4. Make the repository determine required validation; do not rely on an agent or
   author to choose a shallower test scope.
5. Target an ordinary pull-request gate of approximately two minutes p95.
6. Add deeper validation only for changes that can benefit from its evidence.
7. Keep GitHub available for bootstrap and disaster recovery.

## Scope

### Included

- current-state and consumer audit;
- repository lifecycle cleanup;
- frozen K3s organization;
- removal of Argo CD, KSOPS, and SOPS automation;
- retention of reusable Semaphore deployment and backup automation;
- Ansible Vault hardening;
- inventory restructuring;
- Mise and uv toolchain management;
- a thin playbook operator interface;
- agent, contribution, and repository documentation;
- Apache License 2.0 standardization;
- deterministic, change-directed GitHub Actions validation;
- high-value repository, security, and Ansible checks;
- documentation of a future Molecule convention.

### Excluded

- connecting CI to production or staging hosts;
- decrypting production Vault content in CI;
- deploying or updating any managed service or host;
- designing future service or infrastructure topology;
- implementing a VM staging platform;
- converting K3s from Argo CD to Flux;
- migrating secrets to SOPS;
- implementing Molecule scenarios;
- a generalized CI test catalog, dependency graph, or reporting platform.

## Current-state audit

### Active production

- Raspberry Pi 1 runs Pi-hole on bare metal.
- Pi-hole is managed through `playbooks/pihole` and related inventory/roles.

Modernization must keep the current Pi-hole path operable but does not redesign
the service or choose its eventual replacement.

### Frozen K3s

The K3s implementation represents substantial human-authored work and remains a
plausible long-term Kubernetes option. It is not active today and is not the
repository's current deployment direction. It must be retained but frozen.

Frozen means:

- source remains versioned and readable;
- static YAML and Ansible validation remains applicable;
- documentation clearly describes it as non-active;
- no playbook is executed by CI;
- no modernization to Flux is performed;
- no new feature work is bundled into issue #1.

Argo CD is not part of the retained K3s boundary. K3s provisioning remains;
Argo CD and KSOPS are retired.

### Retained Semaphore automation

The Semaphore Compose deployment, database backup configuration, public
variables, version pins, and encrypted variables remain useful implementation
source for issues #4 and #9. They are retained under the staging boundary but
have no inventory host. This preserves the tested automation without restoring
the former localhost deployment target or claiming that Semaphore is active.

### Retired surfaces

The following have no active consumer and are removed rather than archived as
executable automation:

- Argo CD install/uninstall automation and documentation;
- KSOPS and repository SOPS configuration or scripts;
- SOPS-encrypted content in this repository;
- documentation describing removed commands or directory layouts;
- rendered Helm helper code whose only consumer was the retired Argo path.

Removal is verified during implementation through search, configuration review,
and the final diff. CI will not retain permanent checks that merely forbid names
of removed technologies.

### Existing quality concerns

The audit identified several gaps that the implementation plan must address:

- no GitHub Actions validation exists;
- the README describes Podman files and command paths that do not exist;
- existing lint configuration turns important reliability rules into warnings or
  skips them;
- the active Pi-hole path has no repository-owned verification playbook;
- an absent Pi-hole installation can end a play successfully rather than fail an
  expected-service check;
- Pi-hole DNS update change reporting conflates failure with change;
- system maintenance silently skips unsupported operating systems;
- SSH host-key checking is disabled globally;
- validation is concentrated in retiring Argo code and non-active Semaphore
  automation;
- Ansible Galaxy dependencies and controller dependencies are not managed through
  one reproducible toolchain.

These findings guide modernization. They do not authorize production execution.

## Repository responsibility

`homelab-playbook` owns:

- Debian host bootstrap and lifecycle configuration outside Kubernetes;
- Raspberry Pi provisioning;
- off-cluster host configuration introduced through focused future
  specifications;
- Ansible inventories, roles, playbooks, Vault content, and operator workflows;
- offline validation of those sources.

## Supported operating systems

The maintained production target is deliberately narrow:

- Debian 13 for general-purpose hosts;
- Raspberry Pi OS based on Debian for Raspberry Pi hosts.

Arch Linux and Red Hat-family logic may remain only where it is useful historical
or experimental source. It is not a validated production contract until a real
consumer and test target are defined. Unsupported production paths should fail
clearly rather than silently skip required work. The tested Arch Linux package
and locale tasks remain in `roles/system_maintenance/tasks/setup-Archlinux.yml`,
with their exact Galaxy collection dependencies, but the active role dispatcher
remains Debian-only.

## Inventory design

The target inventory layout is:

```text
inventory/
├── production/
├── staging/
└── frozen/
    └── k3s/
```

Requirements:

- each environment is directly selectable by the operator interface;
- public variables and encrypted secret variables remain separate;
- active Pi-hole public variables live in
  `inventory/production/group_vars/pihole/vars.yml`, while encrypted variables
  live in the sibling `vault.yml`;
- retained Semaphore public variables and version pins live under
  `inventory/staging/group_vars/semaphore/`, while encrypted variables live in
  the exact sibling `vault.yml`; staging defines no Semaphore host;
- frozen K3s public variables live in `vars.yml` and `versions.yml` under
  `inventory/frozen/k3s/group_vars/`, while its encrypted variables live in the
  exact sibling `vault.yml`;
- frozen K3s groups and variables move together under `inventory/frozen/k3s`;
- staging contains no placeholder claim that a VM platform exists;
- active inventory parsing is validated offline;
- CI never contacts hosts named in any inventory.

## Secrets design

Ansible Vault remains the only encryption format in this repository.

### Rationale

Ansible Vault follows the natural playbook execution path, avoids a collection or
external decryption integration, and remains useful after Argo CD and KSOPS are
removed. SOPS adds no durable consumer here.

Both Ansible Vault and SOPS require external key material. The Vault password or
password retrieval mechanism remains in the operator's password manager and is
never committed, embedded in Mise configuration, printed by helper scripts, or
made available to pull-request CI.

### Requirements

- encrypted variables use Ansible Vault;
- public variables live outside encrypted files;
- secret filenames and variable boundaries are documented;
- production, staging, and frozen Vault inputs are never decrypted,
  inventory-parsed, or passed to Ansible semantic validation by CI;
- broad redacted repository secret scanning may inspect encrypted file bytes and
  history without decrypting or printing their contents;
- CI validates Vault integration with an ephemeral generated password and fixture;
- scripts must not dump the environment;
- examples contain no real addresses, tokens, passwords, or private keys beyond
  information intentionally public in inventory;
- GitHub secret scanning is supplemented by Gitleaks.

## Licensing

Replace the existing GPL-3.0 license with Apache License 2.0 and identify the
repository as Apache-2.0 in documentation. This preserves an explicit
public-repository license and adds Apache's patent grant.

## Reproducible toolchain

### Mise

Mise is the canonical task and tool entry point. It pins the controller runtime
and supporting command-line tools and exposes memorable repository tasks.

Mise does not store production credentials, Vault passwords, SSH material, or
host-specific secrets.

### Python and uv

- Mise pins Python and uv.
- uv manages and locks Python dependencies, including Ansible and ansible-lint.
- the uv lock is committed and checked for consistency.
- Galaxy roles and collections remain declared with exact versions in
  `requirements.yml` and install into a repository-local path.
- controller and Galaxy dependency installation is explicit and reproducible.

### Bootstrap

`mise run bootstrap` installs or validates the locked development dependencies.
It is explicit and may access package and Galaxy sources.

Operational playbook execution is non-mutating with respect to controller
dependencies. If required tools or roles are absent, the command fails with a
clear instruction to run bootstrap rather than silently installing dependencies
while preparing to modify a host.

Non-mutating dependency verification rejects floating Mise tool declarations,
checks lock consistency, runs `uv sync --frozen --check`, compares installed
Galaxy version metadata with every exact role requirement, and compares each
bootstrap-owned installed override byte-for-byte with its source. Bootstrap
freshness evidence covers `uv.lock`, `requirements.yml`, and both the source and
installed form of every override. Every actionable failure directs the operator
to run `mise run bootstrap`, including missing, unreadable, or malformed local
freshness evidence.

## Operator interface

The canonical playbook command is:

```text
mise run playbook -- <playbook> <action> <inventory> [ansible-args...]
```

Examples:

```bash
mise run playbook -- pihole update production
mise run playbook -- pihole update production --limit pi1 --check
mise run playbook -- os provision staging -vv
```

The command resolves the repository root, validates the selected playbook and
inventory, checks dependencies, and then replaces itself with `ansible-playbook`.
All arguments following the first three positional arguments are forwarded
unchanged.

### Thin shell alias

Retain an executable repository-root alias named `run-playbook`:

```text
./run-playbook <playbook> <action> <inventory> [ansible-args...]
```

It may only:

1. locate the repository root;
2. verify that Mise is available;
3. replace itself with the canonical Mise task while forwarding `"$@"` exactly.

It must not duplicate playbook resolution, inventory resolution, dependency
installation, prompts, Vault policy, or Ansible flags. The current `run.sh` is
replaced after contract tests prove equivalent intended argument forwarding.

## Agent workflow

Repository-root `AGENTS.md` provides the authoritative, vendor-neutral workflow
for coding agents. `CLAUDE.md` imports that policy and contains only
Claude-specific behavior. The policy must describe:

- repository context and policy precedence;
- clear, concrete communication style;
- issue, branch, worktree, design, and implementation-plan expectations;
- preservation of user changes in a dirty worktree;
- Git safety, authority, orchestration, and completion boundaries;
- no production or staging execution without explicit operator direction;
- no decryption or inspection of production secrets;
- public-repository disclosure safeguards;
- use of repository-owned Mise tasks;
- focused validation while iterating;
- final change-directed validation before claiming completion;
- escalation to full validation when classification is uncertain;
- documentation and lifecycle expectations for new automation.

Agents do not choose the minimum sufficient CI depth. They run:

```bash
mise run ci:changed
```

The repository classifier selects and executes the required validation. An agent may
run a deeper command voluntarily, but cannot de-escalate the classifier result.

## Repository command lifecycle

Command names follow their effects and requested result:

- `validate` proves local source, configuration, policy, or evidence correctness;
- `verify` observes a live target without intentionally changing it;
- `check` is a specialized live or external drift/compliance observation;
- `plan` previews a later operation but is neither authorization nor a dry-run;
- `apply` reconciles an existing live or external target;
- `bootstrap` initializes controller or target capability; and
- `test` conducts a bounded experiment that may create or change temporary state.

Purpose-specific verbs remain valid when they describe the requested result more
precisely. Safeguards are selected independently from naming: a consequential
external mutation repeats current-state preconditions, requires operator authority
and a target-bound confirmation, and reads back its result. An observational
command does not gain an accidental-execution confirmation merely for symmetry.

The current public command families therefore use:

```text
validate:fast                 focused local repository validation
validate:ansible              focused local Ansible validation
ci                            complete offline validation aggregate
ci:changed                    change-directed validation selection and execution
github-protection:check       read-only external drift verification
github-protection:plan        read-only reconciliation preview
github-protection:apply       guarded external reconciliation and read-back
```

The playbook operator retains purpose-specific actions such as `install`,
`update`, and observational `verify`. Repository policy—not the verb or a
confirmation variable—owns authority to execute them against a target.

An approved lifecycle rename updates every repository-owned consumer atomically.
The former `check:fast` and `check:ansible` tasks are removed without aliases.

## Validation architecture

### Objective

The ordinary required pull-request gate targets approximately two minutes p95.
Validation first provides the smallest relevant, nonduplicated, deterministic
evidence. Deeper execution is conditional on changes that can benefit from it.

### Validation depths

The architecture defines four ordered depths:

```text
fast < ansible < molecule < full
```

When multiple paths change, the deepest selected depth wins.

#### `fast`

Runs on every pull request and covers repository hygiene, controller-independent
file validation, and security checks.

Initial target: no more than 60 seconds on a warm GitHub runner path.

#### `ansible`

Runs when active or frozen Ansible inventories, playbooks, roles, configuration,
or their direct validation fixtures change. It includes `fast` evidence plus
Ansible-specific static and contract checks.

Initial target: complete required gate no more than 120 seconds on the ordinary
warm path.

#### `molecule`

Reserved for executable testing of explicitly mapped first-party roles. It will
include `ansible` evidence plus affected Molecule scenarios.

Issue #1 documents this convention but does not install Molecule, add a scenario,
add a CI job, or add placeholder classifier mappings. The first durable
first-party role that benefits from converge, idempotence, and verification tests
introduces the implementation through its own focused specification.

An initial scenario will conventionally live at:

```text
roles/<role>/molecule/<scenario>/
```

and expose a future command shaped as:

```text
mise run validate:molecule -- <role>/<scenario>
```

Container scenarios do not claim to validate Raspberry Pi hardware, ARM behavior
on x86, firmware, reboot behavior, real routing/DNS failover, or production
networking. Those require VM or hardware staging.

#### `full`

Runs all implemented offline validation. It is selected for CI, classifier,
toolchain, lock, dependency, and unknown-path changes and is available through
manual and scheduled execution.

Until Molecule scenarios exist, `full` is `fast + ansible` and retains the same
approximately two-minute objective.

### Public commands

```text
mise run validate:fast
mise run validate:ansible
mise run ci:changed
mise run ci:changed -- --dry-run
mise run ci
```

- `validate:fast` and `validate:ansible` are focused local-validation commands.
- `ci:changed` discovers, explains, and executes the required depth.
- `ci:changed -- --dry-run` classifies and explains without executing.
- `ci` forces all currently implemented offline validation.
- there is no public `ci:plan` task.

### Classifier

A small dependency-free repository script is the only source of path
classification. Mise and GitHub Actions call the same script. The workflow YAML
does not duplicate path knowledge.

Local classification includes the union of:

- committed changes from the merge base through `HEAD`;
- staged changes;
- unstaged tracked changes;
- untracked, non-ignored files;
- both old and new paths for renames.

GitHub classification uses the pull request merge base and complete candidate.
Missing bases, invalid output, ambiguous mappings, or unknown paths select `full`.
Changes to the classifier or its tests also select `full`.

Initial mappings are conceptually:

| Change | Depth |
| --- | --- |
| documentation, license, repository metadata | `fast` |
| shell/operator helpers | `fast` |
| playbooks, roles, active/frozen inventory, Ansible configuration | `ansible` |
| requirements, Mise/uv locks, CI workflow, classifier | `full` |
| unknown path | `full` |
| future explicitly mapped executable role | `molecule` |

The final mapping follows the implemented target tree and is covered by
table-driven tests. The classifier should remain understandable as a small script;
it must not become a generalized test catalog or dependency graph.

### GitHub Actions topology

One workflow always triggers for pull requests targeting the protected branch. It
must not use top-level path filters.

```text
classify
   ├── fast (always) ───────────┐
   ├── ansible (conditional) ───┤
   └── molecule (future only) ──┤
                                └── merge-gate (always)
```

Only `merge-gate` is required by branch protection. It runs with `always()`,
validates classifier output, and reconciles the result of every selected job. A
job skipped because it was not selected is acceptable; a selected job that is
skipped, cancelled, or failed makes the gate fail.

Workflow requirements:

- read-only permissions by default;
- SHA-pinned third-party actions;
- `persist-credentials: false` where applicable;
- cancellation of superseded runs for the same pull request;
- bounded job timeouts;
- no production credentials, Vault password, SSH key, kubeconfig, or host access;
- no cached passing result used as evidence;
- concise GitHub summary containing selected depth, reasons, commands, and
  per-group duration;
- no JUnit, Allure, permanent result catalog, or report artifact unless a future
  measured consumer justifies it.

### GitHub main protection

The workflow check and GitHub enforcement are separate controls. The workflow
publishes the stable `merge-gate` conclusion; an active repository Ruleset is the
authority that prevents `main` from changing without that conclusion.

The required live GitHub state is:

- repository pull-request merge methods enable squash and disable merge commits
  and rebase merging;
- one active repository Ruleset named `Protect main` targets only
  `refs/heads/main`;
- the Ruleset has no bypass actors;
- updates require a pull request with zero required approvals and squash as the
  only allowed merge method;
- additional approval for unattributed changes is required;
- `merge-gate` is required from GitHub Actions and the candidate must be current
  with `main`;
- linear history is required; and
- deletion and non-fast-forward updates are blocked.

The Ruleset requires a GitHub plan and repository visibility combination that
supports Rulesets. On the current account plan, public visibility is a protection
prerequisite. A transition to private visibility without a supporting plan is
protection drift and must fail the repository-owned check rather than silently
degrading to advisory CI.

Repository-owned GitHub protection tooling exposes three distinct Mise tasks:

```text
github-protection:check  read-only comparison of live GitHub state
github-protection:plan   read-only preview of the exact repair
github-protection:apply  guarded mutation followed by complete API read-back
```

The tooling resolves the GitHub Actions integration identifier from a recent
successful `merge-gate` check in the check suite associated with a successful
`ci.yml` workflow run instead of retaining a global identifier or accepting an
unrelated same-commit check. The run's check-suite identifier and URL and the
check's reported suite identifier must agree. `check` and `plan` never mutate
GitHub. `apply` requires an exact repository-scoped confirmation value and
explicit operator authorization for that invocation. It reads every API page
and refuses duplicate managed Rulesets, incomplete same-name ownership metadata,
or unexpected effective rules rather than guessing. Live protection checks
remain outside offline CI because they require authenticated
repository-administration access.

The implemented desired-state module is
`scripts/repository/github_protection.py`. It uses only the Python standard
library and the Mise-pinned GitHub CLI, keeps the discovered integration
identifier transient, and exposes pure protection tests through fast offline
validation. Apply recollects immediately before its planned writes and performs
a complete API read-back before reporting success. A write failure also triggers
post-write read-back so partial state is reported explicitly; the tool never
claims or attempts rollback.

Repository policy independently requires feature-branch publication, forbids
committing or pushing directly to `main`, requires explicit authorization for a
merge or auto-merge action, and requires inspecting the remote feature branch and
`origin/main` before each push. Local hooks may provide feedback, but the live
Ruleset—not an optional client-side hook—is the enforcement boundary.

### Canonical validation ownership

Each invariant runs once:

- yamllint owns general YAML syntax/style;
- ansible-lint owns Ansible semantics and syntax under the production profile;
- inventory validation owns inventory parsing and group/host resolution;
- ShellCheck and `bash -n` own shell semantics and parsing;
- wrapper contract tests own operator argument and failure behavior;
- the Vault fixture owns secret integration behavior;
- Gitleaks owns broad repository secret-pattern detection;
- actionlint and zizmor own GitHub workflow correctness and security analysis.

The explicit Ansible source builder includes tracked and untracked non-ignored
candidate files, excludes the exact encrypted inputs, rejects symlinks rather
than following aliases, propagates Git discovery failures, and rejects an empty
manifest. These fail-closed behaviors prevent implicit ansible-lint discovery or
an alternate path to encrypted inputs.

Pre-commit may remain a local convenience, but CI does not run a pre-commit hook
and then rerun the same validator separately. A separate blanket
`ansible-playbook --syntax-check` pass is added only if a measured coverage audit
shows playbooks not reached by ansible-lint.

### `fast` validation

The initial high-value set is:

- candidate whitespace validation over the pull-request merge-base range, or
  over the committed branch range plus cached and unstaged changes for local
  `ci:changed`; invalid Git state fails closed;
- YAML, JSON, and TOML parsing/style;
- `bash -n` and ShellCheck;
- executable-script and shebang consistency;
- Markdown lint and codespell;
- actionlint and zizmor;
- Apache-2.0 license presence/metadata;
- lock/configuration consistency;
- redacted Gitleaks coverage of the pull-request range on clean CI checkouts,
  the branch range plus working tree for local `ci:changed`, full history plus
  working tree for `ci`, and the working tree for standalone `validate:fast`;
- fast operator-wrapper unit contracts that require no Ansible target.

### `ansible` validation

The initial high-value set is:

- production-profile ansible-lint over the explicit tracked/cached plus
  untracked, non-ignored, existing Ansible source set, with warning/skip policy
  narrowed to justified exceptions and only the exact encrypted inventory inputs
  excluded;
- active and frozen inventory parse/graph validation;
- playbook discovery coverage;
- exact Galaxy dependency resolution into the repository-local path;
- operator-wrapper integration contracts;
- an ephemeral Ansible Vault fixture covering creation, encryption, decryption,
  and playbook consumption without production material.

### Scheduled/manual checks

The first implementation may include:

- a full-history plus current-working-tree Gitleaks scan;
- non-blocking link validation;
- full offline validation.

Dependency-update automation, live health checks, VM staging, and hardware
validation are outside pull-request CI.

### Measurement and activation

Before selective execution is accepted, the completed workflow runs the retained
`fast + ansible` suite repeatedly on one unchanged candidate while the classifier
records what it would have selected. Collect at least:

- five GitHub workflow samples;
- three local samples.

Record setup, validation, and reporting separately. Report minimum, median,
maximum, individual samples, and a provisional p95 with its small-sample
limitation. Enable selective execution only after classifier fixtures and observed
plans agree.

#### Implementation evidence

The shadow workflow was measured on candidate
`0d4d60721a92a0262daeb92299a74d319807d484`. Five attempts of the same
GitHub run passed:

| Attempt | Workflow | `fast` job | `ansible` job | Visible Mise cache |
| --- | ---: | ---: | ---: | --- |
| [1](https://github.com/supermorphic/homelab-playbook/actions/runs/33207743436/attempts/1) | 68s | 15s | 36s | classify/fast miss; Ansible/merge gate hit |
| [2](https://github.com/supermorphic/homelab-playbook/actions/runs/33207743436/attempts/2) | 61s | 11s | 31s | all jobs hit |
| [3](https://github.com/supermorphic/homelab-playbook/actions/runs/33207743436/attempts/3) | 55s | 14s | 23s | all jobs hit |
| [4](https://github.com/supermorphic/homelab-playbook/actions/runs/33207743436/attempts/4) | 51s | 12s | 21s | all jobs hit |
| [5](https://github.com/supermorphic/homelab-playbook/actions/runs/33207743436/attempts/5) | 60s | 11s | 30s | all jobs hit |

Workflow elapsed time uses the GitHub attempt creation and update timestamps.
The five samples had a 51s minimum, 60s median, 68s maximum, and 68s
nearest-rank provisional p95. Three unchanged-candidate local warm-cache runs
also passed at 11.11s, 11.18s, and 11.11s (11.11s minimum and median; 11.18s
maximum). Classifier fixtures passed and all observed paths selected the
expected depth. The 120-second objective passed, so selective execution was
activated. The small sample makes the p95 provisional.

If the ordinary gate misses the approximately two-minute target, optimize in this
order:

1. delete checks without a current invariant or consumer;
2. remove duplicate execution;
3. reduce dependency and process startup;
4. improve the retained implementation;
5. use bounded parallelism only after isolation is demonstrated;
6. remeasure the complete gate.

Do not introduce a generalized affected-target planner unless measured retained,
unrelated validation later dominates runtime.

## Staging boundary

VM and hardware staging are separate design concerns. This specification reserves
an inventory boundary for staging but does not choose a platform, host,
architecture, or runner. A future focused specification may introduce those
decisions. Container-based CI must not be described as equivalent to VM or
physical-hardware validation.

## Migration sequence

The implementation plan must preserve reviewable boundaries:

1. establish repository documentation, contribution/agent policy, license, and
   reproducible toolchain;
2. add contract tests around the existing operator behavior;
3. introduce the Mise playbook task and thin `run-playbook` alias;
4. establish deterministic `fast`, `ansible`, and `full` validation;
5. baseline and activate change-directed CI with the stable merge gate;
6. restructure active and frozen inventories with parse validation;
7. remove Argo CD, KSOPS, SOPS, and their orphaned helpers while retaining
   reusable Semaphore automation without a deployment target;
8. harden Ansible Vault and active-role failure/change semantics;
9. reconcile all documentation and run final validation.

The plan may reorder adjacent steps where tests require it, but must not mix
production deployment into repository modernization.

## Acceptance criteria

Issue #1 is complete when:

1. repository responsibilities and current consumers are accurately documented;
2. Apache License 2.0 replaces GPL-3.0 and repository documentation identifies it;
3. Argo CD, KSOPS, SOPS, and their orphaned helpers are removed without permanent
   absence checks, while Semaphore deployment and backup automation remains
   retained without an inventory host;
4. K3s inventory remains under `inventory/frozen/k3s`, its playbooks remain
   retained, and both receive static validation without CI execution;
5. active inventory is divided into production and staging boundaries and parses
   offline;
6. Ansible Vault is the only repository encryption format, with an ephemeral CI
   fixture and no production key material;
7. Mise and uv pin the controller toolchain and `mise run bootstrap` establishes
   exact dependencies;
8. `mise run playbook -- <playbook> <action> <inventory> [ansible-args...]` is the
   canonical operator command;
9. `run-playbook` is a tested ultra-thin alias and the stale `run.sh` interface is
   retired;
10. `AGENTS.md` provides compact vendor-neutral agent policy, including
    communication, Git, authority, security, lifecycle, validation, and
    completion boundaries, while `CLAUDE.md` imports it without duplicating
    shared policy;
11. GitHub Actions always runs `fast`, conditionally runs `ansible`, selects `full`
    for broad or ambiguous changes, and exposes one stable `merge-gate`;
12. `mise run ci:changed` classifies and executes local committed and working-tree
    changes, and `--dry-run` explains without executing;
13. initial CI measurement is recorded and the ordinary gate is evaluated against
    the approximately two-minute p95 target;
14. Molecule is documented as a future conditional convention but is not installed
    or implemented;
15. no validation contacts a live host or uses production secrets;
16. README examples match files and commands that actually exist;
17. all implementation-plan verification commands pass from a clean checkout;
18. repository policy forbids direct publication to `main`, and tracked guarded
    tooling can check, plan, and explicitly reconcile the exact `Protect main`
    Ruleset and squash-only merge settings;
19. local assurance uses the atomic `validate:fast` and `validate:ansible`
    lifecycle names without retaining aliases, while GitHub protection follows
    `check -> plan -> authorize -> confirm -> apply -> read-back`.
