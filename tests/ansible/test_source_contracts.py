"""Offline source contracts for the active Pi-hole Ansible path."""

from __future__ import annotations

import configparser
import re
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_yaml_documents(relative_path: str) -> list[object]:
    with (REPOSITORY_ROOT / relative_path).open(encoding="utf-8") as source:
        return list(yaml.safe_load_all(source))


def load_tasks(relative_path: str) -> list[dict[str, object]]:
    return [task for document in load_yaml_documents(relative_path) for task in document]


class SourceContractTests(unittest.TestCase):
    def test_system_maintenance_asserts_debian_before_include(self) -> None:
        """Unsupported operating systems must fail before Debian tasks are included."""
        first_task = load_tasks("roles/system-maintenance/tasks/main.yml")[0]

        self.assertIn("ansible.builtin.assert", first_task)
        self.assertEqual(
            first_task["ansible.builtin.assert"],
            {
                "that": ["ansible_os_family == 'Debian'"],
                "fail_msg": (
                    "system-maintenance supports Debian-family hosts only; "
                    "received {{ ansible_os_family }}"
                ),
            },
        )

    def test_update_pihole_has_no_end_play(self) -> None:
        """A Pi-hole command failure must fail normally instead of ending a play."""
        role_tasks = [
            task
            for task_file in (REPOSITORY_ROOT / "roles/update-pihole/tasks").glob("*.yml")
            for task in load_tasks(str(task_file.relative_to(REPOSITORY_ROOT)))
        ]

        self.assertFalse(
            any(task.get("ansible.builtin.meta") == "end_play" for task in role_tasks)
        )

    def test_dns_commands_do_not_treat_nonzero_rc_as_changed(self) -> None:
        """DNS command failures must remain failures, never reported as changes."""
        tasks = load_tasks("roles/update-pihole/tasks/update-dns.yml")

        self.assertFalse(
            any(
                re.search(r"\brc\s*!=\s*0\b", str(task.get("changed_when", "")))
                for task in tasks
            )
        )

    def test_verify_playbook_commands_are_read_only(self) -> None:
        """The operator verification playbook may only inspect Pi-hole and Unbound."""
        playbook_path = REPOSITORY_ROOT / "playbooks/pihole/verify.yml"
        self.assertTrue(playbook_path.is_file())
        play = load_yaml_documents("playbooks/pihole/verify.yml")[0][0]
        commands = [
            task["ansible.builtin.command"]
            for task in play["tasks"]
            if "ansible.builtin.command" in task
        ]

        self.assertEqual(play["hosts"], "pihole")
        self.assertTrue(play["become"])
        self.assertEqual(
            commands,
            ["pihole status", "unbound-checkconf /etc/unbound/unbound.conf"],
        )
        self.assertTrue(all(task["changed_when"] is False for task in play["tasks"]))

    def test_ansible_cfg_uses_only_repository_role_paths(self) -> None:
        """Controller roles must resolve only from repository-managed locations."""
        config = configparser.ConfigParser()
        config.read(REPOSITORY_ROOT / "ansible.cfg")

        self.assertEqual(config["defaults"]["roles_path"], "./.ansible/roles:./roles")

    def test_ansible_cfg_does_not_disable_host_key_checking(self) -> None:
        """Host-key verification must remain enabled unless Ansible's default is used."""
        config = configparser.ConfigParser()
        config.read(REPOSITORY_ROOT / "ansible.cfg")

        self.assertNotEqual(
            config["defaults"].get("host_key_checking", "true").lower(), "false"
        )


if __name__ == "__main__":
    unittest.main()
