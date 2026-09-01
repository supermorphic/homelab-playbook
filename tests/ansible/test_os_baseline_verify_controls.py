"""Independent contracts for OS baseline verification parsers."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
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

    def test_firewall_result_layout_rejects_malformed_results_and_active_peer_conflicts(self) -> None:
        with self.assertRaises(ValueError):
            self.controls.os_baseline_verify_firewall_state_from_results([], "homelab")
        active = """\
homelab
  interfaces: eth0
  sources:
trusted
  sources: 192.168.1.0/24
"""
        self.assertEqual(["192.168.1.0/24"], self.controls.os_baseline_verify_conflicting_sources(active, "192.168.1.10"))
        self.assertEqual([], self.controls.os_baseline_verify_conflicting_sources(active, "10.1.2.3"))
        malformed = "external\n  sources: not-a-network\n"
        self.assertEqual(["not-a-network"], self.controls.os_baseline_verify_conflicting_sources(malformed, "192.168.1.10"))

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

    def test_chrony_policy_requires_complete_vendor_or_approved_source_decision(self) -> None:
        vendor = "pool 2.debian.pool.ntp.org iburst\n"
        self.assertEqual(["2.debian.pool.ntp.org"], self.controls.os_baseline_verify_chrony_policy(vendor, []))
        approved = """\
# BEGIN ANSIBLE MANAGED TRUSTED CHRONY SOURCES
server approved.example iburst
# END ANSIBLE MANAGED TRUSTED CHRONY SOURCES
# homelab-disabled: pool vendor.example iburst
"""
        self.assertEqual(["approved.example"], self.controls.os_baseline_verify_chrony_policy(approved, ["approved.example"]))
        for text, requested in (
            ("server arbitrary.example iburst\n", []),
            (approved + "confdir /etc/chrony/conf.d\n", ["approved.example"]),
            (approved.replace("# homelab-disabled", "pool vendor.example"), ["approved.example"]),
            (approved.replace("approved.example", "other.example"), ["approved.example"]),
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.controls.os_baseline_verify_chrony_policy(text, requested)

    def test_chrony_effective_policy_accepts_packaged_vendor_and_override_transitions(self) -> None:
        script = REPOSITORY_ROOT / "roles/os_baseline_verify/files/discover_chrony_sources.py"
        self.assertTrue(script.is_file())
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for platform, source_inputs in (
                ("debian", ["confdir", "sourcedir"]),
                ("rocky", ["sourcedir"]),
            ):
                with self.subTest(platform=platform):
                    config = root / platform / "chrony.conf"
                    config.parent.mkdir(parents=True)
                    input_directories = {}
                    for source_input in source_inputs:
                        input_directory = config.parent / source_input
                        input_directory.mkdir()
                        suffix = ".conf" if source_input == "confdir" else ".sources"
                        (input_directory / f"vendor{suffix}").write_text(
                            "server dhcp.vendor.example iburst\n", encoding="utf-8",
                        )
                        input_directories[source_input] = input_directory
                    vendor_pool = f"2.{platform}.pool.ntp.org"
                    config.write_text(
                        "\n".join(
                            [
                                *(f"{source_input} {input_directories[source_input]}" for source_input in source_inputs if source_input == "confdir"),
                                f"pool {vendor_pool} iburst",
                                *(f"{source_input} {input_directories[source_input]}" for source_input in source_inputs if source_input != "confdir"),
                            ]
                        ) + "\n",
                        encoding="utf-8",
                    )
                    result = subprocess.run(
                        [str(script), str(config)], check=True, capture_output=True, text=True,
                    )
                    vendor_state = json.loads(result.stdout)
                    vendor_sources = (
                        (["dhcp.vendor.example"] if "confdir" in source_inputs else [])
                        + [vendor_pool]
                        + (["dhcp.vendor.example"] if "sourcedir" in source_inputs else [])
                    )
                    self.assertEqual(vendor_sources, self.controls.os_baseline_verify_chrony_effective_policy(vendor_state, []))

                    config.write_text(
                        "# BEGIN ANSIBLE MANAGED TRUSTED CHRONY SOURCES\n"
                        "server approved.example iburst\n"
                        "# END ANSIBLE MANAGED TRUSTED CHRONY SOURCES\n"
                        f"# homelab-disabled: pool {vendor_pool} iburst\n"
                        + "".join(f"# homelab-disabled: {source_input} {input_directories[source_input]}\n" for source_input in source_inputs),
                        encoding="utf-8",
                    )
                    result = subprocess.run(
                        [str(script), str(config)], check=True, capture_output=True, text=True,
                    )
                    approved_state = json.loads(result.stdout)
                    self.assertEqual(
                        ["approved.example"],
                        self.controls.os_baseline_verify_chrony_effective_policy(
                            approved_state, ["approved.example"],
                        ),
                    )
                    config.write_text(
                        "\n".join(
                            [
                                *(f"{source_input} {input_directories[source_input]}" for source_input in source_inputs if source_input == "confdir"),
                                f"pool {vendor_pool} iburst",
                                *(f"{source_input} {input_directories[source_input]}" for source_input in source_inputs if source_input != "confdir"),
                            ]
                        ) + "\n",
                        encoding="utf-8",
                    )
                    result = subprocess.run(
                        [str(script), str(config)], check=True, capture_output=True, text=True,
                    )
                    self.assertEqual(vendor_sources, self.controls.os_baseline_verify_chrony_effective_policy(json.loads(result.stdout), []))

    def test_chrony_effective_policy_resolves_active_include_inputs(self) -> None:
        script = REPOSITORY_ROOT / "roles/os_baseline_verify/files/discover_chrony_sources.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            included = root / "chrony.d"
            included.mkdir()
            (included / "vendor.conf").write_text("server dhcp.vendor.example iburst\n", encoding="utf-8")
            config = root / "chrony.conf"
            config.write_text(
                f"pool 2.debian.pool.ntp.org iburst\ninclude {included}/*.conf\n",
                encoding="utf-8",
            )
            result = subprocess.run([str(script), str(config)], check=True, capture_output=True, text=True)
            self.assertEqual(
                ["2.debian.pool.ntp.org", "dhcp.vendor.example"],
                self.controls.os_baseline_verify_chrony_effective_policy(json.loads(result.stdout), []),
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
