from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPOSITORY_ROOT / "scripts" / "semaphore" / "test.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("semaphore_test_runner", RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Semaphore test runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SemaphoreTestCommandTests(unittest.TestCase):
    def test_unknown_mode_fails_argument_validation(self) -> None:
        result = subprocess.run(
            [sys.executable, str(RUNNER), "unknown"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("invalid choice", result.stderr)

    def test_required_shell_script_stops_at_first_failed_probe(self) -> None:
        runner = load_runner()

        result = subprocess.run(
            ["/bin/sh", "-c", runner._required_shell("false", "true")],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)

    def test_command_timeout_is_recorded_as_a_failure(self) -> None:
        runner = load_runner()
        run = runner.CompatibilityRun()

        with self.assertRaisesRegex(RuntimeError, "timed out"):
            run.command(
                "bounded child",
                [sys.executable, "-c", "import time; time.sleep(1)"],
                timeout=0.01,
            )

        self.assertEqual([("bounded child", 124)], run.results)

    def test_cleanup_failure_marks_evidence_failed(self) -> None:
        runner = load_runner()
        run = runner.CompatibilityRun()
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "evidence.md"
            with mock.patch.object(runner, "EVIDENCE", evidence):
                run.write_evidence(None, ["volume remains: owned-volume"])

            content = evidence.read_text(encoding="utf-8")

        self.assertIn("Overall experiment: failed: cleanup failed", content)
        self.assertIn("volume remains: owned-volume", content)

    def test_container_can_disable_network_for_cache_reuse_proof(self) -> None:
        runner = load_runner()
        run = runner.CompatibilityRun()

        argv = runner._container(
            run,
            run.name("offline"),
            ["tools:/tools:U"],
            "true",
            network="none",
        )

        self.assertIn("--network", argv)
        self.assertEqual("none", argv[argv.index("--network") + 1])

    def test_volume_is_tracked_before_a_create_timeout(self) -> None:
        runner = load_runner()
        run = runner.CompatibilityRun()
        with mock.patch.object(
            run, "command", side_effect=RuntimeError("create volume timed out")
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                run.volume("possibly-created")

        self.assertEqual(
            {f"{run.prefix}-possibly-created"},
            run.volumes,
        )

    def test_network_is_tracked_before_a_create_timeout(self) -> None:
        runner = load_runner()
        run = runner.CompatibilityRun()
        with mock.patch.object(
            run, "command", side_effect=RuntimeError("create network timed out")
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                run.network("possibly-created")

        self.assertEqual(
            {f"{run.prefix}-possibly-created"},
            run.networks,
        )

    def test_fixture_and_controller_are_registered_modes(self) -> None:
        runner = load_runner()

        self.assertEqual("fixture", runner._parser().parse_args(["fixture"]).mode)
        self.assertEqual("controller", runner._parser().parse_args(["controller"]).mode)

    def test_controller_dispatch_enables_only_the_integration_test(self) -> None:
        runner = load_runner()
        with mock.patch.object(runner, "_bounded_child", return_value=0) as child:
            self.assertEqual(0, runner._controller())

        argv = child.call_args.args[0]
        environment = child.call_args.kwargs["env"]
        self.assertIn("test_repository_job_integration.py", argv)
        self.assertEqual("1", environment["SEMAPHORE_REPOSITORY_JOB_INTEGRATION"])
        self.assertEqual(
            "localhost/homelab-playbook-semaphore-debian13:local",
            environment["SEMAPHORE_REPOSITORY_JOB_TEST_IMAGE"],
        )

    def test_restore_dispatch_forwards_only_restore_arguments(self) -> None:
        runner = load_runner()
        restore_arguments = [
            "--fixture",
            "--archive-id",
            "semaphore-20260910T010203Z-deadbeef",
            "--destination",
            "/tmp/restore-target",
        ]
        with mock.patch.object(runner, "_bounded_child", return_value=0) as child:
            self.assertEqual(0, runner._restore(restore_arguments))

        self.assertEqual(
            restore_arguments,
            child.call_args.args[0][-len(restore_arguments) :],
        )
        self.assertIn("restore.py", child.call_args.args[0][1])

    def test_bounded_child_interrupts_before_forced_cleanup(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "interrupted"
            program = (
                "import pathlib, signal, time\n"
                f"marker = pathlib.Path({str(marker)!r})\n"
                "def stop(*_):\n"
                "    marker.write_text('cleanup')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGINT, stop)\n"
                "while True: time.sleep(0.01)\n"
            )

            result = runner._bounded_child(
                [sys.executable, "-c", program],
                "disposable child",
                timeout=0.2,
            )

            self.assertEqual(124, result)
            self.assertEqual("cleanup", marker.read_text(encoding="utf-8"))

    def test_non_restore_mode_rejects_forwarded_arguments(self) -> None:
        result = subprocess.run(
            [sys.executable, str(RUNNER), "unit", "--fixture"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_restore_help_is_forwarded_to_restore_parser(self) -> None:
        result = subprocess.run(
            [sys.executable, str(RUNNER), "restore", "--help"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        self.assertEqual(0, result.returncode)
        self.assertIn("--archive-id", result.stdout)
        self.assertIn("--confirm-restore-experiment", result.stdout)


if __name__ == "__main__":
    unittest.main()
