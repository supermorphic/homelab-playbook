from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


class RoleTests(unittest.TestCase):
    def test_playbooks_fix_each_action_on_localhost(self) -> None:
        for action in ("maintenance-check", "maintenance-enter", "maintenance-exit", "reboot", "abrupt-loss-test"):
            path = ROOT / "playbooks/talos" / f"{action}.yml"
            with self.subTest(action=action):
                play = yaml.safe_load(path.read_text())[0]
                self.assertEqual(play["hosts"], "localhost")
                self.assertEqual(play["connection"], "local")
                self.assertFalse(play["gather_facts"])
                self.assertFalse(play["become"])
                self.assertEqual(play["roles"][0]["vars"]["talos_lifecycle_action"], action)

    def test_role_uses_argv_once_and_always_removes_private_request(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/talos_lifecycle/tasks/main.yml").read_text())
        text = json_text = __import__("json").dumps(tasks)
        self.assertEqual(text.count('"argv"'), 1)
        self.assertIn("always", json_text)
        self.assertIn("no_log", json_text)
        self.assertIn("check_mode", json_text)


if __name__ == "__main__":
    unittest.main()
