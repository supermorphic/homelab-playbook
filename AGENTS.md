# Agent Instructions

Canonical, vendor-neutral rules for autonomous coding agents. `CLAUDE.md`
imports this file.

## Repository context

This repository manages off-cluster homelab hosts with Ansible. Production
contains active hosts, and staging is an explicit future boundary. Start with
the task, applicable policy, relevant source, and tests. Load only needed sections
of owning specifications; expand for cross-component constraints or uncertainty.

This root file is the sole agent-policy surface. Supporting documentation
supplies procedure, not competing instructions. Current repository policy and
source state take precedence over historical specifications, transient plans,
prior conversation context, and assumptions. Necessary tool adapters remain thin;
do not duplicate policy or introduce nested agent-policy files.

## Communication style

Communicate with the operator in clear, concrete English.

Apply principles inspired by ASD-STE100 Simplified Technical English:

- Use plain language when it preserves the same meaning.
- Avoid unnecessary jargon and abstract terminology.
- Prefer concrete descriptions of behavior over abstract labels.
- Reuse terminology established in the conversation or repository.
- Explain specialized or repository-specific terms when they may be unclear.
- Use concrete examples when they clarify an abstract concept.
- Present sequential steps in their logical order.
- Break up complex sentences when doing so improves clarity.
- Prefer active voice.
- Lead with the outcome and omit incidental process detail.
- Be concise without removing important technical content.

Write for a software engineer who may be unfamiliar with the specific tool,
subsystem, or domain. Do not rewrite literal APIs, identifiers, commands,
configuration fields, or quoted text solely to satisfy these style rules.

## Git and worktrees

- Never commit or push directly to `main`. Use an issue-backed feature branch.
  A runtime-managed worktree may operate at detached `HEAD`; preserve useful
  work on an appropriate branch before publication or worktree removal.
- Never merge or enable auto-merge without explicit operator authorization for
  that specific action. General or stale approval does not count.
- Use an isolated task worktree unless the operator explicitly authorizes work
  in the primary checkout. Treat the assigned worktree as the filesystem
  boundary for implementation files and inputs.
- Do not modify or remove another task's worktree. Preserve unrelated changes
  and stop when the current Git or worktree state is inconsistent or unsafe.
- Keep each commit limited to one coherent change. Split changes that can be
  reviewed or reverted independently.
- Before each push, fetch `origin` and inspect `origin/main` and the remote
  feature branch when it exists. Stop on unexpected remote commits. Rebase only
  a clean worktree, rerun required validation, and use `--force-with-lease` only
  when a reviewed rebase requires it.
- Do not use `git reset --hard`, `git clean -fd`, repository-wide
  `git checkout .` or `git restore .`, or an unconditional force-push.

## Authority boundaries

- Run established repository workflows through their pinned Mise tasks. Use
  `mise exec -- <tool> ...` when no task exists and the pinned version matters.
  Ordinary read-only filesystem and Git inspection may use standard commands.
- Proceed autonomously with safe, agent-owned work allowed by repository policy.
  Complete independent safe work before stopping for required operator action.
- Treat a confirmation value as an execution-intent guard, not as operator
  authorization. A `plan` is read-only and does not authorize its corresponding
  `apply`.
- Do not run `apply` or another consequential live or external mutation without
  explicit operator authorization for that exact target and action.
- Treat `check` and `verify` commands as observational toward their targets.
  Use a registered `test` workflow when evidence requires a bounded temporary
  mutation. Classify new or renamed commands with the
  [repository command lifecycle](docs/specs/001-agentic-development-modernization.md#repository-command-lifecycle).
- Never execute a playbook against production or staging without explicit
  operator direction for that target and action. Reconfirm the playbook,
  action, inventory, and extra arguments immediately before execution.
- Do not seek, copy, or use broader, administrative, or break-glass credentials
  as a workaround. Stop and identify the exact operator action required when a
  task crosses its approved authority boundary.

## Agent orchestration

- Use the least expensive capable model for delegated work. Reserve stronger
  models for architecture, cross-cutting judgment, difficult debugging, and
  reviews that require them.
- Delegate only when it provides context isolation, independent work,
  specialization, or safe parallelism. Do not spawn agents merely to repeat
  completed analysis or collect more opinions.
- Give delegated work a bounded objective and sufficient context. Use isolated
  task context when the runtime supports it.
- If the same implementation approach fails twice, diagnose the failure and
  change the approach, split the task, or escalate reasoning capability.
- Prefer focused tests, diffs, queries, and bounded logs over broad output when
  they provide the required evidence.
- Treat repeated context compaction, excessive retries, or rapidly expanding
  delegated work as signals to reassess the task.

## Secrets and credentials

- SOPS with age is the repository encryption boundary. Live age identities and
  their recovery passphrases remain outside the repository. Only public age
  recipients may be committed. Never store live private age identities in
  plaintext files; the macOS operator uses Keychain.
- Never decrypt, print, or inspect protected production, staging, or frozen
  inventory values. Do not expose
  plaintext credentials in agent output, repository artifacts, commits, issues,
  pull requests, or CI logs.
- Secret-related implementation may change templates, schemas, references, and
  non-secret metadata without exposing underlying values.
- Run direct SOPS operations through `mise run secrets:sops -- <sops-args...>`
  so SOPS cannot discover an unintended ambient identity.
- Use the repository's Gitleaks and staged-content safeguards. Do not embed key
  material in Mise configuration, helper scripts, fixtures, or CI. Ephemeral
  identities created inside the isolated `test:secrets` workflow are test data,
  not live identities.

## Public repository

- Treat committed files, branch names, commit messages, pull requests, review
  comments, generated artifacts, and CI logs as public and permanently
  recoverable.
- Do not publish actionable descriptions of unresolved security gaps, exploit
  paths, or remediation schedules. Report sensitive risks directly to the
  operator outside repository artifacts.
- Never commit live public IP addresses, hardware serial numbers, MAC addresses,
  credentials, or other unique infrastructure identifiers. Use documentation
  address ranges, synthetic identifiers, and marked placeholders.
- Treat exposed credentials or materially sensitive information as an
  operator-led security incident. Do not rely on deletion or history rewriting
  to retract disclosed information.

## Repository invariants

- Preserve `mise run playbook` as the canonical interface for repository
  playbook execution, including its dependency and target validation.
- Keep active inventories within `inventory/production` and
  `inventory/staging`.
- Preserve useful Ansible automation and repository-owned role overrides. Do
  not rewrite working roles or introduce a collection without a demonstrated
  repository need.
- Durable design specifications belong in `docs/specs/`; implementation plans,
  generated execution ledgers, review packages, and other transient tool state
  belong uncommitted under `.tmp/` to support execution, resumption, and
  handoff. Detailed evidence stays in established evidence stores; specs and
  comments are not execution diaries.
- A validation assertion must use an independent oracle or encode a current
  invariant. Remove obsolete executable checks instead of adding permanent
  forbidden-reference checks.
- Repeat safety-critical live preconditions immediately before consequential
  mutation. Do not treat an earlier plan or preflight as proof that target state
  is unchanged.

## Documentation

- Documentation is not a default deliverable. Most fixes, refactors, dependency
  updates, and implementation-only schema changes need no prose update.
- General documentation belongs only in root `README.md` and `docs/specs/`.
  Do not add nested READMEs, docs indexes, guides, references, runbooks,
  contribution manuals, archives, or substitute documentation categories.
  Root `AGENTS.md` is the policy exception; necessary adapters remain thin.
- Preserve functional skills, prompts, machine-consumed assets, fixtures, and
  required third-party licensing/provenance. Exceptions must be narrow and
  justified by actual consumers, not a way to disguise general documentation.
- Source, schemas, configuration, and command help own exact implementation
  facts. Do not copy versions, field catalogs, defaults, or command inventories
  into prose. Retain compatibility or migration reasoning that changes a contract.
- Specs own intent, rationale, boundaries, and guarantees. Investigate source/spec
  discrepancies; do not silently rewrite requirements to match code.
- These artifact and communication rules override skill defaults and older
  issue/spec instructions to create guides, committed plans, or exhaustive
  reports. Preserve the underlying requirements and essential recovery knowledge.

## Design lifecycle

- Use consecutive three-digit identifiers for durable specifications, such as
  `001-<name>.md`. Assign the next number after the highest existing
  specification; never reuse or renumber an identifier after merge.
- A specification is a living design record and may be updated during or after
  implementation to reflect the current validated design for its subject.
- Prefer updating an existing specification for iterative work on the same
  subject. Create a new numbered specification when the work introduces a
  distinct, explicitly agreed design subject, not merely because the earlier
  specification merged.
- Preserve original specifications, design rationale, and essential changes over
  time. Keep every existing spec and mark superseded decisions as historical.
  Trim redundant later implementation or status additions; do not compact the
  original design merely to reduce its size.
- When a plan corresponds to a numbered specification, use the same identifier
  and name where practical. Repository-defined artifact locations override tool
  or skill defaults.

## Validation

- Run `mise run bootstrap` after checkout or dependency changes.
- Use `mise run validate:fast` and `mise run validate:ansible` while iterating.
- Before claiming completion or opening or updating a pull request, run
  `mise run ci:changed`. The classifier selects the minimum required depth;
  agents may escalate to `mise run ci` but may not de-escalate its result.
- After a required rebase, rerun all validation affected by the new candidate.
- Pull-request validation is offline and secret-free. Live verification is
  separate operator evidence and is not CI evidence.

## Issue and PR communication

- Post only a new decision with rationale, material finding, changed blocker or
  dependency, answer, required operator request, or meaningful acceptance/closure
  outcome. Skip routine activity, unchanged status, and recaps available in the
  PR or CI. Ending a session does not require a comment.
- Routine comments normally use 3–6 short sentences and fewer than 150 words.
  This is a default, not a cap or template. Longer discussion must answer a real
  question and lead with the outcome. Include the blocker, responsible actor,
  next action, and evidence links only when relevant.
- Retain exact versions, IDs, errors, or measurements only to identify evidence,
  reproduce failures, explain decisions, or act safely. Distinguish implementation,
  deployment, and native acceptance; CI is not production proof.
- Link retrievable evidence and procedures instead of copying them. Vanished
  `.tmp/` files are not durable handoffs; without a retained artifact, keep only
  irreplaceable evidence in the comment. Preserve private evidence boundaries;
  do not create documents or services to accommodate verbosity.
- Keep issue bodies on current scope and acceptance. Read those and the latest
  relevant status first; retrieve older discussion only as needed. Refresh owned
  status text when useful, preserving decisions, authorization provenance, and
  meaningful outcomes. Do not move walls of text into bodies, specs, or collapsed
  comments.

## Completion

Report changed files, validation performed and its results, validation not
performed and why, remaining non-sensitive risks, and required operator actions.
Prefer diff and CI links over repeated inventories, transcripts, or boilerplate.
Report actionable security-sensitive risks outside repository artifacts.
