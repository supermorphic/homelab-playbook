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


def load_chrony_discovery():
    path = REPOSITORY_ROOT / "roles/os_baseline_verify/files/discover_chrony_sources.py"
    spec = importlib.util.spec_from_file_location("os_baseline_verify_chrony", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load chrony resolver: {path}")
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
        discovery = load_chrony_discovery()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for platform, primary, inputs in (
                (
                    "debian",
                    "/etc/chrony/chrony.conf",
                    [
                        "pool 2.debian.pool.ntp.org iburst",
                        "sourcedir /run/chrony-dhcp",
                        "sourcedir /etc/chrony/sources.d",
                        "confdir /etc/chrony/conf.d",
                    ],
                ),
                (
                    "rocky",
                    "/etc/chrony.conf",
                    [
                        "pool 2.rocky.pool.ntp.org iburst",
                        "sourcedir /run/chrony-dhcp",
                    ],
                ),
            ):
                with self.subTest(platform=platform):
                    family = "Debian" if platform == "debian" else "RedHat"
                    platform_root = root / platform
                    config = platform_root / primary.lstrip("/")
                    config.parent.mkdir(parents=True)
                    if platform == "debian":
                        (platform_root / "etc/chrony/conf.d").mkdir(parents=True)
                        (platform_root / "run/chrony-dhcp").mkdir(parents=True)
                        (platform_root / "etc/chrony/sources.d").mkdir(parents=True)
                    else:
                        (platform_root / "run/chrony-dhcp").mkdir(parents=True)
                    (platform_root / "run/chrony-dhcp/dhcp.sources").write_text(
                        "PEER dynamic.example key 7\n", encoding="utf-8",
                    )
                    config.write_text("\n".join(inputs) + "\n", encoding="utf-8")
                    vendor_state = discovery.discover(primary, platform, platform_root)
                    vendor_pool = f"2.{platform}.pool.ntp.org"
                    expected_input_paths = (
                        [("sourcedir", ["/run/chrony-dhcp"]),
                         ("sourcedir", ["/etc/chrony/sources.d"]),
                         ("confdir", ["/etc/chrony/conf.d"])]
                        if platform == "debian"
                        else [("sourcedir", ["/run/chrony-dhcp"])]
                    )
                    self.assertEqual(
                        expected_input_paths,
                        [
                            (entry["kind"], entry["configured_paths"])
                            for entry in vendor_state["active_inputs"]
                        ],
                    )
                    self.assertIn(
                        "/run/chrony-dhcp/dhcp.sources",
                        next(
                            entry["resolved_paths"]
                            for entry in vendor_state["active_inputs"]
                            if entry["configured_paths"] == ["/run/chrony-dhcp"]
                        ),
                    )
                    self.assertEqual(
                        [vendor_pool, "dynamic.example"],
                        self.controls.os_baseline_verify_chrony_effective_policy(
                            vendor_state, [], family,
                        ),
                    )

                    config.write_text(
                        "# BEGIN ANSIBLE MANAGED TRUSTED CHRONY SOURCES\n"
                        "server approved.example iburst\n"
                        "# END ANSIBLE MANAGED TRUSTED CHRONY SOURCES\n"
                        + "".join(f"# homelab-disabled: {line}\n" for line in inputs),
                        encoding="utf-8",
                    )
                    approved_state = discovery.discover(primary, platform, platform_root)
                    self.assertEqual(
                        ["approved.example"],
                        self.controls.os_baseline_verify_chrony_effective_policy(
                            approved_state, ["approved.example"], family,
                        ),
                    )
                    config.write_text("\n".join(inputs) + "\n", encoding="utf-8")
                    self.assertEqual(
                        [vendor_pool, "dynamic.example"],
                        self.controls.os_baseline_verify_chrony_effective_policy(
                            discovery.discover(primary, platform, platform_root), [], family,
                        ),
                    )

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
            result = subprocess.run([str(script), "debian", str(config)], check=True, capture_output=True, text=True)
            self.assertEqual(
                ["2.debian.pool.ntp.org", "dhcp.vendor.example"],
                [entry["source"] for entry in json.loads(result.stdout)["sources"]],
            )

    def test_chrony_discovery_reads_case_insensitive_peer_and_multi_directory_inputs(self) -> None:
        """A source hidden in a valid Chrony input must not evade verification."""
        script = REPOSITORY_ROOT / "roles/os_baseline_verify/files/discover_chrony_sources.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            (first / "duplicate.sources").write_text(
                "SERVER first.example iburst\n", encoding="utf-8",
            )
            (second / "duplicate.sources").write_text(
                "SERVER ignored.example iburst\n", encoding="utf-8",
            )
            (second / "peer.sources").write_text(
                "PEER peer.example key 7\n", encoding="utf-8",
            )
            (second / "ignored.conf").write_text(
                "SERVER suffix-ignored.example iburst\n", encoding="utf-8",
            )
            config = root / "chrony.conf"
            config.write_text(
                f"POOL 2.debian.pool.ntp.org iburst\nSOURCEDIR {first} {second}\n",
                encoding="utf-8",
            )
            result = subprocess.run([str(script), "debian", str(config)], check=True, capture_output=True, text=True)
            self.assertEqual(
                ["2.debian.pool.ntp.org", "first.example", "peer.example"],
                [entry["source"] for entry in json.loads(result.stdout)["sources"]],
            )

    def test_chrony_discovery_rejects_cycles_and_excessive_include_depth(self) -> None:
        """Only active recursion is a cycle, and nesting is intentionally bounded."""
        script = REPOSITORY_ROOT / "roles/os_baseline_verify/files/discover_chrony_sources.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cycle = root / "cycle.conf"
            cycle.write_text(f"include {cycle}\n", encoding="utf-8")
            cycle_result = subprocess.run([str(script), "debian", str(cycle)], check=False, capture_output=True, text=True)
            self.assertNotEqual(0, cycle_result.returncode)
            paths = [root / f"level-{index}.conf" for index in range(12)]
            for index, path in enumerate(paths):
                path.write_text(
                    f"include {paths[index + 1]}\n" if index < len(paths) - 1 else "server terminal.example iburst\n",
                    encoding="utf-8",
                )
            depth_result = subprocess.run([str(script), "debian", str(paths[0])], check=False, capture_output=True, text=True)
            self.assertNotEqual(0, depth_result.returncode)

    def test_chrony_discovery_requires_an_explicit_platform(self) -> None:
        """The task-provided Debian/Rocky platform is part of the policy input."""
        script = REPOSITORY_ROOT / "roles/os_baseline_verify/files/discover_chrony_sources.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "chrony.conf"
            config.write_text("pool 2.debian.pool.ntp.org iburst\n", encoding="utf-8")
            result = subprocess.run([str(script), str(config)], check=False, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)

    def test_chrony_discovery_rejects_missing_include_and_allows_independent_repeats(self) -> None:
        """Chrony include requires a match, but a non-recursive repeat is valid."""
        script = REPOSITORY_ROOT / "roles/os_baseline_verify/files/discover_chrony_sources.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            included = root / "included.conf"
            included.write_text("server repeated.example iburst\n", encoding="utf-8")
            config = root / "chrony.conf"
            config.write_text(f"include {included}\ninclude {included}\n", encoding="utf-8")
            repeated = subprocess.run([str(script), "debian", str(config)], check=False, capture_output=True, text=True)
            self.assertEqual(0, repeated.returncode, repeated.stderr)
            self.assertEqual(
                ["repeated.example", "repeated.example"],
                [entry["source"] for entry in json.loads(repeated.stdout)["sources"]],
            )
            config.write_text(f"include {root / 'missing.conf'}\n", encoding="utf-8")
            missing = subprocess.run([str(script), "debian", str(config)], check=False, capture_output=True, text=True)
            self.assertNotEqual(0, missing.returncode)

    def test_chrony_effective_vendor_policy_rejects_unapproved_input_path(self) -> None:
        """A distribution vendor pool cannot make an arbitrary include acceptable."""
        state = {
            "platform": "debian",
            "sources": [
                {
                    "source": "2.debian.pool.ntp.org", "directive": "pool",
                    "configured_path": "/etc/chrony/chrony.conf",
                    "resolved_path": "/etc/chrony/chrony.conf",
                },
                {
                    "source": "unexpected.example", "directive": "server",
                    "configured_path": "/opt/chrony/unexpected.conf",
                    "resolved_path": "/opt/chrony/unexpected.conf",
                },
            ],
            "disabled_sources": [],
            "active_inputs": [
                {
                    "kind": "confdir", "configured_paths": ["/etc/chrony/conf.d"],
                    "resolved_paths": [],
                },
                {
                    "kind": "sourcedir",
                    "configured_paths": ["/run/chrony-dhcp", "/etc/chrony/sources.d"],
                    "resolved_paths": [],
                },
                {
                    "kind": "include", "configured_paths": ["/opt/chrony/*.conf"],
                    "resolved_paths": ["/opt/chrony/unexpected.conf"],
                },
            ],
            "disabled_inputs": [],
            "markers": {"begin": 0, "end": 0},
        }
        with self.assertRaises(ValueError):
            self.controls.os_baseline_verify_chrony_effective_policy(state, [], "Debian")

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
