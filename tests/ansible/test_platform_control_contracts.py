"""Behavioral contracts for platform-native security controls."""

from __future__ import annotations

import copy
import importlib.util
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

    def rocky_fixture(self, root: Path) -> dict[str, object]:
        repo_dir = root / "etc/dnf/authoritative.repos.d"
        repo_dir.mkdir(parents=True)
        repo_file = repo_dir / "rocky.repo"
        repo_file.touch()
        key = root / "etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-9"
        key.parent.mkdir(parents=True)
        key.touch()
        return {
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
            "interface_zone": "homelab",
            "interfaces": ["eth0"],
            "sources": [],
            "services": [],
            "ports": [],
            "protocols": [],
            "source_ports": [],
            "forward_ports": [],
            "rich_rules": [rule],
        }

    def test_extension_contract_rejects_ssh(self) -> None:
        payload = {
            "management_sources": ["10.0.0.0/24"],
            "services": [
                {"service": "ssh", "sources": ["10.0.1.0/24"]}
            ],
        }
        with self.assertRaises(ValueError):
            self.controls.security_baseline_firewall_rules(payload)

    def test_exact_policy_rejects_alternate_binding_and_every_direct_opening(
        self,
    ) -> None:
        rule = (
            'rule family="ipv4" source address="10.0.0.0/24" '
            'service name="ssh" accept'
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
            'service name="ssh" accept'
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

    def test_exact_policy_comparison_ignores_firewalld_output_order(self) -> None:
        rules = [
            (
                'rule family="ipv4" source address="10.0.0.0/24" '
                'service name="ssh" accept'
            ),
            (
                'rule family="ipv4" source address="10.0.1.0/24" '
                'service name="ssh" accept'
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

    def test_only_source_bindings_relevant_to_management_peer_conflict(self) -> None:
        unrelated = """\
containers
  sources: 10.88.0.0/16
"""
        self.assertEqual(
            [],
            self.controls.security_baseline_firewall_conflicting_sources(
                unrelated,
                "10.0.0.5",
            ),
        )
        matching = unrelated + """\
public
  sources: 10.0.0.0/24
"""
        self.assertEqual(
            ["10.0.0.0/24"],
            self.controls.security_baseline_firewall_conflicting_sources(
                matching,
                "10.0.0.5",
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


if __name__ == "__main__":
    unittest.main()
