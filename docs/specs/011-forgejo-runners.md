# Specification 011: Forgejo runners

Issue: [#57 Deploy isolated Forgejo Runner infrastructure](https://forgejo.infra.supermorphic.com/supermorphic/homelab-playbook/issues/57)

Status: approved design; implementation is in progress. The operator selected
Podman containers, including for
Molecule, to keep the first deployment small. Runtime compatibility and live
acceptance have not yet been established.

## Purpose and scope

Provide Ansible-managed PR validation workers for `career-ops`,
`homelab-playbook`, and `homelab-talos`. Use NUC4 as the initial placement,
subject to an attended capacity and prerequisite check. Preserve the existing
Debian host baseline and rootless Podman foundation. Provision and recover from
the workstation through `mise run playbook`, independently of working CI.

This specification owns runner execution, registration, isolation and lifecycle.
Issue57 delivers an operational runner platform for all three repositories, with
supported job images, labels and tested Molecule/runtime capabilities. Completion
requires working registered runners and authorized foundation probe jobs; an
installed but untested runner is insufficient.

Issue57 can close when Ansible provisions the runners on NUC4 idempotently and
standalone verification is read-only; repository-scoped registration, job images,
isolation, resource limits, networking and cleanup work; the required Molecule
and container-runtime capabilities pass on the selected runtime; and updates,
credential rotation, retirement and recovery have supported operator procedures.

Consumer workflow migration, provider adapters, CI parity and required merge
checks belong to [homelab-playbook#58](https://forgejo.infra.supermorphic.com/supermorphic/homelab-playbook/issues/58),
[homelab-talos#292](https://forgejo.infra.supermorphic.com/supermorphic/homelab-talos/issues/292)
and [career-ops#208](https://forgejo.infra.supermorphic.com/supermorphic/career-ops/issues/208).
They consume the platform after runner acceptance through their own issue-backed
worktrees and repository policies. Their workflow migration is not a prerequisite
for completing issue57. The earlier combined delivery criterion was superseded
by the operator's ownership clarification on 2026-10-06.

Deployment workers, Kubernetes access, application credentials, autoscaling,
VM infrastructure and registry hosting are outside this delivery. Future trusted
deployment jobs require separate workers and authority. PR labels, branches and
workflow event names never grant that authority.

## Selected execution model

Use a fixed repository-scoped worker slot for each repository. A slot processes
one job at a time, with a disposable job container and rootless Podman storage.
The number of simultaneously enabled slots must fit the host's measured resource
budget; enable fewer slots if necessary rather than overcommitting NUC4.

Separate each slot's trusted runner controller from its execution account. Run
the upstream Forgejo Runner in a rootless container under the controller
identity. It communicates with the execution account's dedicated Podman Unix
socket. Jobs never receive the controller runtime socket or its credential
directory. Each repository has its own identities, registration and storage.
Extend the explicit allocations in
[the Podman foundation](006-podman-quadlet-foundation.md); inspect collisions
before selecting live IDs and subordinate ranges.

```text
Forgejo repository
  -> repository-scoped, one-job runner controller
      -> dedicated rootless worker Podman API
          -> disposable CI job container
          -> sibling Molecule, database and runtime-test containers
```

Use the Podman-native client inside jobs for repository tests. Forgejo Runner
uses Podman's Docker-compatible API to create job containers; this does not
introduce a Docker daemon or replace the repository's Podman commands.

The sibling model avoids placing another container engine inside the CI job.
The job can create and inspect containers only within its worker account's
runtime. Treat possession of that socket as full authority over the worker
account, not as a restricted container-management API. Controller credentials,
other workers and application state must remain outside that authority.

Use reviewed immutable runner and job-image references. A maintained job image
provides the OS packages required by all three repositories, Node for JavaScript
actions, Git, certificate trust and a compatible Podman client. Repository Mise
and dependency locks remain authoritative for project tools. Preserve the
existing Molecule test-image acquisition contract; runner-image pins do not
replace its maintained Debian base tags.

### Alternatives considered

Disposable VMs give a separate kernel but add a lifecycle the operator deferred.
Nested Podman adds subordinate-ID, storage and cgroup delegation requirements
without removing the shared-kernel boundary. Neither is the initial model.
If the sibling model cannot pass the required tests under the host baseline,
report the specific incompatibility and revisit the design; do not silently
introduce privileged containers, host execution or reduced test coverage.

## Workload compatibility

| Consumer | Required execution support | Consumer migration handoff (outside issue57) |
| --- | --- | --- |
| `homelab-playbook` | Mise bootstrap, offline validation, rootless Podman with cgroup v2, systemd-capable Debian Molecule containers, stock Forgejo and Semaphore runtime/recovery experiments | Preserve change classification, selected scenarios and the aggregate merge gate on Forgejo. |
| `career-ops` | Mise/Just, PostgreSQL compilation and test dependencies, selected validation domains, plan and evidence artifacts | Replace the GitHub-specific provider API integration and artifact actions while preserving exact-candidate and execution-evidence reconciliation. |
| `homelab-talos` | Mise/Just, selected validation groups, canonical results and generated reports | Adapt actions, provider context and artifact transport while preserving plan/result reconciliation and the merge gate. |

Molecule continues to use the current Ansible-native lifecycle and scenario
capability allowances from
[specification 002](002-multi-os-molecule-validation.md). Its application
scenarios prepare and verify service definitions; separate stock-container tests
prove application startup and recovery. Both forms of evidence remain required.
Do not interpret a passing Molecule scenario as replacing the runtime tests.

Establish these properties in a temporary, isolated runner slot on NUC4 before
installing the production runners. The operator selected NUC4 for both testing
and final placement because no separate test host is available. Disposable means
the test slot's accounts, storage, containers and other owned resources. Preserve
the host, Forgejo, PostgreSQL and unrelated services throughout the test and
cleanup. The original separate disposable-host test approach is superseded.

1. The actual Podman client, Ansible modules and connection plugin can build,
   inspect, exec, copy to and destroy containers through the worker API. The
   existing rootless/cgroup preflight reports the worker server's capabilities.
2. Checkout and temporary bind-mount sources use the same absolute paths in the
   job and API server's filesystem view. Set scratch directories explicitly;
   container-private `/tmp` paths are not assumed to exist at the server.
3. User mappings and `:U` mounts preserve the ownership semantics needed by the
   real fixtures. Cleanup handles subordinate ownership without changing
   ownership outside the disposable worker filesystem.
4. Published loopback ports are reachable by the test client. They refer to the
   worker's private network namespace, never the production host's loopback.
   Exercise the existing HTTP clients, including fixtures that construct
   `127.0.0.1` URLs, rather than proving only container-to-container DNS.
5. Systemd starts inside Molecule containers with the current unprivileged
   settings. Role idempotence, verification and failure cleanup all pass.
6. Real Forgejo workflow jobs exercise matrix outputs, conditional jobs,
   cancellation, artifacts and final gate results with the selected runner.

## Containment and resource ownership

Container workers share the host kernel. This is an explicitly selected
operating tradeoff, not VM-grade isolation. Keep kernels and the runtime patched,
and run only PR validation in these worker identities.

The trusted host configuration must bound the entire execution account,
including processes and containers created through its socket. Container flags
and workflow timeouts alone are insufficient. Use root-owned ancestor cgroups
for CPU, memory and process limits, and an external wall-clock watchdog. Account
for all sibling containers in these limits. Reserve capacity for Forgejo,
PostgreSQL, Semaphore and host management before enabling slots.

Give each execution account a disposable, size-bounded filesystem for its home,
container graph, workspace and scratch space. Use an independently bounded
filesystem or enforced filesystem quota; periodic pruning and free-space checks
are not disk limits. A quota exhaustion must fail that job without filling
application storage. Keep reusable controller state on separate private storage.

Run the worker account's user manager, Podman API, network helpers and jobs in
an administrator-owned private network namespace. Enforce its egress policy
outside worker control. Provide DNS, the authenticated Forgejo HTTPS endpoint,
and package/action/image download access. Deny unrelated private, loopback,
link-local and management endpoints, including over IPv6. Add explicit approved
exceptions only where a workload needs them. Network mode changes requested
through the worker API must remain within this outer namespace.

The job and worker runtime may share this private namespace to preserve fixture
loopback semantics. They must not share the production host network. Prove that
user services and child runtimes cannot leave the outer namespace or evade its
resource limits. Avoid overlapping independently controlled firewalls: install
only runner-owned rules and preserve the existing host policy.

Limit the worker's writable host view to disposable state and its private
runtime directory. Isolate temporary directories and shared memory as well as
the home directory. Worker-accessible mounts contain no controller credentials,
application data, SSH keys, SOPS identities or other runtime sockets. Grant the
controller access only to its worker socket and necessary paths; grant no reverse
access to controller state. Do not weaken the global AppArmor, seccomp, SSH or
sudo baseline to make this work.

## Job lifecycle and cleanup

Use upstream ephemeral registration and `forgejo-runner one-job`. The trusted
supervisor owns the following sequence; workflow code cannot authorize reuse:

1. Verify the slot's declared identity, empty process state, storage ownership,
   network policy and effective resource limits. Create fresh disposable storage.
2. Start the worker runtime and obtain one repository-scoped ephemeral identity.
   Start the controller with capacity for exactly one job.
3. Run the assigned job, allowing its required sibling test containers. Bound
   both idle waiting and execution time; an idle registration must not accumulate
   indefinitely during Forgejo outages or empty queues.
4. On completion, cancellation, timeout, controller failure or host recovery,
   prevent further polling and retire the exact owned registration as needed.
5. Stop the execution account's complete process tree and runtime. Remove its
   disposable filesystem and runtime state, including writable user units and
   container images. Recreate the slot only after independent cleanup checks.

Run cleanup from trusted administrator-owned code outside the worker account.
Validate the allocation marker, exact mount identity and account before acting;
do not follow worker-controlled paths or trust container labels as the sole
ownership proof. Never use an unrestricted system-wide prune. Cleanup failure
quarantines that slot and reports the original job result separately.

On host restart, treat any surviving worker state as incomplete. Clean it before
registering another runner. A lost Forgejo response is not permission to mint
unbounded registrations or reuse ambiguous state. Persist only minimal trusted
slot and registration metadata, without copying job contents into that record.

Start with no shared writable caches and no retained worker image graph. This
trades warm-start speed for a simple cleanup boundary. Forgejo artifacts carry
results between jobs, bound to the repository, candidate and actual run. Cache
optimization requires separate evidence that it cannot cross trust boundaries.

## Registration and workflow authority

Register at repository scope, not owner or instance scope. Enrollment authority
stays in a protected host controller component and never enters worker storage,
workflow secrets or job environments. Deliver secrets through private files
using the existing SOPS boundary, with logging and diffs suppressed. Do not put
tokens in process arguments, images or Quadlets.

Before selecting the registration implementation, prove the pinned server and
runner's supported API, token scope, one-job behavior and retirement path using
synthetic repositories. Use repository-limited enrollment authority. If the
supported API requires broader authority, stop for an explicit design decision;
do not reuse an operator administrator credential in the controller.

Labels select the execution environment; they are not authorization boundaries.
Every job the registration can receive must be safe with only PR-worker
authority, regardless of event or branch. Ordinary PR checks run without live
secrets. A future deployment worker must not share these identities, runtimes,
writable caches or registration scope in a way that lets PR code select it.

## Forgejo and consumer activation

Actions activation extends
[the Forgejo service design](010-forgejo-service.md). Its original delivery
disabled Actions; this specification proposes enabling it after worker and
storage acceptance. Retained Actions logs and artifacts must fit explicit
storage/retention budgets and be covered by backup and restore acceptance.
Keep them within the managed application data boundary unless a separately
reviewed storage change is needed. Runner scratch data is disposable and is not
application backup data.

Use Forgejo as the repository, issue, PR and validation authority. Consumer
workflows use explicitly sourced, reviewed actions compatible with Forgejo.
Adapt artifact actions and provider API code; do not assume GitHub API paths,
permission declarations or run-attempt identifiers have equivalent semantics.
Preserve each gate's independent evidence requirements and reject incomplete,
stale or wrong-candidate evidence. Provider differences must not turn a failure
or skipped required job into a passing gate.

Activation is attended and ordered:

1. Complete disposable compatibility and lifecycle acceptance, then install and
   observationally verify the authorized host configuration.
2. Validate Actions data backup and restore using synthetic data. Enable the
   server feature and selected repository settings only under exact authorization.
3. Enroll the repository-scoped controllers and run authorized foundation probe
   jobs for each repository. Confirm runner availability, supported runtime
   capabilities, failure handling and cleanup. This completes the runner-platform
   handoff when the acceptance gates below also pass.
4. Under the separate consumer migration issues, land reviewed workflows and
   perform representative CI parity acceptance. Observe actual status contexts
   and configure required checks through separately authorized settings changes.
   Prove failed required jobs block merging and valid complete runs satisfy the
   consumer's gate.

Do not retire prior CI enforcement until the replacement evidence and required
checks are accepted. This sequence does not authorize merging or live mutation.

## Provisioning, maintenance and recovery

Implement the runner role and playbook through the canonical gateway. Classify
provisioning, registration, rotation and retirement as mutations; standalone
`verify` observes installed state and registration without repair, registration
refresh or probe jobs. Bounded synthetic jobs belong in a registered `test`
workflow, following the
[command lifecycle](001-agentic-development-modernization.md#repository-command-lifecycle).

Provisioning validates the full desired allocation and repeats live safety
preconditions immediately before changes. A second unchanged invocation is
idempotent and does not replace an active worker. Routine image/configuration
updates stop new polling, drain the current job within a bounded time and replace
the slot only after cleanup. Forced cancellation is a distinct operator choice.

For credential rotation, stop new registrations for the affected repository,
drain or explicitly cancel its job, replace its protected enrollment input,
revoke the old authority and prove fresh registration. Never print either token.
Retirement additionally removes owned registrations and disables supervision;
account/UID retirement follows the foundation contract and is not automatic.

Recovery must work when Forgejo and all runners are unavailable. From the
workstation, use the managed host's existing administrative path to stop runner
supervision, inspect trusted slot metadata, terminate only the owned worker
processes and discard their disposable storage. Repair host prerequisites and
restore reviewed controller definitions and protected enrollment inputs. Once
Forgejo is available, reconcile stale registrations before enabling new polling.
Do not restore old job files or worker container graphs. Preserve unrelated
services throughout recovery. Implementation command help must expose these
bounded actions and their expected results before live delivery.

## Acceptance and implementation gates

First prove one temporary slot on NUC4 with the intended rootless runtime.
Observe host capacity and existing service health before setup. Declare bounded
resource budgets that reserve capacity for those services, and verify the
effective bounds before running exhaustion or failure tests. Abort the experiment
if those preconditions fail or existing service health changes; do not repair
unrelated services as part of the experiment. Recheck each resource's identity
before cleanup and preserve ambiguous resources for operator inspection.

After the temporary slot passes acceptance, remove its owned resources and verify
their absence and the existing services' health. Then provision the production
runner configuration through `mise run playbook` and perform standalone
verification and production runner acceptance. Test-slot removal does not wipe
or reprovision NUC4. Each live action remains subject to the repository's exact
target/action authorization and immediate playbook reconfirmation requirements.

Do not deploy the role until the sibling-runtime, network-namespace and cleanup
contracts above work together. An unsupported capability is a design blocker,
not authorization to lower the acceptance criteria.

Validation must cover:

- registration scope, credential separation and exact one-job admission;
- real job checkout, build tools, output/matrix handling and artifact round trips;
- all selected Molecule scenarios and the separate stock runtime/recovery tests;
- serial admission, aggregate resource enforcement and disk exhaustion;
- denied access to synthetic unrelated state and network endpoints, including
  attempts through user services and alternate container network modes;
- failure, cancellation, restart, interrupted registration and cleanup failure;
- absence of prior-job files, processes, writable configuration and containers
  before a second job, with unrelated fixture services remaining healthy;
- provisioning idempotence and verification that causes no target changes; and
- Forgejo backup/restore of representative Actions logs and artifacts.

Workstation unit and disposable job-runtime checks are offline evidence. The
NUC4 experiment uses synthetic jobs but is live-host evidence; it is not ordinary
PR CI and must not be reported as offline validation. Record the authorized test,
its resource budgets, independent observations and cleanup in the established
evidence store. The experiment receives no persistent application data or live
deployment credentials.

Production runner acceptance is separate operator evidence: host capacity and
effective containment, repository-scoped runner availability for all three
repositories, successful authorized foundation probe jobs, expected failure
behavior, cleanup and recovery. Consumer CI parity, workflow migration and merge
protection remain acceptance for their migration issues. No claim of deployment
readiness follows from documentation lint or unit tests. Record detailed runs in
established evidence stores rather than in this spec.

## Upstream references

- [Podman API and its authority boundary](https://docs.podman.io/en/latest/markdown/podman-system-service.1.html)
- [Forgejo Runner configuration](https://forgejo.org/docs/v15.0/admin/actions/configuration/)
- [Forgejo registration](https://forgejo.org/docs/v15.0/admin/actions/registration/)
- [Forgejo isolation and ephemeral execution](https://forgejo.org/docs/v15.0/admin/actions/security/)
- [Container runtime access from Actions](https://forgejo.org/docs/v15.0/admin/actions/docker-access/)
- [Forgejo and GitHub Actions differences](https://forgejo.org/docs/v15.0/user/actions/github-actions/)
