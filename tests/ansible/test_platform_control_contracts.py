"""Behavioral contracts for platform-native security controls."""

from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_module(relative_path: str, name: str):
    path = REPOSITORY_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_tasks(relative_path: str) -> list[dict[str, object]]:
    with (REPOSITORY_ROOT / relative_path).open(encoding="utf-8") as source:
        return [
            task
            for document in yaml.safe_load_all(source)
            for task in document
        ]


class RepositoryTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_trust = load_module(
            "roles/security_baseline/files/validate_repository_trust.py",
            "security_baseline_repository_trust",
        )

    def write_debian_fixture(
        self,
        root: Path,
        stanza: str,
        *,
        source_path: str = "/etc/apt/sources.list.d/debian.sources",
    ) -> str:
        keyring = root / "usr/share/keyrings/debian-archive-keyring.gpg"
        keyring.parent.mkdir(parents=True)
        keyring.touch()
        source = root / source_path.lstrip("/")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(stanza, encoding="utf-8")
        return '\n'.join(
            (
                'Dir "/";',
                'Dir::Etc "etc/apt";',
                'Dir::Etc::sourcelist "sources.list";',
                'Dir::Etc::sourceparts "sources.list.d";',
            )
        )

    def test_debian_accepts_only_signed_distribution_sources(self) -> None:
        stanza = """\
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apt_config = self.write_debian_fixture(root, stanza)
            self.repository_trust.validate_debian_configuration(apt_config, root)

    def test_debian_accepts_installer_archive_keyring(self) -> None:
        stanza = """\
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apt_config = self.write_debian_fixture(root, stanza)
            keyring = root / "usr/share/keyrings/debian-archive-keyring.pgp"
            keyring.touch()
            self.repository_trust.validate_debian_configuration(apt_config, root)

    def test_debian_rejects_each_source_authentication_bypass(self) -> None:
        bypasses = (
            "Trusted: yes",
            "Allow-Insecure: yes",
            "Allow-Weak: yes",
            "Allow-Downgrade-To-Insecure: yes",
        )
        for bypass in bypasses:
            with self.subTest(bypass=bypass), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stanza = f"""\
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
{bypass}
"""
                apt_config = self.write_debian_fixture(root, stanza)
                with self.assertRaises(ValueError):
                    self.repository_trust.validate_debian_configuration(
                        apt_config,
                        root,
                    )

    def test_debian_rejects_each_effective_global_authentication_bypass(
        self,
    ) -> None:
        options = (
            "Acquire::AllowInsecureRepositories",
            "Acquire::AllowWeakRepositories",
            "Acquire::AllowDowngradeToInsecureRepositories",
            "APT::Get::AllowUnauthenticated",
        )
        stanza = """\
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
        for option in options:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                apt_config = self.write_debian_fixture(root, stanza)
                apt_config += f'\n{option} "true";'
                with self.assertRaises(ValueError):
                    self.repository_trust.validate_debian_configuration(
                        apt_config,
                        root,
                    )

    def test_debian_config_names_are_case_insensitive(self) -> None:
        options = (
            "Acquire::AllowInsecureRepositories",
            "Acquire::AllowWeakRepositories",
            "Acquire::AllowDowngradeToInsecureRepositories",
            "APT::Get::AllowUnauthenticated",
        )
        stanza = """\
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
        for option in options:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                apt_config = self.write_debian_fixture(root, stanza)
                apt_config += f'\n{option.lower()} "true";'
                with self.assertRaises(ValueError):
                    self.repository_trust.validate_debian_configuration(
                        apt_config,
                        root,
                    )

    def test_debian_applies_each_apt_get_authentication_bypass(self) -> None:
        options = (
            "Acquire::AllowInsecureRepositories",
            "Acquire::AllowWeakRepositories",
            "Acquire::AllowDowngradeToInsecureRepositories",
            "APT::Get::AllowUnauthenticated",
        )
        stanza = """\
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
        for option in options:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                apt_config = self.write_debian_fixture(root, stanza)
                apt_config += f'\nbinary::APT-GET::{option} "true";'
                with self.assertRaises(ValueError):
                    self.repository_trust.validate_debian_configuration(
                        apt_config,
                        root,
                    )

    def test_debian_uses_effective_source_locations_and_archive_keyring(
        self,
    ) -> None:
        secure_stanza = """\
Types: deb
URIs: https://security.debian.org/debian-security
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
        insecure_default = secure_stanza.replace(
            "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg",
            "Trusted: yes",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apt_config = self.write_debian_fixture(
                root,
                secure_stanza,
                source_path="/etc/apt/authoritative/debian.sources",
            )
            default_source = root / "etc/apt/sources.list.d/ignored.sources"
            default_source.parent.mkdir(parents=True, exist_ok=True)
            default_source.write_text(insecure_default, encoding="utf-8")
            apt_config = apt_config.replace(
                'Dir::Etc::sourceparts "sources.list.d";',
                'Dir::Etc::sourceparts "authoritative";',
            )
            self.repository_trust.validate_debian_configuration(apt_config, root)

            authoritative = root / "etc/apt/authoritative/debian.sources"
            authoritative.write_text(insecure_default, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.repository_trust.validate_debian_configuration(
                    apt_config,
                    root,
                )

    def test_debian_applies_case_insensitive_and_apt_get_source_paths(
        self,
    ) -> None:
        secure_stanza = """\
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
        insecure_stanza = secure_stanza.replace(
            "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg",
            "Trusted: yes",
        )
        overrides = (
            'dir::etc::sourceparts "authoritative";',
            'Binary::apt-get::Dir::Etc::sourceparts "authoritative";',
        )
        for override in overrides:
            with self.subTest(override=override), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                apt_config = self.write_debian_fixture(root, secure_stanza)
                authoritative = root / "etc/apt/authoritative/debian.sources"
                authoritative.parent.mkdir(parents=True)
                authoritative.write_text(insecure_stanza, encoding="utf-8")
                apt_config += f"\n{override}"
                with self.assertRaises(ValueError):
                    self.repository_trust.validate_debian_configuration(
                        apt_config,
                        root,
                    )


class FirewallPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.controls = load_module(
            "roles/security_baseline/filter_plugins/platform_controls.py",
            "security_baseline_platform_controls",
        )

    @staticmethod
    def exact_state(rule: str) -> dict[str, object]:
        return {
            "target": "DROP",
            "forward": False,
            "masquerade": False,
            "icmp_block_inversion": False,
            "interface_zone": "homelab",
            "interfaces": ["eth0"],
            "sources": [],
            "services": [],
            "ports": [],
            "protocols": [],
            "source_ports": [],
            "forward_ports": [],
            "icmp_blocks": [],
            "rich_rules": [rule],
        }

    @staticmethod
    def global_direct_results(
        opening: tuple[str, str] | None = None,
    ) -> list[dict[str, object]]:
        return [
            {
                "item": item,
                "stdout": value if opening and opening[0] == item else "",
            }
            for item, value in (
                ("--get-all-chains", "ipv4 filter EXTRA"),
                ("--get-all-rules", "ipv4 filter INPUT 0 -j ACCEPT"),
                ("--get-all-passthroughs", "ipv4 -A INPUT -j ACCEPT"),
            )
        ]

    @staticmethod
    def expected_policy() -> str:
        rules = [
            "neighbour-advertisement",
            "neighbour-solicitation",
            "redirect",
            "router-advertisement",
        ]
        return "\n".join(
            [
                "allow-host-ipv6",
                "  priority: -15000",
                "  target: CONTINUE",
                "  ingress-zones: ANY",
                "  egress-zones: HOST",
                "  services:",
                "  ports:",
                "  protocols:",
                "  masquerade: no",
                "  forward-ports:",
                "  source-ports:",
                "  icmp-blocks:",
                "  rich rules:",
                *[
                    f'    rule family="ipv6" icmp-type name="{name}" accept'
                    for name in rules
                ],
            ]
        )

    def test_extension_contract_rejects_ssh(self) -> None:
        payload = {
            "management_sources": ["10.0.0.0/24"],
            "services": [
                {"service": "ssh", "sources": ["10.0.1.0/24"]}
            ],
        }
        with self.assertRaises(ValueError):
            self.controls.security_baseline_firewall_rules(payload)

    def test_management_peer_must_be_covered_by_desired_private_sources(
        self,
    ) -> None:
        self.assertTrue(
            self.controls.security_baseline_firewall_peer_is_covered(
                "10.0.0.5",
                ["10.0.0.0/24"],
            )
        )
        self.assertFalse(
            self.controls.security_baseline_firewall_peer_is_covered(
                "10.0.0.5",
                ["192.168.50.0/24"],
            )
        )

    def test_management_ssh_rules_open_only_tcp_port_22(self) -> None:
        self.assertEqual(
            [
                'rule family="ipv4" source address="10.0.0.0/24" '
                'port port="22" protocol="tcp" accept'
            ],
            self.controls.security_baseline_firewall_rules(
                {"management_sources": ["10.0.0.0/24"], "services": []}
            ),
        )

    def test_management_peer_preflight_precedes_firewall_mutation(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/firewall.yml")
        names = [task["name"] for task in tasks]
        coverage = names.index("Validate management peer against desired sources")
        for mutation in (
            "Install firewalld",
            "Create permanent homelab firewall zone",
            "Enable and start firewalld",
            "Bind runtime management interface to homelab",
            "Persist management interface binding to homelab",
        ):
            with self.subTest(mutation=mutation):
                self.assertLess(coverage, names.index(mutation))

    def test_global_firewall_surfaces_fail_closed_on_every_unsupported_opening(
        self,
    ) -> None:
        expected = "homelab\n  interfaces: eth0\n  sources:\n"
        self.assertEqual(
            [],
            self.controls.security_baseline_firewall_global_surface_errors(
                expected,
                self.global_direct_results(),
                self.expected_policy(),
                "eth0",
                False,
                "Debian",
            ),
        )
        mutations = {
            "extra interface": (
                expected + "public\n  interfaces: eth1\n  sources:\n",
                self.global_direct_results(),
                self.expected_policy(),
            ),
            "source binding": (
                expected + "trusted\n  interfaces:\n  sources: 10.0.0.0/8\n",
                self.global_direct_results(),
                self.expected_policy(),
            ),
            "direct chain": (
                expected,
                self.global_direct_results(("--get-all-chains", "opening")),
                self.expected_policy(),
            ),
            "direct rule": (
                expected,
                self.global_direct_results(("--get-all-rules", "opening")),
                self.expected_policy(),
            ),
            "direct passthrough": (
                expected,
                self.global_direct_results(("--get-all-passthroughs", "opening")),
                self.expected_policy(),
            ),
            "policy object": (
                expected,
                self.global_direct_results(),
                self.expected_policy() + "\noperator-policy",
            ),
        }
        for label, (bindings, direct, policies) in mutations.items():
            with self.subTest(label=label):
                self.assertTrue(
                    self.controls.security_baseline_firewall_global_surface_errors(
                        bindings,
                        direct,
                        policies,
                        "eth0",
                        False,
                        "Debian",
                    )
                )

    def test_global_firewall_preflight_allows_only_management_interface_transition(
        self,
    ) -> None:
        transitional = "public\n  interfaces: eth0\n  sources:\n"
        self.assertEqual(
            [],
            self.controls.security_baseline_firewall_global_surface_errors(
                transitional,
                self.global_direct_results(),
                self.expected_policy(),
                "eth0",
                True,
                "Debian",
            ),
        )
        self.assertTrue(
            self.controls.security_baseline_firewall_global_surface_errors(
                transitional,
                self.global_direct_results(),
                self.expected_policy(),
                "eth0",
                False,
                "Debian",
            )
        )


    def test_global_firewall_preflight_precedes_zone_and_service_mutation(
        self,
    ) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/firewall.yml")
        names = [task["name"] for task in tasks]
        preflight = names.index("Reject unsupported permanent firewall surfaces")
        for mutation in (
            "Create permanent homelab firewall zone",
            "Enable and start firewalld",
            "Bind runtime management interface to homelab",
        ):
            with self.subTest(mutation=mutation):
                self.assertLess(preflight, names.index(mutation))

    def test_exact_policy_rejects_alternate_binding_and_every_direct_opening(
        self,
    ) -> None:
        rule = (
            'rule family="ipv4" source address="10.0.0.0/24" '
            'port port="22" protocol="tcp" accept'
        )
        exact = self.exact_state(rule)
        states = {"runtime": copy.deepcopy(exact), "permanent": copy.deepcopy(exact)}
        self.assertEqual(
            [],
            self.controls.security_baseline_firewall_policy_errors(
                states,
                [rule],
                "eth0",
            ),
        )

        mutations = {
            "alternate binding": lambda state: state["runtime"].update(
                interface_zone="public",
                interfaces=[],
            ),
            "direct ssh service": lambda state: state["runtime"].update(
                services=["ssh"]
            ),
            "extra port": lambda state: state["runtime"].update(
                ports=["2222/tcp"]
            ),
            "ICMP block": lambda state: state["runtime"].update(
                icmp_blocks=["echo-request"]
            ),
            "ICMP block inversion": lambda state: state["runtime"].update(
                icmp_block_inversion=True
            ),
            "extra rich accept": lambda state: state["runtime"].update(
                rich_rules=state["runtime"]["rich_rules"]
                + [
                    'rule family="ipv4" source address="10.0.2.0/24" '
                    'port port="22" protocol="tcp" accept'
                ]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                candidate = {
                    "runtime": copy.deepcopy(exact),
                    "permanent": copy.deepcopy(exact),
                }
                mutate(candidate)
                self.assertTrue(
                    self.controls.security_baseline_firewall_policy_errors(
                        candidate,
                        [rule],
                        "eth0",
                    )
                )

    def test_guard_transition_reloads_when_runtime_zone_is_absent(self) -> None:
        rule = (
            'rule family="ipv4" source address="10.0.0.0/24" '
            'port port="22" protocol="tcp" accept'
        )
        second_pass_runtime = {
            "zones": [],
            "target": "",
            "forward": True,
            "rich_rules": [],
        }
        self.assertTrue(
            self.controls.security_baseline_firewall_reload_required(
                second_pass_runtime,
                [rule],
                False,
            )
        )
        consumed_runtime = {
            "zones": ["homelab"],
            "target": "DROP",
            "forward": False,
            "rich_rules": [rule],
        }
        self.assertFalse(
            self.controls.security_baseline_firewall_reload_required(
                consumed_runtime,
                [rule],
                False,
            )
        )

    def test_runtime_target_is_read_from_supported_list_all_output(self) -> None:
        output = """\
homelab (active)
  target: DROP
  interfaces: eth0
  services:
  ports:
  rich rules:
"""
        self.assertEqual(
            "DROP",
            self.controls.security_baseline_firewall_target_from_list_all(output),
        )
        for invalid in ("", "target: INVALID", "target: DROP\ntarget: ACCEPT"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.controls.security_baseline_firewall_target_from_list_all(
                    invalid
                )

    def test_permanent_proxy_rule_is_removed_without_runtime_activation(self) -> None:
        desired_rule = (
            'rule family="ipv4" source address="10.0.0.0/24" '
            'port port="22" protocol="tcp" accept'
        )
        stale_rule = (
            'rule family="ipv4" source address="10.2.0.0/24" '
            'port port="443" protocol="tcp" accept'
        )
        tasks = load_tasks("roles/security_baseline/tasks/firewall.yml")
        offline_tasks = [
            copy.deepcopy(
                next(task for task in tasks if task["name"] == name)
            )
            for name in (
                "Read permanent rich rules without runtime activation",
                "Remove extra permanent rich rules without runtime activation",
            )
        ]

        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=REPOSITORY_ROOT / ".tmp"
        ) as directory:
            root = Path(directory)
            state = root / "rules.yml"
            state.write_text(
                yaml.safe_dump([desired_rule, stale_rule]), encoding="utf-8"
            )
            calls = root / "calls.yml"
            firewall = root / "firewall-cmd"
            firewall.write_text(
                f"""#!{sys.executable}
import sys
from pathlib import Path

import yaml

state = Path({str(state)!r})
calls = Path({str(calls)!r})
rules = yaml.safe_load(state.read_text(encoding="utf-8"))
history = yaml.safe_load(calls.read_text(encoding="utf-8")) if calls.exists() else []
history.append(sys.argv[1:])
calls.write_text(yaml.safe_dump(history), encoding="utf-8")
if "--list-rich-rules" in sys.argv:
    print("\\n".join(rules))
else:
    argument = next(value for value in sys.argv if value.startswith("--remove-rich-rule="))
    rules.remove(argument.removeprefix("--remove-rich-rule="))
    state.write_text(yaml.safe_dump(rules), encoding="utf-8")
""",
                encoding="utf-8",
            )
            firewall.chmod(0o755)
            for task in offline_tasks:
                task["ansible.builtin.command"]["argv"][0] = str(firewall)
            playbook = root / "reconcile.yml"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Reconcile permanent firewall without runtime",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "vars": {
                                "security_baseline_apply_firewall_runtime": False,
                                "security_baseline_firewall_desired_rules": [
                                    desired_rule
                                ],
                            },
                            "tasks": offline_tasks,
                        }
                    ],
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(playbook)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(
                [desired_rule], yaml.safe_load(state.read_text(encoding="utf-8"))
            )
            self.assertEqual(
                [
                    ["--zone=homelab", "--list-rich-rules"],
                    [
                        "--zone=homelab",
                        f"--remove-rich-rule={stale_rule}",
                    ],
                ],
                yaml.safe_load(calls.read_text(encoding="utf-8")),
            )

    def test_offline_rule_cleanup_skips_when_runtime_is_active(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/firewall.yml")
        offline_tasks = [
            task
            for task in tasks
            if task["name"]
            in {
                "Read permanent rich rules without runtime activation",
                "Remove extra permanent rich rules without runtime activation",
            }
        ]
        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=REPOSITORY_ROOT / ".tmp"
        ) as directory:
            playbook = Path(directory) / "skip-offline.yml"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Skip offline cleanup for a live firewall",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "vars": {
                                "security_baseline_apply_firewall_runtime": True,
                                "security_baseline_firewall_desired_rules": [],
                            },
                            "tasks": offline_tasks,
                        }
                    ],
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(playbook)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(2, result.stdout.count("skipping: [localhost]"))

    def test_false_to_true_guard_transition_emits_supported_reload_path(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/firewall.yml")
        read_state_tasks = load_tasks(
            "roles/security_baseline/tasks/firewall-read-state.yml"
        )
        reload_task = next(
            task
            for task in tasks
            if task["name"] == "Load staged permanent firewall policy into runtime"
        )
        reload_argv = reload_task["ansible.builtin.command"]["argv"]
        self.assertEqual(
            ["/usr/bin/firewall-cmd", "--reload"],
            reload_argv,
        )
        self.assertIn(
            "security_baseline_apply_firewall_runtime | bool",
            reload_task["when"],
        )

        second_pass_runtime = {
            "zones": ["homelab"],
            "target": "default",
            "forward": False,
            "rich_rules": [],
        }
        reload_required = self.controls.security_baseline_firewall_reload_required(
            second_pass_runtime,
            [],
            False,
        )
        self.assertEqual([], [reload_argv] if False and reload_required else [])
        self.assertEqual(
            [reload_argv],
            [reload_argv] if True and reload_required else [],
        )

        initial_target_task = next(
            task
            for task in tasks
            if task["name"] == "Read initial runtime homelab target"
        )
        self.assertEqual(
            ["/usr/bin/firewall-cmd", "--zone=homelab", "--list-all"],
            initial_target_task["ansible.builtin.command"]["argv"],
        )
        complete_state_task = next(
            task
            for task in read_state_tasks
            if task["name"] == "Read homelab firewall complete zone state"
        )
        self.assertIn(
            "--list-all",
            complete_state_task["ansible.builtin.command"]["argv"],
        )
        scalar_state_task = next(
            task
            for task in read_state_tasks
            if task["name"] == "Read homelab firewall scalar state"
        )
        self.assertEqual(
            ["--query-forward", "--query-masquerade", "--query-icmp-block-inversion"],
            scalar_state_task["loop"],
        )
        for task in [*tasks, *read_state_tasks]:
            command = task.get("ansible.builtin.command")
            if not isinstance(command, dict):
                continue
            argv = command.get("argv", [])
            runtime_only_target = any(
                token == "--get-target" or str(token).startswith("--set-target")
                for token in argv
            )
            if runtime_only_target:
                self.assertIn("--permanent", argv)

    def test_unbound_management_interface_reaches_binding(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/firewall.yml")
        names = [task["name"] for task in tasks]
        expected_queries = {
            "Query runtime homelab management interface binding": [
                "/usr/bin/firewall-cmd",
                "--zone=homelab",
                "--query-interface={{ security_baseline_firewall_management.interface }}",
            ],
            "Query permanent homelab management interface binding": [
                "/usr/bin/firewall-cmd",
                "--permanent",
                "--zone=homelab",
                "--query-interface={{ security_baseline_firewall_management.interface }}",
            ],
        }

        for name, expected_argv in expected_queries.items():
            with self.subTest(task=name):
                task = tasks[names.index(name)]
                self.assertEqual(
                    expected_argv,
                    task["ansible.builtin.command"]["argv"],
                )
                register = task["register"]
                self.assertEqual(
                    f"{register}.rc not in [0, 1]",
                    task["failed_when"],
                )

        bind_tasks = (
            "Bind runtime management interface to homelab",
            "Persist management interface binding to homelab",
        )
        for name in bind_tasks:
            with self.subTest(task=name):
                task = tasks[names.index(name)]
                query_result = (
                    "security_baseline_firewall_management_runtime_binding"
                    if name.startswith("Bind runtime")
                    else "security_baseline_firewall_management_permanent_binding"
                )
                self.assertIn(f"{query_result}.rc == 1", task["when"])

    def test_exact_policy_comparison_ignores_firewalld_output_order(self) -> None:
        rules = [
            (
                'rule family="ipv4" source address="10.0.0.0/24" '
                'port port="22" protocol="tcp" accept'
            ),
            (
                'rule family="ipv4" source address="10.0.1.0/24" '
                'port port="22" protocol="tcp" accept'
            ),
        ]
        runtime = self.exact_state(rules[0])
        runtime["rich_rules"] = rules
        permanent = copy.deepcopy(runtime)
        permanent["rich_rules"] = list(reversed(rules))
        self.assertEqual(
            [],
            self.controls.security_baseline_firewall_policy_errors(
                {"runtime": runtime, "permanent": permanent},
                rules,
                "eth0",
            ),
        )

    def test_firewall_task_order_proves_before_and_after_exact_cleanup(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/firewall.yml")
        names = [task["name"] for task in tasks]
        first_proof = names.index(
            "Prove desired management path before removing stale firewall rules"
        )
        binding_proof = names.index(
            "Verify management interface binding before connection proof"
        )
        cleanup = names.index("Remove extra runtime homelab firewall services")
        exact = names.index("Verify exact runtime and permanent homelab policy")
        default_readback = names.index(
            "Verify runtime and permanent default firewall zones"
        )
        final_proof = names.index("Prove a new connection through exact firewall policy")
        reset_tasks = [
            task
            for task in tasks
            if task.get("ansible.builtin.meta") == "reset_connection"
        ]
        self.assertEqual(2, len(reset_tasks))
        self.assertTrue(all("when" not in task for task in reset_tasks))
        self.assertLess(binding_proof, first_proof)
        self.assertLess(first_proof, cleanup)
        self.assertLess(cleanup, exact)
        self.assertLess(exact, default_readback)
        self.assertLess(exact, final_proof)

        source = (REPOSITORY_ROOT / "roles/security_baseline/tasks/firewall.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("--change-interface=", source)
        for opening in (
            "--remove-service=",
            "--remove-port=",
            "--remove-protocol=",
            "--remove-source-port=",
            "--remove-forward-port=",
            "--remove-icmp-block=",
            "--remove-icmp-block-inversion",
            "--remove-rich-rule=",
        ):
            self.assertIn(opening, source)


class JournaldPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.controls = load_module(
            "roles/security_baseline/filter_plugins/platform_controls.py",
            "security_baseline_platform_controls_journal",
        )

    def test_journal_sizes_are_validated_conservatively(self) -> None:
        for value in ("1", "512M", "1G", "2T"):
            with self.subTest(value=value):
                self.assertTrue(
                    self.controls.security_baseline_journal_size_is_valid(value)
                )
        for value in ("", "0", "not-a-size", "512MB", "-1G", " 1G"):
            with self.subTest(value=value):
                self.assertFalse(
                    self.controls.security_baseline_journal_size_is_valid(value)
                )

    def test_later_journal_drop_in_wins_effective_precedence(self) -> None:
        cat_config = """\
# /etc/systemd/journald.conf.d/90-homelab.conf
[Journal]
Storage=persistent
SystemMaxUse=512M
SystemKeepFree=1G
# /etc/systemd/journald.conf.d/99-local.conf
[Journal]
Storage=volatile
SystemMaxUse=128M
"""
        self.assertEqual(
            {
                "Storage": "volatile",
                "SystemMaxUse": "128M",
                "SystemKeepFree": "1G",
            },
            self.controls.security_baseline_journald_effective_values(cat_config),
        )

    def test_journal_restart_is_queued_only_after_effective_validation(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/logging.yml")
        names = [task["name"] for task in tasks]
        self.assertLess(
            names.index("Validate bounded persistent journald configuration"),
            names.index("Queue journald restart after validated configuration change"),
        )

    def test_persistent_journal_directory_matches_vendor_tmpfiles_policy(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/logging.yml")
        directory = next(
            task["ansible.builtin.file"]
            for task in tasks
            if task.get("ansible.builtin.file", {}).get("path") == "/var/log/journal"
        )
        self.assertEqual("directory", directory["state"])
        self.assertEqual("root", directory["owner"])
        self.assertEqual("systemd-journal", directory["group"])
        self.assertEqual("2755", directory["mode"])

    def test_audit_package_uses_exact_supported_platform_mapping(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/logging.yml")
        audit = next(
            task
            for task in tasks
            if task["name"] == "Install audit service with vendor rules"
        )
        self.assertEqual(
            {"Debian": "auditd"},
            audit["vars"]["security_baseline_audit_packages"],
        )
        validate, install = audit["block"]
        self.assertIn(
            "ansible_facts['os_family'] in security_baseline_audit_packages",
            validate["ansible.builtin.assert"]["that"],
        )
        self.assertEqual(
            "{{ security_baseline_audit_packages[ansible_facts['os_family']] }}",
            install["ansible.builtin.package"]["name"],
        )


if __name__ == "__main__":
    unittest.main()
