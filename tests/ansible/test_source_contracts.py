"""Offline safety contracts for repository Ansible sources."""

from __future__ import annotations

import configparser
import subprocess
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
    def test_os_provision_rejects_arch_before_configuration_roles(self) -> None:
        """Complete provisioning must not apply unsupported hardening to Arch."""
        result = subprocess.run(
            [
                "ansible-playbook",
                "playbooks/os/provision.yml",
                "--inventory",
                "servers,",
                "--connection",
                "local",
                "--limit",
                "servers",
                "--start-at-task",
                "Validate complete provisioning platform support",
                "--extra-vars",
                '{"ansible_become": false, "ansible_os_family": "Archlinux"}',
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not support Archlinux", output)
        self.assertNotIn("Install common packages on Arch Linux", output)

    def test_frozen_k3s_manifest_templates_resolve_to_files(self) -> None:
        """Every configured local K3s manifest template must exist."""
        variables = load_yaml_documents(
            "inventory/frozen/k3s/group_vars/k3s_cluster/vars.yml"
        )[0]
        playbook_dir = REPOSITORY_ROOT / "playbooks/k3s"
        template_prefix = "{{ playbook_dir }}/"
        configured_templates = variables.get("k3s_server_manifests_templates")

        self.assertIsInstance(configured_templates, list)
        self.assertTrue(configured_templates)
        for configured_template in configured_templates:
            self.assertTrue(configured_template.startswith(template_prefix))
            relative_template = configured_template.removeprefix(template_prefix)
            resolved_template = (playbook_dir / relative_template).resolve()
            self.assertTrue(resolved_template.is_relative_to(REPOSITORY_ROOT))
            self.assertTrue(
                resolved_template.is_file(),
                f"configured K3s manifest template does not exist: {relative_template}",
            )

    def test_cifs_module_load_uses_available_builtin_with_truthful_status(self) -> None:
        """The best-effort module load must use a resolved action without changes."""
        tasks = load_tasks("roles/prepare_cifs_storage/tasks/setup-Debian.yml")
        module_load_tasks = [
            task
            for task in tasks
            if task["name"] == "Load required kernel module dm_crypt"
        ]

        self.assertEqual(len(module_load_tasks), 1)
        module_load = module_load_tasks[0]
        self.assertEqual(module_load.get("ansible.builtin.command"), "modprobe dm_crypt")
        self.assertIs(module_load.get("changed_when"), False)
        self.assertIs(module_load.get("failed_when"), False)
        self.assertNotIn("ignore_errors", module_load)

    def test_enable_cgroup_preserves_existing_cmdline_mode(self) -> None:
        """Cgroup installation and removal must retain the cmdline file mode."""
        for action in ("install", "uninstall"):
            tasks = load_tasks(f"roles/enable_cgroup/tasks/{action}.yml")
            stat_task = next(task for task in tasks if "ansible.builtin.stat" in task)
            copy_task = next(task for task in tasks if "ansible.builtin.copy" in task)

            self.assertEqual(
                stat_task["register"],
                "enable_cgroup_cmdline_stat",
            )
            self.assertEqual(
                copy_task["ansible.builtin.copy"]["mode"],
                "{{ enable_cgroup_cmdline_stat.stat.mode }}",
            )

    def test_system_maintenance_dispatches_supported_operating_systems(self) -> None:
        """Arch, Debian, and RedHat hosts must resolve to maintained task files."""
        tasks = load_tasks("roles/system_maintenance/tasks/main.yml")
        first_task = tasks[0]
        second_task = tasks[1]

        self.assertIn("ansible.builtin.assert", first_task)
        self.assertEqual(
            first_task["ansible.builtin.assert"],
            {
                "that": [
                    "ansible_os_family in ['Archlinux', 'Debian', 'RedHat']"
                ],
                "fail_msg": (
                    "system-maintenance does not support operating-system family "
                    "received {{ ansible_os_family }}"
                ),
            },
        )
        self.assertEqual(
            second_task["ansible.builtin.include_tasks"],
            "setup-{{ ansible_os_family }}.yml",
        )
        self.assertNotIn("when", second_task)
        for os_family in ("Archlinux", "Debian", "RedHat"):
            self.assertTrue(
                (
                    REPOSITORY_ROOT
                    / "roles/system_maintenance/tasks"
                    / f"setup-{os_family}.yml"
                ).is_file(),
                f"missing system-maintenance tasks for {os_family}",
            )

    def test_redhat_maintenance_sets_kernel_retention_before_upgrade(self) -> None:
        """RedHat updates must retain two kernels through supported DNF policy."""
        tasks = load_tasks("roles/system_maintenance/tasks/setup-RedHat.yml")
        retention_tasks = [
            (index, task)
            for index, task in enumerate(tasks)
            if task.get("community.general.ini_file", {}).get("option")
            == "installonly_limit"
        ]
        upgrade_index = next(
            index
            for index, task in enumerate(tasks)
            if task.get("ansible.builtin.dnf", {}).get("update_only") is True
        )

        self.assertEqual(len(retention_tasks), 1)
        retention_index, retention_task = retention_tasks[0]
        self.assertLess(retention_index, upgrade_index)
        self.assertEqual(
            retention_task["community.general.ini_file"],
            {
                "path": "/etc/dnf/dnf.conf",
                "section": "main",
                "option": "installonly_limit",
                "value": "2",
                "no_extra_spaces": True,
                "mode": "0644",
            },
        )
        self.assertTrue(
            any(
                task.get("ansible.builtin.dnf", {}).get("autoremove") is True
                for task in tasks
            )
        )

    def test_arch_maintenance_generates_the_configured_english_locale(self) -> None:
        """Arch locale setup must use its native locale.gen source."""
        tasks = load_tasks("roles/system_maintenance/tasks/setup-Archlinux.yml")

        self.assertFalse(
            any("community.general.locale_gen" in task for task in tasks)
        )

        inspect_task = next(
            task for task in tasks if task["name"] == "Inspect generated locales"
        )
        self.assertEqual(
            inspect_task["ansible.builtin.command"],
            {"argv": ["locale", "-a"]},
        )
        self.assertEqual(
            inspect_task["register"],
            "system_maintenance_arch_available_locales",
        )
        self.assertIs(inspect_task["changed_when"], False)

        definition_task = next(
            task
            for task in tasks
            if task["name"] == "Enable the English locale definition"
        )
        self.assertEqual(
            definition_task["ansible.builtin.lineinfile"],
            {
                "path": "/etc/locale.gen",
                "regexp": r"^#?\s*en_US\.UTF-8\s+UTF-8$",
                "line": "en_US.UTF-8 UTF-8",
                "mode": "0644",
            },
        )
        self.assertEqual(
            definition_task["register"],
            "system_maintenance_arch_locale_definition",
        )

        generate_task = next(
            task for task in tasks if task["name"] == "Generate the English locale"
        )
        self.assertEqual(
            generate_task["ansible.builtin.command"],
            {"argv": ["locale-gen"]},
        )
        self.assertEqual(
            generate_task["when"],
            (
                "system_maintenance_arch_locale_definition.changed or "
                "'en_US.utf8' not in "
                "system_maintenance_arch_available_locales.stdout_lines"
            ),
        )
        self.assertIs(generate_task["changed_when"], True)

    def test_system_maintenance_reboots_are_enabled_by_default_and_controllable(
        self,
    ) -> None:
        """Container tests may suppress reboots without changing production."""
        defaults_path = REPOSITORY_ROOT / "roles/system_maintenance/defaults/main.yml"
        self.assertTrue(defaults_path.is_file())
        defaults = load_yaml_documents(
            "roles/system_maintenance/defaults/main.yml"
        )[0]
        self.assertEqual(
            True,
            defaults["system_maintenance_reboot_enabled"],
        )

        expected_conditions = {
            "roles/system_maintenance/tasks/setup-Debian.yml": [
                "system_maintenance_reboot_required_file.stat.exists",
                "system_maintenance_reboot_enabled | bool",
            ],
            "roles/system_maintenance/tasks/setup-RedHat.yml": [
                "system_maintenance_needs_restart.rc == 1",
                "system_maintenance_reboot_enabled | bool",
            ],
        }
        for task_path, conditions in expected_conditions.items():
            with self.subTest(task_path=task_path):
                reboot_tasks = [
                    task
                    for task in load_tasks(task_path)
                    if task["name"] == "Reboot if required"
                ]
                self.assertEqual(1, len(reboot_tasks))
                self.assertEqual(conditions, reboot_tasks[0]["when"])

    def test_update_pihole_has_no_end_play(self) -> None:
        """A Pi-hole command failure must fail normally instead of ending a play."""
        tasks = load_tasks("roles/update_pihole/tasks/main.yml")
        command_tasks = [
            task for task in tasks if "ansible.builtin.command" in task
        ]

        self.assertEqual(
            [
                (task["ansible.builtin.command"], task.get("changed_when"))
                for task in command_tasks
            ],
            [("pihole status", False)],
        )
        self.assertFalse(
            any("ignore_errors" in task for task in tasks),
        )
        self.assertFalse(
            any("failed_when" in task for task in tasks),
        )
        self.assertFalse(
            any(task.get("ansible.builtin.meta") == "end_play" for task in tasks)
        )

    def test_dns_commands_do_not_treat_nonzero_rc_as_changed(self) -> None:
        """DNS command failures must remain failures, never reported as changes."""
        tasks = load_tasks("roles/update_pihole/tasks/update-dns.yml")

        self.assertEqual(len(tasks), 2)
        self.assertTrue(
            all("ansible.builtin.command" in task for task in tasks)
        )
        self.assertTrue(
            all(task.get("changed_when") is True for task in tasks)
        )
        self.assertTrue(
            all("failed_when" not in task and "ignore_errors" not in task for task in tasks)
        )

    def test_verify_playbook_commands_are_read_only(self) -> None:
        """The operator verification playbook may only inspect Pi-hole and Unbound."""
        playbook_path = REPOSITORY_ROOT / "playbooks/pihole/verify.yml"
        self.assertTrue(playbook_path.is_file())
        play = load_yaml_documents("playbooks/pihole/verify.yml")[0][0]
        tasks = play["tasks"]

        self.assertEqual(play["hosts"], "pihole")
        self.assertTrue(play["become"])
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            [
                (task.get("ansible.builtin.command"), task.get("changed_when"))
                for task in tasks
            ],
            [
                ("pihole status", False),
                ("unbound-checkconf /etc/unbound/unbound.conf", False),
            ],
        )

    def test_os_inspect_reports_only_allowlisted_facts(self) -> None:
        """Host inspection must remain read-only and omit identifying facts."""
        playbook_path = REPOSITORY_ROOT / "playbooks/os/inspect.yml"
        self.assertTrue(playbook_path.is_file())
        play = load_yaml_documents("playbooks/os/inspect.yml")[0][0]
        tasks = play["tasks"]

        allowed_facts = [
            "ansible_architecture",
            "ansible_distribution",
            "ansible_distribution_release",
            "ansible_distribution_version",
            "ansible_kernel",
            "ansible_os_family",
            "ansible_pkg_mgr",
            "ansible_python_version",
            "ansible_service_mgr",
            "ansible_virtualization_role",
            "ansible_virtualization_type",
        ]

        self.assertEqual(play["hosts"], "servers")
        self.assertIs(play["become"], False)
        self.assertIs(play["gather_facts"], False)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            tasks[0]["ansible.builtin.setup"],
            {
                "filter": allowed_facts,
                "gather_subset": [
                    "!all",
                    "!min",
                    "architecture",
                    "distribution",
                    "distribution_release",
                    "distribution_version",
                    "kernel",
                    "os_family",
                    "pkg_mgr",
                    "python_version",
                    "service_mgr",
                    "virtualization_role",
                    "virtualization_type",
                ],
            },
        )
        self.assertIs(tasks[0]["no_log"], True)
        reported_facts = {
            name: " ".join(expression.split())
            for name, expression in tasks[1]["ansible.builtin.debug"]["msg"].items()
        }
        self.assertEqual(
            reported_facts,
            {
                fact.removeprefix("ansible_"): (
                    "{{ inspected_host.ansible_facts."
                    f"{fact} | default('unknown', true) }}}}"
                )
                for fact in allowed_facts
            },
        )

    def test_ansible_cfg_uses_only_repository_role_paths(self) -> None:
        """Controller roles must resolve only from repository-managed locations."""
        config = configparser.ConfigParser()
        config.read(REPOSITORY_ROOT / "ansible.cfg")

        self.assertEqual(config["defaults"]["roles_path"], "./.ansible/roles:./roles")
        self.assertEqual(config["defaults"]["collections_path"], "./.ansible/collections")

    def test_ansible_cfg_does_not_disable_host_key_checking(self) -> None:
        """Host-key verification must remain enabled unless Ansible's default is used."""
        config = configparser.ConfigParser()
        config.read(REPOSITORY_ROOT / "ansible.cfg")

        if config.has_option("defaults", "host_key_checking"):
            self.assertEqual(config["defaults"]["host_key_checking"].strip().lower(), "true")


if __name__ == "__main__":
    unittest.main()
