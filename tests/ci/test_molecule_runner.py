from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest

from contextlib import redirect_stderr
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
        if not self.responses:
            raise AssertionError(f"unexpected command: {command}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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

    def test_preflight_uses_native_debian_and_rocky_but_emulates_arch_on_arm(
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
                "archlinux": "amd64",
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
                        "archlinux": "amd64",
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


if __name__ == "__main__":
    unittest.main()
