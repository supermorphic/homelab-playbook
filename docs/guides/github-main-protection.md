# GitHub main protection

## Protected delivery path

Every change to `main` follows this path:

```text
issue-backed feature branch -> current pull request -> merge-gate -> squash -> main
```

Do not commit or push directly to `main`, including as a protection test. Fetch
and inspect the remote feature branch and `origin/main` before each push. Merging
or enabling auto-merge requires explicit operator authority for that action.

The required Ruleset is supported only when the repository visibility and
account plan support GitHub Rulesets. On the current plan,
`supermorphic/homelab-playbook` must remain public. Private visibility without a
supporting plan is protection drift, not an acceptable advisory fallback.

## Exact desired state

Repository pull-request merge settings are:

- squash merge enabled;
- merge commits disabled; and
- rebase merging disabled.

One active repository Ruleset has these settings:

- name: `Protect main`;
- target: only `refs/heads/main`;
- bypass actors: none;
- pull requests required with zero approvals;
- squash is the only allowed merge method;
- stale-review dismissal, code-owner review, last-push approval, review-thread
  resolution, and required reviewers are all disabled or empty;
- `merge-gate` is required from GitHub Actions;
- the candidate must be current with `main`;
- enforcement applies when a branch is created;
- linear history is required; and
- deletion and non-fast-forward updates are blocked.

The GitHub Actions integration identifier is discovered from a recent successful
`merge-gate` check in the check suite belonging to a successful `ci.yml` workflow
run. The workflow run's suite identifier and URL must agree, and the accepted
check must report that same suite identifier. A same-commit check from another
workflow is not evidence. The integration identifier is never hardcoded or
stored.

## Inspect in the GitHub UI

With repository-administration access:

1. Open **Settings -> General -> Pull Requests** and confirm squash merging is
   enabled while merge commits and rebase merging are disabled.
2. Open **Settings -> Rules -> Rulesets -> Protect main**.
3. Confirm the Ruleset is active, repository-owned, has no bypass actors, and
   targets only `refs/heads/main`.
4. Compare every pull-request, required-status, linear-history, deletion, and
   non-fast-forward setting with the exact state above.
5. Confirm the required `merge-gate` source is GitHub Actions.

Treat a missing or inaccessible Ruleset as an access, visibility, or plan problem
until proven otherwise. Do not interpret a `403` or other visibility failure as
an absent Ruleset.

## Check and plan

Both observation commands require an authenticated GitHub CLI identity with the
necessary repository-administration visibility:

```bash
mise run github-protection:check
mise run github-protection:plan
```

`check` performs only API reads. It succeeds only when merge settings, the
managed Ruleset, effective protection, and the transient GitHub Actions source
match exactly.

`plan` also performs only API reads. It prints deterministic proposed actions
and the exact repository-bound apply guard. A plan is a preview, not a dry-run
of an authorized operation, and neither the plan nor its confirmation value
grants authority to change GitHub.

Do not add either command to offline validation or GitHub Actions. Their need for
live authenticated administration access makes their output operator evidence,
not CI evidence.

## Guarded apply

After receiving explicit operator authority for this repository and this apply,
use the exact guard printed by `plan`:

```bash
GITHUB_PROTECTION_CONFIRM=apply:github-protection:supermorphic/homelab-playbook \
  mise run github-protection:apply
```

The confirmation is deliberately exact and repository-bound, but it is only a
safety check. It does not replace explicit authority.

Immediately before mutation, `apply` recollects repository settings, every page
of managed `Protect main` Rulesets and applicable effective rules, and a current
GitHub Actions integration identifier. It stops rather than guessing when it
finds duplicate managed Rulesets, incomplete same-name ownership metadata,
unmanaged effective protection, unsupported visibility, or
malformed/inaccessible state. It changes only the merge settings and managed
Ruleset actions in that fresh plan.

After mutation, `apply` recollects the complete state through the API and fails
if any blocker or drift remains, including any missing or incomplete effective
rule. If one write succeeds and a later write fails, it still attempts complete
read-back and reports both the write failure and observed post-write state. It
does not claim or attempt rollback. A successful mutation without an exact
read-back is a failed apply.

## Verify through the normal path

Verify protection with an ordinary feature-branch pull request:

1. Publish the feature branch after fetching and inspecting its remote state and
   `origin/main`.
2. Open or update its pull request.
3. Confirm `merge-gate` runs and becomes the required successful check.
4. Update the branch if GitHub reports it is behind `main`.
5. With explicit merge authority, use squash merge and confirm the resulting
   commit appears on `main`.

Never probe protection by attempting a direct push, deletion, or force-push to
`main`.

## Recovery

For visibility or plan drift, restore public visibility or move to a plan that
supports equivalent Ruleset enforcement before applying. Re-run `check` and
`plan` after the prerequisite is restored.

For workflow drift, restore `ci.yml` with the stable `merge-gate` job and obtain
a successful normal pull-request run. The tool cannot plan a required status
source until it can derive the GitHub Actions integration identifier from that
evidence.

For merge-setting or managed-Ruleset drift, inspect the UI and the read-only plan.
Resolve duplicate managed Rulesets or unmanaged effective protection manually
under explicit operator authority; the tool refuses to choose an owner. Once
ownership is unambiguous, authorize a guarded apply, require complete read-back,
and verify with the normal pull-request path.
