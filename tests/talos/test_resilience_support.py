from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCENARIOS = Path(__file__).resolve().parents[2] / "roles/talos_lifecycle/files/scenarios"
sys.path.insert(0, str(SCENARIOS))
import resilience_support as support  # noqa: E402


class ResilienceSupportTests(unittest.TestCase):
    def supervisor(self, root: Path):  # type: ignore[no-untyped-def]
        path = root / "supervisor.json"
        path.write_text(json.dumps({"lifecyclePgid": os.getpgrp(), "ownerToken": "a" * 64}))
        return mock.patch.dict(os.environ, {
            "TALOS_LIFECYCLE_SUPERVISOR_PATH": str(path),
            "TALOS_LIFECYCLE_SUPERVISOR_TOKEN": "a" * 64,
        })

    def assert_process_stopped(self, pid: int) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-o", "state=", "-p", str(pid)], text=True,
                capture_output=True, check=False,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                return
            time.sleep(0.05)
        self.fail(f"bridge grandchild {pid} survived timeout")

    def test_recovery_bridge_budget_covers_all_sequential_waits(self) -> None:
        node_dir = SCENARIOS.parent / "node"
        recovery_source = (node_dir / "recovery.sh").read_text(encoding="utf-8")
        drain_source = (node_dir / "drain.sh").read_text(encoding="utf-8")
        verifier_source = (node_dir.parent / "verification.py").read_text(encoding="utf-8")
        self.assertIn('NODE_RECOVERY_RETURN_ATTEMPTS:-360', recovery_source)
        self.assertIn('NODE_RECOVERY_POLL_SECONDS:-5', recovery_source)
        self.assertIn('NODE_WORKLOAD_REPLACEMENT_ATTEMPTS:-60', drain_source)
        self.assertIn('NODE_WORKLOAD_REPLACEMENT_POLL_SECONDS:-5', drain_source)
        self.assertIn('timeout_seconds: int = 1800', verifier_source)
        recovery = support._command_timeout(["/trusted/abrupt-loss-bridge.sh", "recover", "/private/request"])
        expected = (
            support.RECOVERY_RETURN_WAIT_SECONDS
            + support.WORKLOAD_REPLACEMENT_WAIT_SECONDS
            + support.VERIFIER_DEADLINE_SECONDS
            + support.RECOVERY_MARGIN_SECONDS
        )
        self.assertEqual(recovery, expected)
        self.assertEqual(recovery, 4500)
        self.assertEqual(
            support._command_timeout(["/trusted/abrupt-loss-bridge.sh", "contain", "/private/request"]),
            support.DEFAULT_BRIDGE_TIMEOUT_SECONDS,
        )

    def test_timeout_terminates_and_reaps_the_whole_bridge_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "grandchild.pid"
            script = (
                "import pathlib,subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "time.sleep(60)"
            )
            with self.supervisor(Path(temporary)), mock.patch.object(
                support, "TERMINATION_GRACE_SECONDS", 0.2
            ), self.assertRaisesRegex(support.ScenarioFailure, "timed out"):
                support.run_command([sys.executable, "-c", script, str(pid_path)], timeout_seconds=0.2)
            grandchild = int(pid_path.read_text())
            self.assert_process_stopped(grandchild)

    def test_timeout_stops_group_after_bridge_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "grandchild.pid"
            script = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
            )
            with self.supervisor(Path(temporary)), mock.patch.object(
                support, "TERMINATION_GRACE_SECONDS", 0.2
            ), self.assertRaisesRegex(support.ScenarioFailure, "timed out"):
                support.run_command([sys.executable, "-c", script, str(pid_path)], timeout_seconds=0.2)
            self.assert_process_stopped(int(pid_path.read_text()))

    def test_success_stops_background_descendant_after_closed_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "grandchild.pid"
            script = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); print('ok')"
            )
            with self.supervisor(Path(temporary)), mock.patch.object(
                support, "TERMINATION_GRACE_SECONDS", 0.2
            ):
                self.assertEqual(
                    support.run_command([sys.executable, "-c", script, str(pid_path)], timeout_seconds=2).strip(),
                    "ok",
                )
            self.assert_process_stopped(int(pid_path.read_text()))

    def test_registration_failure_never_launches_bridge_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "started"
            with mock.patch.object(
                support, "_register_bridge", side_effect=support.ScenarioFailure("registration failed")
            ), mock.patch.object(support, "TERMINATION_GRACE_SECONDS", 0.2), self.assertRaisesRegex(
                support.ScenarioFailure, "registration failed"
            ):
                support.run_command([
                    sys.executable,
                    "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')",
                    str(marker),
                ])
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
