# GitHub Main Protection

GitHub protects `main`, the repository's accepted integration boundary. The
repository tracks a deterministic checker and guarded repair mechanism;
GitHub's effective repository settings and active rules remain the enforcement
authority.

The intended path into `main` is:

```text
issue-backed feature branch
  ↓
pull request
  ↓
branch is current with main
  ↓
merge-gate succeeds for that candidate
  ↓
GitHub performs the allowed pull-request merge
  ↓
main
```

Direct pushes, force pushes, deletion of `main`, and merge methods other than
squash are not valid paths into `main`.

The control objective is:

> `refs/heads/main` can be updated only by GitHub completing a current,
> successful pull-request merge.

## GitHub plan and repository visibility

This repository is user-owned, currently public, and uses GitHub Rulesets to
enforce the `Protect main` contract. On the current GitHub account plan,
Rulesets are available for this repository only while it remains public. Public
visibility is therefore a protection prerequisite for the current environment,
not a universal requirement for GitHub repositories.

Moving the repository to a GitHub plan that supports Rulesets for private
repositories would also satisfy this prerequisite. Without that plan change,
making the repository private causes the Rulesets API to return `403`.

The two relevant cases are:

```text
current plan + public repository
  → Rulesets available
  → Protect main can be enforced
  → github-protection:check can verify it

current plan + private repository
  → Rulesets unavailable
  → GitHub returns HTTP 403
  → Protect main cannot be enforced through this mechanism
```

Treat that `403` as a failed protection prerequisite, not merely as a checker
limitation. Restore public visibility or move the repository to a plan that
supports Rulesets for private repositories before relying on this protection
model.

## Required state

Two GitHub configuration layers work together:

- **Repository merge settings** control which merge buttons GitHub can offer
  for pull requests throughout the repository. This repository enables squash
  merge and disables merge commits and rebase merge.
- The **`Protect main` Ruleset** controls how `main` may be updated. It requires
  a pull request, limits that pull request to squash merge, requires the current
  candidate to pass `merge-gate`, and protects the branch history.

Both layers must allow squash and reject the other merge methods. A mismatch can
either offer a merge method that policy does not allow or block every valid
merge method.

`.github/workflows/ci.yml` publishes the `merge-gate` check. A workflow cannot
create repository Rulesets or change repository merge settings, so the live
GitHub repository must also have:

- repository merge methods: squash enabled, merge commits and rebase disabled;
- one active repository Ruleset named `Protect main`;
- target: only `refs/heads/main`, with no excluded refs and no bypass actors;
- required pull request: zero approvals and squash as its only merge method;
- stale-review dismissal, Code Owner review, last-push approval, conversation
  resolution, and required reviewers: off or empty;
- additional approval for unattributed changes: on;
- required status check: `merge-gate` from GitHub Actions, with the branch
  required to be up to date;
- status checks enforced when a branch is created;
- linear history required; and
- deletion and force pushes blocked.

The tracked implementation is
[`scripts/repository/github_protection.py`](../../scripts/repository/github_protection.py).
It obtains the GitHub Actions integration ID from a recent successful
`merge-gate` check rather than retaining an installation-specific identifier as
a constant. The workflow run, check suite, check run, and GitHub Actions source
must all agree before that identifier is accepted.

## Where to inspect it in GitHub

Use these GitHub pages for a visual inspection:

1. **Settings → General → Pull Requests** shows the repository merge methods.
   **Allow squash merging** should be on; merge commits and rebase merging
   should be off.
2. **Settings → Rules → Rulesets → Protect main** shows the Ruleset target,
   bypass list, enforcement state, and individual rules.
3. **Settings → Branches → Branch protection rules** should have no legacy
   branch protection rule targeting `main`. GitHub layers legacy branch
   protection with Rulesets, so an old rule could add requirements not
   represented by `Protect main`.
4. **Actions → CI** shows workflow runs that produce the required `merge-gate`
   check.
5. A pull request targeting `main` shows the effective merge gate:
   `merge-gate` must pass, the branch must be current, and squash must be the
   only offered merge method.

On **Settings → Rules → Rulesets → Protect main**, confirm that the enforcement
status is **Active**, the target includes only `main`, the bypass list is empty,
and the branch rules have these values:

| Rule | Setting |
| --- | --- |
| Restrict creations | Off |
| Restrict updates | Off |
| Restrict deletions | On |
| Require linear history | On |
| Require deployments to succeed before merging | Off |
| Require signed commits | Off |
| Require a pull request before merging | On |
| Require status checks to pass before merging | On |
| Block force pushes | On |
| Require code scanning results | Off |
| Require code quality results | Off |
| Restrict code coverage | Off |
| Automatically request Copilot code review | Off |

Keep **Restrict updates** off. The pull-request rule rejects direct pushes. An
update restriction with no bypass actors would also prevent GitHub from
completing valid pull-request merges.

Under **Require a pull request before merging**, verify:

- required approvals: `0`;
- stale-review dismissal, Code Owner review, restricted review dismissal,
  last-push approval, and conversation resolution: off;
- required reviewers: none;
- additional approval for unattributed changes: on; and
- allowed merge methods: squash only.

The additional-approval setting has no practical effect while required
approvals remain `0`, but it is explicitly owned as `true` so GitHub's stored
state and the repository contract remain exact.

Under **Require status checks to pass before merging**, verify:

- required check: `merge-gate`;
- expected source: GitHub Actions;
- require branches to be up to date before merging: on; and
- do not require status checks on creation: off.

## Check the enforced contract

### `check`: read-only comparison; no changes are made

From a checkout with an authenticated GitHub CLI and repository Administration
access, run:

```bash
mise run github-protection:check
```

The command reads repository visibility and merge settings, finds every page of
repository-owned `Protect main` Rulesets, reads their complete definitions, and
resolves the expected GitHub Actions source from a recent successful `ci.yml`
workflow run. It also reads every page of active Ruleset rules that applies to
`main` and verifies that each one comes from the expected `Protect main`
Ruleset.

The integration discovery accepts only a completed, successful `merge-gate`
check from GitHub Actions in the check suite reported by a completed, successful
`ci.yml` workflow run. A same-commit check from another workflow or suite is not
accepted as evidence.

Administration access is needed because GitHub can omit Ruleset details such as
the bypass list without sufficient access. The command fails closed on
malformed or inaccessible responses, duplicate managed Rulesets, unexpected
effective protection, and any difference from the exact desired state.

GitHub exposes legacy branch protection through a separate API. The checker
does not read that API, so also confirm the absence of a legacy rule under
**Settings → Branches** as described above.

For this repository, a passing result is:

```text
GitHub main protection is exact for supermorphic/homelab-playbook.
```

A pass means that repository visibility, merge methods, the complete managed
Ruleset, and all active Ruleset rules applying to `main` match the repository
contract.

Run the check after changes to repository ownership, GitHub plan, repository
visibility, merge settings, Rulesets, or the `ci` workflow. The check stays
outside offline CI validation because live GitHub state requires authenticated
repository-administration access.

## Preview and repair drift

The three commands have different authority:

```text
check  → read-only comparison of live state
plan   → read-only preview of a proposed repair
apply  → mutation of live GitHub repository settings
```

### `plan`: read-only repair preview

```bash
mise run github-protection:plan
```

The plan reports whether it would change repository merge methods and create or
update `Protect main`. It makes no GitHub changes. Running or reviewing the plan
does not authorize apply. The plan fails closed when visibility, ownership, or
effective protection makes safe reconciliation ambiguous.

### `apply`: live repository-administration mutation

Applying the proposed repair changes live GitHub administration settings. An
operator may run it. An agent may run it only when the operator explicitly
authorizes that specific invocation and the required administrative credential.
After reviewing the plan, use the exact repository-scoped guard:

```bash
GITHUB_PROTECTION_CONFIRM=apply:github-protection:supermorphic/homelab-playbook \
  mise run github-protection:apply
```

Apply recollects live state immediately before mutation and is idempotent:

- when everything matches, it performs no mutation;
- when merge methods drift, it restores squash-only merging;
- when `Protect main` drifts, it updates that Ruleset;
- when `Protect main` was deleted, it recreates it and accepts GitHub's new ID;
  and
- after a mutation, it performs the same complete API read-back as `check`.

For safety, apply refuses to guess when duplicate `Protect main` Rulesets exist,
same-name ownership metadata is incomplete, repository visibility is
unsupported, or another Ruleset contributes effective rules to `main`. Inspect
and resolve those cases deliberately, then rerun the plan.

If one write succeeds and a later write fails, apply still attempts a complete
read-back and reports both the write failure and the observed post-write state.
It does not claim or attempt rollback. A successful mutation without an exact
read-back is a failed apply.

The confirmation value guards and scopes only this GitHub-protection apply
invocation. It does not authorize the apply, merging a pull request, or any
other repository mutation.

## Safe functional verification

Use a normal implementation pull request rather than probing the production
branch with a direct push:

1. Confirm the pull request starts CI and cannot merge while `merge-gate` is
   pending or failing.
2. Push a real follow-up commit to the feature branch. That commit creates a new
   candidate revision. Confirm it starts a new CI run and that a successful
   check on the older revision does not authorize the newer revision.
3. Confirm GitHub requires the branch to be current with `main` and offers only
   squash merge.
4. After the operator authorizes and performs that specific merge, confirm no
   push-to-`main` workflow reruns the same full validation.
5. Confirm the squash commit appears on `main`, then run
   `mise run github-protection:check` to verify the live protection contract.

Do not intentionally test a direct push, deletion, or force-push against `main`.
The complete API read-back, Ruleset insights, and normal pull-request behavior
are the safe verification seams.

GitHub Ruleset history supports recent inspection and rollback only; it is not
permanent audit storage. The tracked checker and guarded apply path provide the
repeatable verification and recovery mechanism if the live Ruleset is later
changed or deleted.

## Recovery

For visibility or plan drift, restore public visibility or move to a plan that
supports equivalent Ruleset enforcement before applying. Rerun `check` and
`plan` after restoring the prerequisite.

For workflow drift, restore `ci.yml` with the stable `merge-gate` job and obtain
a successful normal pull-request run. The tool cannot plan a required status
source until it can derive the GitHub Actions integration identifier from that
evidence.

For merge-setting or managed-Ruleset drift, inspect the GitHub UI and the
read-only plan. Resolve duplicate managed Rulesets, incomplete ownership
metadata, or unmanaged effective protection manually under explicit operator
authority. Once ownership is unambiguous, authorize a guarded apply, require
complete read-back, and verify through the normal pull-request path.
