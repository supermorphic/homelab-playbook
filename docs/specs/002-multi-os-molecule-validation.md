# Specification 002: Debian Molecule validation

Issues: [#11](https://github.com/supermorphic/homelab-playbook/issues/11),
[#32](https://github.com/supermorphic/homelab-playbook/issues/32),
[#35](https://github.com/supermorphic/homelab-playbook/issues/35),
and [#40](https://github.com/supermorphic/homelab-playbook/issues/40).

## Purpose and evidence boundary

Provide change-directed executable evidence for first-party roles and complete
service compositions using Molecule and rootless Podman. Debian 13 runs natively
on ARM64 and AMD64 with the same worker lifecycle locally and in GitHub Actions.
The [runner registry](../../scripts/molecule.py) owns supported selectors and
platforms; scenario source owns test composition and explicit allowances.

Containers prove configuration, task decisions, idempotence, and independently
observable state. They do not prove physical reboots, boot persistence, firmware,
host-kernel enforcement, production networking, DNS failover, recovery access,
or live scheduling. No workflow contacts inventory hosts, uses production
credentials, or presents container success as deployment acceptance.

## Runtime and dependency decisions

Mise and uv own the pinned controller dependencies; repository bootstrap owns
Galaxy installation. Workers verify and reuse them rather than running a
scenario-specific dependency installer. Molecule's Ansible-native driver uses
repository create/destroy playbooks and the Podman collection. Docker, the
legacy Molecule Podman plugin, rootful operation, and elevated fallback are
outside this contract.

Podman is an operator-provided host capability. macOS needs a started rootless
Podman machine; Linux needs a functioning rootless installation for the current
user. Preflight reports missing capability without installing software, starting
a machine, or using sudo. Hosted jobs use their runner-provided Podman runtime.
Identical host Podman versions are unnecessary; required capabilities are not.

Tests deliberately use maintained moving OS-release tags instead of committed
image digests. Fully qualified references prevent short-name resolution. Every
worker pulls the selected base once per invocation, records its resolved image
identity, then builds and tests without another registry check. Base and built
images remain cached, while test containers are always recreated. A registry
outage fails acquisition after bounded retries; cached content is never silently
substituted. Prebuilt images, registry mirrors, and image-store caching need a
later measured justification.

## Bounded worker lifecycle

`test:molecule` follows the controlled-test contract in
[specification 001](001-agentic-development-modernization.md). It embeds its
Podman preflight and requires no ordinary confirmation because its effects are
rootless, unprivileged, exactly owned disposable state.

Before touching containers, the runner validates the exact registered selector,
controller dependencies, reachable rootless Podman, cgroup v2, supported native
architecture, and a non-blocking host-local invocation lock. Invalid usage fails
before Podman inspection; unavailable capability fails with an actionable
message. Preflight failure leaves existing containers and images untouched.
The lock derives from Git's common directory so linked worktrees cannot treat
another active invocation as stale.

Each worker checks its exact stable container name and ownership labels before
removing stale state, pulls the maintained base, builds the systemd fixture
without another pull, and runs the registered Molecule sequence. A same-name
container with missing or different labels is a collision, never a deletion
target. Scenario names, images, logs, and ephemeral state remain distinct.
Cleanup runs after success and failure, removes only owned containers, and never
prunes unrelated storage, images, networks, or volumes. Cleanup failure makes
the worker fail while preserving the original test outcome.

A bounded readiness check proves that systemd is PID 1 and responds inside the
container. This is the authoritative test of rootless cgroup delegation;
startup failure remains distinct from a role assertion failure. Containers do
not mount host cgroups or run privileged. Scenario-specific capabilities must
have an explicit consumer and remain inside the rootless namespace. Ansible's
container-root user is not host root. Production security restrictions must not
be weakened to make nested tests pass.

## Assertion and reboot contract

The normal sequence covers fresh creation, preparation, convergence,
deterministic-task idempotence, independent verification, and cleanup/destruction.
Idempotence requires a second pass with no changed deterministic tasks. Full
upgrade tasks may be explicitly excluded from that phase because upstream
repository metadata or packages can change between runs; installation,
configuration, cleanup, and decision tasks remain checked.

Independent assertions observe package facts, file metadata, service-manager
state, or direct read-only commands. They do not call the producer role as their
oracle or promote package defaults to guarantees the role does not own.

The maintenance role reports reboot-required state; OS playbooks own actual
reboot/reconnection. Disposable scenarios suppress native automatic reboots and
other unavailable kernel actions while retaining configuration and decision
coverage. Production defaults remain enabled. Suppression does not justify
omitting updates, installed packages, reboot-state inspection, or the explicit
playbook lifecycle.

## Change selection and merge gate

[The impact planner](../../scripts/ci/molecule_plan.py) consumes the
[impact map](../../scripts/ci/molecule-impact.json) and runner registry. The map
owns exact consuming scenarios, so this spec does not duplicate a scenario
catalog. More-specific directory rules and exact-file rules resolve consumers;
equally specific rules union them. Scenario-local paths require an explicit
scenario-directory rule. Adding a scenario requires both registration and impact
consumers.

Unknown relevant inputs or a missing/invalid map select the complete registry;
an absent or invalid registry fails instead of claiming partial coverage.
Discovery includes deletion paths, both rename/copy paths, and local committed,
staged, unstaged, and untracked changes. Failed discovery and an empty changed
set select full validation. CI, classifier, runner, impact-map, and dependency
changes require the complete suite. An explicitly forced Molecule tier without
mapped inputs also selects the full suite.

Plans record exact matrix rows and reasons, resolved revisions, and whether
worktree changes are included. Missing Git provenance remains unknown; a commit
identifier cannot describe uncommitted content. Local execution clears internal
platform overrides that could omit required coverage. Scheduled and manual full
validation select every registered scenario.

GitHub matrix jobs use the same worker on isolated ephemeral hosts, bounded
timeouts, read-only repository permissions, and no registry credentials or sudo.
They retain `fail-fast: false` and no continue-on-error behavior. The stable
merge gate rejects malformed, duplicate, unknown, empty, or incomplete rows and
requires complete registry coverage in full mode. Selected failed, cancelled,
or skipped jobs fail the gate. Intentionally unselected Molecule jobs may be
skipped at shallower depths.

## Diagnostics and measurements

Report acquisition, build, Molecule lifecycle, worker total, invocation total,
and cleanup separately. Infrastructure failure must remain distinguishable from
assertion failure. Lifecycle observations retain repeated phases and mark an
interrupted phase incomplete; missing data never means zero cost.

The Molecule-only Ansible timing callback retains bounded aggregates of approved
source locations, opaque role invocation identifiers, and durations. It emits
no task names, variables, arguments, host identifiers, result objects, or
unsalted internal identities. Timing includes controller and dispatch overhead;
it is not container CPU time. Collection limits and missing records stay visible.
Structured timing survives ephemeral cleanup without copying raw playbook logs.
A killed runner cannot guarantee final reports.

Compare matching source, architecture, cache state, and concurrency; moving
package repositories can change outcomes. Keep detailed run measurements and
optimization proposals in established evidence or ignored task state. Retain
focused contracts for selection, ownership, cleanup, timing privacy, and gate
reconciliation, and use current independent invariants for executable assertions.
