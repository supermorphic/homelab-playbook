from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import tomllib
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
    def test_failed_probe_start_cleans_up_the_created_container(self) -> None:
        runner = load_runner()
        probe_name = None
        present = False

        def engine(argv, **_kwargs):
            nonlocal probe_name, present
            code, stdout = 0, ""
            if argv[:2] == ["podman", "info"]:
                stdout = "true x86_64"
            elif argv[:2] == ["podman", "create"]:
                probe_name = argv[argv.index("--name") + 1]
                present = True
                stdout = "synthetic-probe"
            elif argv[:2] == ["podman", "start"]:
                # The API rejects start before attachment or auto-removal.
                code = 125
            elif argv[1:3] == ["container", "exists"]:
                self.assertEqual(probe_name, argv[-1])
                code = 0 if present else 1
            elif argv[1:3] == ["container", "rm"]:
                self.assertEqual(probe_name, argv[-1])
                present = False
            return subprocess.CompletedProcess(argv, code, stdout, "")

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(runner.subprocess, "run", side_effect=engine),
            mock.patch.object(runner, "EVIDENCE", Path(directory) / "evidence.md"),
        ):
            self.assertEqual(1, runner._compatibility())
        self.assertIsNotNone(probe_name)
        self.assertFalse(present, "failed start left the owned probe container")

    def test_cold_image_preparation_has_a_separate_budget_from_runtime_checks(self) -> None:
        runner = load_runner()
        prepared = False
        checked = False

        def engine(argv, **kwargs):
            nonlocal prepared, checked
            code, stdout = 0, ""
            if argv[:2] == ["podman", "info"]:
                stdout = "true x86_64"
            elif (runner.SEMAPHORE_IMAGE in argv and "--name" in argv
                  and argv[argv.index("--name") + 1].endswith("-probe")):
                # The initial ownership copy can take 130 seconds, while the
                # shell checks take one second. Model the two Podman phases.
                if kwargs["timeout"] < 130:
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                prepared = True
                stdout = "synthetic-probe"
            elif argv[:2] == ["podman", "start"]:
                self.assertTrue(prepared)
                self.assertEqual(120, kwargs["timeout"])
                checked = True
                code = runtime_exit
                stdout = "x86_64"
            elif "SHOW server_version;" in argv:
                stdout = "17.11"
            elif "--version" in argv and any(tool in argv for tool in ("pg_dump", "pg_restore")):
                stdout = "PostgreSQL) 17.11"
            return subprocess.CompletedProcess(argv, code, stdout, "")

        for runtime_exit in (0, 23):
            prepared = checked = False
            with self.subTest(runtime_exit=runtime_exit), tempfile.TemporaryDirectory() as directory:
                with (
                    mock.patch.object(runner.subprocess, "run", side_effect=engine),
                    mock.patch.object(runner, "EVIDENCE", Path(directory) / "evidence.md"),
                ):
                    self.assertEqual(0 if runtime_exit == 0 else 1, runner._compatibility())
                self.assertTrue(checked)

    def test_compatibility_waits_for_final_postgres_before_querying_version(self) -> None:
        runner = load_runner()
        tcp_ready = False

        def container_command(argv, **_kwargs):
            code, stdout = 0, ""
            if argv[:2] == ["podman", "info"]:
                stdout = "true x86_64"
            elif any("uname -m" in argument for argument in argv):
                stdout = "x86_64"
            elif "pg_isready" in argv:
                # The initialization server accepts sockets before the final
                # server opens TCP and the initialized database is stable.
                host = next((argv[index + 1] for index, argument in enumerate(argv)
                             if argument in ("--host", "-h")), None)
                ready = host is None or (host == "127.0.0.1" and tcp_ready)
                code = 0 if ready else 1
            elif "SHOW server_version;" in argv:
                code, stdout = (0, "17.11") if tcp_ready else (1, "")
            elif "--version" in argv and any(
                argument in ("pg_dump", "pg_restore") for argument in argv
            ):
                stdout = "PostgreSQL) 17.11"
            return subprocess.CompletedProcess(argv, code, stdout, "")

        def finish_initialization(_seconds):
            nonlocal tcp_ready
            tcp_ready = True

        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.md"
            with (
                mock.patch.object(runner.subprocess, "run", side_effect=container_command),
                mock.patch.object(runner.time, "sleep", side_effect=finish_initialization),
                mock.patch.object(runner, "EVIDENCE", evidence),
            ):
                self.assertEqual(0, runner._compatibility())
            self.assertIn("Overall experiment: passed", evidence.read_text())

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

    def test_opted_in_probe_failure_reports_exit_code_and_output(self) -> None:
        runner = load_runner()
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; print('probe started'); print('probe error', file=sys.stderr); sys.exit(17)"],
            capture_output=True, text=True, check=False,
        )
        with self.assertRaises(RuntimeError) as raised:
            runner._must(result, "probe failed", include_output=True)
        self.assertIn("exit 17", str(raised.exception))
        self.assertIn("probe started", str(raised.exception))
        self.assertIn("probe error", str(raised.exception))

    def test_failure_output_is_not_reported_without_explicit_opt_in(self) -> None:
        runner = load_runner()
        result = subprocess.CompletedProcess([], 1, "private stdout", "private stderr")
        with self.assertRaises(RuntimeError) as raised:
            runner._must(result, "command failed")
        self.assertEqual("command failed", str(raised.exception))

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

    def test_stock_app_maps_private_host_fixture_owner_to_runtime_user(self) -> None:
        runner = load_runner()
        run = runner.CompatibilityRun()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            runner._fixture(fixture)
            self.assertEqual(0o700, fixture.stat().st_mode & 0o777)
            config = tomllib.loads((fixture / ".mise.toml").read_text())
            for registry in config["settings"].get("aqua", {}).get("registries", []):
                self.assertTrue((fixture / registry).is_file(), registry)
            argv = runner._container(
                run, run.name("private-fixture"), [f"{fixture}:/fixture:ro"], "true"
            )
        self.assertIn("--userns", argv)
        self.assertEqual("keep-id:uid=1001", argv[argv.index("--userns") + 1])
        self.assertEqual("1001:0", argv[argv.index("--user") + 1])

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
        self.assertEqual(1800, child.call_args.kwargs.get("timeout"))
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
