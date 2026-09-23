# Specification 011: Talos node maintenance

Status: design approved for implementation planning. Implementation and live
acceptance are pending.

Issue: [#55](https://github.com/supermorphic/homelab-playbook/issues/55).

## Purpose and scope

Move established-node `maintenance-check`, `maintenance-enter`,
`maintenance-exit`, and `reboot` into the canonical playbook gateway. The
workstation is the supported maintenance interface. Preserve the existing single-node behavior:
entry evacuates Longhorn replicas and shuts down the node; exit accepts an
already powered-on node. Physical power-on remains an operator action.

This repository owns and runs the lifecycle implementation. Issue #6 reuses it
internally in this repository. After cutover, retained cluster workflows have
no runtime dependency on migrated lifecycle code: no imports, command wrappers,
or automatic dispatch to this repository. Desired configuration, workload tests,
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
| `talos_test_run`, `talos_lease_holder` | Run identity and optional existing holder for the abrupt-loss test handoff only; never bypass live ownership checks |

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
simulated transaction as completed maintenance.

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
and outcome in bounded non-secret output. Resolve this repository's pinned tool
paths once. Prepare the verification command's own tool environment separately
and validate it before admission; invoking verification does not install tools
or credentials. The desired-data and verifier revision identifies that input,
not a lifecycle-code dependency consumed by another repository.

## State machine and transaction safeguards

| Observed state | Allowed behavior |
| --- | --- |
| All nodes healthy and schedulable; no lifecycle record | Check, enter, or reboot one explicitly selected node after fresh admission |
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

1. Acquire and renew the same Lease, or join the verified holder for the
   abrupt-loss test handoff below. Use recovery-aware admission that allows the
   target's expected cordon and record while checking the other nodes.
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
the last confirmed phase and require observation before another action.

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
unknown or active records exist; observational scenario steps may track their
expected record. They do not restore lifecycle-owned state or clear records.
Protocol fixtures prove interoperability without a shared runtime library.

## Implementation ownership and caller migration

The shared role contains the migrated node transaction, drain, capacity,
Longhorn maintenance, persisted-state, and recovery mechanics. Keep focused
shell/Python helpers as internal implementation files. Imports resolve relative
to those files. Public maintenance and reboot actions enter through the canonical
playbook gateway; issue #6 reuses the role internally. There is no exported
shell/Python lifecycle library or cross-repository lifecycle distribution contract.

Split mixed-purpose helpers by responsibility before removal. Generic Lease
coordination and standalone storage verification can stay with their existing
workflows. Move node containment, drain sequencing, owned-state restoration,
shutdown/reboot, and recovery acceptance orchestration here. Do not preserve
an old node transaction by renaming it as a generic helper.

| Existing functionality or caller | Required end state |
| --- | --- |
| Maintenance and reboot commands | Owned and run here; old commands and forwarding wrappers removed |
| Abrupt-loss containment/recovery mechanics | Owned here; the higher-level scenario uses the operator handoff below |
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

### Abrupt-loss scenario handoff

Move containment and recovery mechanics here. A retained scenario may observe
workloads, coordinate physical power actions, collect timing evidence, and hold
and renew the generic disruption Lease. It must remove automatic calls to the
old containment/recovery bridge and imports of migrated capacity helpers.

Use an explicit operator handoff between the scenario and the playbook. The
scenario reports the exact target, run identity, current holder, and observed
phase, then waits with a bounded deadline. It never starts a playbook process
or loads this repository to complete the handoff. The playbook independently
validates live state; a scenario report is not proof of admission or authority.

Provide `mise run playbook -- talos abrupt-loss-contain production` as the
containment stage of an explicitly authorized disruption test, under the
command lifecycle's controlled-test profile. It takes the same explicit target
and credential inputs plus the test run and optional existing Lease-holder
identity. Require `contain:<node>:<resolved-address>:<run>` confirmation and
reject check mode. Verify the live holder, absence of other lifecycle records, target
identity from approved inputs, survivor safety, and observed target loss before
persisting the schema 1 abrupt-loss record and cordon. Never submit a shutdown
or reboot from this action. Failure to establish target loss blocks containment.
If the scenario ended before containment, the operator can run this action
without a joined holder; it must acquire and renew the Lease normally before
mutation. A failed join never silently falls back to acquiring a different Lease.

Recovery uses `maintenance-exit` after operator power-on. For this test handoff,
it may join the verified test holder for the matching abrupt-loss record; ordinary
maintenance and reboot acquire their own Lease. The scenario keeps renewal alive
and suspends consequential actions while the operator runs a joined stage.
The playbook checks ownership before every mutation and does not release a Lease
owned by the waiting scenario. If the scenario has ended or renewal has failed,
exit can acquire the Lease normally once available and recover the persisted
record. There is no takeover based solely on a supplied holder string.

Before a planned power loss, require playbook-owned admission for the selected
node. The scenario performs its own current generic safety checks and waits for
the operator's deliberate power action; it does not treat a historical check as
current authority. If admission becomes stale, the operator repeats it before
power loss. The scenario accepts phase completion only after observing the
matching live containment or its guarded removal and healthy return. Timeout,
interruption, or recovery failure reports unresolved recovery and fails the test;
it must not silently skip recovery or remove containment. The operator can
complete recovery through the playbook even if the test process is unavailable.

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

Use preparation and recovery modes on this same command. Preparation validates
source and locally prepared dependencies without target API calls. Its result
cannot satisfy recovery acceptance. Recovery runs the complete observational
chain against the expected contained node. Neither mode fetches dependencies.

The narrow request/response contract is:

| Field or behavior | Requirement |
| --- | --- |
| Request identity | Version 1 request schema, explicit preparation/recovery mode, and a fresh per-invocation request ID |
| Target binding | Explicit node, approved node set/endpoints, and exact desired-data/verification revision |
| Authentication | Absolute Kubernetes and Talos config paths with explicit contexts, including Talos diagnostics used by Cilium postflight; no credential values in the request or response |
| Expected containment | Required for recovery: exact selected node and schema 1 record; permit that cordon only and reject another contained node |
| Observation | Re-read target binding and containment, run all required checks, and enforce a bounded deadline |
| Result | Exit zero only when the mode's checks pass; structured result echoes request ID, mode, target, contexts, revision, and stage outcomes; recovery requires source validation, Cilium, and foundation to pass |
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
2. Deliver and validate maintenance, public reboot, abrupt-loss mechanics,
   internal lifecycle reuse, migrated regressions, and the workstation guide
   here. Preserve current regression fixes and old schema 1 recovery behavior.
3. Prepare and validate the verification owner's observational boundary. Prove
   Cilium/foundation recovery acceptance without moving their implementations.
4. Untangle retained workflows from lifecycle-specific imports and dispatch.
   Keep independently owned generic coordination where needed. Update all
   tests, catalogs, publication inputs, and runbooks; prove Lease interoperability
   and the abrupt-loss operator handoff without installing playbook code there.
5. Once the playbook replacement and untangled workflows pass their required
   validation, remove the old maintenance and reboot commands, containment and
   recovery bridge, and migrated lifecycle implementation. Remove obsolete
   dependency/import declarations as part of that cutover. Generic protocol
   implementations and owned verification code remain with their workflows.
6. Do not replace code underneath a running operation. An interrupted operation
   may leave a schema 1 record for recovery through the playbook after cutover.
   Keep an explicit recovery route during transition; removing the old command
   must not strand its persisted records.
7. Verify the end state: lifecycle implementation exists and runs here; retained
   cluster workflows are untangled; old maintenance/reboot/lifecycle code is
   removed; no runtime lifecycle-code dependency remains between repositories.
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
- Abrupt-loss scenario handoff, stale/lost holder, unavailable test process,
  timeout with unresolved recovery, and playbook-owned containment/recovery.
- Cilium/foundation failures that prevent uncordon, the permitted target
  containment case through the actual transitive verification code, and invalid
  verification responses or unavailable verification commands.
- Retained workflows exercising their real entrypoints without playbook code
  available, including local coordination and observation of lifecycle records.

Use synthetic APIs, credentials, and infrastructure identifiers in CI. Run the
repository's required `mise run ci:changed` and the affected workflows' required
checks. Do not enroll credentials, operate a production node, change Semaphore,
or enable schedules as part of offline validation.
