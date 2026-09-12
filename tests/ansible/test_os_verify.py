"""Offline contracts for standalone OS baseline verification."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VERIFY_PLAYBOOK = REPOSITORY_ROOT / "playbooks/os/verify.yml"


def load_verify_play() -> dict[str, object]:
    if not VERIFY_PLAYBOOK.is_file():
        raise AssertionError(f"standalone OS verifier is missing: {VERIFY_PLAYBOOK}")
    with VERIFY_PLAYBOOK.open(encoding="utf-8") as source:
        documents = list(yaml.safe_load_all(source))
    return documents[0][0]


def load_tasks(relative_path: str) -> list[dict[str, object]]:
    with (REPOSITORY_ROOT / relative_path).open(encoding="utf-8") as source:
        return [task for document in yaml.safe_load_all(source) for task in document]


class StandaloneOsVerifyTests(unittest.TestCase):
    def test_protected_verifier_metadata_is_not_logged(self) -> None:
        cases = (
            (
                "roles/os_baseline_verify/tasks/identity.yml",
                "Read effective system timezone",
            ),
            (
                "roles/os_baseline_verify/tasks/identity.yml",
                "Verify effective host identity",
            ),
            (
                "roles/os_baseline_verify/tasks/access.yml",
                "Read effective SSH policy for the administrative connection",
            ),
        )

        for path, name in cases:
            with self.subTest(path=path, name=name):
                task = next(task for task in load_tasks(path) if task["name"] == name)
                self.assertIs(task.get("no_log"), True)

    def test_playbook_runs_fresh_observational_verification(self) -> None:
        play = load_verify_play()

        self.assertEqual("os_managed", play["hosts"])
        self.assertEqual(1, play["serial"])
        self.assertIs(play["any_errors_fatal"], True)
        self.assertIs(play["gather_facts"], False)
        self.assertIs(play["become"], False)

        pre_tasks = play["pre_tasks"]
        connection_preflight = next(
            task
            for task in pre_tasks
            if task.get("ansible.builtin.import_role")
            == {"name": "os_bootstrap", "tasks_from": "connection-preflight.yml"}
        )
        setup = next(
            task
            for task in pre_tasks
            if "ansible.builtin.setup" in task
        )
        self.assertLess(pre_tasks.index(connection_preflight), pre_tasks.index(setup))
        self.assertIsNone(setup["ansible.builtin.setup"])
        self.assertIs(setup["become"], True)

        defaults_by_name = {
            task["name"]: task["ansible.builtin.include_vars"]
            for task in pre_tasks
            if "ansible.builtin.include_vars" in task
        }
        self.assertEqual(
            {
                "Load OS security policy defaults": {
                    "file": "../../roles/security_baseline/defaults/main.yml",
                    "name": "os_verify_security_defaults",
                },
                "Load OS maintenance policy defaults": {
                    "file": "../../roles/system_maintenance/defaults/main.yml",
                    "name": "os_verify_maintenance_defaults",
                },
            },
            defaults_by_name,
        )

        verifier = play["tasks"][-1]
        self.assertEqual(
            {"name": "os_baseline_verify", "public": False},
            verifier["ansible.builtin.import_role"],
        )
        self.assertIs(verifier["become"], True)
        self.assertEqual(
            {
                "os_baseline_verify_expected_authorized_keys": (
                    "{{ security_baseline_authorized_keys }}"
                ),
                "os_baseline_verify_expected_firewall_services": (
                    "{{ os_verify_expected_firewall_services }}"
                ),
                "os_baseline_verify_expected_hostname": (
                    "{{ host_identity_hostname }}"
                ),
                "os_baseline_verify_expected_journal_system_keep_free": (
                    "{{ os_verify_expected_journal_system_keep_free }}"
                ),
                "os_baseline_verify_expected_journal_system_max_use": (
                    "{{ os_verify_expected_journal_system_max_use }}"
                ),
                "os_baseline_verify_expected_management_sources": (
                    "{{ security_baseline_management_sources }}"
                ),
                "os_baseline_verify_expected_timezone": (
                    "{{ host_identity_timezone }}"
                ),
                "system_maintenance_native_reboot_enabled": (
                    "{{ os_verify_native_reboot_enabled }}"
                ),
                "system_maintenance_security_reboot_time": (
                    "{{ os_verify_security_reboot_time }}"
                ),
                "system_maintenance_security_update_calendar": (
                    "{{ os_verify_security_update_calendar }}"
                ),
            },
            verifier["vars"],
        )

        prohibited_modules = {
            "ansible.builtin.apt",
            "ansible.builtin.package",
            "ansible.builtin.reboot",
            "ansible.builtin.service",
            "ansible.builtin.systemd_service",
        }
        self.assertTrue(
            all(
                prohibited_modules.isdisjoint(task)
                for task in [*pre_tasks, *play["tasks"]]
            )
        )

    def test_operator_docs_publish_the_standalone_read_only_action(self) -> None:
        readme = (REPOSITORY_ROOT / "playbooks/os/README.md").read_text(
            encoding="utf-8"
        )
        guide = (
            REPOSITORY_ROOT / "docs/guides/managed-host-onboarding.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`verify.yml`", readme)
        self.assertIn(
            "mise run playbook -- os verify production --limit nuc4",
            guide,
        )

    def test_policy_defaults_and_inventory_overrides_resolve_without_roles(self) -> None:
        play = load_verify_play()
        policy_tasks = [
            task
            for task in pre_tasks_without_connection(play["pre_tasks"])
            if task["name"]
            in {
                "Load OS security policy defaults",
                "Load OS maintenance policy defaults",
                "Resolve standalone OS verification policy",
            }
        ]
        self.assertEqual(3, len(policy_tasks))

        cases = {
            "producer-defaults": ({}, [[], "512M", "1G", True, "*-*-* 04:00:00", "04:30"]),
            "producer-overrides": (
                {
                    "security_baseline_firewall_services": ["ssh", "https"],
                    "security_baseline_journal_system_max_use": "768M",
                    "security_baseline_journal_system_keep_free": "2G",
                    "system_maintenance_native_reboot_enabled": False,
                    "system_maintenance_security_update_calendar": "*-*-* 02:00:00",
                    "system_maintenance_security_reboot_time": "02:30",
                },
                [["ssh", "https"], "768M", "2G", False, "*-*-* 02:00:00", "02:30"],
            ),
            "verifier-overrides": (
                {
                    "security_baseline_firewall_services": ["ssh"],
                    "os_baseline_verify_expected_firewall_services": ["ssh", "cockpit"],
                    "os_baseline_verify_expected_journal_system_max_use": "640M",
                    "os_baseline_verify_expected_journal_system_keep_free": "1536M",
                },
                [["ssh", "cockpit"], "640M", "1536M", True, "*-*-* 04:00:00", "04:30"],
            ),
        }
        for label, (variables, expected) in cases.items():
            with self.subTest(label=label):
                self._assert_policy_resolution(policy_tasks, variables, expected)

    def _assert_policy_resolution(
        self,
        policy_tasks: list[dict[str, object]],
        variables: dict[str, object],
        expected: list[object],
    ) -> None:
        executable_policy_tasks = deepcopy(policy_tasks)
        for task in executable_policy_tasks:
            include = task.get("ansible.builtin.include_vars")
            if include is not None:
                include["file"] = str(
                    (VERIFY_PLAYBOOK.parent / include["file"]).resolve()
                )
        assertion = {
            "name": "Assert resolved policy",
            "ansible.builtin.assert": {
                "that": [
                    "os_verify_expected_firewall_services == expected_policy[0]",
                    "os_verify_expected_journal_system_max_use == expected_policy[1]",
                    "os_verify_expected_journal_system_keep_free == expected_policy[2]",
                    "os_verify_native_reboot_enabled == expected_policy[3]",
                    "os_verify_security_update_calendar == expected_policy[4]",
                    "os_verify_security_reboot_time == expected_policy[5]",
                ]
            },
        }
        probe = [
            {
                "name": "Probe standalone OS verification policy",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "vars": {**variables, "expected_policy": expected},
                "tasks": [*executable_policy_tasks, assertion],
            }
        ]
        playbook_directory = REPOSITORY_ROOT / ".tmp"
        playbook_directory.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yml",
            dir=playbook_directory,
            encoding="utf-8",
        ) as source:
            yaml.safe_dump(probe, source, sort_keys=False)
            source.flush()
            result = subprocess.run(
                ["ansible-playbook", source.name, "--inventory", "localhost,"],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


def pre_tasks_without_connection(
    pre_tasks: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        task
        for task in pre_tasks
        if "ansible.builtin.include_vars" in task
        or "ansible.builtin.set_fact" in task
    ]


if __name__ == "__main__":
    unittest.main()
