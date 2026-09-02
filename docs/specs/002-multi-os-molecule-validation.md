# Specification 002: Multi-OS Molecule validation

Issue: [#11 Establish multi-OS Molecule validation](https://github.com/supermorphic/homelab-playbook/issues/11)

## Purpose

Add change-directed executable validation for the repository-owned
`system_maintenance` role. The validation uses Molecule and rootless Podman to
exercise Debian 13 and Rocky Linux 9 in systemd-capable containers.

This initiative adds and activates the change-directed `molecule` validation
depth. It adds container evidence without contacting an inventory host, using a
repository secret, or claiming VM or physical-hardware coverage.

## Governing decisions

1. Podman is the only supported container runtime locally and in GitHub Actions.
2. Podman runs rootless and every test container runs unprivileged.
3. Tests use maintained, moving operating-system release tags rather than
   committed image digests.
4. Each platform explicitly pulls its base once per invocation, then builds and
   tests without another registry check.
5. Debian and Rocky run natively on ARM64 and AMD64. The same two-platform set
   runs locally and in GitHub Actions.
6. Local platform workers and GitHub matrix jobs use the same worker lifecycle.
7. Local and GitHub platform validation runs concurrently.
8. Base and built images remain cached. Test containers are recreated for every
   invocation and removed afterward, including after a failed test.
9. Pull, image-build, Molecule, per-platform total, and invocation total times
   are reported separately.

These decisions replace issue #11's initial privileged-container and immutable
image-pin assumptions. They preserve its systemd, executable behavior,
change-directed validation, and container-only boundaries.

## Scope

### Included

- one `default` Molecule scenario for `roles/system_maintenance`;
- Debian 13 and Rocky Linux 9 container platforms;
- rootless Podman preflight, image acquisition, image building, container
  lifecycle, and cleanup;
- converge, deterministic-task idempotence, and independent verification
  phases;
- explicit suppression of real reboot attempts during container tests while
  retaining the production default;
- a public scenario test command and shared per-platform worker;
- parallel local execution and a two-job GitHub Actions matrix;
- activation of classifier, `full`, and merge-gate support for the `molecule`
  depth;
- separate timing and image-provenance reporting; and
- adoption of the repository command lifecycle reference for current and future
  command classification.

### Excluded

- Docker CLI, Docker daemon, Docker socket, Docker-compatible wrappers, or a
  Docker fallback;
- the legacy Molecule Podman plugin;
- privileged containers, rootful Podman, `sudo`, added Linux capabilities, or
  host cgroup mounts;
- committed OCI image digests;
- prebuilt repository test images or a container-registry publishing workflow;
- a registry mirror, fallback registry, stale-cache fallback, or offline test
  mode;
- GitHub Actions caching of Podman image storage;
- VMs, libvirt, Vagrant, self-hosted runners, live staging, production hosts, or
  physical hardware;
- testing third-party Galaxy roles or every repository playbook;
- validation of real reboot, boot persistence, firmware, kernel parameters,
  networking, routing, DNS failover, mounts, or hardware behavior.

## Toolchain and dependency model

Mise remains the task and tool entry point. uv remains the Python resolver and
lock owner.

The Molecule dependency group pins:

```text
molecule==26.6.0
```

The repository Galaxy requirements pin:

```text
containers.podman==1.20.2
```

The scenario uses Molecule's default, Ansible-native driver with explicit
Ansible create and destroy playbooks. It does not install or use
`molecule-plugins[podman]`. The create and destroy playbooks use modules from
`containers.podman`. Molecule's dependency phase is disabled because repository
bootstrap owns exact controller and Galaxy installation. This prevents parallel
workers from attempting concurrent dependency installation into shared paths.

The dependency audit retains every declared Galaxy role and collection.
`community.general` remains necessary for Rocky Linux DNF configuration,
`community.library_inventory_filtering_v1` remains its explicit dependency, and
`containers.podman` remains necessary for the Molecule lifecycle.

Podman is a host runtime, not a Python project dependency. The repository does
not install it through bootstrap or invoke it with elevated privileges. The
embedded test preflight checks required capabilities and reports the detected
version. It does not require the local and GitHub hosts to use an identical
Podman release.

On macOS, the operator must install Podman and start a rootless Podman machine
before validation. On Linux, the operator must provide a working rootless
Podman installation. GitHub uses the Podman installation supplied by the fixed
`ubuntu-24.04` hosted-runner image; the workflow does not install or configure a
rootful service.

## Command lifecycle classification

The [repository command lifecycle](../reference/repository-command-lifecycle.md)
classifies a command by its effects. Molecule deliberately creates and removes
bounded local containers to obtain executable evidence, so it is a controlled
`test`, not a read-only `validate` command.

The public command is therefore `test:molecule`. There is no
`validate:molecule` alias. The `molecule` classifier depth and GitHub job retain
their names because they identify validation selection and CI execution, not a
public lifecycle operation.

The test embeds its Podman preflight because that check has no independent
operator value. It requires no confirmation: rootless, unprivileged, exactly
owned local test containers are a bounded consequence for which a static token
would add friction without a proportionate safeguard.

## Platform matrix

| Platform | Maintained base tag | Local ARM64 | GitHub AMD64 |
| --- | --- | --- | --- |
| Debian 13 | `docker.io/library/debian:13` | native ARM64 | native AMD64 |
| Rocky Linux 9 | `docker.io/rockylinux/rockylinux:9` | native ARM64 | native AMD64 |

The Debian and Rocky tags follow their selected major release lines. They
intentionally move as upstream publishers release maintenance and security
updates.

Every reference is fully qualified to avoid short-name registry resolution.
The repository records the locally resolved image identifier after each pull,
but does not commit it or use it as a later pin.

## Image acquisition and build lifecycle

Before local workers or a GitHub platform worker starts, the repository runner:

1. validates the exact registered role/scenario selector;
2. confirms the locked controller and Galaxy dependencies are present, otherwise
   directing the operator to `mise run bootstrap`;
3. confirms that `podman` exists in `PATH`;
4. runs bounded `podman info` inspection as the current user;
5. confirms rootless operation and cgroup v2;
6. selects the default platform set for the Podman host architecture without
   pulling a probe image; and
7. acquires one non-blocking host-local invocation lock shared by linked
   worktrees of this repository.

Invalid usage, an unknown selector, or an unknown workflow platform exits with
status `2`. An unavailable dependency, Podman executable, Podman service or
machine, rootless mode, cgroup v2 host, or invocation lock exits with status `1`
and one actionable message. Expected setup failures print no traceback. The
runner does not pull an image, inspect or delete a container, or start workers
until this preflight succeeds.

On macOS, an unreachable Podman service reports that the operator should run
`podman machine start`. On Linux, it reports that rootless Podman must be made
available to the current user. Neither path installs software, starts a machine,
uses `sudo`, or selects a different runtime automatically.

After global preflight, each platform worker owns this sequence:

1. inspect the worker's exact stable container name;
2. if that container exists, verify its repository, scenario, and platform
   ownership labels before removing it;
3. run one `podman pull --policy=always --retry=3` for the maintained base tag;
4. read the pulled image's local identifier and, when Podman supplies it, its
   repository digest for the log;
5. build the repository-owned systemd test image with `--pull=never`;
6. run the Molecule test sequence using that local built image with container
   pull policy `never`;
7. remove the scenario container in an unconditional cleanup path; and
8. retain the base image, built image, and reusable build layers.

A container with the expected name but missing or different ownership labels is
not deleted; that worker fails with a collision message. The invocation lock
prevents overlapping local runs from treating another active run as stale while
still allowing the two workers within one invocation to run concurrently.
The lock identity derives from Git's common directory so linked worktrees on the
same host coordinate with each other. The lock is released on success, failure,
or interruption. GitHub matrix jobs run on separate ephemeral hosts and
therefore do not share this lock.

The explicit pull is inside the worker. There is no separate local pre-pull
stage and no GitHub digest-resolution job. This keeps local and GitHub behavior
the same and avoids a registry check that cannot transfer image layers to
ephemeral matrix runners.

`--policy=always` makes inability to confirm the maintained tag an acquisition
failure even when a local image exists. The workflow never silently substitutes
a stale cached image. Podman's bounded retry handles short transient failures.
A continuing registry outage fails the affected platform with a clear image
acquisition result. A fresh GitHub runner cannot continue without the base
image, so rerunning after registry recovery is the initial recovery procedure.

The repository owns one minimal Containerfile per platform. Each derives from
the selected base and installs only the prerequisites needed to start systemd
and let Ansible manage the container. Test images are built during validation;
they are not published in this initiative.

The first implementation measures the cost of these builds. Prebuilt images or
a repository-controlled registry mirror require a later measured design. They
are justified only if build duration or public-registry reliability materially
limits the validation workflow.

## Least-privilege container model

The preflight must prove:

- Podman is reachable without `sudo`;
- Podman reports rootless operation;
- the Podman host supplies cgroup v2; and
- the default platform set matches the Podman host architecture.

The preflight does not pull or start a separate probe image. The default run
selects Debian and Rocky on both supported host architectures. The internal
platform selector overrides the default with one exact worker. GitHub sets it
to Debian or Rocky for each matrix job; it is not a public command argument.

After container creation, a bounded readiness check confirms that systemd is PID
1 and responds inside the container. This is the authoritative proof that the
host's rootless cgroup delegation is sufficient. A failure reports systemd or
cgroup delegation as the container-start cause instead of misclassifying it as
a role assertion failure.

Scenario inventory uses:

```yaml
container_privileged: false
container_systemd: always
```

It adds no capabilities and does not mount the host cgroup filesystem. The
Podman collection supplies the systemd-specific writable tmpfs and cgroup setup
required by `container_systemd: always`.

Ansible operates as root inside the container because package management,
system configuration, and systemd require it. With rootless Podman, this user is
mapped into the invoking user's namespace and is not host root. No host
administrative credential or rootful service is an accepted fallback.

## Scenario structure and lifecycle

The scenario lives at:

```text
roles/system_maintenance/molecule/default/
```

It contains repository-owned configuration, create, converge, verify, cleanup,
destroy, and platform Containerfile inputs. The normal test sequence is:

```text
destroy -> syntax -> create -> prepare -> converge -> idempotence -> verify ->
cleanup -> destroy
```

The outer worker also requests destroy on failure so an interrupted Molecule
sequence does not intentionally retain a test container. Cleanup targets only
the exact names owned by this scenario. It does not prune Podman storage or
remove unrelated containers, images, networks, or volumes.

Workers use distinct container names, built-image tags, logs, and Molecule
ephemeral state. This isolation permits two local workers to execute at the same
time without sharing mutable scenario state.

## Role contract and reboot control

The role gains one explicit default shaped as:

```yaml
system_maintenance_reboot_enabled: true
```

Both existing reboot tasks require this value in addition to their current
operating-system reboot signal. The default preserves production behavior.
Molecule convergence sets it to `false`; tests therefore exercise the update
and reboot-decision logic without attempting to reboot a container.

The new variable controls only reboot execution. It does not suppress package
updates, cleanup, package installation, reboot-signal inspection, or any other
role behavior.

## Verification contract

Molecule's idempotence phase runs the role a second time and requires no changed
deterministic tasks. Each operating system's live full-upgrade task carries
Molecule's `molecule-idempotence-notest` tag. Molecule runs these tasks during
convergence and automatically skips that tag during idempotence. This prevents a
package or repository-metadata publication between the two runs from being
reported as role non-idempotence. Package installation, cleanup, configuration,
reboot-signal inspection, and all other maintenance tasks remain part of the
strict second pass.

Verification then uses Ansible modules and commands that observe the result
rather than calling the role again.

The initial assertions cover current role-owned invariants:

- all platforms complete converge with the expected operating-system family;
- representative packages that are not part of the Containerfile baseline are
  installed;
- Debian package maintenance and representative common-package installation
  complete successfully;
- Rocky Linux has `installonly_limit=2`, the EPEL package is installed, and the
  EPEL repository is disabled by default;
- a functioning systemd process exists in each test container.

Assertions use package facts, file metadata and content, service-manager facts,
or a direct read-only command as appropriate. They do not reproduce the role's
task implementation as the oracle.

The scenario does not assert service enablement that the role does not own.
Package-provided defaults are not promoted to a role contract merely because
they occur in a base image or package release.

## Public command and parallel execution

The public command is:

```text
mise run test:molecule -- system_maintenance/default
```

It validates the exact role/scenario selector and returns failure if any worker
fails. It runs Debian and Rocky on both ARM64 and AMD64. Unknown roles,
scenarios, workflow platforms, or extra public arguments fail with a concise
usage message and status `2` before Podman is inspected.

The local runner starts the selected isolated platform workers concurrently.
It waits for all workers so one failure does not discard useful results from
the other platform. Output identifies the originating platform, and the final
summary reports every result.

Molecule's experimental collection-only worker interface is not part of this
design. Repository orchestration supplies bounded parallelism for this
role-based scenario.

## Timing and diagnostic output

For each platform, the runner reports:

- Podman version, host architecture, requested architecture, and native or
  emulated execution;
- maintained source tag and resolved local image identifier;
- pull/check duration;
- test-image build duration;
- Molecule lifecycle duration;
- per-platform total duration; and
- pass, test failure, acquisition failure, build failure, or cleanup failure.

The local summary also reports wall-clock invocation duration. It does not use
the sum of concurrent worker durations as the overall time.

GitHub writes the same platform data to each job summary. The workflow and
merge-gate summaries make infrastructure acquisition failures distinguishable
from role assertion failures. The implementation report records GitHub's
workflow elapsed time from the completed run rather than adding API access only
to calculate it inside the workflow. Logs and summaries contain no environment
dump, credentials, inventory content, or Vault material.

## GitHub Actions topology

The workflow retains the existing `classify`, `fast`, `ansible`, and stable
`merge-gate` jobs and adds a conditional `molecule` matrix:

```text
classify
   |-- fast (always) -----------------------------|
   |-- ansible (ansible, molecule, or full) ------|
   `-- molecule (molecule or full)                 |-- merge-gate
         |-- Debian 13                             |
         `-- Rocky Linux 9 ------------------------|
```

The matrix uses `ubuntu-24.04`, `fail-fast: false`, and at most two concurrent
jobs. Both platforms are native AMD64 in GitHub Actions. Each matrix job invokes
the same platform worker used by the local command and has a bounded timeout.
The initial timeout allows for a cold image build and full package upgrade; it
must be tightened later if measured results support a smaller reliable bound.

The workflow has read-only repository permissions, uses no container-registry
credentials, and does not use `sudo`. GitHub runners are ephemeral, so their
images and containers disappear with the job. The workflow does not add a
redundant cleanup or image-pruning stage beyond scenario cleanup.

## Change-directed validation

The classifier begins emitting all four ordered depths:

```text
fast < ansible < molecule < full
```

The intended mappings are:

| Change | Selected depth |
| --- | --- |
| `system_maintenance` role source or defaults | `molecule` |
| its Molecule scenario, Containerfiles, runner, or focused tests | `molecule` |
| other Ansible roles, playbooks, or inventories | `ansible` |
| toolchain locks, Galaxy requirements, classifier, CI, or merge gate | `full` |
| documentation and ordinary policy files | `fast` |
| unknown or ambiguous path | `full` |

Specific `system_maintenance` mappings take precedence over the existing generic
`roles/ -> ansible` mapping. Classifier fixtures cover both the positive
Molecule paths and unchanged shallower paths.

`run_molecule` is true for `molecule` and `full`. `full` now means all
implemented validation and therefore includes Molecule. The public `ci` task and
`ci:changed -- --force-depth molecule` follow the same rule.

The merge gate accepts `molecule` as an implemented depth, requires successful
Ansible and Molecule results when selected, permits a skipped Molecule job only
for `fast` or `ansible`, and continues to fail closed for missing or unknown
results.

## Failure and cleanup behavior

- A missing or unusable Podman runtime fails before container creation.
- Rootful Podman, cgroup v1, an unavailable invocation lock, an unavailable
  required architecture, or a request for privilege fails without attempting an
  elevated fallback.
- A Podman preflight failure leaves existing images and containers untouched.
- A same-name container without the complete expected ownership labels is not
  deleted and fails as a collision.
- Registry failure after Podman's bounded retries fails image acquisition; a
  cached image is not silently substituted.
- Build, converge, idempotence, and verify failures retain their distinct stage
  in the summary.
- Cleanup runs after success and failure and removes only scenario-owned
  containers.
- Cleanup failure makes the worker fail even if the test assertions passed.
- Images and layers are never pruned by the validation workflow.
- Parallel workers continue to completion so the final report contains all
  available platform evidence.

## Validation and measurement

Implementation follows repository test and validation policy:

1. add focused unit and contract tests for selector parsing, platform planning,
   timing aggregation, classifier selection, and merge-gate reconciliation;
2. run `mise run bootstrap` after dependency changes;
3. use `mise run validate:fast` and `mise run validate:ansible` while iterating;
4. run the scenario locally through its public command on the supported local
   Podman environment;
5. run `mise run ci:changed` before claiming completion or publishing the pull
   request; and
6. collect successful GitHub matrix timing from the pull request.

Focused tests use positive current invariants as their oracles. They require the
exact Debian and Red Hat role dispatch set, the exact Debian 13 and Rocky Linux
9 Molecule set, and the exact two-entry GitHub matrix. Complete OS provisioning
must reject an unsupported operating-system family before configuration roles
run, and the Molecule runner must reject an unknown workflow platform before
invoking Podman. No permanent forbidden-reference scan is part of the contract.

The implementation report records, for every platform and for the local and
GitHub workflow totals:

- pull duration;
- build duration;
- Molecule duration;
- overall duration;
- whether execution was native or emulated; and
- the proportion of overall platform time spent building the test image.

Run-specific measurements belong in implementation reports rather than this
durable specification.

## Acceptance criteria

The current contract is satisfied when:

1. Molecule and `containers.podman` are exactly pinned through uv and Galaxy
   dependency management;
2. `mise run test:molecule -- system_maintenance/default` runs Debian and Rocky
   concurrently with Podman only;
3. local ARM64 and AMD64 runs select native Debian and Rocky, and GitHub's AMD64
   matrix runs both platforms natively;
4. Podman is rootless and all scenario hosts use `container_privileged: false`,
   `container_systemd: always`, and no added capabilities;
5. one embedded global preflight rejects invalid selectors, unavailable or
   rootful Podman, cgroup v1, and overlapping local invocations before touching
   container state, without a privilege or runtime fallback;
6. every worker pulls its fully qualified maintained tag exactly once with an
   always-refresh policy, builds with no second pull, and logs the resolved
   local image identity;
7. base and built images remain after local testing, while exact label-owned
   scenario containers are destroyed after both success and failure;
8. converge, deterministic-task idempotence, and independent verification pass
   for Debian and Rocky; live full-upgrade tasks run during converge and are
   excluded only from the idempotence pass because maintained package
   repositories can change during a scenario run;
9. the role exposes an explicit reboot control whose production default remains
   enabled and whose scenario value prevents actual container reboot;
10. verification covers representative current role invariants, including Rocky
    kernel retention and EPEL settings;
11. the classifier selects `molecule` only for explicitly mapped role/scenario
    changes, retains shallower depths for unaffected changes, and selects all
    implemented validation for `full`;
12. GitHub executes a bounded two-platform matrix in parallel and the stable
    merge gate requires its success when selected;
13. local and GitHub workers report pull, build, Molecule, and platform-total
    timing; the local summary and implementation report add their respective
    overall elapsed times to evaluate future prebuilt images;
14. acquisition, build, assertion, and cleanup failures are distinguishable;
15. no test contacts inventory hosts, reads Vault material, uses infrastructure
    credentials, or claims VM or hardware evidence; and
16. support claims, executable behavior, Molecule coverage, CI execution, and
    documentation agree on the Debian and Rocky platform set while generic CPU-
    architecture logic remains; and
17. all repository-required change-directed validation passes from the issue
    branch before completion is claimed.
