# Specification 002: Debian Molecule validation

Issues: [#11 Establish multi-OS Molecule validation](https://github.com/supermorphic/homelab-playbook/issues/11)
and [#32 Deterministic Molecule CI gating](https://github.com/supermorphic/homelab-playbook/issues/32)

## Purpose

Add change-directed executable validation for the repository-owned
`system_maintenance` role. The validation uses Molecule and rootless Podman to
exercise Debian 13 in systemd-capable containers.

The original initiative added the change-directed `molecule` validation depth.
Issue #32 extends it with deterministic impact selection across all registered
scenarios. It adds container evidence without contacting an inventory host, using a
repository secret, or claiming VM or physical-hardware coverage.

## Governing decisions

1. Podman is the only supported container runtime locally and in GitHub Actions.
2. Podman runs rootless and every test container runs unprivileged.
3. Tests use maintained, moving operating-system release tags rather than
   committed image digests.
4. Each platform explicitly pulls its base once per invocation, then builds and
   tests without another registry check.
5. Debian runs natively on ARM64 and AMD64. The same Debian platform runs
   locally and in GitHub Actions.
6. Local platform workers and GitHub matrix jobs use the same worker lifecycle.
7. Local scenario invocations select one Debian worker. GitHub runs scenario
   jobs concurrently on separate hosts.
8. Base and built images remain cached. Test containers are recreated for every
   invocation and removed afterward, including after a failed test.
9. Pull, image-build, Molecule, per-platform total, and invocation total times
   are reported separately.

These decisions replace issue #11's initial privileged-container and immutable
image-pin assumptions. They preserve its systemd, executable behavior,
change-directed validation, and container-only boundaries.

## Scope

### Included

- `default` and `baseline` scenarios for `roles/system_maintenance`, plus
  `reverse_proxy/default` and `semaphore/default` integration coverage;
- the Debian 13 container platform;
- rootless Podman preflight, image acquisition, image building, container
  lifecycle, and cleanup;
- converge, deterministic-task idempotence, and independent verification
  phases;
- suppression of native automatic reboots during container tests while
  retaining the production default;
- a public scenario test command and shared per-platform worker;
- one local Debian worker per scenario and a generated GitHub Actions matrix;
- activation of classifier, `full`, and merge-gate support for the `molecule`
  depth;
- separate timing and image-provenance reporting; and
- adoption of the repository command lifecycle reference for current and future
  command classification.

### Excluded

- Docker CLI, Docker daemon, Docker socket, Docker-compatible wrappers, or a
  Docker fallback;
- the legacy Molecule Podman plugin;
- privileged containers, rootful Podman, host `sudo`, host cgroup mounts, or
  capabilities beyond the scenario-specific allowances defined below;
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
bootstrap owns exact controller and Galaxy installation. Scenario workers reuse
the verified dependencies without running their own dependency installation.

The dependency audit retains every declared Galaxy role and collection.
`community.general` remains necessary for host timezone configuration,
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

The Debian tag follows its selected major release line. It intentionally moves
as the upstream publisher releases maintenance and security updates.

Every reference is fully qualified to avoid short-name registry resolution.
The repository records the locally resolved image identifier after each pull,
but does not commit it or use it as a later pin.

## Image acquisition and build lifecycle

Before a local or GitHub scenario worker starts, the repository runner:

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
prevents overlapping local runs from treating another active run as stale.
Each scenario invocation selects one Debian worker.
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
selects Debian on both supported host architectures. The internal
platform selector overrides the default with one exact worker. GitHub sets it
to Debian for each matrix job; it is not a public command argument.

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

No scenario mounts the host cgroup filesystem. The Podman collection supplies
the systemd-specific writable tmpfs and cgroup setup required by
`container_systemd: always`.

The two maintenance scenarios add no capabilities. The reverse proxy scenario
adds `SYS_PTRACE` for socket inspection and `SYS_ADMIN` for its systemd sandbox
tests, as defined in [Specification 008](008-shared-private-reverse-proxy.md).
The Semaphore scenario adds `SYS_PTRACE` for socket inspection. These allowances
apply only within the rootless test containers and do not grant host privileges.

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

Each scenario uses distinct container names, built-image tags, logs, and
Molecule ephemeral state. GitHub runs the four scenario jobs on separate
ephemeral hosts. Local scenario invocations use the shared invocation lock.

## Role contract and reboot control

The maintenance role reports the Debian reboot-required marker without
rebooting the host. OS provisioning and maintenance playbooks own explicit
reboots and subsequent verification, as defined by Specification 003.

Native automatic security-update reboots have a separate default:

```yaml
system_maintenance_native_reboot_enabled: true
```

Molecule convergence sets this value to `false` to disable native automatic
reboots in disposable containers. The production default remains enabled.
This control does not suppress package updates, cleanup, package installation,
reboot-signal inspection, or explicit playbook reboot handling.

## Verification contract

Molecule's idempotence phase runs the role a second time and requires no changed
deterministic tasks. Debian's live full-upgrade task carries
Molecule's `molecule-idempotence-notest` tag. Molecule runs these tasks during
convergence and automatically skips that tag during idempotence. This prevents a
package or repository-metadata publication between the two runs from being
reported as role non-idempotence. Package installation, cleanup, configuration,
reboot-signal inspection, and all other maintenance tasks remain part of the
strict second pass.

Verification then uses Ansible modules and commands that observe the result
rather than calling the role again.

The initial assertions cover current role-owned invariants:

- Debian 13 completes convergence with the expected distribution and version;
- representative packages that are not part of the Containerfile baseline are
  installed;
- Debian package maintenance and representative common-package installation
  complete successfully;
- a functioning systemd process exists in each test container.

Assertions use package facts, file metadata and content, service-manager facts,
or a direct read-only command as appropriate. They do not reproduce the role's
task implementation as the oracle.

The scenario does not assert service enablement that the role does not own.
Package-provided defaults are not promoted to a role contract merely because
they occur in a base image or package release.

## Public command and scenario execution

The public command is:

```text
mise run test:molecule -- system_maintenance/default
```

It validates the exact role/scenario selector and returns failure if any worker
fails. It runs Debian natively for the host's ARM64 or AMD64 architecture. Unknown roles,
scenarios, workflow platforms, or extra public arguments fail with a concise
usage message and status `2` before Podman is inspected.

The local runner starts the selected isolated platform worker. Output identifies
the platform, and the final summary reports its result.

Molecule's experimental collection-only worker interface is not part of this
design. The repository runner owns each role-based scenario lifecycle;
GitHub supplies parallel execution across the four scenario jobs.

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

The local summary reports wall-clock invocation duration separately from the
worker stage durations.

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
         `-- Debian 13 ----------------------------|
```

The matrix uses `ubuntu-24.04`, `fail-fast: false`, and at most four concurrent
jobs. Debian is native AMD64 in GitHub Actions. Each matrix job invokes
the same platform worker used by the local command and has a bounded timeout.
The initial timeout allows for a cold image build and full package upgrade; it
must be tightened later if measured results support a smaller reliable bound.

The workflow has read-only repository permissions, uses no container-registry
credentials, and does not use `sudo`. GitHub runners are ephemeral, so their
images and containers disappear with the job. The workflow does not add a
redundant cleanup or image-pruning stage beyond scenario cleanup.

## Change-directed validation

The classifier retains the four ordered depths:

```text
fast -> ansible -> molecule -> full
```

Within `molecule`, `scripts/ci/molecule_plan.py` selects scenarios using the
checked-in `scripts/ci/molecule-impact.json`. The runner's `SCENARIOS` registry
supplies the complete scenario/platform set, including fallback when the map is
missing or invalid. Every selected scenario runs Debian 13. Platform-specific
impact selection is not supported.

Each map rule declares directory `prefixes`, exact-file `paths`, or both, plus
consuming selectors and a reason. Exact files outrank directory prefixes;
otherwise the most specific matching directory wins. Equally specific rules union their
consumers. This lets scenario-local tests select their own scenario while role
changes select all consumers. Scenario paths require an explicit scenario-directory
rule; a broad role rule or exact-file entry cannot cover a missing scenario
declaration. Exact-file entries do not participate in scenario-path matching.
Rules naming `all` broaden to the complete suite.
Selections and reasons are deduplicated and emitted in stable registry order.

The complete registry contains exactly these four rows:

| Scenario | Platform |
| --- | --- |
| `system_maintenance/default` | `debian13` |
| `system_maintenance/baseline` | `debian13` |
| `reverse_proxy/default` | `debian13` |
| `semaphore/default` | `debian13` |

| Changed input | Scenario selection |
| --- | --- |
| Maintenance role | Both maintenance scenarios |
| OS playbooks, host identity, or bootstrap | Baseline scenario |
| Podman foundation | Baseline and Semaphore scenarios |
| Shared security policy and OS baseline verifier | Baseline and proxy scenarios |
| Proxy or TLS role/playbook | Baseline and proxy scenarios |
| `tests/tls/` and the three TLS-specific Ansible test files | Baseline and proxy scenarios |
| Semaphore role or playbook | Semaphore scenario |
| Default maintenance scenario assertions | Default maintenance scenario |
| Proxy scenario assertions | Proxy scenario |
| Baseline or Semaphore scenario assertions | Corresponding scenario |
| Shared default scenario create, cleanup, or destroy | Complete suite |
| Molecule configuration, Containerfiles, create/destroy/cleanup | Complete suite |
| Runner, CI, classifier, impact map, dependencies | Complete suite |
| Unknown Molecule-relevant path or invalid impact map | Complete suite |
| Documentation under `docs/` or direct subsystem README | No Molecule scenarios |

The baseline scenario is a declared consumer of both proxy and TLS roles. A
proxy-only role change therefore excludes `system_maintenance/default` but
retains `system_maintenance/baseline`. The proxy also imports shared firewall
policy and verification from `security_baseline` and `os_baseline_verify`, so
changes to those roles select both consumers. A maintenance role change excludes
the proxy scenario. Shared create, cleanup, and destroy implementations under the
default maintenance scenario serve all four scenarios.

The TLS-specific Ansible files are `test_tls_role.py`, `test_tls_proxy_policy.py`,
and `test_tls_fixture_material.py` under `tests/ansible/`. These exact files and
`tests/tls/` cover TLS runtime, policy, and disposable certificate fixtures used
by the baseline and proxy scenarios. Other files under `tests/ansible/` continue
to require full validation. Changes to this classifier or impact map also require
the complete suite, even when combined with TLS changes.

The existing Git discovery retains deletion paths, both rename/copy paths, and
local committed, staged, unstaged, and untracked paths. Discovery failure or an
empty changed-path set selects `full`. A new scenario path enters the Molecule
tier even if its role was previously static-only; missing impact mapping selects
the complete registered suite. Add its runner registration and impact consumers
together. An absent or invalid runner registry fails CI rather than reporting
partial coverage as successful.

The generated plan records resolved base/head commit SHAs, whether worktree
changes are included, `none`, `selective`, or `full` mode, and exact matrix rows
with reasons. SHAs are null when Git cannot resolve the requested range; the
result still requires full validation. Local output explicitly marks worktree
inclusion because a commit SHA does not identify uncommitted content.

GitHub Actions consumes the generated matrix and appends the plan to the job
summary. Scheduled and manually dispatched workflows force `full`, which always
selects every registered scenario on Debian. An explicitly forced
Molecule tier without mapped Molecule inputs also selects the complete suite.
`mise run ci:changed` executes only the selected scenarios and clears the internal
platform override so local validation cannot silently omit Debian.
`mise run ci` continues to execute the complete local validation suite.

The merge gate requires successful Ansible and aggregate Molecule job results
for `molecule` and `full`. It also rejects empty, malformed, duplicate, unknown,
or incomplete platform rows, and requires complete registry coverage in `full`
mode. Skipped Molecule jobs remain acceptable only for `fast` and `ansible`.
A failed or cancelled matrix worker makes the required aggregate result fail.
The matrix retains `fail-fast: false` and no continue-on-error behavior.

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
- GitHub scenario jobs continue independently after another job fails, so the
  final workflow report includes all available scenario evidence.

## Validation and measurement

Implementation follows repository test and validation policy:

1. add focused unit and contract tests for selector parsing, platform planning,
   timing aggregation, classifier selection, and merge-gate reconciliation;
2. run `mise run bootstrap` after dependency changes;
3. use `mise run validate:fast` and `mise run validate:ansible` while iterating;
4. run the scenario locally through its public command on the supported local
   Podman environment;
5. run `mise run ci:changed` before claiming completion or publication; and
6. collect successful GitHub matrix timing from CI.

Focused tests use positive current invariants as their oracles. They require the
exact Debian role dispatch set, the exact Debian 13 Molecule set, and the
complete four-row planned matrix. Complete OS provisioning
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
2. `mise run test:molecule -- system_maintenance/default` runs Debian with
   Podman only;
3. local ARM64 and AMD64 runs select native Debian, and GitHub's AMD64 matrix
   runs Debian natively;
4. Podman is rootless and all scenario hosts use `container_privileged: false`
   and `container_systemd: always`; maintenance scenarios add no capabilities,
   while application scenarios use only the allowances defined above;
5. one embedded global preflight rejects invalid selectors, unavailable or
   rootful Podman, cgroup v1, and overlapping local invocations before touching
   container state, without a privilege or runtime fallback;
6. every worker pulls its fully qualified maintained tag exactly once with an
   always-refresh policy, builds with no second pull, and logs the resolved
   local image identity;
7. base and built images remain after local testing, while exact label-owned
   scenario containers are destroyed after both success and failure;
8. converge, deterministic-task idempotence, and independent verification pass
   for Debian; live full-upgrade tasks run during converge and are
   excluded only from the idempotence pass because maintained package
   repositories can change during a scenario run;
9. native automatic reboot policy remains enabled by default and disabled in
   disposable scenarios; explicit playbook reboots remain a separate lifecycle;
10. verification covers representative current Debian role invariants;
11. the classifier retains four depths and selects affected scenario/platform
    rows within `molecule`; unknown impact selects the complete Molecule suite,
    shallower changes retain their depth, and `full` runs all validation;
12. GitHub executes the bounded four-row scenario matrix and the stable
    merge gate requires its success when selected;
13. local and GitHub workers report pull, build, Molecule, and platform-total
    timing; the local summary and implementation report add their respective
    overall elapsed times to evaluate future prebuilt images;
14. acquisition, build, assertion, and cleanup failures are distinguishable;
15. no test contacts inventory hosts, reads Vault material, uses infrastructure
    credentials, or claims VM or hardware evidence; and
16. support claims, executable behavior, Molecule coverage, CI execution, and
    documentation agree on the Debian platform set while generic CPU-
    architecture logic remains; and
17. all repository-required change-directed validation passes from the issue
    branch before completion is claimed.
