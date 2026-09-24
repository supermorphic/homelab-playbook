from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys


FILES = Path(__file__).resolve().parents[2] / "roles/talos_lifecycle/files"
sys.path.insert(0, str(FILES))
import runtime  # noqa: E402


class RuntimeTests(unittest.TestCase):
    def test_prepared_request_uses_derived_target_and_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            request_path.write_text("{}", encoding="utf-8")
            request = {
                "talos_node": "node-a",
                "talos_source_dir": str(root),
                "talos_source_revision": "a" * 40,
                "talos_kubeconfig": "/operator/kubeconfig",
                "talos_kube_context": "selected-kube",
                "talos_talosconfig": "/operator/talosconfig",
                "talos_talos_context": "selected-talos",
                "talos_confirmation": "reboot:node-a:192.0.2.10",
            }
            source = {
                "revision": "a" * 40,
                "snapshot_dir": str(root / "snapshot"),
                "verification_dir": str(root / "snapshot"),
                "api_server": "https://192.0.2.20:6443",
                "nodes": {"node-a": "192.0.2.10", "node-b": "192.0.2.11", "node-c": "192.0.2.12"},
                "talos_endpoints": ["192.0.2.10", "192.0.2.11", "192.0.2.12"],
            }
            captured: dict[str, object] = {}

            class Process:
                def __init__(self, argv: list[str], **kwargs: object) -> None:
                    captured["argv"] = argv
                    captured["env"] = kwargs["env"]
                    captured["prepared"] = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))

                def wait(self) -> int:
                    return 0

                def poll(self) -> int:
                    return 0

            with (
                mock.patch.object(runtime, "load_request", return_value=request),
                mock.patch.object(runtime, "prepare_source", return_value=source),
                mock.patch.object(runtime, "_tools", return_value={name: f"/tools/{name}" for name in
                                  ("bash", "python3", "kubectl", "talosctl", "yq", "mise")}),
                mock.patch.object(runtime, "_bash_major", return_value=5),
                mock.patch.object(runtime, "invoke_verifier", return_value={}),
                mock.patch.object(runtime.subprocess, "Popen", Process),
                mock.patch.dict(runtime.os.environ, {"TALOS_LIFECYCLE_MISE_DATA_DIR": "/mise-data",
                                                     "TALOS_LIFECYCLE_CANCEL_PATH": str(root / "cancel"),
                                                     "TALOS_LIFECYCLE_TRUSTED_ORIGIN":
                                                         "https://github.com/supermorphic/homelab-talos.git"}),
            ):
                self.assertEqual(runtime.run("reboot", request_path), 0)

            prepared = captured["prepared"]
            self.assertEqual(prepared["address"], "192.0.2.10")
            self.assertEqual(prepared["talos_kube_context"], "selected-kube")
            self.assertEqual(prepared["talos_talos_context"], "selected-talos")
            environment = captured["env"]
            self.assertNotIn("NODE_KUBECTL", environment)
            self.assertNotIn("KUBECONFIG", environment)
            self.assertNotIn("ANSIBLE_CONFIG", environment)

    def test_confirmation_is_bound_to_derived_address(self) -> None:
        request = {"talos_node": "node-a", "talos_confirmation": "reboot:node-a:192.0.2.99"}
        with self.assertRaises(runtime.RuntimeFailure):
            runtime._confirmation("reboot", request, "192.0.2.10")

    def test_abrupt_evidence_directory_is_unique_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = {"talos_evidence_dir": str(Path(temporary) / "evidence")}
            first = runtime._make_evidence_dir(request)
            second = runtime._make_evidence_dir(request)
            self.assertNotEqual(first, second)
            self.assertEqual(first.stat().st_mode & 0o777, 0o700)
            self.assertEqual(first.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
