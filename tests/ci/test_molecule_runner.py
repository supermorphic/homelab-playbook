from __future__ import annotations

import importlib.util
import hashlib
import gc
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPOSITORY_ROOT / "scripts" / "molecule.py"


def load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("molecule_runner", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner_module = load_runner()


@dataclass(frozen=True)
class FakeResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeCommandRunner:
    def __init__(self, *responses: FakeResult | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], Path, float | None]] = []
        self.operations: list[tuple[str, list[str]]] = []
        self.stream_calls: list[
            tuple[list[str], Path, dict[str, str] | None, float | None, str]
        ] = []

    def next_response(self, command: list[str]) -> FakeResult:
        if not self.responses:
            raise AssertionError(f"unexpected command: {command}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def capture(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> FakeResult:
        del env
        self.calls.append((list(command), cwd, timeout))
        self.operations.append(("capture", list(command)))
        return self.next_response(list(command))

    def stream(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        line_prefix: str,
    ) -> FakeResult:
        copied_env = None if env is None else dict(env)
        self.stream_calls.append(
            (list(command), cwd, copied_env, timeout, line_prefix)
        )
        self.operations.append(("stream", list(command)))
        return self.next_response(list(command))


class RunnerCliTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER_PATH), *arguments],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_cli_rejects_every_input_except_the_registered_selector(self) -> None:
        invalid_arguments = (
            (),
            ("unknown/default",),
            ("system_maintenance/unknown",),
            ("system_maintenance/default", "additional"),
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)

                self.assertEqual(2, result.returncode)
                self.assertIn("usage:", result.stderr.lower())
                self.assertIn("system_maintenance/default", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_expected_preflight_failure_exits_one_without_traceback(self) -> None:
        function = getattr(runner_module, "run", None)
        if function is None:
            self.fail("runner must provide an injectable run function")
        stderr = io.StringIO()
        fake = FakeCommandRunner(FakeResult(returncode=1, stdout="stale"))

        with redirect_stderr(stderr):
            result = function(["system_maintenance/default"], fake)

        self.assertEqual(1, result)
        self.assertIn("mise run bootstrap", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class PreflightTests(unittest.TestCase):
    def podman_info(
        self,
        *,
        architecture: str = "arm64",
        rootless: bool = True,
        cgroup_version: str = "v2",
    ) -> str:
        return json.dumps(
            {
                "host": {
                    "arch": architecture,
                    "cgroupVersion": cgroup_version,
                    "security": {"rootless": rootless},
                }
            }
        )

    def call_preflight(self, fake: FakeCommandRunner):
        function = getattr(runner_module, "preflight", None)
        if function is None:
            self.fail("runner must provide preflight")
        with mock.patch("shutil.which", return_value="/usr/local/bin/podman"):
            return function(REPOSITORY_ROOT, fake)

    def successful_runner(
        self,
        *,
        architecture: str = "arm64",
        cgroup_version: str = "v2",
    ) -> FakeCommandRunner:
        return FakeCommandRunner(
            FakeResult(stdout="Locked dependencies are current.\n"),
            FakeResult(stdout="podman version 5.6.2\n"),
            FakeResult(
                stdout=self.podman_info(
                    architecture=architecture,
                    cgroup_version=cgroup_version,
                )
            ),
        )

    def test_preflight_uses_native_debian_and_rocky_on_arm(
        self,
    ) -> None:
        fake = self.successful_runner(architecture="aarch64")

        plan = self.call_preflight(fake)

        self.assertEqual("podman version 5.6.2", plan.podman_version)
        self.assertEqual("arm64", plan.host_architecture)
        self.assertEqual(
            {
                "debian13": "arm64",
                "rockylinux9": "arm64",
            },
            plan.requested_architectures,
        )
        self.assertEqual(15.0, fake.calls[-1][2])

    def test_preflight_normalizes_amd64_host_aliases(self) -> None:
        for architecture in ("amd64", "x86_64"):
            with self.subTest(architecture=architecture):
                plan = self.call_preflight(
                    self.successful_runner(architecture=architecture)
                )

                self.assertEqual("amd64", plan.host_architecture)
                self.assertEqual(
                    {
                        "debian13": "amd64",
                        "rockylinux9": "amd64",
                    },
                    plan.requested_architectures,
                )

    def test_preflight_rejects_stale_dependencies_before_resolving_podman(
        self,
    ) -> None:
        fake = FakeCommandRunner(FakeResult(returncode=1, stdout="stale"))
        function = getattr(runner_module, "preflight", None)
        if function is None:
            self.fail("runner must provide preflight")

        with mock.patch("shutil.which") as which:
            with self.assertRaisesRegex(RuntimeError, "mise run bootstrap"):
                function(REPOSITORY_ROOT, fake)

        which.assert_not_called()
        self.assertEqual(1, len(fake.calls))

    def test_preflight_rejects_missing_podman(self) -> None:
        fake = FakeCommandRunner(FakeResult())
        function = getattr(runner_module, "preflight", None)
        if function is None:
            self.fail("runner must provide preflight")

        with mock.patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Podman"):
                function(REPOSITORY_ROOT, fake)

        self.assertEqual(1, len(fake.calls))

    def test_preflight_requires_rootless_podman(self) -> None:
        fake = FakeCommandRunner(
            FakeResult(),
            FakeResult(stdout="podman version 5.6.2"),
            FakeResult(stdout=self.podman_info(rootless=False)),
        )

        with self.assertRaisesRegex(RuntimeError, "rootless"):
            self.call_preflight(fake)

    def test_preflight_accepts_only_cgroup_v2(self) -> None:
        for cgroup_version in ("v1", "1", ""):
            with self.subTest(cgroup_version=cgroup_version):
                fake = self.successful_runner(cgroup_version=cgroup_version)

                with self.assertRaisesRegex(RuntimeError, "cgroup v2"):
                    self.call_preflight(fake)

        for cgroup_version in ("v2", "2"):
            with self.subTest(cgroup_version=cgroup_version):
                plan = self.call_preflight(
                    self.successful_runner(cgroup_version=cgroup_version)
                )
                self.assertEqual("arm64", plan.host_architecture)

    def test_preflight_reports_platform_specific_runtime_recovery(self) -> None:
        for operating_system, expected in (
            ("Darwin", "podman machine start"),
            ("Linux", "rootless Podman"),
        ):
            with self.subTest(operating_system=operating_system):
                fake = FakeCommandRunner(
                    FakeResult(),
                    FakeResult(stdout="podman version 5.6.2"),
                    FakeResult(returncode=125, stderr="service unavailable"),
                )
                with mock.patch("platform.system", return_value=operating_system):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        self.call_preflight(fake)

    def test_preflight_reports_timed_out_runtime_without_traceback_contract(
        self,
    ) -> None:
        fake = FakeCommandRunner(
            FakeResult(),
            FakeResult(stdout="podman version 5.6.2"),
            subprocess.TimeoutExpired(["podman", "info"], 15),
        )

        with mock.patch("platform.system", return_value="Darwin"):
            with self.assertRaisesRegex(RuntimeError, "podman machine start"):
                self.call_preflight(fake)

    def test_preflight_rejects_unsupported_host_architecture(self) -> None:
        fake = self.successful_runner(architecture="ppc64le")

        with self.assertRaisesRegex(RuntimeError, "unsupported.*ppc64le"):
            self.call_preflight(fake)


class InvocationLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temporary_root = Path(self.temporary_directory.name)
        self.common_directory = self.temporary_root / "repository.git"
        self.common_directory.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def git_runner(self) -> FakeCommandRunner:
        return FakeCommandRunner(
            FakeResult(stdout=f"{self.common_directory}\n"),
        )

    def lock_path(self, repo_root: Path, fake: FakeCommandRunner) -> Path:
        function = getattr(runner_module, "lock_path", None)
        if function is None:
            self.fail("runner must provide lock_path")
        with mock.patch(
            "tempfile.gettempdir",
            return_value=str(self.temporary_root),
        ):
            return function(repo_root, fake)

    def invocation_lock(self, fake: FakeCommandRunner):
        function = getattr(runner_module, "invocation_lock", None)
        if function is None:
            self.fail("runner must provide invocation_lock")
        return function(REPOSITORY_ROOT, fake)

    def test_linked_worktrees_derive_one_lock_from_the_git_common_directory(
        self,
    ) -> None:
        first_root = self.temporary_root / "first-worktree"
        second_root = self.temporary_root / "second-worktree"
        expected_digest = hashlib.sha256(
            str(self.common_directory.resolve()).encode("utf-8")
        ).hexdigest()[:16]

        first = self.lock_path(first_root, self.git_runner())
        second = self.lock_path(second_root, self.git_runner())

        self.assertEqual(first, second)
        self.assertEqual(
            self.temporary_root
            / f"homelab-playbook-molecule-{expected_digest}.lock",
            first,
        )

    def test_invocation_lock_is_nonblocking_and_releases_after_success(
        self,
    ) -> None:
        with mock.patch(
            "tempfile.gettempdir",
            return_value=str(self.temporary_root),
        ):
            with self.invocation_lock(self.git_runner()):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with self.invocation_lock(self.git_runner()):
                        self.fail("a second invocation acquired the same lock")

            with self.invocation_lock(self.git_runner()):
                pass

    def test_invocation_lock_releases_after_an_exception(self) -> None:
        with mock.patch(
            "tempfile.gettempdir",
            return_value=str(self.temporary_root),
        ):
            with self.assertRaisesRegex(ValueError, "test failure"):
                with self.invocation_lock(self.git_runner()):
                    raise ValueError("test failure")

            with self.invocation_lock(self.git_runner()):
                pass

    def test_invocation_lock_rejects_a_symlink(self) -> None:
        fake = self.git_runner()
        path = self.lock_path(REPOSITORY_ROOT, fake)
        path.symlink_to(self.temporary_root / "untrusted-target")

        with mock.patch(
            "tempfile.gettempdir",
            return_value=str(self.temporary_root),
        ):
            with self.assertRaisesRegex(RuntimeError, "safe lock file"):
                with self.invocation_lock(self.git_runner()):
                    self.fail("a symlink was accepted as an invocation lock")


class ContainerOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform = runner_module.Platform(
            name="debian13",
            base_image="docker.io/library/debian:13",
            image="localhost/homelab-playbook-system-maintenance-debian13:local",
            container="homelab-playbook-system-maintenance-debian13",
            containerfile=Path("Containerfile.debian13"),
        )

    def call_remove(self, fake: FakeCommandRunner) -> bool:
        function = getattr(runner_module, "remove_owned_container", None)
        if function is None:
            self.fail("runner must provide remove_owned_container")
        return function(REPOSITORY_ROOT, fake, self.platform)

    def owned_inspect(self, *, platform: str = "debian13") -> str:
        return json.dumps(
            [
                {
                    "Config": {
                        "Labels": {
                            "io.supermorphic.homelab-playbook.repository": (
                                "homelab-playbook"
                            ),
                            "io.supermorphic.homelab-playbook.scenario": (
                                "system_maintenance/default"
                            ),
                            "io.supermorphic.homelab-playbook.platform": platform,
                        }
                    }
                }
            ]
        )

    def test_absent_container_requires_no_removal(self) -> None:
        fake = FakeCommandRunner(FakeResult(returncode=1))

        removed = self.call_remove(fake)

        self.assertIs(removed, False)
        self.assertEqual(
            [
                (
                    [
                        "podman",
                        "container",
                        "exists",
                        "homelab-playbook-system-maintenance-debian13",
                    ],
                    REPOSITORY_ROOT,
                    None,
                )
            ],
            fake.calls,
        )

    def test_owned_container_is_inspected_then_removed(self) -> None:
        fake = FakeCommandRunner(
            FakeResult(),
            FakeResult(stdout=self.owned_inspect()),
            FakeResult(),
        )

        removed = self.call_remove(fake)

        self.assertIs(removed, True)
        self.assertEqual(
            [
                [
                    "podman",
                    "container",
                    "exists",
                    "homelab-playbook-system-maintenance-debian13",
                ],
                [
                    "podman",
                    "container",
                    "inspect",
                    "homelab-playbook-system-maintenance-debian13",
                    "--format",
                    "json",
                ],
                [
                    "podman",
                    "rm",
                    "--force",
                    "homelab-playbook-system-maintenance-debian13",
                ],
            ],
            [command for command, _, _ in fake.calls],
        )

    def test_colliding_container_is_never_removed(self) -> None:
        fake = FakeCommandRunner(
            FakeResult(),
            FakeResult(stdout=self.owned_inspect(platform="rockylinux9")),
        )

        with self.assertRaisesRegex(RuntimeError, "collision"):
            self.call_remove(fake)

        self.assertEqual(2, len(fake.calls))

    def test_container_inspection_error_is_not_treated_as_absence(self) -> None:
        fake = FakeCommandRunner(
            FakeResult(returncode=125, stderr="storage unavailable")
        )

        with self.assertRaisesRegex(RuntimeError, "inspect"):
            self.call_remove(fake)


class QueueClock:
    def __init__(self, *values: float) -> None:
        self.values = list(values)

    def __call__(self) -> float:
        if not self.values:
            raise AssertionError("worker read the clock more often than expected")
        return self.values.pop(0)


class PlatformWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform = runner_module.Platform(
            name="debian13",
            base_image="docker.io/library/debian:13",
            image="localhost/homelab-playbook-system-maintenance-debian13:local",
            container="homelab-playbook-system-maintenance-debian13",
            containerfile=Path("Containerfile.debian13"),
        )
        self.host_plan = runner_module.HostPlan(
            podman_version="podman version 5.6.2",
            host_architecture="arm64",
            requested_architectures={"debian13": "arm64"},
        )

    def owned_inspect(self) -> str:
        return ContainerOwnershipTests().owned_inspect()

    def image_inspect(self) -> str:
        return json.dumps(
            [
                {
                    "Id": "sha256:base-image-id",
                    "RepoDigests": [
                        "docker.io/library/debian@sha256:observed-digest"
                    ],
                }
            ]
        )

    def call_worker(
        self,
        fake: FakeCommandRunner,
        clock: QueueClock,
    ):
        function = getattr(runner_module, "run_platform", None)
        if function is None:
            self.fail("runner must provide run_platform")
        return function(
            self.platform,
            self.host_plan,
            REPOSITORY_ROOT,
            fake,
            "run-123",
            clock,
        )

    def test_worker_pulls_builds_and_tests_one_platform_with_portable_options(
        self,
    ) -> None:
        fake = FakeCommandRunner(
            FakeResult(returncode=1),
            FakeResult(),
            FakeResult(stdout=self.image_inspect()),
            FakeResult(),
            FakeResult(),
            FakeResult(returncode=1),
        )
        clock = QueueClock(0, 1, 3, 4, 7, 8, 13, 14, 15, 16)
        scenario_directory = (
            REPOSITORY_ROOT
            / "roles"
            / "system_maintenance"
            / "molecule"
            / "default"
        )

        result = self.call_worker(fake, clock)

        self.assertIs(result.success, True)
        self.assertEqual("pass", result.status)
        self.assertEqual("sha256:base-image-id", result.image_id)
        self.assertEqual(
            "docker.io/library/debian@sha256:observed-digest",
            result.repository_digest,
        )
        self.assertEqual(2, result.pull_seconds)
        self.assertEqual(3, result.build_seconds)
        self.assertEqual(5, result.molecule_seconds)
        self.assertEqual(1, result.cleanup_seconds)
        self.assertEqual(16, result.platform_seconds)
        self.assertEqual(
            [
                (
                    "capture",
                    [
                        "podman",
                        "container",
                        "exists",
                        "homelab-playbook-system-maintenance-debian13",
                    ],
                ),
                (
                    "stream",
                    [
                        "podman",
                        "pull",
                        "--platform",
                        "linux/arm64",
                        "docker.io/library/debian:13",
                    ],
                ),
                (
                    "capture",
                    [
                        "podman",
                        "image",
                        "inspect",
                        "docker.io/library/debian:13",
                        "--format",
                        "json",
                    ],
                ),
                (
                    "stream",
                    [
                        "podman",
                        "build",
                        "--pull=never",
                        "--platform",
                        "linux/arm64",
                        "--tag",
                        "localhost/homelab-playbook-system-maintenance-debian13:local",
                        "--file",
                        str(scenario_directory / "Containerfile.debian13"),
                        str(scenario_directory),
                    ],
                ),
                (
                    "stream",
                    [
                        "molecule",
                        "test",
                        "--scenario-name",
                        "default",
                        "--platform-name",
                        "debian13",
                        "--no-report",
                        "--no-command-borders",
                    ],
                ),
                (
                    "capture",
                    [
                        "podman",
                        "container",
                        "exists",
                        "homelab-playbook-system-maintenance-debian13",
                    ],
                ),
            ],
            fake.operations,
        )
        molecule_call = fake.stream_calls[2]
        self.assertEqual(REPOSITORY_ROOT / "roles/system_maintenance", molecule_call[1])
        self.assertEqual("[debian13]", molecule_call[4])
        self.assertEqual(
            str(REPOSITORY_ROOT / ".tmp/molecule/run-123/debian13"),
            molecule_call[2]["MOLECULE_EPHEMERAL_DIRECTORY"],
        )
        self.assertEqual(
            str(REPOSITORY_ROOT / "roles"),
            molecule_call[2].get("ANSIBLE_ROLES_PATH"),
        )
        self.assertEqual(
            str(REPOSITORY_ROOT / ".ansible/collections"),
            molecule_call[2].get("ANSIBLE_COLLECTIONS_PATH"),
        )
        self.assertEqual(
            "debian13",
            molecule_call[2].get("HOMELAB_MOLECULE_PLATFORM"),
        )

    def test_worker_cleans_up_after_each_primary_stage_failure(self) -> None:
        cases = (
            (
                "acquisition failure",
                [
                    FakeResult(returncode=1),
                    FakeResult(returncode=1, stderr="pull failed"),
                    FakeResult(returncode=1),
                ],
            ),
            (
                "build failure",
                [
                    FakeResult(returncode=1),
                    FakeResult(),
                    FakeResult(stdout=self.image_inspect()),
                    FakeResult(returncode=1, stderr="build failed"),
                    FakeResult(returncode=1),
                ],
            ),
            (
                "test failure",
                [
                    FakeResult(returncode=1),
                    FakeResult(),
                    FakeResult(stdout=self.image_inspect()),
                    FakeResult(),
                    FakeResult(returncode=1, stderr="molecule failed"),
                    FakeResult(returncode=1),
                ],
            ),
        )
        for expected_status, responses in cases:
            with self.subTest(expected_status=expected_status):
                fake = FakeCommandRunner(*responses)

                result = self.call_worker(
                    fake,
                    QueueClock(*range(20)),
                )

                self.assertIs(result.success, False)
                self.assertEqual(expected_status, result.status)
                self.assertEqual(
                    [
                        "podman",
                        "container",
                        "exists",
                        "homelab-playbook-system-maintenance-debian13",
                    ],
                    fake.operations[-1][1],
                )

    def test_cleanup_failure_preserves_the_primary_failure(self) -> None:
        fake = FakeCommandRunner(
            FakeResult(returncode=1),
            FakeResult(returncode=1, stderr="registry unavailable"),
            FakeResult(),
            FakeResult(stdout=self.owned_inspect()),
            FakeResult(returncode=1, stderr="remove failed"),
        )

        result = self.call_worker(fake, QueueClock(*range(20)))

        self.assertEqual("cleanup failure", result.status)
        self.assertIn("acquisition failure", result.message)
        self.assertIn("could not remove owned container", result.message)


class ParallelExecutionTests(unittest.TestCase):
    def platform_result(self, platform: str, status: str = "pass"):
        return runner_module.PlatformResult(
            platform=platform,
            status=status,
            message="test result",
            image_id="sha256:image",
            repository_digest=None,
            pull_seconds=1,
            build_seconds=2,
            molecule_seconds=3,
            cleanup_seconds=1,
            platform_seconds=7,
        )

    def registered_platforms(self):
        value = getattr(runner_module, "PLATFORMS", None)
        if value is None:
            self.fail("runner must register the platform matrix")
        return value

    def test_registered_platforms_have_stable_names_and_images(self) -> None:
        platforms = self.registered_platforms()

        self.assertEqual(
            [
                (
                    "debian13",
                    "docker.io/library/debian:13",
                    "localhost/homelab-playbook-system-maintenance-debian13:local",
                    "homelab-playbook-system-maintenance-debian13",
                    "Containerfile.debian13",
                ),
                (
                    "rockylinux9",
                    "docker.io/rockylinux/rockylinux:9",
                    "localhost/homelab-playbook-system-maintenance-rockylinux9:local",
                    "homelab-playbook-system-maintenance-rockylinux9",
                    "Containerfile.rockylinux9",
                ),
            ],
            [
                (
                    platform.name,
                    platform.base_image,
                    platform.image,
                    platform.container,
                    str(platform.containerfile),
                )
                for platform in platforms
            ],
        )

    def test_default_selection_runs_both_platforms_on_supported_architectures(
        self,
    ) -> None:
        for architecture in ("arm64", "amd64"):
            with self.subTest(architecture=architecture):
                selected = runner_module.select_platforms({}, architecture)
                self.assertEqual(
                    ["debian13", "rockylinux9"],
                    [platform.name for platform in selected],
                )

    def test_explicit_platform_selection_overrides_host_default(self) -> None:
        function = getattr(runner_module, "select_platforms", None)
        if function is None:
            self.fail("runner must provide select_platforms")

        for platform_name in ("debian13", "rockylinux9"):
            with self.subTest(platform_name=platform_name):
                selected = function(
                    {"HOMELAB_MOLECULE_PLATFORM": platform_name},
                    "arm64",
                )

                self.assertEqual(
                    [platform_name], [platform.name for platform in selected]
                )

    def test_unknown_workflow_platform_fails_before_any_command(self) -> None:
        fake = FakeCommandRunner()
        stderr = io.StringIO()

        with mock.patch.dict(
            "os.environ",
            {"HOMELAB_MOLECULE_PLATFORM": "archlinux"},
            clear=True,
        ):
            with redirect_stderr(stderr):
                result = runner_module.run(["system_maintenance/default"], fake)

        self.assertEqual(2, result)
        self.assertIn("unknown Molecule platform: archlinux", stderr.getvalue())
        self.assertEqual([], fake.operations)

    def test_all_workers_start_before_any_worker_can_finish(self) -> None:
        function = getattr(runner_module, "run_platforms", None)
        if function is None:
            self.fail("runner must provide run_platforms")
        platforms = self.registered_platforms()
        started: set[str] = set()
        started_lock = threading.Lock()
        all_started = threading.Event()

        def worker(platform):
            with started_lock:
                started.add(platform.name)
                if len(started) == 2:
                    all_started.set()
            if not all_started.wait(timeout=2):
                raise AssertionError("platform workers did not overlap")
            return self.platform_result(platform.name)

        results = function(platforms, worker)

        self.assertEqual(
            {"debian13", "rockylinux9"},
            {result.platform for result in results},
        )

    def test_parallel_execution_waits_for_results_after_one_failure(self) -> None:
        function = getattr(runner_module, "run_platforms", None)
        if function is None:
            self.fail("runner must provide run_platforms")
        completed: set[str] = set()
        completed_lock = threading.Lock()

        def worker(platform):
            with completed_lock:
                completed.add(platform.name)
            status = "test failure" if platform.name == "debian13" else "pass"
            return self.platform_result(platform.name, status)

        results = function(self.registered_platforms(), worker)

        self.assertEqual(
            {"debian13", "rockylinux9"},
            completed,
        )
        self.assertEqual(2, len(results))
        failed_result = next(
            result for result in results if result.platform == "debian13"
        )
        self.assertFalse(failed_result.success)


class OutputTests(unittest.TestCase):
    def result(self, platform: str = "debian13"):
        return runner_module.PlatformResult(
            platform=platform,
            status="pass",
            message="all Molecule phases passed",
            image_id="sha256:base-image-id",
            repository_digest=(
                "docker.io/library/debian@sha256:observed-digest"
            ),
            pull_seconds=1.25,
            build_seconds=2.5,
            molecule_seconds=3.75,
            cleanup_seconds=0.5,
            platform_seconds=8.0,
        )

    def host_plan(self):
        return runner_module.HostPlan(
            podman_version="podman version 5.6.2",
            host_architecture="arm64",
            requested_architectures={
                "debian13": "arm64",
                "rockylinux9": "arm64",
            },
        )

    def test_subprocess_stream_prefixes_every_combined_output_line(self) -> None:
        command_runner = runner_module.SubprocessCommandRunner()
        stream = getattr(command_runner, "stream", None)
        if stream is None:
            self.fail("subprocess runner must provide stream")
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as directory:
            with warnings.catch_warnings(record=True) as captured_warnings:
                warnings.simplefilter("always", ResourceWarning)
                with redirect_stdout(output):
                    result = stream(
                        [
                            sys.executable,
                            "-c",
                            (
                                "import sys; print('standard output'); "
                                "print('standard error', file=sys.stderr)"
                            ),
                        ],
                        cwd=Path(directory),
                        line_prefix="[debian13]",
                    )
                gc.collect()

        self.assertEqual(0, result.returncode)
        self.assertEqual([], captured_warnings)
        self.assertEqual(
            {
                "[debian13] standard output",
                "[debian13] standard error",
            },
            set(output.getvalue().splitlines()),
        )

    def test_summary_reports_provenance_architecture_timings_and_result(
        self,
    ) -> None:
        function = getattr(runner_module, "emit_summary", None)
        if function is None:
            self.fail("runner must provide emit_summary")
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "github-summary.md"
            summary_path.touch(mode=0o600)
            environment = {"GITHUB_STEP_SUMMARY": str(summary_path)}
            with redirect_stdout(output):
                function(self.host_plan(), [self.result()], 12.5, environment)
            github_summary = summary_path.read_text(encoding="utf-8")

        terminal_summary = output.getvalue()
        for fragment in (
            "podman version 5.6.2",
            "debian13",
            "host=arm64",
            "requested=arm64",
            "native",
            "docker.io/library/debian:13",
            "sha256:base-image-id",
            "docker.io/library/debian@sha256:observed-digest",
            "pull=1.25s",
            "build=2.50s",
            "molecule=3.75s",
            "cleanup=0.50s",
            "total=8.00s",
            "result=pass",
            "Invocation total: 12.50s",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, terminal_summary)
        self.assertIn("### Molecule platform summary", github_summary)
        self.assertIn("| debian13 | arm64 | arm64 | native |", github_summary)
        self.assertIn("Build: 2.50 seconds", github_summary)
        self.assertIn("Invocation total: 12.50 seconds", github_summary)

    def test_summary_never_follows_a_github_summary_symlink(self) -> None:
        function = getattr(runner_module, "emit_summary", None)
        if function is None:
            self.fail("runner must provide emit_summary")

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            target = directory_path / "target.md"
            target.write_text("operator content\n", encoding="utf-8")
            symlink = directory_path / "summary.md"
            symlink.symlink_to(target)
            with redirect_stdout(io.StringIO()):
                function(
                    self.host_plan(),
                    [self.result()],
                    12.5,
                    {"GITHUB_STEP_SUMMARY": str(symlink)},
                )

            self.assertEqual("operator content\n", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
