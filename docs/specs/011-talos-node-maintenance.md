# Specification 011: Talos node maintenance

Status: proposed for operator review. Implementation and live acceptance are pending.

Issue: [#55](https://github.com/supermorphic/homelab-playbook/issues/55).

## Purpose and scope

Move established-node `maintenance-check`, `maintenance-enter`, and
`maintenance-exit` into the canonical playbook gateway. The workstation is the
supported maintenance interface. Preserve the existing single-node behavior:
entry evacuates Longhorn replicas and shuts down the node; exit accepts an
already powered-on node. Physical power-on remains an operator action.

This repository owns one shared lifecycle implementation. Existing callers and
future upgrade orchestration consume that implementation. Desired configuration,
workload tests, storage resizing, and exceptional bootstrap recovery retain
their existing ownership.

[Issue #6](https://github.com/supermorphic/homelab-playbook/issues/6) and
Specification 010 own upgrade sequencing, Semaphore execution, upgrade
credentials, and upgrade verification. Specification 011 owns maintenance and
the shared implementation boundary. Semaphore maintenance templates,
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
This preserves the existing transaction structure and avoids a second
implementation for upgrades. Moving every consumer here would unnecessarily
expand maintenance into workload testing and storage operations. A third
package repository would add an owner without a demonstrated need.

Specification 010 exists on the separate `talos-upgrade-orchestration` branch.
Preserve its identifier and upgrade design. When integrating the two branches,
update its delivery boundary, ownership table, coordinated migration section,
and maintenance command references to this specification. Its shared role is a
dependency delivered by #55, not a second migration owned by #6. Its reference
to a public reboot playbook is replaced by the retained public wrapper below.
Upgrade-specific records, orchestration, and Semaphore work remain with #6.
Integration of that branch is not a prerequisite for implementing maintenance.

## Operator interface

The canonical commands are:

```text
mise run playbook -- talos maintenance-check production
mise run playbook -- talos maintenance-enter production
mise run playbook -- talos maintenance-exit production
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
| `talos_source_dir`, `talos_source_revision` | Prepared, verified cluster desired-data checkout and full commit ID |
| `talos_confirmation` | Required only for mutation; bound to the resolved action and target as described below |

Preserve gateway dependency verification. Production is the only supported
live inventory initially. Reject staging, frozen inventories, task selection,
custom inventory substitution, host limits, ambiguous targets, and arbitrary
executable overrides before target access. Validate the complete input set
before starting the transaction. Direct library callers must satisfy the same
target, credential, and transaction guards as the gateway.

`maintenance-check` is observational: no write Lease, cordon, annotations,
Longhorn changes, or disruptive API calls. Client-side drain simulation and
local temporary observations are allowed. It proves current admission, target
identity, node health and pressure, etcd health, survivor capacity, eviction
eligibility, networking, and storage safety. A passed check grants no authority
and does not replace entry's fresh checks.

Entry and exit are purpose-specific mutating actions under the
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
snapshot. Reject path escapes and untracked substitutes. No cluster script,
Mise configuration, Ansible plugin, or arbitrary callback is executed from that
checkout. Credentials are separate from desired-data preparation.

Record the automation revision, desired-data revision, action, node, phase,
and outcome in bounded non-secret output. Resolve pinned tool paths once;
library consumers do not activate the owner's Mise configuration as a side
effect. Tool versions required by the exported interface are declared and
validated during dependency preparation and admission.

## State machine and transaction safeguards

| Observed state | Allowed behavior |
| --- | --- |
| All nodes healthy and schedulable; no lifecycle record | Check, or enter for one explicitly selected node after fresh admission |
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

1. Acquire and renew the same Lease. Use recovery-aware admission that allows
   the target's expected cordon and record while checking the other nodes.
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
Child consumers join an existing holder only after checking current ownership;
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

## Shared implementation and supported consumers

The shared role contains the migrated `node/{lifecycle,common,drain,longhorn,
recovery}.sh`, `node/capacity.py`, and the Lease, lifecycle-state, and shared
storage-verification libraries. Move the abrupt-loss bridge's transaction
adapter here as well. Preserve helper behavior before targeted adaptation.
Imports resolve relative to their own files, never the caller's working directory.

Publish `roles/talos_lifecycle/files/interface-v1.json` with interface major,
exported entrypoints, their argument/environment contracts, supported record
schemas, and required tools. Version 1 supports these boundaries:

| Consumer boundary | Supported behavior |
| --- | --- |
| `node/lifecycle.sh` | Complete check, enter, exit, and reboot transactions, with explicit action, node, credential references, and validated execution inputs |
| `node/abrupt-loss-bridge.sh` | `contain` or `recover` for one node under a verified existing Lease holder |
| `lib/lease.sh` | Existing acquire, renew, verify-holder, start/stop renewal, and release functions with their current arguments |
| `lib/node-lifecycle-state.sh` | Record validation and disruption admission, including the expected recovery target |
| `node/common.sh` and `lib/longhorn-verification.sh` | Shared target/admission and storage predicates used by retained resize and verification callers |
| `node/capacity.py` | Existing capacity analysis entrypoint used by preflight and disruption testing |
| `verify/` | Shared Cilium/foundation acceptance and their extracted validation dependencies, with explicit desired data and expected containment |

Document and test the precise exported signatures in that manifest before the
first consumer pin is published. Preserve existing coordination signatures;
adapt controller/desired-data initialization once at each entry boundary.
Private transaction helpers are not a supported consumer import. Consumers
cannot substitute shell callbacks, tool overrides, or guard-disabling flags.
The gateway and wrappers use the same input validation and transaction code.

| Existing consumer | Destination and change |
| --- | --- |
| Maintenance commands | Retire after the playbook replacement is available and validated |
| Reboot command | Keep the public command as a thin wrapper over the shared reboot transaction |
| Abrupt-loss scenario | Keep the live scenario and physical-power interaction with its existing owner; call the shared bridge and capacity interface |
| Storage resize | Keep resize orchestration; adapt shared locking, admission, and storage predicates |
| Etcd retry-join | Keep exceptional bootstrap workflow; adapt shared Lease and admission imports |
| Test campaigns and suites | Keep test coordination; retain verified existing-holder joining |
| Report publication | Keep publication; update shared coordination and dependency input lists |
| Workload restore and test workflows | Keep workload behavior; adapt common Lease imports |
| Storage verification workflows | Keep workflow; consume the shared storage-verification predicates |
| Cilium/foundation verification commands | Retain public commands as wrappers around the extracted shared verification implementation |

Update catalogs, fixture imports, release/publication inputs, command references,
and runbooks together with executable callers. Issue #6 uses this same role's
preparation, storage, Lease, and acceptance components; it adds upgrade policy
without copying the transaction implementation. Reboot's public command need
not move for its implementation to have one owner.

## Transitive Cilium and foundation acceptance

Preserve the assertions behind the existing `cilium-verify` and
`foundation-verify` calls, including source-validation prerequisites. Extract
their required verification logic into the shared owner and replace existing
commands with wrappers. Do not invoke an arbitrary command-runner boundary
or accept a caller-supplied success result as recovery evidence.

The shared source adapter reads allowed public inputs from the pinned cluster
data snapshot: node/endpoint mapping, Cilium values and chart expectations,
Flux and foundation manifests, networking constants, public trust material,
and provider ciphertext revision metadata. Move required source-validation
predicates with the verifier. Validate those inputs before target mutation;
do not decrypt provider secrets or machine configuration. Preserve desired-data
ownership and derive expected values from that exact revision.

Preserve these independent checks:

- Cilium HelmRelease and OCI status, observed generation, expected chart and
  values, node/component readiness, CLI status, and postflight networking.
- Flux readiness and foundation source validation.
- Certificate deployments, issuer and certificate readiness; MetalLB
  configuration; gateway and Envoy state; ExternalDNS configuration and
  provider revision metadata; DNS answers, trusted HTTPS, and echo workload.
- Recovery permits exactly the target's expected cordon and matching record.
  Other node, survivor, component, and networking assertions remain enforced.

Test actual transitive validators with fixture responses, including a contained
target that passes recovery and an unexpected second cordon that fails. A stub
at the command-runner boundary does not prove acceptance parity.

## Immutable cross-repository distribution

External consumers use this repository as a Git submodule at
`vendor/homelab-playbook`. Its `.gitmodules` fixes the source URL to
`https://github.com/supermorphic/homelab-playbook.git`; the reviewed parent
commit's gitlink selects the exact dependency commit. The first pin must refer
to a published, validated implementation commit, not an invented future SHA.
This uses Git's [submodule model](https://git-scm.com/docs/gitsubmodules).

Consumers add dependency preparation to their registered bootstrap workflow.
Preparation checks the declared source and local URL overrides, uses checkout
mode at the gitlink, rejects custom update commands and branch-following
options, and never overwrites a modified dependency. Only explicit preparation
may fetch a missing commit. Runtime never fetches, follows a branch, initializes
a submodule, installs dependencies, or falls back to a sibling checkout.

Before loading helpers, verify the parent gitlink against its reviewed revision,
the dependency's origin and exact HEAD, unchanged tracked contents, path
containment, and absence of unexpected executable inputs in the export tree.
Reject symlinks escaping that tree and incompatible interface/tool versions.
The bootstrap and operational resolver use the same verification rules.
Consumers load only the declared export surface, not dependency inventories,
Mise activation, provisioning, or secret-loading machinery.

A prepared checkout works without GitHub. Recovery material includes the
verified dependency checkout or a Git bundle containing the pinned commit;
offline preparation applies the same content and origin contract. The commit
hash identifies content; approval comes from the reviewed parent revision.
Do not change dependencies in place during an active operation.

Updates change the gitlink, wrappers, tests, and publication inputs together.
Rollback selects a previously validated commit only when it supports every
record the newer version may have written. Code rollback never clears live
state. Unsupported records require a compatible forward recovery version.

## Cutover and acceptance evidence

1. Approve this design and reconcile Specification 010's ownership
   references when integrating its branch. Keep implementation planning under
   `.tmp/` and preserve the existing specification identifiers.
2. Deliver the shared implementation, workstation gateway, transitive checks,
   migrated regressions, and operator guide in #55. Recheck current maintenance
   fixes and record source provenance for migrated code and fixtures.
3. Validate and publish a fixed implementation commit. During preparation,
   keep the old released command available; make shared fixes in the new owner
   and incorporate any necessary transition fixes before the consumer cutover.
4. Prepare each consumer's submodule pin and migrate its retained callers,
   wrappers, tests, catalogs, and publication inputs. Prove old/new Lease
   contention and recovery of old records against the actual shared code.
5. After owner and consumer validation passes and the documented workstation
   replacement is available, retire old maintenance entrypoints and migrated
   helper bodies in the coordinated cutover. Leave one maintained source;
   retained public commands are adapters. Never replace code underneath a
   running operation. A pre-cutover recovery record can remain and be accepted
   later through the supported replacement.
6. Record any separately authorized live maintenance evidence with the exact
   revisions and target/action. Coordinate recovery guidance with issue #9.
   Offline acceptance does not establish production behavior.

Required offline evidence exercises the real migrated helpers and gateway:

- Read-only check request traces, input/context rejection, single-node scope,
  mutation check-mode refusal, dependency verification, and confirmation guards.
- Existing pressure/etcd parsing, eviction discovery, PVC/PV identity, detached
  volume safety, affinity/topology spread, workload-owner UID, and drain cases.
- Lease contention, renewal loss, verified child joining, expiry without state
  clearing, interruption after each mutation stage, and cleanup failures.
- Exact schema 1 fixtures, partial owned-state restoration, changed resource
  versions, foreign records, and recovery after command migration.
- Cilium/foundation failures that prevent uncordon, plus the permitted target
  containment case through the actual transitive verification code.
- Fresh dependency preparation, cached offline execution, missing or wrong
  commits, wrong origin, modified exports, incompatible versions, and execution
  from an unrelated working directory.

Use synthetic APIs, credentials, and infrastructure identifiers in CI. Run the
repository's required `mise run ci:changed` and each consumer's required checks.
Do not enroll credentials, operate a production node, change Semaphore, or
enable schedules as part of offline validation.
