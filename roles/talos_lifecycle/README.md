# Talos lifecycle role

This controller-side role owns the complete Lease-protected node lifecycle
transaction. It validates one normalized request, writes it to a private
run-owned directory, and invokes `runtime.py` once. The runtime prepares an
exact private desired-source snapshot, validates the prepared recovery chart
cache, runs the fixed observational verifier, and starts one Bash transaction.

Public callers use `mise run playbook`; the role is not a second operator
interface. The gateway fixes the action, local connection, interpreter, role
path, trusted source origin, tool paths, terminal device, cancellation path,
and child environment. Request data cannot override these values.

Mutating transactions acquire and renew the shared Kubernetes Lease. The
attended abrupt-loss controller joins that holder for containment and recovery.
Failures after containment preserve the exact Node lifecycle record and cordon.
`maintenance-exit` is the independent recovery action for supported schema 1
records.

The abrupt-loss gateway reads the terminal device from its own standard input.
Ansible and runtime receive that fixed internal path so prompts remain visible
after local Ansible starts the command. A private cancellation marker tells the
runtime to stop and reap its lifecycle process group. Scenario bridge calls
start blocked, register their separate group before executing the target
command. Recovery gets a 75-minute deadline that covers the sequential node
return, workload replacement, and verifier waits plus ten minutes of margin.
The gateway has a bounded emergency fallback tied to the current run's private
ownership token and live lifecycle group leader; it stops that group before it
stops Ansible. Each nested bridge group is registered in that private record so
the bridge helper, runtime, and gateway fallback can stop it before lifecycle
ownership ends. The runtime also removes same-group descendants after a normal
leader exit.
The role keeps command output private and reports only an allowlisted last
confirmed phase when the transaction fails.
