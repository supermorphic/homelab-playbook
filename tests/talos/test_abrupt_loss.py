from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCENARIOS = Path(__file__).resolve().parents[2] / "roles/talos_lifecycle/files/scenarios"
sys.path.insert(0, str(SCENARIOS))
import node_abrupt_loss as abrupt  # noqa: E402


class AbruptLossTests(unittest.TestCase):
    @staticmethod
    def prepared(node: str = "node-a") -> dict[str, object]:
        nodes = {"node-a": "192.0.2.10", "node-b": "192.0.2.11", "node-c": "192.0.2.12"}
        return {"node": node, "address": nodes[node], "nodes": nodes,
                "talos_kubeconfig": "/operator/kube", "talos_kube_context": "kube",
                "talos_talosconfig": "/operator/talos", "talos_talos_context": "talos",
                "talos_confirmation": f"remove-power:{node}:{nodes[node]}",
                "talos_test_confirmation": "chaos:node-abrupt-loss",
                "tools": {"kubectl": "/tools/kubectl", "talosctl": "/tools/talosctl", "dig": "/tools/dig", "curl": "/tools/curl"},
                "probe_dns_name": "echo.example.test", "probe_https_url": "https://echo.example.test/"}

    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def now(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds

    class Monitor:
        def __init__(self, events: list[str], violations: list[str] | None = None) -> None:
            self.events = events
            self._violations = violations or []

        def start(self) -> None:
            self.events.append("probes-start")

        def stop(self) -> list[dict[str, object]]:
            self.events.append("probes-stop")
            return [{"api": True, "dns": True, "https": True, "at": 0.0}]

        def violations(self) -> list[str]:
            return self._violations

    def test_all_four_loss_signals_are_required(self) -> None:
        observed = {
            "talosLost": True,
            "nodeNotReady": True,
            "etcdTargetLost": True,
            "quorumRetained": True,
        }
        self.assertTrue(abrupt.Controller._loss_observed(observed))
        for key in observed:
            with self.subTest(missing=key):
                incomplete = dict(observed)
                incomplete[key] = False
                self.assertFalse(abrupt.Controller._loss_observed(incomplete))

    def test_complete_attended_sequence_preserves_passive_and_recovery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events: list[str] = []
            clock = self.Clock()
            observations = [
                {"talosLost": False, "nodeNotReady": False, "etcdTargetLost": False, "quorumRetained": True},
                {"talosLost": True, "nodeNotReady": True, "etcdTargetLost": True, "quorumRetained": True},
            ]
            stable = {"quorumRetained": True, "readySurvivors": 2, "readyCiliumSurvivors": 2,
                 "workloads": [{"namespace": "apps", "ownerKind": "ReplicaSet", "ownerName": "echo", "state": "ready-on-survivor"}],
                 "storage": {"survivingReplicaAvailable": True, "pvcIdentityPreserved": True}}
            def observe() -> dict[str, object]:
                return observations.pop(0) if observations else stable
            controller = abrupt.Controller(
                prepared=self.prepared(),
                run_dir=Path(temporary), baseline=lambda: {"healthy": True},
                observe=observe, bridge=lambda action: events.append(f"bridge:{action}"),
                prompt=lambda message: events.append("disconnect" if "disconnect" in message.lower() else "restore") or "",
                monotonic=clock.now, sleep=clock.sleep, monitor=self.Monitor(events),
                loss_timeout=15, passive_seconds=600, poll_seconds=5,
            )
            controller.run()
            state = json.loads((Path(temporary) / "diagnostics" / abrupt.STATE_NAME).read_text())
            self.assertEqual(state["phase"], "recovered")
            self.assertEqual(events, ["probes-start", "disconnect", "bridge:contain", "restore", "bridge:recover", "probes-stop"])
            self.assertEqual(state["workloadRecoverySeconds"]["apps/ReplicaSet/echo"], 0.0)
            self.assertEqual(len(state["passiveObservation"]), 120)

    def test_probe_failure_cannot_be_erased_by_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = self.Clock()
            observation = {"talosLost": True, "nodeNotReady": True, "etcdTargetLost": True, "quorumRetained": True,
                           "readySurvivors": 2, "readyCiliumSurvivors": 2, "workloads": [],
                           "storage": {"survivingReplicaAvailable": True, "pvcIdentityPreserved": True}}
            controller = abrupt.Controller(
                prepared=self.prepared(),
                run_dir=Path(temporary), baseline=lambda: {}, observe=lambda: observation,
                bridge=lambda _action: None, prompt=lambda _message: "", monotonic=clock.now,
                sleep=clock.sleep, monitor=self.Monitor([], ["api"]), passive_seconds=5, poll_seconds=5,
            )
            with self.assertRaisesRegex(abrupt.ScenarioFailure, "api"):
                controller.run()
            recovery = json.loads((Path(temporary) / "recovery.json").read_text())
            self.assertEqual(recovery["status"], "passed")

    def test_external_monitor_uses_fixed_tools_context_targets_and_threshold(self) -> None:
        clock = self.Clock()
        calls: list[list[str]] = []
        def status(command: list[str]) -> tuple[int, str]:
            calls.append(command)
            return (1, "") if command[0] == "/tools/kubectl" else (0, "ok")
        monitor = abrupt.ExternalProbeMonitor(self.prepared(), status_runner=status,
                                              interval=5, no_success_limit=60, monotonic=clock.now)
        monitor._sample()
        clock.sleep(59)
        monitor._sample()
        self.assertEqual(monitor.violations(), [])
        clock.sleep(1)
        monitor._sample()
        self.assertEqual(monitor.violations(), ["api"])
        self.assertIn("--context", calls[0])
        self.assertTrue(any("echo.example.test" in call for call in calls))

    def test_baseline_preserves_owner_claim_and_off_target_replica_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            objects = {
                "nodes": {"items": [{"metadata": {"name": name}} for name in ("node-a", "node-b", "node-c")]},
                "pods": {"items": [{"metadata": {"name": "echo-old", "namespace": "apps", "uid": "pod-uid",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "echo", "uid": "owner-uid", "controller": True}]},
                    "spec": {"nodeName": "node-a", "volumes": [{"persistentVolumeClaim": {"claimName": "echo-data"}}]}}]},
                "replicas.longhorn.io": {"items": [{"spec": {"volumeName": "lh-volume", "nodeID": "node-a", "failedAt": ""}},
                    {"spec": {"volumeName": "lh-volume", "nodeID": "node-b", "failedAt": ""}}]},
                "persistentvolumeclaims": {"items": [{"metadata": {"name": "echo-data", "namespace": "apps", "uid": "pvc-uid"}, "spec": {"volumeName": "pv-echo"}}]},
                "persistentvolumes": {"items": [{"metadata": {"name": "pv-echo", "uid": "pv-uid"}, "spec": {"csi": {"volumeHandle": "lh-volume"}}}]},
            }
            calls: list[list[str]] = []
            def status(command: list[str]) -> tuple[int, str]:
                calls.append(command)
                resource = command[command.index("get") + 1]
                return 0, json.dumps(objects[resource])
            capacity_calls: list[list[str]] = []
            controller = abrupt.Controller(self.prepared(), Path(temporary), runner=lambda command: capacity_calls.append(command) or "",
                                            status_runner=status, monitor=self.Monitor([]))
            baseline = controller._baseline()
            self.assertEqual(baseline["targetWorkloads"][0]["ownerUid"], "owner-uid")
            self.assertEqual(baseline["affectedClaims"][0]["volumeUid"], "pv-uid")
            self.assertEqual(baseline["affectedVolumes"], ["lh-volume"])
            self.assertTrue(all("--context" in command for command in calls))
            self.assertEqual(len(capacity_calls), 1)
            self.assertNotIn("just", capacity_calls[0])

    def test_missing_loss_signal_never_contains_and_reports_uncontained_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = self.Clock()
            events: list[str] = []
            observation = {"talosLost": True, "nodeNotReady": True, "etcdTargetLost": False, "quorumRetained": True}
            controller = abrupt.Controller(self.prepared(), Path(temporary), baseline=lambda: {},
                observe=lambda: observation, bridge=lambda action: events.append(action),
                prompt=lambda message: events.append("restore" if "restore" in message.lower() else "disconnect") or "",
                monotonic=clock.now, sleep=clock.sleep, monitor=self.Monitor(events), loss_timeout=10, poll_seconds=5)
            with self.assertRaisesRegex(abrupt.ScenarioFailure, "four signals"):
                controller.run()
            self.assertNotIn("contain", events)
            recovery = json.loads((Path(temporary) / "recovery.json").read_text())
            self.assertEqual(recovery["status"], "failed")
            self.assertIn("uncontained", recovery["reason"])

    def test_eof_before_disruption_does_not_contain_or_request_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events: list[str] = []
            controller = abrupt.Controller(self.prepared(), Path(temporary), baseline=lambda: {},
                observe=dict, bridge=lambda action: events.append(action),
                prompt=lambda _message: (_ for _ in ()).throw(EOFError("closed")), monitor=self.Monitor(events))
            with self.assertRaisesRegex(abrupt.ScenarioFailure, "closed"):
                controller.run()
            self.assertNotIn("contain", events)
            recovery = json.loads((Path(temporary) / "recovery.json").read_text())
            self.assertEqual(recovery["status"], "passed")
            self.assertIn("before disruption", recovery["reason"])

    def test_primary_failure_and_recovery_failure_remain_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = self.Clock()
            events: list[str] = []
            observation = {"talosLost": True, "nodeNotReady": True, "etcdTargetLost": True, "quorumRetained": True,
                           "readySurvivors": 1, "readyCiliumSurvivors": 2, "workloads": [],
                           "storage": {"survivingReplicaAvailable": True, "pvcIdentityPreserved": True}}
            def bridge(action: str) -> None:
                events.append(action)
                if action == "recover":
                    raise abrupt.ScenarioFailure("recovery rejected")
            controller = abrupt.Controller(self.prepared(), Path(temporary), baseline=lambda: {}, observe=lambda: observation,
                bridge=bridge, prompt=lambda _message: "", monotonic=clock.now, sleep=clock.sleep,
                monitor=self.Monitor(events), passive_seconds=5, poll_seconds=5)
            with self.assertRaisesRegex(abrupt.ScenarioFailure, "exactly two"):
                controller.run()
            self.assertEqual(events.count("recover"), 1)
            recovery = json.loads((Path(temporary) / "recovery.json").read_text())
            self.assertEqual(recovery["status"], "failed")


if __name__ == "__main__":
    unittest.main()
