from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock


FILES = Path(__file__).resolve().parents[2] / "roles/talos_lifecycle/files"
sys.path.insert(0, str(FILES))
from inputs import InputError, load_request, validate_request  # noqa: E402


class InputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.kubeconfig = root / "kubeconfig"
        self.talosconfig = root / "talosconfig"
        self.source = root / "source"
        self.source.mkdir()
        self.kubeconfig.write_text(
            "apiVersion: v1\ncurrent-context: ambient\ncontexts:\n"
            "- name: ambient\n  context: {}\n- name: fixture\n  context: {}\n",
            encoding="utf-8",
        )
        self.talosconfig.write_text(
            "context: ambient\ncontexts:\n  ambient: {}\n  fixture: {}\n",
            encoding="utf-8",
        )
        self.base = {
            "talos_node": "node-a",
            "talos_kubeconfig": str(self.kubeconfig),
            "talos_kube_context": "fixture",
            "talos_talosconfig": str(self.talosconfig),
            "talos_talos_context": "fixture",
            "talos_source_dir": str(self.source),
            "talos_source_revision": "a" * 40,
        }

    def test_check_accepts_exact_base_keys_and_explicit_contexts(self) -> None:
        result = validate_request(dict(self.base), "maintenance-check")
        self.assertEqual(result["talos_kube_context"], "fixture")
        self.assertEqual(result["talos_talos_context"], "fixture")

    def test_unknown_and_control_keys_are_rejected(self) -> None:
        for key in ("ansible_connection", "action", "holder", "tool", "callback"):
            with self.subTest(key=key), self.assertRaises(InputError):
                validate_request({**self.base, key: "value"}, "maintenance-check")

    def test_bad_node_path_context_and_revision_are_rejected(self) -> None:
        cases = [
            {"talos_node": "node-*"},
            {"talos_node": ["node-a"]},
            {"talos_kubeconfig": "relative"},
            {"talos_kube_context": "missing"},
            {"talos_talos_context": "missing"},
            {"talos_source_revision": "main"},
        ]
        for update in cases:
            with self.subTest(update=update), self.assertRaises(InputError):
                validate_request({**self.base, **update}, "maintenance-check")

    def test_mutation_confirmations_are_action_bound(self) -> None:
        with self.assertRaises(InputError):
            validate_request(dict(self.base), "reboot")
        value = {**self.base, "talos_confirmation": "reboot:node-a:192.0.2.10"}
        self.assertEqual(validate_request(value, "reboot")["talos_confirmation"], value["talos_confirmation"])

    def test_abrupt_inputs_are_action_specific(self) -> None:
        evidence = Path(self.temporary.name) / "evidence"
        value = {
            **self.base,
            "talos_confirmation": "remove-power:node-a:192.0.2.10",
            "talos_test_confirmation": "chaos:node-abrupt-loss",
            "talos_evidence_dir": str(evidence),
        }
        with self.assertRaises(InputError):
            validate_request(value, "reboot")
        with mock.patch("inputs.has_controlling_terminal", return_value=True):
            result = validate_request(value, "abrupt-loss-test")
        self.assertEqual(result["talos_evidence_dir"], str(evidence.resolve()))

    def test_duplicate_json_keys_are_rejected(self) -> None:
        request = Path(self.temporary.name) / "request.json"
        request.write_text('{"talos_node":"node-a","talos_node":"node-b"}', encoding="utf-8")
        with self.assertRaises(InputError):
            load_request(request, "maintenance-check")

    def test_template_expressions_are_rejected(self) -> None:
        with self.assertRaises(InputError):
            validate_request({**self.base, "talos_node": "{{ lookup('env','NODE') }}"}, "maintenance-check")


if __name__ == "__main__":
    unittest.main()
