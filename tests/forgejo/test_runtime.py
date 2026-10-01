"""Catch removal of foreign resources and leakage of command output."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/forgejo/runtime.py"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), "Forgejo resource ownership is missing")
        spec = importlib.util.spec_from_file_location("forgejo_runtime", MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.fake = self.root / "podman"
        self.fake.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
p = pathlib.Path(__file__).with_name("state.json")
state = json.loads(p.read_text())
a = sys.argv[1:]
name = a[-1]
if a[:2] == ["container", "inspect"]:
    if name not in state: sys.exit(1)
    print(json.dumps([{"Config": {"Labels": state[name]}}]))
elif a[:2] == ["network", "inspect"]:
    print(json.dumps([{"labels": state[name]}]))
elif a[:2] == ["container", "exists"]:
    sys.exit(0 if name in state else 1)
elif a[:2] == ["network", "exists"]:
    sys.exit(0 if name in state else 1)
elif a[:2] == ["container", "rm"]:
    del state[name]
    p.write_text(json.dumps(state))
elif a[:2] == ["network", "rm"]:
    del state[name]
    p.write_text(json.dumps(state))
else: sys.exit(2)
''')
        self.fake.chmod(0o700)

    def test_cleanup_removes_owned_container(self):
        run = self.module.Run(podman=str(self.fake), run_id="owned123")
        self.state.write_text(json.dumps({"fixture": {run.label: "owned123"}}))
        run.resources.append(("container", "fixture"))
        self.assertEqual([], run.cleanup())
        self.assertEqual({}, json.loads(self.state.read_text()))

    def test_cleanup_preserves_foreign_container_and_reports_failure(self):
        run = self.module.Run(podman=str(self.fake), run_id="owned123")
        self.state.write_text(json.dumps({"foreign": {run.label: "another-run"}}))
        run.resources.append(("container", "foreign"))
        self.assertTrue(run.cleanup())
        self.assertIn("foreign", json.loads(self.state.read_text()))

    def test_cleanup_handles_network_inspection_schema(self):
        run = self.module.Run(podman=str(self.fake), run_id="owned123")
        self.state.write_text(json.dumps({"network": {run.label: "owned123"}}))
        run.resources.append(("network", "network"))
        self.assertEqual([], run.cleanup())
        self.assertEqual({}, json.loads(self.state.read_text()))

    def test_command_failure_does_not_expose_protected_output(self):
        run = self.module.Run(podman=str(self.fake), run_id="owned123")
        with self.assertRaises(RuntimeError) as caught:
            run.command(["/bin/sh", "-c", "echo fixture-secret >&2; exit 1"])
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_private_inputs_have_private_permissions(self):
        run = self.module.Run(podman=str(self.fake), run_id="owned123")
        path = run.private_file(self.root, "settings", "fixture-secret")
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual("fixture-secret", path.read_text())


if __name__ == "__main__":
    unittest.main()
