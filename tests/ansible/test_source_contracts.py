"""Offline safety contracts for repository Ansible sources."""

from __future__ import annotations

import configparser
import subprocess
import tempfile
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
    def test_os_bootstrap_uses_only_raw_until_python_is_available(self) -> None:
        tasks = load_tasks("roles/os_bootstrap/tasks/main.yml")
        python_task = next(
            task for task in tasks
            if task["name"] == "Install Python only when it is absent"
        )
        python_index = tasks.index(python_task)

        self.assertTrue(
            all(
                "ansible.builtin.raw" in task
                for task in tasks[: python_index + 1]
            )
        )
        self.assertIn("sudo -n", python_task["ansible.builtin.raw"])
        self.assertNotIn("become", python_task)
        self.assertEqual(
            "'HOMELAB_PYTHON_INSTALLED' in os_bootstrap_python.stdout",
            python_task["changed_when"],
        )

        post_bootstrap_task = next(
            task for task in tasks
            if task["name"] == "Validate facts and passwordless sudo after bootstrap"
        )
        sudo_task = next(
            task for task in post_bootstrap_task["block"]
            if task["name"] == "Recheck non-interactive sudo"
        )
        self.assertEqual(
            ["sudo", "-n", "true"],
            sudo_task["ansible.builtin.command"]["argv"],
        )

    def test_os_bootstrap_has_no_root_or_password_fallback(self) -> None:
        source = (
            REPOSITORY_ROOT / "roles/os_bootstrap/tasks/main.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("id -un", source)
        self.assertIn("sudo -n true", source)
        self.assertNotIn("ansible_password", source)
        self.assertNotIn("ansible_become_password", source)
        self.assertNotIn("PermitRootLogin", source)

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
        """Debian and RedHat hosts must resolve to maintained task files."""
        tasks = load_tasks("roles/system_maintenance/tasks/main.yml")
        first_task = tasks[0]

        self.assertIn("ansible.builtin.assert", first_task)
        self.assertEqual(
            first_task["ansible.builtin.assert"],
            {
                "that": [
                    "ansible_os_family in ['Debian', 'RedHat']"
                ],
                "fail_msg": (
                    "system-maintenance does not support operating-system family "
                    "received {{ ansible_os_family }}"
                ),
            },
        )
        self.assertEqual(
            [task["ansible.builtin.import_tasks"] for task in tasks[1:]],
            ["full-update.yml", "automatic-updates.yml", "reboot-state.yml"],
        )
        for os_family in ("Debian", "RedHat"):
            for task_file in (
                f"setup-{os_family}.yml",
                f"automatic-updates-{os_family}.yml",
                f"reboot-state-{os_family}.yml",
            ):
                self.assertTrue(
                    (
                        REPOSITORY_ROOT
                        / "roles/system_maintenance/tasks"
                        / task_file
                    ).is_file(),
                    f"missing system-maintenance tasks: {task_file}",
                )

    def test_prepare_cifs_storage_dispatches_only_implemented_families(self) -> None:
        """Every accepted CIFS operating-system family must have setup tasks."""
        tasks = load_tasks("roles/prepare_cifs_storage/tasks/main.yml")
        dispatch_task = tasks[0]
        supported_families = ("Debian",)

        self.assertEqual(
            "setup-{{ ansible_os_family }}.yml",
            dispatch_task["ansible.builtin.include_tasks"],
        )
        self.assertEqual(
            "ansible_os_family in ['Debian']",
            dispatch_task["when"],
        )
        for os_family in supported_families:
            self.assertTrue(
                (
                    REPOSITORY_ROOT
                    / "roles/prepare_cifs_storage/tasks"
                    / f"setup-{os_family}.yml"
                ).is_file(),
                f"missing prepare-cifs-storage tasks for {os_family}",
            )

    def test_system_maintenance_never_reboots_or_reduces_kernel_retention(
        self,
    ) -> None:
        task_paths = [
            "roles/system_maintenance/tasks/setup-Debian.yml",
            "roles/system_maintenance/tasks/setup-RedHat.yml",
        ]
        tasks = [task for path in task_paths for task in load_tasks(path)]

        self.assertFalse(any("ansible.builtin.reboot" in task for task in tasks))
        self.assertFalse(
            any(
                task.get("community.general.ini_file", {}).get("option")
                == "installonly_limit"
                for task in tasks
            )
        )

    def test_security_baseline_access_inputs_fail_closed(self) -> None:
        defaults = load_yaml_documents(
            "roles/security_baseline/defaults/main.yml"
        )[0]
        self.assertEqual([], defaults["security_baseline_authorized_keys"])
        self.assertEqual([], defaults["security_baseline_management_sources"])
        self.assertEqual([], defaults["security_baseline_firewall_services"])

    def test_security_baseline_validates_sudo_and_owns_keys_authoritatively(
        self,
    ) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/access.yml")
        sudo = next(
            task
            for task in tasks
            if task["name"] == "Install validated ansible sudo policy"
        )
        keys = next(
            task
            for task in tasks
            if task["name"] == "Reconcile authorized controller keys"
        )

        self.assertEqual(
            "/usr/sbin/visudo -cf %s",
            sudo["ansible.builtin.template"]["validate"],
        )
        self.assertEqual("0440", sudo["ansible.builtin.template"]["mode"])
        self.assertIs(keys["ansible.posix.authorized_key"]["exclusive"], True)
        self.assertIs(keys["no_log"], True)

    def test_firewall_policy_is_private_source_only_and_runtime_guarded(
        self,
    ) -> None:
        source = (
            REPOSITORY_ROOT / "roles/security_baseline/tasks/firewall.yml"
        ).read_text(encoding="utf-8")
        rule_source = (
            REPOSITORY_ROOT
            / "roles/security_baseline/filter_plugins/platform_controls.py"
        ).read_text(encoding="utf-8")
        self.assertIn("security_baseline_management_sources", source)
        self.assertIn('service name=\"ssh\"', rule_source)
        self.assertIn("security_baseline_apply_firewall_runtime", source)
        for forbidden in ("tailscale0", "http", "https", "dns", "public"):
            self.assertNotIn(forbidden, (source + rule_source).lower())

    def test_platform_mac_policy_does_not_mix_frameworks(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/mac.yml")
        selinux = next(
            task
            for task in tasks
            if task.get("ansible.posix.selinux", {}).get("state") == "enforcing"
        )
        apparmor = next(
            task
            for task in tasks
            if task["name"] == "Install Debian AppArmor packages"
        )
        self.assertEqual("targeted", selinux["ansible.posix.selinux"]["policy"])
        self.assertEqual("enforcing", selinux["ansible.posix.selinux"]["state"])
        self.assertIn("ansible_os_family == 'RedHat'", selinux["when"])
        self.assertIn("ansible_os_family == 'Debian'", apparmor["when"])

    def test_logging_uses_persistent_bounded_journal_and_vendor_audit_rules(
        self,
    ) -> None:
        template = (
            REPOSITORY_ROOT
            / "roles/security_baseline/templates/journald-homelab.conf.j2"
        ).read_text(encoding="utf-8")
        audit_source = (
            REPOSITORY_ROOT / "roles/security_baseline/tasks/logging.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Storage=persistent", template)
        self.assertIn("SystemMaxUse=", template)
        self.assertIn("SystemKeepFree=", template)
        self.assertNotIn("audit.rules", audit_source)

    def test_chrony_override_keeps_managed_sources_active(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/pre-update.yml")
        add_sources = next(
            task
            for task in tasks
            if task["name"]
            == "Add trusted chrony sources before disabling vendor sources"
        )
        disable_vendor = next(
            task
            for task in tasks
            if task["name"]
            == "Disable vendor chrony sources after adding trusted sources"
        )

        self.assertEqual(
            "BOF",
            add_sources["ansible.builtin.blockinfile"]["insertbefore"],
        )
        self.assertEqual(
            "(?m)^# END ANSIBLE MANAGED TRUSTED CHRONY SOURCES$",
            disable_vendor["ansible.builtin.replace"]["after"],
        )

    def test_system_maintenance_managed_packages_are_minimal(self) -> None:
        source = "\n".join(
            (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
            for path in (
                "roles/system_maintenance/tasks/setup-Debian.yml",
                "roles/system_maintenance/tasks/setup-RedHat.yml",
            )
        )
        for forbidden in (
            "qemu-guest-agent",
            "xterm",
            "vim",
            "tree",
            "git",
            "python3-pip",
            "python3-venv",
            "python3-netaddr",
            "apache2-utils",
            "httpd-tools",
            "epel-release",
            "fail2ban",
        ):
            with self.subTest(package=forbidden):
                self.assertNotIn(forbidden, source)

    def test_native_security_updates_are_daily_and_reboot_controllable(self) -> None:
        defaults = load_yaml_documents(
            "roles/system_maintenance/defaults/main.yml"
        )[0]
        self.assertEqual(
            "*-*-* 04:00:00",
            defaults["system_maintenance_security_update_calendar"],
        )
        self.assertEqual(
            "04:30",
            defaults["system_maintenance_security_reboot_time"],
        )
        self.assertIs(
            defaults["system_maintenance_native_reboot_enabled"],
            True,
        )

    def test_system_maintenance_configures_native_security_updater_mechanisms(
        self,
    ) -> None:
        platform_contracts = {
            "Debian": {
                "module": "ansible.builtin.apt",
                "package": "unattended-upgrades",
                "timer": "apt-daily-upgrade.timer",
            },
            "RedHat": {
                "module": "ansible.builtin.dnf",
                "package": "dnf-automatic",
                "timer": "dnf-automatic.timer",
            },
        }
        for os_family, contract in platform_contracts.items():
            with self.subTest(os_family=os_family):
                tasks = load_tasks(
                    "roles/system_maintenance/tasks/"
                    f"automatic-updates-{os_family}.yml"
                )
                package_tasks = [
                    task
                    for task in tasks
                    if task.get(contract["module"], {}).get("name")
                    == contract["package"]
                ]
                self.assertEqual(1, len(package_tasks))
                if not package_tasks:
                    continue

                package_task = package_tasks[0]
                self.assertEqual(
                    {
                        "name": contract["package"],
                        "state": "present",
                        "lock_timeout": (
                            "{{ system_maintenance_package_lock_timeout }}"
                        ),
                    },
                    package_task[contract["module"]],
                )
                timer_task = next(
                    task for task in tasks
                    if "ansible.builtin.systemd_service" in task
                )
                self.assertEqual(
                    {
                        "name": contract["timer"],
                        "enabled": True,
                        "state": "started",
                        "daemon_reload": True,
                    },
                    timer_task["ansible.builtin.systemd_service"],
                )
                self.assertLess(tasks.index(package_task), tasks.index(timer_task))

        apt_policy = (
            REPOSITORY_ROOT
            / "roles/system_maintenance/templates/apt-unattended-upgrades.j2"
        ).read_text(encoding="utf-8")
        apt_origins = [
            line.strip()
            for line in apt_policy.splitlines()
            if line.strip().startswith('"origin=')
        ]
        self.assertEqual(
            [
                '"origin=Debian,codename=${distro_codename}-security,'
                'label=Debian-Security";'
            ],
            apt_origins,
        )

        dnf_policy = (
            REPOSITORY_ROOT
            / "roles/system_maintenance/templates/dnf-automatic.conf.j2"
        ).read_text(encoding="utf-8")
        dnf_policy = dnf_policy.replace(
            "{{ 'when-needed' if system_maintenance_native_reboot_enabled "
            "else 'never' }}",
            "when-needed",
        )
        dnf_config = configparser.ConfigParser()
        dnf_config.read_string(dnf_policy)
        self.assertEqual("security", dnf_config["commands"]["upgrade_type"])
        self.assertEqual("yes", dnf_config["commands"]["download_updates"])
        self.assertEqual("yes", dnf_config["commands"]["apply_updates"])

        self.assertEqual(
            [],
            load_tasks(
                "roles/system_maintenance/tasks/automatic-updates-Archlinux.yml"
            ),
        )

    def test_system_maintenance_idempotence_skips_only_live_upgrades(self) -> None:
        """Live upgrades must run during converge but not strict idempotence."""
        imported_task_files = [
            "setup-Debian.yml",
            "setup-RedHat.yml",
        ]
        playbook = [
            {
                "name": "List system maintenance tasks",
                "hosts": "localhost",
                "gather_facts": False,
                "tasks": [
                    {
                        "name": f"Import {task_file}",
                        "ansible.builtin.import_tasks": str(
                            REPOSITORY_ROOT
                            / "roles/system_maintenance/tasks"
                            / task_file
                        ),
                    }
                    for task_file in imported_task_files
                ],
            }
        ]

        with tempfile.TemporaryDirectory() as temporary_directory:
            playbook_path = Path(temporary_directory) / "list-maintenance.yml"
            playbook_path.write_text(
                yaml.safe_dump(playbook, sort_keys=False),
                encoding="utf-8",
            )

            def list_tasks(*extra_arguments: str) -> str:
                result = subprocess.run(
                    [
                        "ansible-playbook",
                        str(playbook_path),
                        "--inventory",
                        "localhost,",
                        "--connection",
                        "local",
                        "--list-tasks",
                        *extra_arguments,
                    ],
                    cwd=REPOSITORY_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                return result.stdout

            converge_tasks = list_tasks()
            idempotence_tasks = list_tasks(
                "--skip-tags", "molecule-idempotence-notest"
            )

        live_upgrade_tasks = [
            "Update package cache and upgrade all packages",
            "Fully update installed packages",
        ]
        for task_name in live_upgrade_tasks:
            with self.subTest(task_name=task_name):
                self.assertIn(task_name, converge_tasks)
                self.assertNotIn(task_name, idempotence_tasks)

        stable_tasks = [
            "Autoremove unused packages",
            "Install package-management utilities",
        ]
        for task_name in stable_tasks:
            with self.subTest(task_name=task_name):
                self.assertIn(task_name, idempotence_tasks)

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
