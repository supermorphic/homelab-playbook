"""Disposable host preflight rejects unsafe inputs before remote operations."""

import base64
import hashlib
import importlib
from pathlib import Path
import struct
import unittest
from unittest.mock import patch


def target_descriptor():
    key_type = b"ssh-ed25519"
    key = struct.pack(">I", len(key_type)) + key_type + struct.pack(">I", 32) + b"x" * 32
    return {
        "ssh_alias": "runner-debian-fixture",
        "host_key": "ssh-ed25519 " + base64.b64encode(key).decode(),
        "host_key_fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("="),
        "ownership_marker": "synthetic-57",
        "state_root": "/var/lib/forgejo-runner-fixture/synthetic-57",
        "controller": {"user": "fixture-controller", "uid": 2201, "gid": 2201,
                       "subuid_start": 1000000, "subgid_start": 1000000, "subid_count": 65536},
        "worker": {"user": "fixture-worker", "uid": 2202, "gid": 2202,
                   "subuid_start": 1065536, "subgid_start": 1065536, "subid_count": 65536},
        "limits": {"memory_bytes": 2147483648, "pids": 512, "disk_bytes": 8589934592,
                   "cpu_percent": 100, "idle_seconds": 60, "job_seconds": 7200},
        "allowed_endpoints": [{"address": "192.0.2.10", "port": 443}],
        "denied_endpoints": [{"address": "198.51.100.10", "port": 8080}],
    }


def observation():
    return {
        "os_id": "debian", "os_version": "13", "architecture": "x86_64",
        "effective_uid": 0, "cgroup_v2": True, "user_namespaces": True,
        "marker": {"value": "synthetic-57", "uid": 0, "mode": 0o600, "regular": True},
        "state_root_exists": False, "state_parent_secure": True,
        "users": [{"user": "root", "uid": 0, "gid": 0}],
        "groups": [{"group": "root", "gid": 0}],
        "subuids": [], "subgids": [],
        "commands": {name: True for name in (
            "podman", "systemctl", "systemd-run", "ip", "nft", "mount", "umount",
            "newuidmap", "newgidmap")},
    }


class HostFixtureTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module("scripts.forgejo_runner.host_fixture")
        except ModuleNotFoundError:
            self.fail("disposable host descriptor/preflight implementation is missing")

    def test_valid_target_keeps_explicit_allocations(self):
        target = target_descriptor()
        self.assertEqual(target, self.module.validate_target(target))

    def test_preflight_rejects_unsafe_descriptor_without_remote_operation(self):
        cases = []
        for key, value in (("ssh_alias", "-oProxyCommand=bad"),
                           ("ssh_alias", "fixture; touch /tmp/bad"),
                           ("state_root", "/"),
                           ("state_root", "/var/lib/forgejo-runner-fixture/../production"),
                           ("ownership_marker", "../other"),
                           ("host_key_fingerprint", "SHA256:wrong"),
                           ("host_key", target_descriptor()["host_key"] + "\nother")):
            item = target_descriptor()
            item[key] = value
            cases.append(item)
        for key in ("uid", "gid", "subuid_start", "subgid_start"):
            item = target_descriptor()
            item["worker"][key] = item["controller"][key]
            cases.append(item)
        for key in target_descriptor()["limits"]:
            for value in (0, -1, True):
                item = target_descriptor()
                item["limits"][key] = value
                cases.append(item)
        item = target_descriptor()
        item["inventory"] = "inventory/production"
        cases.append(item)
        with patch("subprocess.run", side_effect=AssertionError("invalid input reached SSH")):
            for item in cases:
                with self.subTest(item=item), self.assertRaises(ValueError):
                    self.module.inspect_target(item)

    def test_preflight_rejects_wrong_host_or_owner_without_mutation(self):
        cases = []
        for key, value in (("os_id", "fedora"), ("os_version", "12"),
                           ("cgroup_v2", False), ("effective_uid", 2202),
                           ("user_namespaces", False), ("state_root_exists", True),
                           ("state_parent_secure", False)):
            item = observation()
            item[key] = value
            cases.append(item)
        for key, value in (("value", "foreign"), ("uid", 2202), ("mode", 0o666), ("regular", False)):
            item = observation()
            item["marker"][key] = value
            cases.append(item)
        item = observation()
        item["users"].append({"user": "other", "uid": 2202, "gid": 5000})
        cases.append(item)
        item = observation()
        item["groups"].append({"group": "other", "gid": 2201})
        cases.append(item)
        for key in ("subuids", "subgids"):
            item = observation()
            item[key] = [{"user": "other", "start": 1065500, "count": 65536}]
            cases.append(item)
        item = observation()
        item["commands"]["nft"] = False
        cases.append(item)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.module.validate_observation(target_descriptor(), item)

    def test_preflight_rejects_account_ids_in_existing_subordinate_ranges(self):
        for key in ("subuids", "subgids"):
            item = observation()
            item[key] = [{"user": "another-account", "start": 2000, "count": 65536}]
            with self.subTest(allocation=key), self.assertRaises(ValueError):
                self.module.validate_observation(target_descriptor(), item)

    def test_successful_preflight_reports_observed_architecture(self):
        result = self.module.validate_observation(target_descriptor(), observation())
        self.assertEqual("amd64", result["architecture"])
        self.assertFalse(result["state_root_exists"])
        self.assertNotIn("ready", result)  # Preflight is not containment acceptance.

    def test_malformed_observation_fails_closed(self):
        for value in (None, [], {}, {"os_id": "debian"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module.validate_observation(target_descriptor(), value)

    def test_ssh_pins_host_key_and_restricts_interactive_side_effects(self):
        target = target_descriptor()
        captured = {}

        def execute(argv, **kwargs):
            import subprocess
            captured["argv"] = argv
            captured["input"] = kwargs["input"]
            host_file = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("UserKnownHostsFile="))
            self.assertEqual("synthetic-57 " + target["host_key"] + "\n", Path(host_file).read_text())
            return subprocess.CompletedProcess(argv, 0, __import__("json").dumps(observation()), "")

        with patch("subprocess.run", side_effect=execute):
            result = self.module.inspect_target(target)
        self.assertEqual("amd64", result["architecture"])
        for option in ("StrictHostKeyChecking=yes", "BatchMode=yes", "ClearAllForwardings=yes",
                       "PermitLocalCommand=no", "RequestTTY=no", "RemoteCommand=none", "ForwardAgent=no"):
            self.assertIn(option, captured["argv"])
        self.assertEqual(["runner-debian-fixture", "sudo -n -- /usr/bin/python3 -"], captured["argv"][-2:])
        self.assertIn("synthetic-57", captured["input"])

    def test_preflight_does_not_claim_full_host_acceptance(self):
        import json
        import tempfile
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=root / ".tmp") as directory:
            descriptor = Path(directory) / "target.json"
            descriptor.write_text(json.dumps(target_descriptor()))
            with patch.object(self.module, "inspect_target", return_value={**observation(), "architecture": "amd64"}):
                self.assertEqual(0, self.module.run(None, descriptor, preflight_only=True))
                with self.assertRaises(RuntimeError):
                    self.module.run(None, descriptor)

    def test_host_descriptor_cannot_read_inventory_inputs(self):
        root = Path(__file__).resolve().parents[2]
        with patch("pathlib.Path.read_text", side_effect=AssertionError("inventory was read")):
            with self.assertRaises(ValueError):
                self.module.run(None, root / "inventory/production/host_vars/host/secret.sops.yml",
                                preflight_only=True)

    def test_ssh_error_does_not_publish_remote_output(self):
        import subprocess
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "private-output", "private-error")):
            with self.assertRaises(RuntimeError) as caught:
                self.module.inspect_target(target_descriptor())
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
