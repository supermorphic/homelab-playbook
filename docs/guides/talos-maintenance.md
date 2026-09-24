# Talos node maintenance

This guide describes the supported workstation interface for an established
multi-node Talos cluster. Every live invocation requires explicit operator
authorization for the action, production target, node, and request file.
Confirmation values record execution intent; they do not grant authorization.
The [recovery verification contract](../reference/recovery-verification.md)
records the current implementation's topology limit.

## Prepare the source and chart cache

Use a clean checkout of the approved desired-state revision. The checkout must
have the fixed trusted origin and its `HEAD` must equal the full revision in the
request. Prepare the five pinned Helm archives before an operation:

```text
cd /absolute/path/to/approved/homelab-talos
mise exec -- just kube recovery-cache-prepare "$PWD/.cache/recovery-helm"
git status --short
git rev-parse HEAD
```

The operation does not fetch Git data, charts, tools, or credentials. It copies
the prepared cache into its private source snapshot and verifies the manifest,
source revision, chart names, versions, archive identities, and digests before
target access. Missing, extra, linked, stale, or changing cache content fails
closed.

Install this repository's pinned tools and locked controller dependencies with
`mise install` and `mise run bootstrap` before invoking the gateway. Bash 5 or
newer is required.

## Create the request

Store the request outside both repositories in a private directory. Use
absolute paths to existing operator credential files and select explicit
contexts. This documentation-only example uses reserved addresses and
placeholders:

```json
{
  "talos_node": "node-a",
  "talos_kubeconfig": "/Users/operator/.kube/homelab.yaml",
  "talos_kube_context": "homelab-operator",
  "talos_talosconfig": "/Users/operator/.talos/config",
  "talos_talos_context": "homelab-operator",
  "talos_source_dir": "/Users/operator/src/homelab-talos",
  "talos_source_revision": "0123456789abcdef0123456789abcdef01234567"
}
```

The selected source must resolve `node-a` and its API address from the committed
desired input. The request cannot supply inventory, connection, role, callback,
plugin, executable, trusted-origin, or terminal-path overrides.

## Check maintenance admission

`maintenance-check` is observational. It uses read-only API calls and a
client-side drain simulation. It does not acquire the write Lease, cordon a
node, change Longhorn, or submit a Talos power action.

```text
mise run playbook -- talos maintenance-check production \
  -e @/absolute/private/check.json --check
```

A passed check does not authorize a later mutation. Every mutation repeats its
safety checks under the live Lease.

## Enter and exit maintenance

Add this field for entry, using the address resolved from the approved source:

```json
"talos_confirmation": "enter:node-a:192.0.2.10"
```

Then run:

```text
mise run playbook -- talos maintenance-enter production \
  -e @/absolute/private/enter.json
```

Entry cordons the node, records its owned Longhorn values, drains workloads,
evacuates Longhorn replicas, and submits shutdown to that node. After observed
shutdown, perform the authorized physical work and restore electrical input.
Firmware automatic power-on must start the node; the playbook does not power it
on.

For exit, change the confirmation to the exact stored record kind:

```json
"talos_confirmation": "accept:node-a:maintenance"
```

Run exit after the node has powered on:

```text
mise run playbook -- talos maintenance-exit production \
  -e @/absolute/private/exit.json
```

Exit verifies the exact record and cordon, node identity, Secure Boot and UKI,
encrypted Talos volumes, Longhorn convergence, etcd, Cilium, foundation, and
workload recovery. It restores only recorded Longhorn values and removes
containment only after final Lease and verifier checks.

Use `accept:node-a:reboot` or `accept:node-a:abrupt-loss` to recover a contained
supported record left by those actions. An unknown record kind remains
contained and needs a separately reviewed recovery procedure.

## Reboot

Add the target-bound confirmation:

```json
"talos_confirmation": "reboot:node-a:192.0.2.10"
```

Run:

```text
mise run playbook -- talos reboot production \
  -e @/absolute/private/reboot.json
```

The transaction cordons and drains the node, submits one reboot, observes loss
and return, completes the same recovery acceptance, and then removes its exact
record. A lost response does not submit a second reboot. Failure after
containment leaves the node cordoned for `maintenance-exit`.

## Attended abrupt-loss test

Add both confirmations and an optional private evidence root:

```json
"talos_confirmation": "remove-power:node-a:192.0.2.10",
"talos_test_confirmation": "chaos:node-abrupt-loss",
"talos_evidence_dir": "/Users/operator/private-evidence"
```

Run the command in a real terminal:

```text
mise run playbook -- talos abrupt-loss-test production \
  -e @/absolute/private/abrupt-loss.json
```

The gateway derives the terminal device from its own standard input. The
request and inherited environment cannot select a different device. At the
first prompt, disconnect electrical input without using the node's power
button, then press Enter. The scenario requires Kubernetes NotReady, classified
Talos API loss, loss of the target etcd member, and retained quorum before it
persists containment. Authentication or malformed client output does not prove
physical loss.

The scenario checks that every surviving node is Ready and runs Cilium. It also
checks Longhorn availability and external DNS/HTTPS paths for the full
600-second passive window. Every workload
owner captured on the target before loss must have a Ready replacement on a
surviving node in the final sample before that deadline. Pending or absent
replacements are allowed during earlier samples. At the restoration prompt,
restore electrical input and require firmware automatic power-on, then press
Enter. Final recovery uses the same guarded acceptance as exit. Evidence is
written atomically in a unique private run directory.

EOF, timeout, or a signal after the removal prompt reports unresolved possible
loss, requests physical restoration when an interactive failure can still be
answered, stops child process groups, and
preserves containment unless recovery acceptance succeeds. Lease-holder loss
stops further mutation. A scenario failure and a later recovery failure remain
separate outcomes.

On command failure, the gateway reports only the action, node, failed outcome,
automation revision, desired-source revision, last allowlisted phase, and
whether recovery is required. `containment-confirmed` means the lifecycle record
and cordon must be treated as durable before recovery. It does not mean recovery
acceptance passed. A failure after `preflight-confirmed` reports recovery need as
unknown because containment and its phase record are separate durable writes.
A `preflight-pending` failure also reports unknown because maintenance exit can
start with an existing durable containment record.

## Evidence and recovery limits

Offline tests prove request validation, transaction ordering, exact target
selection, cache-only verification, terminal prompts, signals, Lease behavior,
and synthetic recovery. They do not prove production credentials, physical
power behavior, network reachability, workload service levels, or hardware
recovery. Those live checks require separate authorization and operator
attendance.

Do not retry a disruptive action when its response is unknown. Observe the
node, Lease, cordon, and lifecycle annotation first. Use `maintenance-exit`
only when the exact supported record and intended target are confirmed.
