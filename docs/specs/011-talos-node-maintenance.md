# Specification 011: Talos node maintenance

Status: implemented with offline acceptance. Live acceptance is pending explicit
operator authorization.

Issue: [#55](https://github.com/supermorphic/homelab-playbook/issues/55).

## Purpose and scope

Move established-node `maintenance-check`, `maintenance-enter`,
`maintenance-exit`, `reboot`, and the complete attended abrupt-loss scenario
into the canonical playbook gateway. The workstation is the supported
maintenance interface. Preserve the existing single-node behavior:
entry evacuates Longhorn replicas and shuts down the node; exit accepts an
already powered-on node. Physical power-on remains an operator action.

This repository owns and runs the lifecycle implementation. Issue #6 reuses it
internally in this repository. After cutover, retained cluster workflows have
no runtime dependency on migrated lifecycle code: no imports, command wrappers,
or automatic dispatch to this repository. Desired configuration, other workload tests,
storage resizing, exceptional bootstrap recovery, and Cilium/foundation
verification retain their existing ownership. Coordination across those
workflows uses compatible protocols with independently owned implementations.

[Issue #6](https://github.com/supermorphic/homelab-playbook/issues/6) and
Specification 010 own upgrade sequencing, Semaphore execution, upgrade
credentials, and upgrade verification. Specification 011 owns maintenance and
the internal lifecycle implementation boundary. Semaphore maintenance templates,
Semaphore credentials, autonomous credential renewal, and a credential issuer
spike are outside this delivery. They are not prerequisites for maintenance.
Design approval does not authorize live operations or credential enrollment.

## Baseline and design choice

Use the current maintenance implementation and regression tests. Recheck
changes before moving code; retain the relevant behavior and regression fixes
in the migration. Record inspected source provenance in the implementation notes.

Keep focused shell and Python helpers under `roles/talos_lifecycle/files/`.
Ansible validates inputs and invokes complete transactions. It must not split
Lease acquisition, renewal, mutation, and cleanup into separate task processes.
This preserves the existing transaction structure and allows internal reuse by
upgrades. Lifecycle-specific functionality moves here; retained workflows are
refactored away from its imports. Generic coordination, such as Lease handling
and refusing disruption while a lifecycle record exists, may be independently
implemented by each workflow owner. This does not duplicate node transactions.

Specification 010 exists on the separate `talos-upgrade-orchestration` branch.
Preserve its identifier and upgrade design. When integrating the two branches,
update its delivery boundary, ownership table, coordinated migration section,
and maintenance command references to this specification. Its shared role is a
component delivered by #55, not a second migration owned by #6. Its public
reboot playbook is delivered here together with maintenance.
Upgrade-specific records, orchestration, and Semaphore work remain with #6.
Integration of that branch is not a prerequisite for implementing maintenance.

## Operator interface

The canonical commands are:

```text
mise run playbook -- talos maintenance-check production
mise run playbook -- talos maintenance-enter production
mise run playbook -- talos maintenance-exit production
mise run playbook -- talos reboot production
mise run playbook -- talos abrupt-loss-test production
```

Each invocation also supplies one `talos_node` and explicit input references
through validated Ansible variables. The implementation adds these actions
under `playbooks/talos/` and documents complete examples in its README and the
maintenance guide. Commands run on the controller through Kubernetes and Talos
APIs. Talos nodes do not become SSH-managed inventory hosts.

| Input | Contract |
| --- | --- |
| `talos_node` | Exactly one node name in the approved three-node desired input; no lists, patterns, or implicit selection |
| `talos_kubeconfig`, `talos_kube_context` | Explicit absolute credential-file path and selected Kubernetes context |
| `talos_talosconfig`, `talos_talos_context` | Explicit absolute credential-file path and selected Talos context |
| `talos_source_dir`, `talos_source_revision` | Prepared, verified desired-data and verification checkout and full commit ID |
| `talos_confirmation` | Required only for mutation; bound to the resolved action and target as described below |
| `talos_test_confirmation` | Required for `abrupt-loss-test`: exactly `chaos:node-abrupt-loss`, in addition to target-bound `talos_confirmation` |
| `talos_evidence_dir` | Optional absolute private output root for abrupt-loss evidence; create a unique run-owned subdirectory |

Preserve gateway dependency verification. Production is the only supported
live inventory initially. Reject staging, frozen inventories, task selection,
custom inventory substitution, host limits, ambiguous targets, and arbitrary
executable overrides before target access. Validate the complete input set
before starting the transaction. Internal role callers must satisfy the same
target, credential, and transaction guards as the gateway.

`maintenance-check` is observational: no write Lease, cordon, annotations,
Longhorn changes, or disruptive API calls. Client-side drain simulation and
local temporary observations are allowed. It proves current admission, target
identity, node health and pressure, etcd health, survivor capacity, eviction
eligibility, networking, and storage safety. A passed check grants no authority
and does not replace entry's fresh checks.

Entry, exit, and reboot are purpose-specific mutating actions under the
[command lifecycle](../reference/repository-command-lifecycle.md). Reject
Ansible check mode for these actions before acquiring a Lease or contacting
mutation APIs; direct the operator to `maintenance-check`. Do not report a
simulated transaction as completed maintenance. `abrupt-loss-test` uses the
controlled-test profile and also rejects check mode.

## Workstation credentials and execution inputs

Consume the operator's existing authorized Kubernetes and Talos configuration
files by explicit absolute path. Do not discover credential locations, issue
credentials, copy administrative credentials, or change contexts in the original
files. Credential renewal and recovery use the existing operator procedures,
outside this workflow.

Use the supplied contexts consistently in every helper, including Cilium and
foundation verification. Match configured API endpoints and authenticated
node identity against the approved desired inputs before disruption. Reject
missing, expired, unauthorized, or mismatched access. Do not try another
context, identity, or insecure endpoint after failure. Configurations requiring
interactive authentication must be usable by the attending operator; this
delivery promises no unattended authentication or renewal.

Pass credential references rather than embedded values. Do not print rendered
configuration, tokens, certificates with private keys, or raw authentication
errors containing secrets. Use private temporary directories and remove only
run-owned files. Inventory protected inputs, if needed, use the existing SOPS
boundary; loading the lifecycle library never decrypts an inventory.

Prepare desired data before execution from an explicitly configured trusted
Git source at the supplied full commit. Bind the allowed origin in repository
configuration; an invocation cannot override that trust setting.
The workflow accepts a verified local checkout; it does not fetch during an
operation. Check origin, HEAD, tracked content, and the allowed input paths;
read selected files from the committed tree into a private immutable run
snapshot. Reject path escapes and untracked substitutes. The only executable
boundary into that checkout is the fixed read-only verification command defined
below. Do not load lifecycle helpers, Ansible plugins, or arbitrary callbacks
from it. Credentials are separate from desired-data preparation.

Record the automation revision, desired-data revision, action, node, phase,
outcome, and whether recovery is required in bounded non-secret output. Resolve
this repository's pinned tool paths once. Prepare the verification command's
own tool environment separately
and validate it before admission; invoking verification does not install tools
or credentials. The desired-data and verifier revision identifies that input,
not a lifecycle-code dependency consumed by another repository.

## State machine and transaction safeguards

| Observed state | Allowed behavior |
| --- | --- |
| All nodes healthy and schedulable; no lifecycle record | Check, enter, reboot, or start the attended abrupt-loss test for one explicitly selected node after fresh admission |
| Target cordoned with a supported matching record | Exit after power-on and recovery acceptance; fresh entry is refused |
| Target shut down with a maintenance record | Physical maintenance and operator power-on; exit does not power on |
| Partial entry or interrupted recovery with matching record | Exit restores only owned state and repeats acceptance |
| Unknown record, conflicting state, another contained node, or lost Lease | Stop further mutation and preserve containment |
| Acceptance completed and containment atomically removed | Normal scheduling; exit without a record is refused |

### Entry

1. Validate inputs and existing authority. Acquire and renew the common Lease.
2. Repeat full preflight under the Lease. Require confirmation
   `enter:<node>:<resolved-address>`; confirmation records intent, not authority.
3. Capture the target's Longhorn scheduling and eviction settings. Immediately
   before containment, recheck Lease ownership and disruption admission.
4. Persist the schema 1 maintenance record and cordon using optimistic
   concurrency. Apply only the recorded Longhorn maintenance values.
5. Capture drain inventory, evict with PodDisruptionBudget enforcement, wait for
   replacements, and prove no drainable workload remains. Preserve DaemonSet,
   local temporary storage, workload-owner, PVC/PV, and capacity policies.
6. Evacuate all target replicas. Repeat storage, survivor, etcd, target, and
   Lease checks immediately before submitting Talos shutdown to that one node.
7. Observe shutdown using the existing bounded checks. Preserve cordon and the
   lifecycle record for recovery; release the Lease without clearing them.

### Exit

1. Acquire and renew the same Lease. The abrupt-loss scenario reuses this
   recovery implementation internally under its own live Lease. Use
   recovery-aware admission that allows the target's expected cordon and record
   while checking the other nodes.
2. Read and validate the existing record. Require confirmation
   `accept:<node>:<kind>`. Support schema 1 maintenance, reboot, and abrupt-loss
   recovery, including operations begun before migration.
3. Wait for the node to be Ready with the exact expected containment. Verify
   its identity, Secure Boot, UKI, ready volumes, and STATE/EPHEMERAL encryption.
4. For maintenance, restore owned Longhorn scheduling and eviction values
   before waiting for replica convergence. An already restored value is a
   no-op; a value outside the recorded before/during pair is a conflict.
5. Prove storage convergence, etcd membership/health, Cilium, foundation health,
   and workload replacement acceptance when the transaction has that inventory.
6. Recheck Lease ownership, admission, and exact record immediately before the
   concurrency-guarded removal of the record and uncordon. Read back success.

### Reboot

Reboot is a public playbook operation backed by the same internal transaction
helpers. Acquire and renew the common Lease, perform fresh preflight, and
require `reboot:<node>:<resolved-address>` confirmation. Persist the schema 1
reboot record and cordon with concurrency guards, drain, and accept replacement
workloads. Apply the existing short-absence storage checks and retain reusable
replicas; full replica evacuation belongs to maintenance entry.

Repeat safety and holder checks immediately before the single-node Talos reboot.
Observe departure and return, then run the same guarded recovery acceptance and
final uncordon used by exit, including platform verification. Failure preserves
containment for `maintenance-exit`. Retire the old public reboot command during
cutover; do not retain a forwarding wrapper.

### Failure and interruption

Every wait is bounded. Lease renewal loss, authentication failure, API outage,
signal, or failed safety assertion stops further consequential mutation. A
completed shutdown is not retried merely because its response was lost. Report
the last confirmed phase from the fixed set `preflight-pending`,
`preflight-confirmed`, `containment-confirmed`, and `completed`; require
observation before another action. Keep command output private and expose only
the action, node, revisions, failed outcome, allowlisted phase, and recovery
need on failure. Report recovery need as unknown after `preflight-confirmed`
because containment and its phase record cannot be one atomic write.

After containment, failure preserves the record and cordon. Cleanup must not
uncordon, clear records, or automatically roll back partially completed
maintenance. Interrupted restoration is recoverable through exit's owned-value
comparison. Missing local drain snapshots do not erase durable recovery state;
recovery uses the current cluster acceptance checks and does not invent a lost
workload inventory. Keep primary failure, cleanup failure, and recovery-needed
status distinguishable. Release only this invocation's Lease ownership.

Kubernetes availability is required for coordination, draining, and return to
scheduling. A Kubernetes outage does not authorize bypassing the Lease. Recovery
outside these conditions follows the separately authorized recovery procedure.

## Lease and persistent-record compatibility

Keep the Lease in namespace `flux-system`, named `homelab-test-run-lock`, with
the current 90-second duration and 30-second renewal defaults. Preserve holder
verification, optimistic concurrency, expiry handling, and refusal to release
another holder's Lease. An expired Lease never clears persistent containment.
Joined operations use an existing holder only after checking current ownership;
a passed holder string alone is insufficient. The parent retains renewal and
reports renewal loss to joined work, which checks ownership before mutation.

Keep the Node annotation `homelab.supermorphic.com/node-lifecycle` and its exact
schema 1 validation:

- Reboot and abrupt loss contain only `schemaVersion: 1` and their `kind`.
- Maintenance additionally contains `longhorn.allowScheduling` and
  `longhorn.evictionRequested`. Each contains boolean `before` and `during`;
  maintenance's `during` values are `false` and `true`, respectively.
- Preserve strict supported fields and kinds. Do not invent identity fields
  in old records or silently reinterpret unknown records.
- Guard restoration and final removal against the actual record, resource
  version, target, and current Lease holder. Never clear another operation.

An operation begun by the old command can finish through the new exit command
without rewriting its record. Upgrade records and their acceptance belong to
issue #6; maintenance must refuse them with a clear recovery direction. That future
extension must preserve the common coordination protocol.

These are protocol and cutover requirements. The lifecycle implementation and
retained workflows may independently implement generic Lease operations and
read-only record admission. Retained workflows refuse new disruption while
unknown or active records exist. They do not restore lifecycle-owned state or
clear records.
Protocol fixtures prove interoperability without a shared runtime library.

## Implementation ownership and caller migration

The shared role contains the migrated node transaction, drain, capacity,
Longhorn maintenance, persisted-state, and recovery mechanics. Keep focused
shell/Python helpers as internal implementation files. Imports resolve relative
to those files. Public maintenance, reboot, and abrupt-loss test actions enter
through the canonical playbook gateway; issue #6 reuses the role internally. There is no exported
shell/Python lifecycle library or cross-repository lifecycle distribution contract.

Split mixed-purpose helpers by responsibility before removal. Generic Lease
coordination and standalone storage verification can stay with their existing
workflows. Move node containment, drain sequencing, owned-state restoration,
shutdown/reboot, and recovery acceptance orchestration here. Do not preserve
an old node transaction by renaming it as a generic helper.

| Existing functionality or caller | Required end state |
| --- | --- |
| Maintenance and reboot commands | Owned and run here; old commands and forwarding wrappers removed |
| Complete abrupt-loss scenario, containment/recovery, and regression tests | Owned and run here; old scenario, tests, catalog entries, and references removed after replacement acceptance |
| Capacity checks for node disruption | Lifecycle admission owned here; retained tests remove imports of migrated capacity code |
| Storage resize | Retain workflow; replace lifecycle imports with locally owned storage predicates and generic coordination |
| Etcd retry-join | Retain exceptional bootstrap workflow; use locally owned coordination and read-only record admission |
| Test campaigns, suites, and report publication | Retain workflows; use independently maintained generic Lease operations |
| Workload restore and test workflows | Retain workload behavior and local coordination; remove lifecycle imports |
| Standalone storage verification | Retain its owner and public behavior; separate it from maintenance-specific state restoration |
| Cilium/foundation verification | Keep implementations and source validators with their current owner; expose the read-only verification command below |

Update catalogs, fixtures, release/publication inputs, command references, and
runbooks together with each affected caller. Retained workflows must run their
required checks without this repository installed. No migrated lifecycle helper
is sourced, copied back, downloaded, or invoked from a retained workflow.

### Complete abrupt-loss scenario

Move the entire scenario, its required support code, and abrupt-loss regression
coverage here. Expose `mise run playbook -- talos abrupt-loss-test production`
as an attended controlled test. It owns baseline checks, physical-power prompts,
loss detection, containment, passive observation, external probes, recovery,
and evidence. There is no retained observer scenario or cross-repository stage
handoff. Remove the old scenario only after this replacement passes acceptance.

Require `talos_test_confirmation: chaos:node-abrupt-loss` and
`talos_confirmation: remove-power:<node>:<resolved-address>`. Generate the run
identity locally. Keep evidence in a private unique subdirectory of the supplied
root, defaulting to this checkout's `.tmp/talos-evidence/`; preserve it after
transaction cleanup. Neither confirmation grants live-operation authority. Before
acquiring the Lease or accessing mutation APIs, require a usable controlling
terminal. The gateway derives the terminal device from its own standard input
and passes that fixed internal path through local Ansible to the runtime. Public
inputs and inherited environment cannot select the path. Run-owned children use
dedicated process groups. The gateway records cancellation and waits; the
runtime normally signals and reaps the lifecycle group. The gateway wait is
bounded. Its fallback accepts only the current run's private random ownership
token and a still-live group leader whose process-group ID equals its PID; it
stops that lifecycle group before stopping Ansible. Each scenario bridge call
starts blocked, registers its separate group in the same private token-bound
supervisor record, and only then releases the child to execute the bridge
command. Recovery gets a 75-minute deadline: 30 minutes for node return, five
minutes for workload replacement, 30 minutes for the verifier, and ten minutes
of command and scheduling margin. Normal
completion, timeout, runtime cancellation, and the gateway fallback stop the
registered bridge group before releasing lifecycle ownership. This keeps prompts
on the gateway-bound terminal without competing supervisors. Prove
this path through the actual gateway with a synthetic pseudo-terminal. Reject
noninteractive execution before disruption; do not add an alternate launcher.

One transaction acquires and renews the common Lease for the full scenario.
Internal containment and recovery calls verify that live ownership, do not
start another renewal loop, and do not release the parent's Lease. Public inputs
cannot select an existing holder. Keep the following scenario behavior:

1. Run full current baseline verification, lifecycle admission, survivor capacity,
   and workload/storage safety checks. Capture workload owner UIDs, replica
   placement, and PVC/PV identity. Prepare monitoring before prompting for loss.
2. Prompt the operator to disconnect the selected node's electrical input.
   Repeat safety-critical admission and holder checks immediately before the
   prompt. Never cordon, drain, shut down, or reboot before physical loss.
3. Require all four loss signals: target Talos unavailable, target Kubernetes
   Node NotReady, target etcd member unavailable, and survivor quorum retained.
   Authentication failure alone is not proof. Default loss deadline is 180
   seconds. Recheck holder, target identity, survivor safety, and absence of
   conflicting records before atomically persisting the schema 1 abrupt-loss
   record and cordon. Insufficient evidence blocks containment.
4. Observe passively for 600 seconds, sampling every 5 seconds. Preserve exactly
   two Ready survivors, Cilium on both, surviving Longhorn replicas, PVC/PV
   identity, owner UIDs, workload placement/readiness, and timing evidence.
   Do not force pod deletion, volume detachment, or failover settings.
5. Monitor API readiness, DNS, and HTTPS throughout the disruption and recovery.
   Preserve the 60-second no-success threshold and failure evidence. Resolve
   endpoints from validated desired inputs; never use ambient contexts.
6. Prompt restoration of electrical input with firmware automatic power-on.
   Run internal guarded recovery acceptance and final uncordon under the same
   Lease. Preserve the platform verifier's full recovery checks.
7. Write private atomic phase/evidence records and a separate recovery result.
   Preserve the primary scenario failure even when recovery succeeds. Bound
   prompts, observation, recovery, and cleanup; stop and wait for probe children.

On a scenario assertion failure, request physical restoration and attempt guarded
recovery when loss and containment are established and authority is still valid.
Lease loss, authentication failure, or conflicting state stops further mutation.
EOF, prompt timeout, or interruption must report unresolved recovery without
hanging or hiding the primary error. Cleanup itself never clears containment.
If the scenario process dies after containment, ordinary `maintenance-exit` can
acquire the Lease once available and recover its schema 1 record without local
scenario files. Failure before proven containment reports uncontained loss;
do not fabricate a record or claim recovery. Keep this attended scenario out of
unattended campaigns and scheduled execution.

## Cilium and foundation verification boundary

Keep Cilium/foundation implementations, desired configuration, and their
source-validation prerequisites with the cluster verification owner. The role
orchestrates recovery and calls a supported observational command; it does not
copy those implementations into its files or expose lifecycle code to them.

Add the fixed public command `just kube recovery-verify <request-file>` with the
verification owner. It runs the existing source validation, Cilium verification,
foundation verification, and their transitive checks in that owner's prepared
checkout and tool environment. It must have no imports from the lifecycle role.
The name describes observation of recovery, not permission to repair a node.

The playbook invokes this command synchronously as a subprocess, using the
explicit verified checkout described above. The command path and operation are
fixed by the adapter; callers cannot supply executable paths or callbacks. No
fetch, dependency installation, credential enrollment, or remote mutation occurs
as part of verification. Missing or incompatible verification capability blocks
admission before disruption; loss of it during recovery preserves containment.

Use preparation, baseline, and recovery modes on this same command. Preparation
validates source and locally prepared dependencies without target API calls.
Baseline runs the complete observational chain before abrupt loss, requiring all
nodes healthy and schedulable with no lifecycle record. Recovery runs that chain
against the expected contained node. Results bind to their exact mode; preparation
or baseline cannot satisfy recovery acceptance. No mode fetches dependencies.

The narrow request/response contract is:

| Field or behavior | Requirement |
| --- | --- |
| Request identity | Version 1 request schema, explicit preparation/baseline/recovery mode, and a fresh per-invocation request ID |
| Target binding | Explicit node, approved node set/endpoints, and exact desired-data/verification revision |
| Authentication | Absolute Kubernetes and Talos config paths with explicit contexts, including Talos diagnostics used by Cilium postflight; no credential values in the request or response |
| Expected containment | Null for preparation/baseline; required for recovery: exact selected node and schema 1 record; permit that cordon only and reject another contained node |
| Observation | Re-read target binding and containment, run all required checks, and enforce a bounded deadline |
| Result | Exit zero only when the mode's checks pass; structured result echoes request ID, mode, target, contexts, revision, and stage outcomes; baseline and recovery require source validation, Cilium, and foundation to pass |
| Failure | Nonzero status for failed checks, invalid inputs, unsupported schema, missing capability, or cleanup failure; redact sensitive diagnostics |

Store request and response files privately. The playbook validates the direct
child's status and complete matching response. It does not accept a prewritten
success file, a cached result, or a caller-supplied Boolean as recovery evidence.
After verification, the lifecycle transaction rechecks the Lease and persisted
record immediately before final acceptance. The verifier cannot uncordon,
change records, renew or release the disruption Lease, or invoke lifecycle code.

Preserve these independent checks:

- Cilium HelmRelease and OCI status, observed generation, expected chart and
  values, node/component readiness, CLI status, and postflight networking.
- Flux readiness and foundation source validation.
- Certificate deployments, issuer and certificate readiness; MetalLB
  configuration; gateway and Envoy state; ExternalDNS configuration and
  provider revision metadata; DNS answers, trusted HTTPS, and echo workload.
- Recovery permits exactly the target's expected cordon and matching record.
  Other node, survivor, component, and networking assertions remain enforced.

The verification owner reads expected values and public trust material from
the selected desired revision and validates provider ciphertext revision
metadata without decrypting provider secrets or machine configuration. Preserve
explicit context selection through every transitive call.

Test the real verification command and transitive validators with fixture
responses, including the allowed contained target and a rejected second cordon.
The owner tests its adapter against the documented request/result schema; this
repository tests orchestration, malformed or mismatched results, and failure
containment. Record combined fixture evidence against both reviewed revisions
before cutover. This read-only verification dependency points out from the
playbook; it does not create a reverse dependency on migrated lifecycle code.

## Cutover and acceptance evidence

1. Approve this design and reconcile Specification 010's ownership references
   when integrating its branch. Keep implementation planning and source
   provenance under `.tmp/`; preserve the specification identifiers.
2. Deliver and validate maintenance, public reboot, the complete abrupt-loss test,
   internal lifecycle reuse, migrated regressions, and the workstation guide
   here. Preserve current regression fixes and old schema 1 recovery behavior.
3. Prepare and validate the verification owner's observational boundary. Prove
   Cilium/foundation recovery acceptance without moving their implementations.
4. Untangle retained workflows from lifecycle-specific imports and dispatch.
   Keep independently owned generic coordination where needed. Update all
   tests, catalogs, publication inputs, and runbooks; prove Lease interoperability
   without installing playbook code there.
5. Once the playbook replacement and untangled workflows pass their required
   validation, remove the old maintenance and reboot commands, containment and
   recovery bridge, complete old abrupt-loss scenario and its tests/catalog
   entries/references, and migrated lifecycle implementation. Remove obsolete
   dependency/import declarations as part of that cutover. Generic protocol
   implementations and owned verification code remain with their workflows.
6. Do not replace code underneath a running operation. An interrupted operation
   may leave a schema 1 record for recovery through the playbook after cutover.
   Keep an explicit recovery route during transition; removing the old command
   must not strand its persisted records.
7. Verify the end state: lifecycle implementation exists and runs here; retained
   cluster workflows are untangled; old maintenance/reboot/lifecycle code
   and the abrupt-loss scenario are removed; no runtime lifecycle-code dependency
   remains between repositories.
8. Record separately authorized live evidence with exact revisions and
   target/action. Coordinate recovery guidance with issue #9. Offline acceptance
   does not establish production behavior.

Required offline evidence exercises the real migrated helpers and gateway:

- Read-only check request traces, input/context rejection, single-node scope,
  mutation check-mode refusal, tool verification, and confirmation guards.
- Existing pressure/etcd parsing, eviction discovery, PVC/PV identity, detached
  volume safety, affinity/topology spread, workload-owner UID, and drain cases.
- Reboot's short-absence storage policy, observed restart, recovery acceptance,
  and interruption behavior through the canonical gateway.
- Independently implemented Lease contention and expiry behavior, renewal loss,
  verified joining, interruption after each mutation stage, and cleanup failures.
- Exact schema 1 fixtures, partial owned-state restoration, changed resource
  versions, foreign records, and recovery after command migration.
- Full abrupt-loss baseline, four-signal loss detection, passive workload/storage
  observation, external probes, attended gateway prompts, and separate primary
  test/recovery outcomes. Cover stale/lost holder, timeout, signals, unavailable
  scenario process, and recovery from its persisted record through exit.
- Cilium/foundation failures that prevent uncordon, the permitted target
  containment case through the actual transitive verification code, and invalid
  verification responses or unavailable verification commands.
- Retained workflows exercising their real entrypoints without playbook code
  available, including local coordination and observation of lifecycle records.

Use synthetic APIs, credentials, and infrastructure identifiers in CI. Run the
repository's required `mise run ci:changed` and the affected workflows' required
checks. Do not enroll credentials, operate a production node, change Semaphore,
or enable schedules as part of offline validation.
