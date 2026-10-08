# Specification 013: Forgejo CI and repository cutover

Issue: [#58](https://forgejo.infra.supermorphic.com/supermorphic/homelab-playbook/issues/58)

Status: proposed design; operator review and implementation remain pending.

## Purpose and starting state

Establish Forgejo Actions as the required remote validation service for
`homelab-playbook`. Keep Forgejo authoritative for Git, issues, pull requests,
reviews and merges. Retain GitHub as a nightly recovery copy with independent
access and an attended recovery procedure.

The operator confirmed that GitHub CI is no longer operational. Local CI is the
only operational validation path at the start of this work. The existing
[GitHub workflow](../../.github/workflows/ci.yml) is a configuration baseline,
not an available service or a fallback gate. Historical GitHub runs do not prove
the current candidate or the new Forgejo workflow.

Forgejo already contains imported collaboration history and subsequent native
work. Native code mirroring and the separate issue metadata mirror already
exist. Complete and validate those transitions without repeating the migration
or replacing established services.

[Specification 001](001-agentic-development-modernization.md#validation-architecture)
owns the validation architecture.
[Specification 002](002-multi-os-molecule-validation.md) owns Molecule execution.
[Specification 011](011-forgejo-runners.md) and issue #57 own the runner platform
and its accepted execution environment. Work on repository code and local
validation can proceed before #57 completes. Remote CI acceptance requires its
completed runner handoff.

## Selected approach and alternatives

Adapt the existing workflow into `.forgejo/workflows/ci.yml`. Reuse the repository
classifier, scenario selection, Mise commands and merge gate. Make only the
provider and runtime changes needed by Forgejo and the accepted #57 job image.
Validate those changes locally, then establish remote acceptance and required
checks in an attended sequence.

A single job running local `ci:changed` would reuse command execution but lose
the existing independent job results, conditional execution and matrix behavior.
A new CI framework would add a second validation contract. Neither is selected.
Parallel operation with GitHub CI is unavailable and is not a prerequisite.

The acceptance reference is the intended behavior encoded by the existing YAML,
repository commands, current tests and protection design. Record any deliberate
change to that reference explicitly. Do not preserve an obsolete hosting detail
or silently weaken validation to accommodate a provider difference.

## Workflow and execution contract

Preserve PR validation for `main`, manual full runs and the existing scheduled
full-run intent. Ordinary PR validation has no top-level path filter. Changes to
Forgejo workflows select full validation through the same classifier as local CI.
Keep workflow event handling separate from path selection.

Preserve the logical jobs for classification, always-required fast validation,
selected Ansible validation, selected Molecule matrix rows and the always-running
aggregate merge gate. Scenario selection and associated runtime experiments
remain repository-owned; workflow YAML must not become a second path map. A
service change selects its required scenarios and experiments. Shared changes
select every affected consumer. Full runs cover all registered scenarios and
their required runtime tests.

Use the label and supported job image delivered by #57. Do not request host
execution, nested privileged containers or a different runtime to make this
workflow pass. Bootstrap through the pinned Mise toolchain and repository
dependency locks. Preserve the accepted worker socket, workspace, scratch and
loopback contracts needed by Podman clients and Ansible container connections.

Source actions explicitly and pin them to reviewed immutable revisions.
Verify checkout, Node support, step outputs, conditional jobs, dynamic matrices,
cancellation and summaries on the accepted runner. Do not assume that GitHub
context names or permission declarations provide equivalent Forgejo behavior.
Use Forgejo-compatible artifact actions only where an actual evidence exchange
requires them. Shared writable caches remain outside this delivery.

Keep ordinary CI offline and secret-free with respect to infrastructure targets.
Package, action and image downloads use the accepted runner access policy.
Jobs receive no production credentials, operator age identities or deployment
authority. Checkout must not retain authentication in the workspace. Local
validation remains usable while Forgejo or its runners are unavailable.

Bound job timeouts and matrix concurrency by the accepted runner capacity and
watchdog limits. Cancelling a superseded PR run must not produce passing evidence
for either candidate. A queued or timed-out run is incomplete until its required
checks finish successfully.

## Candidate identity and merge decisions

Resolve the PR base and head from the Forgejo event and fetch the necessary Git
objects. Classify the merge-base-to-head change range. Each validation job must
prove that its checkout corresponds to the declared candidate. Never mix a
head revision with unrelated workflow or default-branch results.

Manual and scheduled runs validate their exact selected revision at full depth.
They do not satisfy an unrelated PR's required check. A new PR revision requires
new successful evidence. If the base branch advances, protection must require a
current candidate and validation for the resulting revision.

Preserve the merge gate's independent checks of classification, plan validity and
required job results. A job skipped because it was not selected is acceptable;
a selected job that is skipped, missing, failed or cancelled blocks the gate.
Require all planned matrix rows and their associated experiments. Establish
whether Forgejo's aggregate matrix result supplies that evidence before relying
on it. If evidence files are necessary, bind them to the repository, candidate,
actual workflow run and expected row; reject missing, duplicate and stale files.

Protect `main` through the stable gate context observed from the actual Forgejo
workflow. Require PR updates, current-branch validation and squash merging.
Preserve the intended restrictions on direct updates, deletion, history rewrites
and other merge methods. Evaluate check attribution and contributor permissions
on Forgejo; matching a context name alone is not proof of equivalent enforcement.
Do not enable the protection change until a successful candidate and the negative
acceptance cases have established the check's behavior.

Expose observational protection checking and a read-only reconciliation preview
through repository-owned Mise tasks. Any application of repository settings is
a separate guarded mutation with exact operator authorization and immediate
read-back. Use the configured `teacli` client; use its authenticated API operation
only for capabilities that its metadata commands do not expose. Keep actual
protection findings and detailed acceptance evidence out of public artifacts.

## Collaboration history and contributor access

Inventory existing imports and native Forgejo work before changing history.
Compare issues, PRs, comments, reviews, labels, milestones, attachments and source
links. Check representative records and count differences against the chosen
scope. Record lost attribution, unsupported imports and retained external assets
without claiming that the migration is lossless.

Preserve existing imports unless a specific defect justifies a reviewed repair.
Retain historical GitHub discussions that were not imported as accessible archive
history. Choose and record the accepted import/archive boundary with the operator.
Resolve remaining open PRs, contributor access and cross-links in Forgejo.
Normal collaboration and merging take place there.

GitHub's repository must remain writable by its approved code and metadata mirror
actors. A repository-wide GitHub archive flag would prevent those services from
updating it. Treat historical discussions as archive material without enabling
competing source changes or ordinary collaboration on the recovery destination.
The metadata service retains the ownership rules in
[Specification 012](012-forgejo-github-metadata-mirror.md).

## Nightly code mirror and independent recovery

Keep the native Forgejo push mirror to the existing GitHub repository. Follow
[Specification 010](010-forgejo-service.md#representative-nightly-github-mirror)
for scheduling around backup downtime, credential delivery, lag observation and
failure handling. The mirror must operate without CI runners. No synchronization
on push, automatic failover or reverse synchronization is introduced.

Accept the exact source, destination, visibility and branch/tag behavior. Review
force updates, deletions and rewrites with the destination's protections and
actor scope. Compare refs at an explicit recorded revision; concurrent source
changes and ordinary nightly lag must not be mistaken for corruption. Establish
whether this repository requires LFS data and prove transfer when it does.

Preserve independent GitHub access and workstation bootstrap. After Forgejo CI
acceptance, retain any GitHub recovery workflow only as an explicitly attended
manual operation. Ordinary mirror updates must not start duplicate CI or
deployment operations. This does not assume GitHub CI is available during the
implementation or an outage.

When Forgejo, its runners and NUC4 are unavailable, recover the code from GitHub:

1. Clone `https://github.com/supermorphic/homelab-playbook.git` to an isolated
   workstation checkout using independently available GitHub access.
2. Identify the intended branch/tag revision, its last mirror observation and
   the accepted recovery lag. Compare it with any independently retained revision
   evidence. Do not select a revision merely because it is the newest available.
3. Inspect that revision's toolchain requirements. Run `mise run bootstrap`, then
   its local validation through `mise run ci`, on a workstation with the supported
   rootless Podman runtime. Record missing dependencies or incomplete tests.
4. Obtain exact operator authorization before promoting the recovery repository
   or performing live infrastructure recovery. Reconcile source authority and
   subsequent changes before restoring one-way mirroring.

Test independent code retrieval and local bootstrap without requiring the
Forgejo service, runner or host. Coordinate full application recovery with
issue #9. Git refs and metadata shadows do not replace Forgejo backups for PR reviews,
settings, Actions history, attachments and other application state.

## Acceptance sequence

1. Review this design and the implementation plan. Preserve an explicit baseline
   revision and detailed evidence in the established evidence store.
2. Implement the provider adaptation and focused behavioral tests. Run local
   validation through the pinned Mise workflows. Required local CI depth cannot
   be reduced because the remote service is unavailable.
3. After #57 handoff, run separately authorized Forgejo acceptance for passing
   and failing candidates. Compare each decision with local execution and the
   configuration baseline, not a presumed concurrent GitHub run.
4. Exercise selective service changes, shared changes and full manual/scheduled
   coverage. Prove cancellation, missing required jobs or rows, stale candidate
   evidence and advancing-base behavior. Check real status contexts and merge
   eligibility without merging acceptance candidates.
5. Reconcile the exact Forgejo protection settings under separate authorization.
   Read them back and prove that failing or incomplete evidence blocks merging
   while a valid current candidate permits only the intended merge method.
6. Accept the collaboration import/archive record, contributor access, mirror
   behavior and independent recovery. Disable ordinary GitHub workflow triggers
   in the recovery copy and confirm that normal work remains in Forgejo.

Issue #58 is complete only when the workflow is operational, required protection
is accepted, the history boundary is agreed and independent recovery is proven.
Local success, a proposed workflow or runner provisioning alone is insufficient.
Do not describe this sequence as a handover from an operational GitHub CI service.

## Upstream references

- [Forgejo Actions differences from GitHub Actions](https://forgejo.org/docs/v15.0/user/actions/github-actions/)
- [Forgejo branch and tag protection](https://forgejo.org/docs/v15.0/user/repository/protection/)
- [Forgejo repository mirrors](https://forgejo.org/docs/v15.0/user/repo-mirror/)
