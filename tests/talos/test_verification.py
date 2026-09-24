from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys


FILES = Path(__file__).resolve().parents[2] / "roles/talos_lifecycle/files"
sys.path.insert(0, str(FILES))
from verification import VerificationError, invoke_verifier, validate_response  # noqa: E402


def request(mode: str = "recovery") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "requestId": "4e17c4dd-f983-4e2c-b261-b620916201a3",
        "mode": mode,
        "node": "node-a",
        "sourceRevision": "a" * 40,
        "apiServer": "https://192.0.2.20:6443",
        "nodes": {"node-a": "192.0.2.10", "node-b": "192.0.2.11", "node-c": "192.0.2.12"},
        "talosEndpoints": ["192.0.2.10", "192.0.2.11", "192.0.2.12"],
        "credentials": {
            "kubeconfig": "/operator/kubeconfig",
            "kubeContext": "fixture-kube",
            "talosconfig": "/operator/talosconfig",
            "talosContext": "fixture-talos",
        },
        "expectedContainment": None if mode != "recovery" else {
            "node": "node-a", "record": '{"schemaVersion":1,"kind":"reboot"}'
        },
        "timeoutSeconds": 1800,
    }


def response(value: dict[str, object]) -> dict[str, object]:
    mode = str(value["mode"])
    return {
        "schemaVersion": 1,
        "requestId": value["requestId"],
        "mode": mode,
        "node": value["node"],
        "sourceRevision": value["sourceRevision"],
        "kubeContext": value["credentials"]["kubeContext"],  # type: ignore[index]
        "talosContext": value["credentials"]["talosContext"],  # type: ignore[index]
        "checks": {
            "source": "passed",
            "cilium": "not-run" if mode == "prepare" else "passed",
            "foundation": "not-run" if mode == "prepare" else "passed",
        },
    }


class ResponseTests(unittest.TestCase):
    def test_exact_prepare_baseline_and_recovery_results_are_accepted(self) -> None:
        for mode in ("prepare", "baseline", "recovery"):
            value = request(mode)
            validate_response(value, response(value), 0)

    def test_baseline_response_cannot_satisfy_recovery(self) -> None:
        value = request("baseline")
        result = response(value)
        value["mode"] = "recovery"
        with self.assertRaises(VerificationError):
            validate_response(value, result, 0)

    def test_nonzero_mismatch_missing_extra_and_incomplete_are_rejected(self) -> None:
        value = request()
        cases: list[tuple[dict[str, object], int]] = []
        cases.append((response(value), 7))
        for key in ("requestId", "mode", "node", "sourceRevision", "kubeContext", "talosContext"):
            result = response(value)
            result[key] = "wrong"
            cases.append((result, 0))
        result = response(value)
        del result["checks"]
        cases.append((result, 0))
        result = response(value)
        result["extra"] = True
        cases.append((result, 0))
        result = response(value)
        result["checks"]["foundation"] = "not-run"  # type: ignore[index]
        cases.append((result, 0))
        for result, returncode in cases:
            with self.subTest(result=result, returncode=returncode), self.assertRaises(VerificationError):
                validate_response(value, result, returncode)

    def test_fixed_invocation_rejects_malformed_multiple_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir()
            source = {"verification_dir": str(root), "mise": sys.executable, "bash": "/bin/bash",
                      "recovery_helm_cache": str(cache)}
            value = request("prepare")
            for stdout in ("not-json", "{}\n{}\n"):
                completed = mock.Mock(returncode=0, stdout=stdout, stderr="")
                with mock.patch("verification.subprocess.run", return_value=completed), self.assertRaises(VerificationError):
                    invoke_verifier(source, value, deadline_seconds=10)
            with mock.patch("verification.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("just", 1)), self.assertRaises(VerificationError):
                invoke_verifier(source, value, deadline_seconds=1)


if __name__ == "__main__":
    unittest.main()
