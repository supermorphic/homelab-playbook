# Agent Instructions

This repository manages off-cluster homelab hosts with Ansible. Production is
active; staging is a future boundary. Root `AGENTS.md` alone owns execution
policy. Necessary adapters such as `CLAUDE.md` import/adapt it without duplicating
policy. Do not add nested policy files.

## Task inputs and artifacts

- Start with the task, applicable policy, relevant source, and tests. Load only
  needed sections of owning specs; expand for cross-component constraints or
  uncertainty. Investigate source/spec discrepancies; do not silently rewrite
  requirements to match code.
- Documentation is not a default deliverable. Most fixes, refactors, dependency
  updates, and implementation-only schema changes need no prose update.
- General documentation belongs only in root `README.md` and `docs/specs/`:
  no nested READMEs, docs index, guides, references, runbooks, contribution
  manuals, archives, or substitute categories. Functional skills, prompts,
  machine-consumed assets, fixtures, and required third-party licensing/provenance
  need narrow exceptions justified by actual consumers.
- Source, schemas, configuration, and command help own exact facts. Do not copy
  versions, field catalogs, defaults, or command inventories into prose.
  Retain compatibility/migration reasoning when it changes the contract.
- Specs own current intent, rationale, boundaries, and guarantees. Update the
  existing owner when its contract changes. Create a spec only for an explicitly
  agreed distinct durable subject, with the next consecutive three-digit ID.
  Never reuse or renumber merged IDs.
- Keep plans, review packages, execution ledgers, and temporary handoffs ignored
  under `.tmp/`; match the owning spec's ID where practical. Detailed evidence
  stays in established evidence stores. Specs and comments are not diaries.
- These artifact/communication rules override skill defaults and older issue/spec
  instructions. Preserve real requirements, not obsolete demands for guides,
  committed plans, or exhaustive reports.

## Git and worktrees

- Use an issue-backed feature branch and isolated task worktree. Never commit or
  push directly to `main`. Primary-checkout work requires explicit authorization.
  Preserve detached-HEAD work on a branch before publication or worktree removal.
- Keep implementation files and inputs inside the assigned worktree. Preserve
  unrelated changes and other tasks' worktrees; stop on unsafe/inconsistent Git
  state. Keep commits limited to independently reviewable, coherent changes.
- Before every push, fetch `origin` and inspect `origin/main` and the remote
  feature branch if present. Stop on unexpected remote commits. Rebase only a
  clean worktree and rerun affected validation; use `--force-with-lease` only
  after a reviewed rebase. Never use unconditional force-push, `git reset --hard`,
  `git clean -fd`, or repository-wide `git checkout .` / `git restore .`.
- Merge and auto-merge each require explicit authorization for that specific
  action. General or stale approval does not count.

## Execution boundaries

- Use pinned Mise tasks for established workflows; otherwise use
  `mise exec -- <tool> ...` when pins matter. Standard tools are allowed for
  read-only filesystem/Git inspection. Complete safe agent-owned work autonomously.
- Consequential live/external mutations require explicit authorization for the
  exact target and action. Confirmation values guard intent, not authorization;
  `plan` does not authorize `apply`. Repeat safety-critical live preconditions
  immediately before mutation; earlier preflight results can be stale.
- Classify new/renamed commands by effects: `check`/`verify` observe their target;
  registered `test` workflows may make bounded temporary changes.
  [Spec 001](docs/specs/001-agentic-development-modernization.md) owns the lifecycle.
- Preserve `mise run playbook`, including dependency and target validation.
  Never execute against production or staging without explicit operator direction;
  reconfirm playbook, action, inventory, and extra arguments immediately before
  execution. Active inventories remain in `inventory/production` and
  `inventory/staging`.
- Preserve useful automation and repository-owned overrides. Do not rewrite
  working roles or add collections without a demonstrated need.
- Never obtain broader, administrative, or break-glass credentials as a workaround.
  Stop at authority boundaries and identify the exact required operator action.

## Credentials and public data

- SOPS with age is the encryption boundary; only public recipients may be
  committed. Live identities and recovery passphrases stay outside the repository.
  Never store live private age identities in plaintext files; macOS uses Keychain.
- Never decrypt, print, or inspect protected production, staging, or frozen
  inventory. Secret-related work may change templates, schemas, references, and
  non-secret metadata only. Never expose plaintext credentials in output,
  artifacts, commits, issues, PRs, or CI logs.
- Run direct SOPS operations through `mise run secrets:sops -- <sops-args...>`
  to exclude ambient identities. Use Gitleaks and staged-content safeguards.
  Do not embed key material in Mise configuration, helpers, fixtures, or CI;
  disposable identities in isolated `test:secrets` are test data.
- Published files, branch names, messages, artifacts, and logs are public and
  permanently recoverable. Never commit live public IPs, serials, MAC addresses,
  credentials, or other unique infrastructure identifiers. Use documentation
  addresses, synthetic identifiers, and marked placeholders.
- Report actionable unresolved security gaps, exploit paths, and remediation
  schedules privately to the operator. Sensitive exposure is an operator-led
  incident; deletion or history rewriting cannot retract it.

## Validation and delegation

- Run `mise run bootstrap` after checkout/dependency changes. Iterate with
  `mise run validate:fast` and `mise run validate:ansible`. Before claiming
  completion or opening/updating a PR, run `mise run ci:changed`. Its classifier
  sets minimum depth; escalation to `mise run ci` is allowed, de-escalation is not.
  Rerun affected validation after a required rebase.
- Assertions need an independent oracle or current invariant. Remove obsolete
  checks instead of adding permanent forbidden-reference tests. PR validation
  is offline and secret-free; live verification is separate operator evidence.
- Delegate only for useful isolation, specialization, or independent/parallel work.
  Give bounded objectives and isolated context; use the least expensive capable
  model, reserving stronger models for difficult judgment. Do not repeat analysis
  for more opinions. Prefer focused tests, diffs, queries, and bounded logs.
- After two failures of one approach, diagnose and change it, split the task, or
  escalate. Reassess repeated compaction, retries, or expanding delegation.

## Communication and completion

Use concrete English, active voice, and established terminology. Lead with the
outcome; explain unfamiliar terms and order steps logically. Preserve literal
identifiers and commands.

- Comment on issues/PRs only for a new decision with rationale, material finding,
  changed blocker/dependency, answer, required operator request, or meaningful
  acceptance/closure outcome. Skip routine activity, unchanged status, and recaps
  available in the PR/CI. Session end requires no comment.
- Routine comments normally use 3–6 short sentences and fewer than 150 words:
  a default, not a cap/template. Longer discussions must answer a real question
  and lead with a summary. Add blockers, responsible actor, next action, and
  evidence links only when relevant.
- Retain exact versions, IDs, errors, or measurements only to locate evidence,
  reproduce failures, explain decisions, or act safely. Distinguish implementation,
  deployment, and native acceptance; CI is not production proof.
- Link retrievable evidence/procedures instead of copying them. Vanished `.tmp/`
  files are not durable handoffs; without a retained artifact, keep the smallest
  irreplaceable evidence in the comment. Preserve private evidence boundaries;
  do not create documents or services to accommodate verbosity.
- Keep issue bodies on current scope/acceptance. Read that and latest relevant
  status first; retrieve older discussion only as needed. Refresh owned status
  text when useful; preserve decisions, authorization provenance, and meaningful
  outcomes. Do not move walls of text into bodies, specs, or collapsed comments.
- Completion responses identify changed files, relevant validation/results,
  unperformed validation and why, remaining risks, and required operator actions.
  Prefer diff/CI links over repeated inventories, transcripts, or boilerplate.
  Report sensitive risks privately.
