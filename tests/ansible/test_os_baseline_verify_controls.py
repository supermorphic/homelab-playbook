"""Independent contracts for OS baseline verification parsers."""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_controls():
    path = REPOSITORY_ROOT / "roles/os_baseline_verify/filter_plugins/controls.py"
    spec = importlib.util.spec_from_file_location("os_baseline_verify_controls", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load verifier controls: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifierControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.controls = load_controls()

    def test_firewall_policy_rejects_non_private_or_empty_sources(self) -> None:
        for payload in (
            {"management_sources": [], "services": []},
            {"management_sources": ["203.0.113.0/24"], "services": []},
            {"management_sources": ["192.168.1.1/24"], "services": []},
            {"management_sources": ["192.168.1.0/24"], "services": [{"service": "dns", "sources": []}]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.controls.os_baseline_verify_firewall_rules(payload)

    def test_firewall_state_rejects_each_policy_bypass(self) -> None:
        desired = ['rule family="ipv4" source address="192.168.1.0/24" service name="ssh" accept']
        state = {
            "target": "DROP", "forward": False, "masquerade": False,
            "interface_zone": "homelab", "interfaces": ["eth0"],
            "sources": [], "services": [], "ports": [], "protocols": [],
            "source_ports": [], "forward_ports": [], "rich_rules": desired,
        }
        for field, value in (
            ("interfaces", ["eth0", "eth1"]), ("sources", ["192.168.2.0/24"]),
            ("masquerade", True), ("forward_ports", ["22:proto=tcp:toport=22"]),
        ):
            mutated = {**state, field: value}
            with self.subTest(field=field):
                self.assertTrue(self.controls.os_baseline_verify_firewall_state_errors(mutated, desired, "eth0"))

    def test_firewall_state_accepts_only_an_unbound_staged_policy_without_interface(
        self,
    ) -> None:
        desired = [
            'rule family="ipv4" source address="10.0.0.0/8" '
            'service name="ssh" accept'
        ]
        state = {
            "target": "DROP",
            "forward": False,
            "masquerade": False,
            "interface_zone": "homelab",
            "interfaces": [],
            "sources": [],
            "services": [],
            "ports": [],
            "protocols": [],
            "source_ports": [],
            "forward_ports": [],
            "rich_rules": desired,
        }
        self.assertEqual(
            [],
            self.controls.os_baseline_verify_firewall_state_errors(
                state,
                desired,
                "",
            ),
        )
        self.assertEqual(
            ["interfaces"],
            self.controls.os_baseline_verify_firewall_state_errors(
                state,
                desired,
                "eth0",
            ),
        )

    def test_firewall_result_layout_reduces_clean_and_mutated_state(self) -> None:
        desired = ['rule family="ipv4" source address="192.168.1.0/24" service name="ssh" accept']
        results = [
            {"stdout": "homelab (active)\n  target: DROP", "stdout_lines": []},
            {"stdout": "eth0\n", "stdout_lines": ["eth0"]},
            {"stdout": "", "stdout_lines": []}, {"stdout": "", "stdout_lines": []},
            {"stdout": "", "stdout_lines": []}, {"stdout": "", "stdout_lines": []},
            {"stdout": "", "stdout_lines": []}, {"stdout": "", "stdout_lines": []},
            {"stdout": desired[0], "stdout_lines": desired}, {"rc": 1}, {"rc": 1},
        ]
        state = self.controls.os_baseline_verify_firewall_state_from_results(results, "homelab")
        self.assertEqual([], self.controls.os_baseline_verify_firewall_state_errors(state, desired, "eth0"))
        wrong_zone = self.controls.os_baseline_verify_firewall_state_from_results(results, "public")
        self.assertEqual(
            ["interface-zone"],
            self.controls.os_baseline_verify_firewall_state_errors(wrong_zone, desired, "eth0"),
        )

        mutations = {
            0: {"stdout": "  target: ACCEPT", "stdout_lines": []},
            1: {"stdout": "eth0 eth1", "stdout_lines": ["eth0", "eth1"]},
            2: {"stdout": "source", "stdout_lines": ["source"]},
            3: {"stdout": "service", "stdout_lines": ["service"]},
            4: {"stdout": "22/tcp", "stdout_lines": ["22/tcp"]},
            5: {"stdout": "icmp", "stdout_lines": ["icmp"]},
            6: {"stdout": "1024-65535", "stdout_lines": ["1024-65535"]},
            7: {"stdout": "22:proto=tcp:toport=22", "stdout_lines": ["22:proto=tcp:toport=22"]},
            8: {"stdout": "unexpected", "stdout_lines": ["unexpected"]},
            9: {"rc": 0}, 10: {"rc": 0},
        }
        for index, replacement in mutations.items():
            with self.subTest(index=index):
                changed = [dict(result) for result in results]
                changed[index] = replacement
                mutated = self.controls.os_baseline_verify_firewall_state_from_results(changed, "homelab")
                self.assertTrue(self.controls.os_baseline_verify_firewall_state_errors(mutated, desired, "eth0"))

    def test_firewall_result_layout_rejects_malformed_results(self) -> None:
        with self.assertRaises(ValueError):
            self.controls.os_baseline_verify_firewall_state_from_results([], "homelab")

    def test_global_firewall_surface_reducer_rejects_bindings_direct_and_policies(
        self,
    ) -> None:
        bindings = "homelab\n  interfaces: eth0\n  sources:\n"
        direct = [
            {"item": "--get-all-chains", "stdout": ""},
            {"item": "--get-all-rules", "stdout": ""},
            {"item": "--get-all-passthroughs", "stdout": ""},
        ]
        policies = """\
allow-host-ipv6
  priority: -15000
  target: CONTINUE
  ingress-zones: ANY
  egress-zones: HOST
  services:
  ports:
  protocols:
  masquerade: no
  forward-ports:
  source-ports:
  icmp-blocks:
  rich rules:
    rule family="ipv6" icmp-type name="neighbour-advertisement" accept
    rule family="ipv6" icmp-type name="neighbour-solicitation" accept
    rule family="ipv6" icmp-type name="redirect" accept
    rule family="ipv6" icmp-type name="router-advertisement" accept
"""
        self.assertEqual(
            [],
            self.controls.os_baseline_verify_firewall_global_surface_errors(
                bindings,
                direct,
                policies,
                "eth0",
                "Debian",
            ),
        )
        mutations = (
            (bindings + "public\n  interfaces: eth1\n  sources:\n", direct, policies),
            (bindings + "trusted\n  interfaces:\n  sources: 10.0.0.0/8\n", direct, policies),
            (bindings, [{**direct[0], "stdout": "ipv4 filter EXTRA"}, *direct[1:]], policies),
            (bindings, direct, policies + "\noperator-policy"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertTrue(
                    self.controls.os_baseline_verify_firewall_global_surface_errors(
                        *mutation,
                        "eth0",
                        "Debian",
                    )
                )

    def test_ordered_journald_reducer_keeps_empty_last_assignment(self) -> None:
        text = """\
[Other]
Storage=volatile
[Journal]
Storage=persistent
SystemMaxUse=512M
SystemKeepFree=1G
SystemMaxUse=
# SystemKeepFree=2G
"""
        self.assertEqual(
            {"Storage": "persistent", "SystemMaxUse": "", "SystemKeepFree": "1G"},
            self.controls.os_baseline_verify_journald_values(text),
        )

    def test_apt_policy_parses_exact_real_dump_origin_list(self) -> None:
        apt = """\
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Origins-Pattern "";
Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
Unattended-Upgrade::Remove-Unused-Dependencies "false";
"""
        policy = self.controls.os_baseline_verify_apt_policy(apt)
        self.assertEqual(
            ["origin=Debian,codename=${distro_codename}-security,label=Debian-Security"],
            policy["origins"],
        )

    def test_apt_policy_rejects_missing_extra_and_replaced_dump_origins(self) -> None:
        prefix = 'Unattended-Upgrade::Origins-Pattern "";\n'
        expected = 'Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";\n'
        for output in (
            prefix,
            prefix + expected + 'Unattended-Upgrade::Origins-Pattern:: "origin=Debian";\n',
            prefix + 'Unattended-Upgrade::Origins-Pattern:: "origin=Debian";\n',
        ):
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    self.controls.os_baseline_verify_apt_policy(output)

    def test_updater_parsers_reject_later_override_and_extra_origin(self) -> None:
        apt = """\
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Update-Package-Lists "0";
Unattended-Upgrade::Origins-Pattern "";
Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
"""
        self.assertEqual("0", self.controls.os_baseline_verify_apt_policy(apt)["scalar"]["APT::Periodic::Update-Package-Lists"])
        dnf = """\
[commands]
upgrade_type = security
download_updates = yes
apply_updates = yes
reboot = when-needed
upgrade_type = default
[emitters]
emit_via = stdio
"""
        self.assertEqual("default", self.controls.os_baseline_verify_ini_values(dnf)["commands"]["upgrade_type"])

    def test_apt_policy_rejects_missing_origins(self) -> None:
        with self.assertRaises(ValueError):
            self.controls.os_baseline_verify_apt_policy(
                'APT::Periodic::Unattended-Upgrade "1";\n'
            )

    def test_verifier_owns_management_interface_discovery_script(self) -> None:
        script = REPOSITORY_ROOT / "roles/os_baseline_verify/files/discover_management_interface.py"
        self.assertTrue(script.is_file())
        self.assertTrue(os.access(script, os.X_OK))
        self.assertIn("SSH_CONNECTION", script.read_text(encoding="utf-8"))

    def test_apparmor_distribution_profile_names_match_path_and_filename_forms(self) -> None:
        self.assertEqual(
            ["usr.sbin.tcpdump", "/usr/sbin/tcpdump", "usr.bin.man", "/usr/bin/man"],
            self.controls.os_baseline_verify_apparmor_profile_names(
                [
                    "/etc/apparmor.d/abstractions/base",
                    "/etc/apparmor.d/usr.sbin.tcpdump",
                    "/etc/apparmor.d/usr.bin.man",
                ]
            ),
        )

    def test_timer_parser_honors_reset_and_rejects_extra_calendar(self) -> None:
        text = """\
[Timer]
OnCalendar=weekly
OnCalendar=
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=0
Persistent=true
"""
        self.assertEqual(
            {"OnCalendar": ["*-*-* 04:00:00"], "RandomizedDelaySec": "0", "Persistent": "true"},
            self.controls.os_baseline_verify_timer_values(text),
        )
