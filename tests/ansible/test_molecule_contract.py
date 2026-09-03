from __future__ import annotations

import shlex
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIRECTORY = (
    REPOSITORY_ROOT / "roles" / "system_maintenance" / "molecule" / "default"
)
BASELINE_SCENARIO_DIRECTORY = (
    REPOSITORY_ROOT / "roles" / "system_maintenance" / "molecule" / "baseline"
)


def load_yaml(name: str):
    path = SCENARIO_DIRECTORY / name
    if not path.is_file():
        raise AssertionError(f"required Molecule scenario file is missing: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_yaml_documents(relative_path: str) -> list[object]:
    path = REPOSITORY_ROOT / relative_path
    if not path.is_file():
        raise AssertionError(f"required YAML file is missing: {path}")
    return list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


def load_baseline_yaml(name: str):
    path = BASELINE_SCENARIO_DIRECTORY / name
    if not path.is_file():
        raise AssertionError(f"required baseline scenario file is missing: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def containerfile_run_commands(content: str) -> list[str]:
    """Return every logical RUN command, including its continued lines."""
    commands = []
    lines = content.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].startswith("RUN "):
            index += 1
            continue

        command_lines = []
        while index < len(lines):
            line = lines[index].rstrip()
            command_lines.append(line.removesuffix("\\").strip())
            index += 1
            if not line.endswith("\\"):
                break
        commands.append(" ".join(command_lines).removeprefix("RUN "))
    return commands


def shell_command_segments(command: str) -> list[list[str]]:
    """Split a RUN command into shell-command token groups."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    tokens = list(lexer)
    segments = [[]]
    for token in tokens:
        if token in {"&&", "||", ";", "|"}:
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def containerfile_installed_packages(content: str) -> list[str]:
    """Return packages installed by every supported manager in every RUN command."""
    installed_packages = []
    for run_command in containerfile_run_commands(content):
        for command in shell_command_segments(run_command):
            for index, manager in enumerate(command):
                if manager not in {"apt-get", "dnf"}:
                    continue
                try:
                    package_start = command.index("install", index + 1) + 1
                except ValueError:
                    continue
                package_tokens = command[package_start:]
                installed_packages.extend(
                    token for token in package_tokens if not token.startswith("-")
                )

    if not installed_packages:
        raise AssertionError(
            "Containerfile must install packages with apt-get or dnf"
        )
    return installed_packages


class MoleculeScenarioContractTests(unittest.TestCase):
    def test_scenario_uses_the_ansible_native_lifecycle_and_least_privilege(
        self,
    ) -> None:
        configuration = load_yaml("molecule.yml")

        self.assertEqual(
            {"name": "galaxy", "enabled": False},
            configuration["dependency"],
        )
        self.assertEqual(
            {"name": "default", "options": {"managed": True}},
            configuration["driver"],
        )
        self.assertEqual(
            [
                "destroy",
                "syntax",
                "create",
                "prepare",
                "converge",
                "idempotence",
                "verify",
                "cleanup",
                "destroy",
            ],
            configuration["scenario"]["test_sequence"],
        )

        expected_platforms = {
            "debian13": (
                "localhost/homelab-playbook-system-maintenance-debian13:local",
                "homelab-playbook-system-maintenance-debian13",
            ),
            "rockylinux9": (
                "localhost/homelab-playbook-system-maintenance-rockylinux9:local",
                "homelab-playbook-system-maintenance-rockylinux9",
            ),
        }
        platforms = {
            platform["name"]: platform for platform in configuration["platforms"]
        }
        self.assertEqual(set(expected_platforms), set(platforms))
        for name, (image, container_name) in expected_platforms.items():
            with self.subTest(platform=name):
                platform = platforms[name]
                self.assertEqual(image, platform["image"])
                self.assertEqual(container_name, platform["container_name"])
                self.assertEqual(["molecule"], platform["groups"])
                self.assertEqual(
                    "/usr/lib/systemd/systemd",
                    platform["container_command"],
                )
                self.assertIs(platform["container_privileged"], False)
                self.assertEqual("always", platform["container_systemd"])
                self.assertEqual("never", platform["pull"])
                self.assertTrue(
                    {
                        "cap_add",
                        "capabilities",
                        "devices",
                        "volumes",
                    }.isdisjoint(platform)
                )

    def test_create_and_destroy_use_only_the_podman_collection(self) -> None:
        create_plays = load_yaml("create.yml")
        destroy_plays = load_yaml("destroy.yml")

        create_modules = {
            key
            for play in create_plays
            for task in play["tasks"]
            for key in task
            if "." in key
        }
        destroy_modules = {
            key
            for play in destroy_plays
            for task in play["tasks"]
            for key in task
            if "." in key
        }
        self.assertIn("containers.podman.podman_container", create_modules)
        self.assertIn("containers.podman.podman_container", destroy_modules)
        self.assertTrue(
            all("docker" not in module.lower() for module in create_modules)
        )
        self.assertTrue(
            all("docker" not in module.lower() for module in destroy_modules)
        )
        create_tasks = load_yaml("create.yml")[0]["tasks"]
        start = next(
            task["containers.podman.podman_container"]
            for task in create_tasks
            if "containers.podman.podman_container" in task
        )
        self.assertEqual(
            "{{ system_maintenance_molecule_platform.restart_policy | default('no') }}",
            start.get("restart_policy"),
        )

    def test_controller_playbooks_select_the_worker_platform_from_environment(
        self,
    ) -> None:
        for name in ("create.yml", "cleanup.yml", "destroy.yml"):
            with self.subTest(playbook=name):
                play = load_yaml(name)[0]
                variables = play["vars"]
                self.assertIn(
                    "lookup('ansible.builtin.env', 'HOMELAB_MOLECULE_PLATFORM')",
                    variables["system_maintenance_molecule_platform_name"],
                )
                self.assertIn(
                    "selectattr('name', 'equalto',",
                    variables["system_maintenance_molecule_platforms"],
                )
                assertions = [
                    task["ansible.builtin.assert"]["that"]
                    for task in play["tasks"]
                    if "ansible.builtin.assert" in task
                ]
                self.assertTrue(
                    any(
                        "system_maintenance_molecule_platforms | length == 1"
                        in conditions
                        for conditions in assertions
                    )
                )

        create_play = load_yaml("create.yml")[0]
        instance_fact = next(
            task["ansible.builtin.set_fact"]
            for task in create_play["tasks"]
            if "ansible.builtin.set_fact" in task
        )
        instance = instance_fact[
            "system_maintenance_molecule_instance_configuration"
        ][0]
        self.assertEqual(
            "{{ system_maintenance_molecule_platform.container_name }}",
            instance["address"],
        )

    def test_default_converge_disables_native_reboot_and_keeps_verify_independent(
        self,
    ) -> None:
        converge = load_yaml("converge.yml")[0]
        verify = load_yaml("verify.yml")[0]

        self.assertEqual("molecule", converge["hosts"])
        self.assertEqual(
            [
                {
                    "role": "system_maintenance",
                    "system_maintenance_native_reboot_enabled": False,
                }
            ],
            converge["roles"],
        )
        self.assertNotIn("roles", verify)
        self.assertFalse(
            any(
                "ansible.builtin.include_role" in task
                or "ansible.builtin.import_role" in task
                for task in verify["tasks"]
            )
        )

    def test_default_scenario_matches_minimal_maintenance_contract(self) -> None:
        converge = load_yaml_documents(
            "roles/system_maintenance/molecule/default/converge.yml"
        )[0][0]
        role = converge["roles"][0]
        self.assertIs(role["system_maintenance_native_reboot_enabled"], False)
        verify = load_yaml("verify.yml")[0]
        tasks = {task["name"]: task for task in verify["tasks"]}
        debian_policy = tasks["Assert Debian native security maintenance"][
            "ansible.builtin.assert"
        ]["that"]
        rocky_policy = tasks["Assert Rocky native security maintenance"][
            "ansible.builtin.assert"
        ]["that"]
        self.assertIn("'unattended-upgrades' in ansible_facts.packages", debian_policy)
        self.assertIn("'dnf-automatic' in ansible_facts.packages", rocky_policy)
        self.assertIn("'epel-release' not in ansible_facts.packages", rocky_policy)

    def test_containerfiles_use_maintained_bases(self) -> None:
        expected_containerfiles = {
            "Containerfile.debian13": (
                "FROM docker.io/library/debian:13",
                ["dbus", "python3", "systemd"],
                "RUN apt-get update && apt-get install --yes --no-install-recommends tree\n",
            ),
            "Containerfile.rockylinux9": (
                "FROM docker.io/rockylinux/rockylinux:9",
                ["dbus-daemon", "python3", "systemd"],
                "RUN dnf install --assumeyes tree\n",
            ),
        }
        for (
            name,
            (expected_base, expected_packages, later_install),
        ) in expected_containerfiles.items():
            with self.subTest(containerfile=name):
                path = SCENARIO_DIRECTORY / name
                self.assertTrue(path.is_file())
                content = path.read_text(encoding="utf-8")
                self.assertEqual(expected_base, content.splitlines()[0])
                self.assertEqual(
                    expected_packages,
                    containerfile_installed_packages(content),
                )
                with self.assertRaises(AssertionError):
                    self.assertEqual(
                        expected_packages,
                        containerfile_installed_packages(
                            f"{content}\n{later_install}",
                        ),
                    )
                self.assertIn('CMD ["/usr/lib/systemd/systemd"]', content)
                self.assertIn("STOPSIGNAL SIGRTMIN+3", content)

    def test_baseline_scenario_has_only_complete_provisioning_platforms(self) -> None:
        for name in (
            "molecule.yml",
            "create.yml",
            "prepare.yml",
            "converge.yml",
            "verify.yml",
            "cleanup.yml",
            "destroy.yml",
            "Containerfile.debian13",
            "Containerfile.rockylinux9",
        ):
            with self.subTest(name=name):
                self.assertTrue((BASELINE_SCENARIO_DIRECTORY / name).is_file())

        configuration = load_baseline_yaml("molecule.yml")
        self.assertEqual(
            ["debian13", "rockylinux9"],
            [platform["name"] for platform in configuration["platforms"]],
        )
        for platform in configuration["platforms"]:
            self.assertEqual(["servers"], platform["groups"])
            self.assertEqual("ansible", platform["user"])
            self.assertIs(platform["container_privileged"], False)
            self.assertEqual("always", platform["container_systemd"])
            self.assertEqual("always", platform.get("restart_policy"))
            self.assertEqual("never", platform["pull"])
            self.assertTrue(
                {"cap_add", "capabilities", "devices", "volumes"}.isdisjoint(
                    platform
                )
            )

        for name in ("create.yml", "cleanup.yml", "destroy.yml"):
            with self.subTest(shared_controller_playbook=name):
                imported = load_baseline_yaml(name)[0]
                self.assertEqual(
                    f"../default/{name}",
                    imported["ansible.builtin.import_playbook"],
                )

        for name in ("create.yml", "cleanup.yml", "destroy.yml"):
            source = (SCENARIO_DIRECTORY / name).read_text(encoding="utf-8")
            controller = load_yaml(name)[0]
            with self.subTest(dynamic_ownership=name):
                self.assertIn(
                    "system_maintenance_molecule_scenario_selector", source
                )
                selector_source = controller["vars"][
                    "system_maintenance_molecule_scenario_selector"
                ]
                self.assertIn(
                    "HOMELAB_MOLECULE_SCENARIO_SELECTOR",
                    selector_source,
                )
                self.assertNotIn(".labels", selector_source)
                self.assertNotIn("== 'system_maintenance/default'", source)

        bootstrap_tasks = load_yaml_documents(
            "roles/os_bootstrap/tasks/main.yml"
        )[0]
        connection_preflight_tasks = load_yaml_documents(
            "roles/os_bootstrap/tasks/connection-preflight.yml"
        )[0]
        prepare_tasks = load_baseline_yaml("prepare.yml")[0]["tasks"]
        raw_tasks = [
            task
            for task in [
                *connection_preflight_tasks,
                *bootstrap_tasks,
                *prepare_tasks,
            ]
            if "ansible.builtin.raw" in task
        ]
        self.assertEqual(4, len(raw_tasks))
        for task in raw_tasks:
            with self.subTest(raw_task=task["name"]):
                self.assertEqual("/bin/sh", task.get("args", {}).get("executable"))

    def test_baseline_containerfiles_preserve_the_installer_boundary(self) -> None:
        expected_bases = {
            "Containerfile.debian13": "FROM docker.io/library/debian:13",
            "Containerfile.rockylinux9": "FROM docker.io/rockylinux/rockylinux:9",
        }
        for name, base in expected_bases.items():
            with self.subTest(name=name):
                source = (BASELINE_SCENARIO_DIRECTORY / name).read_text(
                    encoding="utf-8"
                )
                lowered = source.lower()
                self.assertEqual(base, source.splitlines()[0])
                for required in ("systemd", "dbus", "sudo", "openssh-server"):
                    self.assertIn(required, lowered)
                self.assertIn("useradd", lowered)
                self.assertIn("ansible", lowered)
                self.assertIn("passwd", lowered)
                self.assertIn("nopasswd: all", lowered)
                self.assertIn('CMD ["/usr/lib/systemd/systemd"]', source)

        debian = (BASELINE_SCENARIO_DIRECTORY / "Containerfile.debian13").read_text(
            encoding="utf-8"
        )
        rocky = (BASELINE_SCENARIO_DIRECTORY / "Containerfile.rockylinux9").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("python3", debian)
        self.assertIn("debian-archive-keyring.gpg", debian)
        self.assertIn("python3", rocky)
        self.assertIn("rm -f /usr/bin/python3", rocky)
        self.assertNotIn("/usr/libexec/platform-python", rocky.split("rm -f", 1)[1])
        self.assertIn("chmod 0400 /etc/shadow", rocky)

    def test_baseline_generates_disposable_keys_and_imports_production_provisioning(
        self,
    ) -> None:
        documents = load_yaml_documents(
            "roles/system_maintenance/molecule/baseline/converge.yml"
        )
        self.assertEqual(1, len(documents))
        key_play = documents[0][0]
        imported_playbook = documents[0][1]
        key_source = str(key_play)
        self.assertIn("molecule_ephemeral_directory", key_source)
        self.assertIn("ssh-keygen", key_source)
        self.assertTrue(
            all(task.get("no_log") is True for task in key_play["tasks"])
        )
        self.assertEqual(
            "../../../../playbooks/os/provision.yml",
            imported_playbook["ansible.builtin.import_playbook"],
        )
        variables = imported_playbook["vars"]
        self.assertEqual(["10.0.0.0/8"], variables["security_baseline_management_sources"])
        self.assertEqual(
            "/usr/bin/stat -c %y /proc/1",
            variables["os_reboot_boot_time_command"],
        )
        for name in (
            "security_baseline_apply_firewall_runtime",
            "security_baseline_apply_kernel_controls",
            "security_baseline_apply_time_runtime",
            "security_baseline_apply_audit_runtime",
            "os_baseline_verify_runtime_controls",
            "system_maintenance_native_reboot_enabled",
        ):
            with self.subTest(variable=name):
                self.assertIs(variables[name], False)

    def test_baseline_verify_is_independent_and_states_evidence_limits(self) -> None:
        verify = load_baseline_yaml("verify.yml")[0]
        self.assertEqual("servers", verify["hosts"])
        self.assertIs(verify["gather_facts"], True)
        self.assertIs(verify["become"], True)
        self.assertNotIn("roles", verify)
        self.assertFalse(
            any(
                "ansible.builtin.include_role" in task
                or "ansible.builtin.import_role" in task
                for task in verify["tasks"]
            )
        )
        evidence_limit = next(
            task
            for task in verify["tasks"]
            if task["name"] == "State the container evidence boundary"
        )
        self.assertEqual(
            {
                "name",
                "ansible.builtin.debug",
                "changed_when",
            },
            set(evidence_limit),
        )
        self.assertIs(evidence_limit["changed_when"], False)
        self.assertEqual(
            "Container evidence does not prove runtime clock synchronization, "
            "firewall reachability, kernel enforcement, audit-kernel attachment, "
            "or physical reboot behavior.",
            " ".join(evidence_limit["ansible.builtin.debug"]["msg"].split()),
        )

    def test_baseline_firewall_verifier_reads_and_compares_exact_permanent_state(self) -> None:
        verify = load_baseline_yaml("verify.yml")[0]
        firewall_reads = next(
            task
            for task in verify["tasks"]
            if task.get("ansible.builtin.command", {}).get("argv", [None])[0]
            == "/usr/bin/firewall-offline-cmd"
            and "--zone=homelab" in task["ansible.builtin.command"]["argv"]
        )
        self.assertEqual(
            [
                "--list-all",
                "--list-interfaces",
                "--list-sources",
                "--list-services",
                "--list-ports",
                "--list-protocols",
                "--list-source-ports",
                "--list-forward-ports",
                "--list-icmp-blocks",
                "--list-rich-rules",
                "--query-forward",
                "--query-masquerade",
                "--query-icmp-block-inversion",
            ],
            firewall_reads["loop"],
        )
        direct_reads = next(
            task
            for task in verify["tasks"]
            if task.get("ansible.builtin.command", {}).get("argv", [])[:2]
            == ["/usr/bin/firewall-offline-cmd", "--direct"]
        )
        self.assertEqual(
            ["--get-all-chains", "--get-all-rules", "--get-all-passthroughs"],
            direct_reads["loop"],
        )
        bindings_read = next(
            task
            for task in verify["tasks"]
            if task.get("ansible.builtin.command", {}).get("argv")
            == ["/usr/bin/firewall-offline-cmd", "--list-all-zones"]
        )
        policies_read = next(
            task
            for task in verify["tasks"]
            if task.get("ansible.builtin.command", {}).get("argv")
            == ["/usr/bin/firewall-offline-cmd", "--list-all-policies"]
        )
        self.assertIs(bindings_read["changed_when"], False)
        self.assertIs(policies_read["changed_when"], False)
        assertion = next(
            task
            for task in verify["tasks"]
            if task["name"] == "Verify permanent private-management firewall policy"
        )
        conditions = assertion["ansible.builtin.assert"]["that"]
        self.assertEqual(
            ["system_maintenance_molecule_baseline_firewall_problems | length == 0"],
            conditions,
        )
        problems = assertion["vars"][
            "system_maintenance_molecule_baseline_firewall_problems"
        ]
        normalized_problems = " ".join(problems.split())
        self.assertIn(
            "system_maintenance_molecule_baseline_firewall_errors",
            normalized_problems,
        )
        self.assertIn(
            "firewalld_configuration.content | b64decode",
            normalized_problems,
        )
        self.assertIn("permanent_firewall.results", normalized_problems)
        self.assertIn("direct_firewall.results", normalized_problems)
        self.assertIn("firewall_bindings.stdout", normalized_problems)
        self.assertIn("firewall_policies.stdout", normalized_problems)

    def test_baseline_sshd_oracle_exercises_matching_connection_policy(self) -> None:
        verify = load_baseline_yaml("verify.yml")[0]
        tasks = verify["tasks"]
        effective = next(
            task
            for task in tasks
            if task["name"]
            == "Read effective OpenSSH policy for the administrative context"
        )
        adversarial = next(
            task
            for task in tasks
            if task["name"]
            == "Exercise an adversarial matching OpenSSH policy in memory"
        )
        for task, expected_host in (
            (effective, "127.0.0.1"),
            (adversarial, "controller.example.invalid"),
        ):
            argv = task["ansible.builtin.command"]["argv"]
            self.assertIn("-C", argv)
            context = argv[argv.index("-C") + 1]
            for field in (
                "user=ansible",
                f"host={expected_host}",
                "addr=127.0.0.1",
                "laddr=127.0.0.1",
                "lport=22",
            ):
                self.assertIn(field, context)
            self.assertIs(task["changed_when"], False)
        self.assertEqual(
            "/dev/stdin",
            adversarial["ansible.builtin.command"]["argv"][3],
        )
        adversarial_input = adversarial["ansible.builtin.command"]["stdin"]
        self.assertIn(
            "Match Host controller.example.invalid User ansible",
            adversarial_input,
        )
        self.assertIn("PasswordAuthentication yes", adversarial_input)
        self.assertIn("AllowTcpForwarding yes", adversarial_input)

        rejection = next(
            task
            for task in tasks
            if task["name"] == "Prove the oracle rejects matching SSH weakening"
        )
        self.assertIn(
            "system_maintenance_molecule_baseline_sshd_errors",
            str(rejection),
        )

    def test_baseline_verifier_proves_repository_sudo_policy_before_effective_sudo(self) -> None:
        verify = load_baseline_yaml("verify.yml")[0]
        tasks = verify["tasks"]
        policy_read = next(
            task
            for task in tasks
            if task.get("ansible.builtin.slurp", {}).get("src")
            == "/etc/sudoers.d/90-ansible"
        )
        syntax = next(
            task
            for task in tasks
            if task.get("ansible.builtin.command", {}).get("argv")
            == ["/usr/sbin/visudo", "-cf", "/etc/sudoers.d/90-ansible"]
        )
        effective = next(
            task
            for task in tasks
            if task.get("ansible.builtin.command", {}).get("argv")
            == ["sudo", "-n", "true"]
        )
        self.assertLess(tasks.index(policy_read), tasks.index(syntax))
        self.assertLess(tasks.index(syntax), tasks.index(effective))
        assertion = next(
            task
            for task in tasks
            if task.get("ansible.builtin.assert", {}).get("fail_msg")
            == "The administrative account, key, or sudo policy is incorrect"
        )
        conditions = assertion["ansible.builtin.assert"]["that"]
        content_condition = next(
            condition
            for condition in conditions
            if "sudo_policy_content.content" in condition
        )
        self.assertIn("| b64decode | trim | split('\\n')", content_condition)
        self.assertNotIn("splitlines", content_condition)
        self.assertIn(
            "['# Ansible managed', 'ansible ALL=(ALL:ALL) NOPASSWD: ALL']",
            content_condition,
        )

    def test_baseline_verifier_feeds_complete_updater_evidence_to_independent_oracle(self) -> None:
        verify = load_baseline_yaml("verify.yml")[0]
        tasks = verify["tasks"]
        enabled = next(
            task
            for task in tasks
            if task.get("ansible.builtin.command", {}).get("argv", [None])[:2]
            == ["/usr/bin/systemctl", "is-enabled"]
        )
        self.assertIn("apt-daily-upgrade.timer", str(enabled))
        self.assertIn("dnf-automatic.timer", str(enabled))
        assertion = next(
            task
            for task in tasks
            if task["name"] == "Verify native security updater policy and timer"
        )
        problems = assertion["vars"][
            "system_maintenance_molecule_baseline_updater_problems"
        ]
        normalized_problems = " ".join(problems.split())
        self.assertIn(
            "system_maintenance_molecule_baseline_debian_updater_errors",
            normalized_problems,
        )
        self.assertIn(
            "system_maintenance_molecule_baseline_rocky_updater_errors",
            normalized_problems,
        )
        self.assertIn("debian_updater.stdout", normalized_problems)
        self.assertIn("rocky_updater.content | b64decode", normalized_problems)
        self.assertIn("updater_timer.stdout", normalized_problems)
        self.assertIn("updater_timer_enabled.stdout", normalized_problems)
        self.assertEqual(
            ["system_maintenance_molecule_baseline_updater_problems | length == 0"],
            assertion["ansible.builtin.assert"]["that"],
        )

    def test_ci_registers_exact_four_selector_platform_jobs(self) -> None:
        workflow = yaml.safe_load(
            (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(
                encoding="utf-8"
            )
        )
        molecule_job = workflow["jobs"]["molecule"]
        matrix = molecule_job["strategy"]["matrix"]
        self.assertEqual(4, molecule_job["strategy"]["max-parallel"])
        self.assertEqual(
            [
                {"selector": "system_maintenance/default", "platform": "debian13"},
                {"selector": "system_maintenance/default", "platform": "rockylinux9"},
                {"selector": "system_maintenance/baseline", "platform": "debian13"},
                {"selector": "system_maintenance/baseline", "platform": "rockylinux9"},
            ],
            matrix["include"],
        )
        run_source = str(molecule_job["steps"][-1]["run"])
        self.assertIn("matrix.selector", run_source)

if __name__ == "__main__":
    unittest.main()
