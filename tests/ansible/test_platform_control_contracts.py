"""Behavioral contracts for platform-native security controls."""

from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
import types
import unittest
from unittest import mock
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

    def rocky_fixture(self, root: Path) -> dict[str, object]:
        repo_dir = root / "etc/dnf/authoritative.repos.d"
        repo_dir.mkdir(parents=True)
        repo_file = repo_dir / "rocky.repo"
        repo_file.touch()
        key = root / "etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-9"
        key.parent.mkdir(parents=True)
        key.touch()
        return {
            "gpgcheck": True,
            "localpkg_gpgcheck": True,
            "reposdir": ["/etc/dnf/authoritative.repos.d"],
            "tsflags": ["nodocs"],
            "repos": [
                {
                    "id": "baseos",
                    "enabled": True,
                    "gpgcheck": True,
                    "gpgkey": [
                        "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-9"
                    ],
                    "repofile": "/etc/dnf/authoritative.repos.d/rocky.repo",
                }
            ],
        }

    def test_rocky_accepts_effective_distribution_repository_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            effective = self.rocky_fixture(root)
            self.repository_trust.validate_rocky_configuration(effective, root)

    def test_rocky_rejects_signature_bypasses_and_non_authoritative_files(
        self,
    ) -> None:
        mutations = {
            "global gpgcheck off": lambda value: value.update(gpgcheck=False),
            "local package gpgcheck off": lambda value: value.update(
                localpkg_gpgcheck=False
            ),
            "inherited gpgcheck off": lambda value: value["repos"][0].update(
                gpgcheck=False
            ),
            "nocrypto": lambda value: value.update(tsflags=["nocrypto"]),
            "outside reposdir": lambda value: value["repos"][0].update(
                repofile="/etc/yum.repos.d/rocky.repo"
            ),
            "non-distribution repository": lambda value: value["repos"][0].update(
                id="third-party"
            ),
            "non-distribution key": lambda value: value["repos"][0].update(
                gpgkey=["file:///etc/pki/rpm-gpg/THIRD-PARTY"]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                effective = self.rocky_fixture(root)
                mutate(effective)
                with self.assertRaises(ValueError):
                    self.repository_trust.validate_rocky_configuration(
                        effective,
                        root,
                    )

    def test_rocky_collects_plugin_mutation_after_repository_loading(self) -> None:
        events: list[str] = []

        class FakeSubstitutions(dict[str, str]):
            def update_from_etc(self, _installroot: str) -> None:
                self["releasever"] = "9"

        class FakeConfiguration:
            def __init__(self) -> None:
                self.reposdir = ["/etc/dnf/authoritative.repos.d"]
                self.tsflags = ["nodocs"]
                self.gpgcheck = True
                self.localpkg_gpgcheck = True
                self.substitutions = FakeSubstitutions()

            def read(self) -> None:
                events.append("read-config")

            def prepend_installroot(self, _option: str) -> None:
                return None

        class FakeRepository:
            id = "baseos"
            enabled = True
            gpgcheck = True
            gpgkey = ["file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-9"]
            repofile = "/etc/dnf/authoritative.repos.d/rocky.repo"

        class FakeRepositories:
            def __init__(self) -> None:
                self.repository = FakeRepository()

            def all(self) -> list[FakeRepository]:
                return [self.repository]

        class FakeBase:
            def __init__(self) -> None:
                self.conf = FakeConfiguration()
                self.repos = FakeRepositories()

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def setup_loggers(self) -> None:
                events.append("setup-loggers")

            def init_plugins(
                self,
                disabled: set[str],
                enabled: set[str],
            ) -> None:
                self.assert_empty_plugin_overrides(disabled, enabled)
                events.append("init-plugins")

            @staticmethod
            def assert_empty_plugin_overrides(
                disabled: set[str],
                enabled: set[str],
            ) -> None:
                if disabled or enabled:
                    raise AssertionError("unexpected plugin override")

            def pre_configure_plugins(self) -> None:
                events.append("pre-configure-plugins")

            def read_all_repos(self) -> None:
                events.append("read-repositories")

            def configure_plugins(self) -> None:
                events.append("configure-plugins")
                self.repos.repository.gpgcheck = False

        fake_dnf = types.SimpleNamespace(Base=FakeBase)
        with mock.patch.dict(sys.modules, {"dnf": fake_dnf}):
            effective = self.repository_trust._collect_rocky_configuration()

        self.assertEqual(
            [
                "read-config",
                "setup-loggers",
                "init-plugins",
                "pre-configure-plugins",
                "read-repositories",
                "configure-plugins",
            ],
            events,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.rocky_fixture(root)
            effective["reposdir"] = fixture["reposdir"]
            with self.assertRaises(ValueError):
                self.repository_trust.validate_rocky_configuration(effective, root)

    def test_rocky_collector_preserves_disabled_global_and_inherited_repo_trust(
        self,
    ) -> None:
        class FakeSubstitutions(dict[str, str]):
            def update_from_etc(self, _installroot: str) -> None:
                self["releasever"] = "9"

        class FakeConfiguration:
            def __init__(self) -> None:
                self.reposdir = ["/etc/dnf/authoritative.repos.d"]
                self.tsflags = ["nodocs"]
                self.gpgcheck = False
                self.localpkg_gpgcheck = False
                self.substitutions = FakeSubstitutions()

            def read(self) -> None:
                return None

            def prepend_installroot(self, _option: str) -> None:
                return None

        class FakeRepository:
            id = "baseos"
            enabled = True
            gpgkey = ["file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-9"]
            repofile = "/etc/dnf/authoritative.repos.d/rocky.repo"

            def __init__(self, gpgcheck: bool) -> None:
                self.gpgcheck = gpgcheck

        class FakeRepositories:
            def __init__(self) -> None:
                self.repository: FakeRepository | None = None

            def all(self) -> list[FakeRepository]:
                if self.repository is None:
                    raise AssertionError("repositories were not loaded")
                return [self.repository]

        class FakeBase:
            def __init__(self) -> None:
                self.conf = FakeConfiguration()
                self.repos = FakeRepositories()

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def setup_loggers(self) -> None:
                return None

            def init_plugins(
                self,
                _disabled: set[str],
                _enabled: set[str],
            ) -> None:
                return None

            def pre_configure_plugins(self) -> None:
                return None

            def read_all_repos(self) -> None:
                self.repos.repository = FakeRepository(self.conf.gpgcheck)

            def configure_plugins(self) -> None:
                return None

        fake_dnf = types.SimpleNamespace(Base=FakeBase)
        with mock.patch.dict(sys.modules, {"dnf": fake_dnf}):
            effective = self.repository_trust._collect_rocky_configuration()

        self.assertIs(effective["gpgcheck"], False)
        self.assertIs(effective["localpkg_gpgcheck"], False)
        self.assertIs(effective["repos"][0]["gpgcheck"], False)
        with self.assertRaises(ValueError):
            self.repository_trust.validate_rocky_configuration(effective)


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
    def expected_policy(os_family: str = "Debian") -> str:
        rules = [
            "neighbour-advertisement",
            "neighbour-solicitation",
            "redirect",
            "router-advertisement",
        ]
        if os_family == "RedHat":
            rules.extend(
                (
                    "mld-listener-done",
                    "mld-listener-query",
                    "mld-listener-report",
                    "mld2-listener-report",
                )
            )
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

        self.assertEqual(
            [],
            self.controls.security_baseline_firewall_global_surface_errors(
                "homelab\n  interfaces: eth0\n  sources:\n",
                self.global_direct_results(),
                self.expected_policy("RedHat"),
                "eth0",
                False,
                "RedHat",
            ),
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
            {"Debian": "auditd", "RedHat": "audit"},
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

    def test_rocky_selinux_install_owns_targeted_policy_configuration(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/mac.yml")
        install = next(
            task
            for task in tasks
            if task["name"] == "Install Rocky SELinux packages"
        )
        self.assertEqual(
            ["policycoreutils", "selinux-policy-targeted"],
            install["ansible.builtin.package"]["name"],
        )
        self.assertEqual("ansible_facts['os_family'] == 'RedHat'", install["when"])


if __name__ == "__main__":
    unittest.main()
