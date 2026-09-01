"""Independent contracts for OS baseline verification parsers."""

from __future__ import annotations

import importlib.util
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

    def test_updater_parsers_reject_later_override_and_extra_origin(self) -> None:
        apt = """\
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Origins-Pattern { "origin=Debian,codename=${distro_codename}-security,label=Debian-Security"; "origin=Debian"; };
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
Unattended-Upgrade::Remove-Unused-Dependencies "false";
"""
        with self.assertRaises(ValueError):
            self.controls.os_baseline_verify_apt_policy(apt)
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
        self.assertIn("SSH_CONNECTION", script.read_text(encoding="utf-8"))

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
