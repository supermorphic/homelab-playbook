# Recovery verification data contract

This defines observational requests and results, not exported lifecycle code.

Design authority: [Specification 011](../specs/011-talos-node-maintenance.md).

## Command and ownership

The existing verification owner adds one public command:

```text
just kube recovery-verify /absolute/private/request.json
```

The caller uses the verifier's separately prepared tool environment and a verified snapshot of its source. No dynamic recipe, helper path, code fetch, bootstrap, or credential issuer is selected by the request. The command invokes existing Cilium/foundation/source validators owned there. It never imports playbook lifecycle files or changes lifecycle state.

Preparation, baseline, and recovery use the same fixed command with a `mode` field:

- `prepare` checks request shape, source identity, required tools and cached chart/render inputs, and runs source validation without target API requests. It returns `source: passed`, with live checks `not-run`. Missing dependencies fail here, before disruption.
- `baseline` repeats source validation and runs the full read-only chain with every expected node healthy and schedulable and no lifecycle record, before the abrupt-loss power prompt. All checks must pass.
- `recovery` repeats source validation and performs the full read-only Cilium/foundation chain with exact expected containment. All checks must pass. Preparation and baseline responses cannot be accepted as recovery.

This distinction implements the spec's requirement to detect unavailable verification capability before disruption. It does not create a mutation or remote service protocol.

## Request schema

Every key below is required; unknown keys, duplicate JSON keys, wrong types, unbounded numeric values, and non-absolute credential paths are errors. `requestId` is a caller-generated UUID used only for response binding. `sourceRevision` is a full commit ID of the reviewed desired-data/verifier snapshot, not a lifecycle dependency pin.

```json
{
  "schemaVersion": 1,
  "requestId": "4e17c4dd-f983-4e2c-b261-b620916201a3",
  "mode": "recovery",
  "node": "node-a",
  "sourceRevision": "0123456789abcdef0123456789abcdef01234567",
  "apiServer": "https://192.0.2.20:6443",
  "nodes": {
    "node-a": "192.0.2.10",
    "node-b": "192.0.2.11",
    "node-c": "192.0.2.12"
  },
  "talosEndpoints": ["192.0.2.10", "192.0.2.11", "192.0.2.12"],
  "credentials": {
    "kubeconfig": "/operator/credentials/kubeconfig",
    "kubeContext": "operator",
    "talosconfig": "/operator/credentials/talosconfig",
    "talosContext": "operator"
  },
  "expectedContainment": {
    "node": "node-a",
    "record": "{\"schemaVersion\":1,\"kind\":\"reboot\"}"
  },
  "timeoutSeconds": 1800
}
```

Addresses, UUID, SHA, paths, and context names above are marked synthetic examples. Tests calculate real temporary Git SHAs and create run-owned fake config files; no production endpoint is reachable from fixtures.

Rules:

- `schemaVersion` must be integer 1, not Boolean/string; `mode` is exactly `prepare`, `baseline`, or `recovery`.
- Require exactly three unique approved nodes/addresses. The verifier independently checks this mapping against the selected source and validates live identity during baseline/recovery.
- `expectedContainment` is null in `prepare` and `baseline`; in `recovery` it contains the selected node and the exact persisted annotation string. Parse/validate schema 1 while retaining the string for exact live comparison. A kind supplied by the caller does not authorize restoration.
- Require explicit Kubernetes and Talos contexts in all modes. Never switch to diagnostic/ambient contexts. Cilium postflight uses Talos diagnostics/etcd and therefore needs its explicit configuration as well as Kubernetes access.
- `timeoutSeconds` is an integer in 1..3600. The controller may select a smaller remaining transaction budget. Enforce a monotonic deadline and terminate/wait for subprocesses after expiration; no child survives with inherited access.
- Endpoint strings and names are validated against source, not trusted because they appear in a request. Do not accept insecure TLS or alternative trust material in this schema.
- The verifier receives no disruption authorization, Lease-holder control, executable override, callback, or path to lifecycle implementation.

## Response schema

Write exactly one JSON object to stdout. Bounded redacted diagnostics go to stderr. Emit success only when the selected mode completes its required checks; nonzero child status, source-validation failure, timeout, or cleanup failure makes the command fail.

```json
{
  "schemaVersion": 1,
  "requestId": "4e17c4dd-f983-4e2c-b261-b620916201a3",
  "mode": "recovery",
  "node": "node-a",
  "sourceRevision": "0123456789abcdef0123456789abcdef01234567",
  "kubeContext": "operator",
  "talosContext": "operator",
  "checks": {
    "source": "passed",
    "cilium": "passed",
    "foundation": "passed"
  }
}
```

A zero process status is necessary but insufficient. The playbook validates exact request bindings and all stages; `not-run`, a missing result, a different mode, or extra JSON output fails recovery. A failure may omit this object entirely; do not render raw client output into it. The role keeps containment on every invalid result.

After observing a valid response, the lifecycle transaction repeats Lease and exact-record checks immediately before uncordon. The verifier never grants authority to remove a record.

## Transitive behavior to preserve

- Run source validation against the selected revision and locally prepared exact chart artifacts. Preparation may download dependencies through the owner's registered bootstrap/preparation workflow; invocation is cache-only. Missing or mismatched cached artifacts fail instead of fetching or skipping validation.
- Preserve Cilium values, HelmRelease/OCI generation and readiness, component health, CLI checks, diagnostics, and postflight checks.
- Preserve Flux source/controller readiness. For baseline/recovery modes, compare the live artifact revision with `sourceRevision`; do not re-resolve a moving remote branch during the transaction. Existing standalone Flux verification may retain its current explicit-main observation contract.
- Preserve cert-manager, MetalLB, gateway/Envoy, DNS provider revision, CA trust, DNS, HTTPS, and echo assertions.
- Baseline requires all expected nodes healthy/schedulable without records. Recovery permits exactly the expected target's cordon plus matching record. Reject an unexpected cordon/record on any other node. Standalone verification retains its normal all-schedulable behavior.
- Reuse existing verifier implementations through explicit context/containment parameters; do not copy them into the lifecycle role. Source-validation and platform failure must propagate through every nested command.
- This workflow creates only private local request/result/cache-read state. It must not create connectivity-test workloads, take a write Lease, publish reports remotely, reconcile resources, or clear lifecycle state.

## Paired contract evidence

Each owner maintains its own fixtures and tests against this documented schema; no shared runtime package is needed. The integration evidence must exercise the real command with synthetic Kubernetes/Talos/Flux/Cilium responses, including Talos diagnostics and source validators. A universal-success stub at `just` does not count.

Test preparation without API calls, healthy baseline, rejected baseline cordon/record, baseline response rejected for recovery, accepted expected containment, rejected second cordon, wrong record, wrong config context, wrong source revision, missing charts/tools, no runtime Git network resolution, nonzero stage status, malformed/stale response, timeout, and cleanup failure. Record both exact implementation revisions in the uncommitted acceptance ledger.
