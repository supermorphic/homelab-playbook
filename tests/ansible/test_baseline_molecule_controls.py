"""Mutation-sensitive contracts for the independent baseline scenario oracle."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTROLS_PATH = (
    REPOSITORY_ROOT
    / "roles/system_maintenance/molecule/baseline/filter_plugins/baseline_controls.py"
)
DEBIAN_FIREWALL_POLICY = """\
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


def load_controls():
    if not CONTROLS_PATH.is_file():
        raise AssertionError(f"required independent controls do not exist: {CONTROLS_PATH}")
    spec = importlib.util.spec_from_file_location(
        "system_maintenance_molecule_baseline_controls",
        CONTROLS_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load independent controls: {CONTROLS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command_result(item: str, stdout: str = "", rc: int = 0) -> dict[str, object]:
    return {
        "item": item,
        "stdout": stdout,
        "stdout_lines": stdout.splitlines(),
        "rc": rc,
    }


def exact_firewall_results() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    zone = [
        command_result("--list-all", "homelab (default)\n  target: DROP"),
        command_result("--list-interfaces"),
        command_result("--list-sources"),
        command_result("--list-services"),
        command_result("--list-ports"),
        command_result("--list-protocols"),
        command_result("--list-source-ports"),
        command_result("--list-forward-ports"),
        command_result("--list-icmp-blocks"),
        command_result(
            "--list-rich-rules",
            'rule family="ipv4" source address="10.0.0.0/8" '
            'service name="ssh" accept',
        ),
        command_result("--query-forward", "no", 1),
        command_result("--query-masquerade", "no", 1),
    ]
    direct = [
        command_result("--get-all-chains"),
        command_result("--get-all-rules"),
        command_result("--get-all-passthroughs"),
    ]
    return zone, direct


class FirewallOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.controls = load_controls()

    def errors(
        self,
        zone: list[dict[str, object]],
        direct: list[dict[str, object]],
        configuration: str = "DefaultZone=homelab\n",
        bindings: str = "homelab\n  interfaces:\n  sources:\n",
        policies: str = DEBIAN_FIREWALL_POLICY,
        os_family: str = "Debian",
    ) -> list[str]:
        return self.controls.system_maintenance_molecule_baseline_firewall_errors(
            configuration,
            zone,
            direct,
            bindings,
            policies,
            os_family,
        )

    def test_exact_permanent_firewall_policy_has_no_errors(self) -> None:
        zone, direct = exact_firewall_results()
        self.assertEqual([], self.errors(zone, direct))

    def test_wrong_or_duplicate_default_zone_is_rejected(self) -> None:
        zone, direct = exact_firewall_results()
        for configuration in (
            "DefaultZone=public\n",
            "DefaultZone=homelab\nDefaultZone=homelab\n",
        ):
            with self.subTest(configuration=configuration):
                self.assertIn("default-zone", self.errors(zone, direct, configuration))

    def test_scalar_and_target_mutations_are_rejected(self) -> None:
        for item, mutation, expected in (
            ("--list-all", "homelab (default)\n  target: ACCEPT", "target"),
            ("--query-forward", "yes", "forward"),
            ("--query-masquerade", "yes", "masquerade"),
        ):
            zone, direct = exact_firewall_results()
            result = next(value for value in zone if value["item"] == item)
            result["stdout"] = mutation
            result["stdout_lines"] = mutation.splitlines()
            if item.startswith("--query-"):
                result["rc"] = 0
            with self.subTest(item=item):
                self.assertIn(expected, self.errors(zone, direct))

    def test_every_forbidden_zone_opening_is_rejected(self) -> None:
        mutations = {
            "--list-interfaces": ("eth0", "interfaces"),
            "--list-sources": ("192.0.2.0/24", "sources"),
            "--list-services": ("cockpit", "services"),
            "--list-ports": ("8443/tcp", "ports"),
            "--list-protocols": ("gre", "protocols"),
            "--list-source-ports": ("1024-65535/tcp", "source-ports"),
            "--list-forward-ports": (
                "port=443:proto=tcp:toport=8443:toaddr=",
                "forward-ports",
            ),
            "--list-icmp-blocks": ("echo-request", "icmp-blocks"),
        }
        for item, (opening, expected) in mutations.items():
            zone, direct = exact_firewall_results()
            result = next(value for value in zone if value["item"] == item)
            result["stdout"] = opening
            result["stdout_lines"] = [opening]
            with self.subTest(item=item):
                self.assertIn(expected, self.errors(zone, direct))

    def test_every_direct_opening_is_rejected(self) -> None:
        mutations = {
            "--get-all-chains": "ipv4 filter TEST",
            "--get-all-rules": "ipv4 filter INPUT 0 -j ACCEPT",
            "--get-all-passthroughs": "ipv4 -A INPUT -j ACCEPT",
        }
        for item, opening in mutations.items():
            zone, direct = exact_firewall_results()
            result = next(value for value in direct if value["item"] == item)
            result["stdout"] = opening
            result["stdout_lines"] = [opening]
            with self.subTest(item=item):
                self.assertIn("direct-openings", self.errors(zone, direct))

    def test_global_bindings_and_policy_objects_are_rejected(self) -> None:
        zone, direct = exact_firewall_results()
        self.assertIn(
            "zone-bindings",
            self.errors(
                zone,
                direct,
                bindings="homelab\n  interfaces:\n  sources:\npublic\n  interfaces: eth1\n  sources:\n",
            ),
        )
        self.assertIn(
            "policy-objects",
            self.errors(
                zone,
                direct,
                policies=DEBIAN_FIREWALL_POLICY + "\noperator-policy",
            ),
        )

    def test_missing_or_extra_rich_rule_is_rejected(self) -> None:
        for rules in (
            [],
            [
                'rule family="ipv4" source address="10.0.0.0/8" '
                'service name="ssh" accept',
                'rule family="ipv4" source address="10.0.0.0/8" '
                'service name="https" accept',
            ],
        ):
            zone, direct = exact_firewall_results()
            result = next(
                value for value in zone if value["item"] == "--list-rich-rules"
            )
            result["stdout"] = "\n".join(rules)
            result["stdout_lines"] = rules
            with self.subTest(rules=rules):
                self.assertIn("rich-rules", self.errors(zone, direct))


DEBIAN_APT_POLICY = """\
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
Unattended-Upgrade::Remove-Unused-Dependencies "false";
"""

ROCKY_DNF_POLICY = """\
[commands]
upgrade_type = security
download_updates = yes
apply_updates = yes
reboot = never

[emitters]
emit_via = stdio
"""

TIMER_POLICY = """\
[Timer]
OnCalendar=
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=0
Persistent=true
"""


class NativeUpdaterOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.controls = load_controls()

    def test_complete_debian_updater_policy_has_no_errors(self) -> None:
        self.assertEqual(
            [],
            self.controls.system_maintenance_molecule_baseline_debian_updater_errors(
                DEBIAN_APT_POLICY,
                TIMER_POLICY,
                "enabled",
            ),
        )

    def test_each_debian_policy_mutation_is_rejected(self) -> None:
        mutations = {
            'Update-Package-Lists "1"': 'Update-Package-Lists "0"',
            'Unattended-Upgrade "1"': 'Unattended-Upgrade "0"',
            "label=Debian-Security": "label=Debian",
            'Automatic-Reboot "false"': 'Automatic-Reboot "true"',
            'Automatic-Reboot-WithUsers "true"': 'Automatic-Reboot-WithUsers "false"',
            'Automatic-Reboot-Time "04:30"': 'Automatic-Reboot-Time "03:30"',
            'Remove-Unused-Dependencies "false"': 'Remove-Unused-Dependencies "true"',
        }
        for original, replacement in mutations.items():
            with self.subTest(original=original):
                errors = self.controls.system_maintenance_molecule_baseline_debian_updater_errors(
                    DEBIAN_APT_POLICY.replace(original, replacement),
                    TIMER_POLICY,
                    "enabled",
                )
                self.assertTrue(errors)

    def test_extra_debian_origin_is_rejected(self) -> None:
        mutation = DEBIAN_APT_POLICY + (
            'Unattended-Upgrade::Origins-Pattern:: "origin=Debian,label=Debian";\n'
        )
        self.assertIn(
            "origins",
            self.controls.system_maintenance_molecule_baseline_debian_updater_errors(
                mutation,
                TIMER_POLICY,
                "enabled",
            ),
        )

    def test_complete_rocky_updater_policy_has_no_errors(self) -> None:
        self.assertEqual(
            [],
            self.controls.system_maintenance_molecule_baseline_rocky_updater_errors(
                ROCKY_DNF_POLICY,
                TIMER_POLICY,
                "enabled",
            ),
        )

    def test_each_rocky_policy_mutation_is_rejected(self) -> None:
        mutations = {
            "upgrade_type = security": "upgrade_type = default",
            "download_updates = yes": "download_updates = no",
            "apply_updates = yes": "apply_updates = no",
            "reboot = never": "reboot = when-needed",
            "emit_via = stdio": "emit_via = motd",
        }
        for original, replacement in mutations.items():
            with self.subTest(original=original):
                errors = self.controls.system_maintenance_molecule_baseline_rocky_updater_errors(
                    ROCKY_DNF_POLICY.replace(original, replacement),
                    TIMER_POLICY,
                    "enabled",
                )
                self.assertTrue(errors)

    def test_timer_schedule_and_enablement_mutations_are_rejected(self) -> None:
        for timer, enabled, expected in (
            (TIMER_POLICY.replace("04:00:00", "03:00:00"), "enabled", "timer"),
            (TIMER_POLICY.replace("RandomizedDelaySec=0", "RandomizedDelaySec=1h"), "enabled", "timer"),
            (TIMER_POLICY.replace("Persistent=true", "Persistent=false"), "enabled", "timer"),
            (TIMER_POLICY, "disabled", "timer-enabled"),
        ):
            with self.subTest(timer=timer, enabled=enabled):
                debian = self.controls.system_maintenance_molecule_baseline_debian_updater_errors(
                    DEBIAN_APT_POLICY,
                    timer,
                    enabled,
                )
                rocky = self.controls.system_maintenance_molecule_baseline_rocky_updater_errors(
                    ROCKY_DNF_POLICY,
                    timer,
                    enabled,
                )
                self.assertIn(expected, debian)
                self.assertIn(expected, rocky)


if __name__ == "__main__":
    unittest.main()
