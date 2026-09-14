"""Exercise the timing callback with the pinned Ansible dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class CallbackTests(unittest.TestCase):
    def test_role_execution_ids_do_not_expose_ansible_node_identifiers(self):
        from scripts.callback_plugins.homelab_timing import CallbackModule
        from scripts.molecule_timing import EVENT_PREFIX

        with mock.patch.dict(os.environ, {"HOMELAB_TIMING_ROOT": str(ROOT)}):
            callback = CallbackModule()
        role = SimpleNamespace(
            _uuid="synthetic-node-identifier",
            get_role_path=lambda: str(ROOT / "roles/fixture"),
        )
        task = SimpleNamespace(
            _role=role, get_path=lambda: str(ROOT / "roles/fixture/tasks/main.yml:1")
        )
        records = []
        callback._display = SimpleNamespace(display=records.append)
        callback.v2_playbook_on_task_start(task, False)
        callback.v2_playbook_on_stats(None)
        first = json.loads(records[0].removeprefix(EVENT_PREFIX))["role_run"]
        self.assertNotIn("synthetic-node", first)
        callback.v2_playbook_on_task_start(task, False)
        callback.v2_playbook_on_stats(None)
        self.assertEqual(
            first, json.loads(records[1].removeprefix(EVENT_PREFIX))["role_run"]
        )

    def test_real_ansible_callback_emits_only_source_identifiers_and_timings(self):
        callback = ROOT / "scripts/callback_plugins/homelab_timing.py"
        self.assertTrue(callback.exists(), "Molecule needs a task timing callback")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "roles/fixture/tasks"
            tasks.mkdir(parents=True)
            (tasks / "main.yml").write_text(
                "- name: synthetic-sensitive-name\n"
                "  ansible.builtin.debug:\n"
                "    msg: synthetic-sensitive-result\n"
                "  no_log: true\n"
            )
            playbook = root / "play.yml"
            playbook.write_text(
                "- hosts: localhost\n  gather_facts: false\n  tasks:\n"
                "    - ansible.builtin.include_role:\n        name: fixture\n"
                "    - ansible.builtin.include_role:\n        name: fixture\n"
                "    - ansible.builtin.fail:\n        msg: synthetic-failure\n"
            )
            config = root / "ansible.cfg"
            config.write_text("[defaults]\nvars_plugins_enabled=host_group_vars\n")
            environment = dict(
                os.environ,
                ANSIBLE_CONFIG=str(config),
                ANSIBLE_CALLBACK_PLUGINS=str(callback.parent),
                ANSIBLE_CALLBACKS_ENABLED="homelab_timing",
                HOMELAB_TIMING_ROOT=str(root),
                ANSIBLE_NOCOLOR="1",
            )
            result = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
                env=environment,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(2, result.returncode, result.stderr)
            from scripts.molecule_timing import EVENT_PREFIX

            events = [
                json.loads(line.removeprefix(EVENT_PREFIX))
                for line in result.stdout.splitlines()
                if line.startswith(EVENT_PREFIX)
            ]
            self.assertGreaterEqual(len(events), 5, "missing callback events")
            encoded = json.dumps(events)
            self.assertNotIn("synthetic-sensitive", encoded)
            self.assertNotIn("synthetic-failure", encoded)
            self.assertNotIn(str(root), encoded)
            role_events = [
                event for event in events if event["role"] == "roles/fixture"
            ]
            self.assertEqual(2, len(role_events))
            self.assertEqual(2, len({event["role_run"] for event in role_events}))
            self.assertTrue(all(event["seconds"] >= 0 for event in events))


if __name__ == "__main__":
    unittest.main()
