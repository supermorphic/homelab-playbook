"""Behavioral contracts for the host reverse proxy role."""

from __future__ import annotations

import configparser
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ROLE_ROOT = REPOSITORY_ROOT / "roles/reverse_proxy"


def load_tasks(name: str) -> list[dict[str, object]]:
    with (ROLE_ROOT / "tasks" / name).open(encoding="utf-8") as source:
        return yaml.safe_load(source)


def named_task(tasks: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(task for task in tasks if task["name"] == name)


class CaddyfileRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.environment = Environment(
            loader=FileSystemLoader(ROLE_ROOT / "templates"),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
        )
        plugin_path = ROLE_ROOT / "filter_plugins" / "proxy.py"
        spec = importlib.util.spec_from_file_location(
            "reverse_proxy_template_filter", plugin_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.environment.filters.update(module.FilterModule().filters())

    def render(self, config: dict[str, object]) -> str:
        return self.environment.get_template("Caddyfile.j2").render(
            reverse_proxy_config=config
        )

    def test_empty_route_set_has_only_private_admin_api(self) -> None:
        rendered = self.render(
            {"bind_addresses": [], "client_sources": [], "routes": []}
        )

        self.assertEqual(
            """\
{
\tadmin unix//run/caddy/admin.sock
\tauto_https off
\tservers {
\t\tprotocols h1 h2
\t}
}
""",
            rendered,
        )
        self.assertNotIn(":443", rendered)

    def test_routes_bind_only_declared_addresses_and_loopback_backends(self) -> None:
        rendered = self.render(
            {
                "bind_addresses": ["10.23.4.5", "fd00:23::5"],
                "client_sources": ["10.23.4.0/24"],
                "routes": [
                    {
                        "hostname": "notes.example.test",
                        "backend_port": 9080,
                        "certificate_name": "notes",
                    },
                    {
                        "hostname": "status.example.test",
                        "backend_port": 9081,
                        "certificate_name": "status-2026",
                    },
                ],
            }
        )

        self.assertEqual(
            """\
{
\tadmin unix//run/caddy/admin.sock
\tauto_https off
\tservers {
\t\tprotocols h1 h2
\t}
}

https://notes.example.test:443 {
\tbind 10.23.4.5 fd00:23::5
\ttls /etc/caddy/tls/notes/current/fullchain.pem /etc/caddy/tls/notes/current/privkey.pem
\treverse_proxy 127.0.0.1:9080
}

https://status.example.test:443 {
\tbind 10.23.4.5 fd00:23::5
\ttls /etc/caddy/tls/status-2026/current/fullchain.pem /etc/caddy/tls/status-2026/current/privkey.pem
\treverse_proxy 127.0.0.1:9081
}
""",
            rendered,
        )
        self.assertNotIn("persist_config", rendered)
        self.assertNotIn("stream_close_delay", rendered)
        self.assertNotIn(":80", rendered)
        self.assertNotIn("h3", rendered)


class PackageInstallationTests(unittest.TestCase):
    def test_runtime_command_dependencies_are_distribution_packages(self) -> None:
        tasks = load_tasks("install.yml")
        debian = named_task(
            tasks, "Install Debian reverse proxy runtime dependencies"
        )

        self.assertEqual(
            {
                "name": [
                    "ca-certificates",
                    "iproute2",
                    "openssl",
                    "passwd",
                    "util-linux",
                ],
                "state": "present",
            },
            debian["ansible.builtin.apt"],
        )
        self.assertEqual("ansible_facts['distribution'] == 'Debian'", debian["when"])

    def test_filesystem_safety_runs_before_package_install(self) -> None:
        tasks = load_tasks("install.yml")
        self.assertEqual(
            "filesystem-preflight.yml",
            tasks[0]["ansible.builtin.import_tasks"],
        )
        preflight = load_tasks("filesystem-preflight.yml")
        parents = named_task(
            preflight, "Inspect reverse proxy parent directories"
        )["loop"]
        files = named_task(preflight, "Inspect managed reverse proxy files")["loop"]
        self.assertIn("/usr/local", parents)
        self.assertIn("/etc/tmpfiles.d", parents)
        self.assertIn("/run/lock", parents)
        self.assertIn("/run/lock/homelab-reverse-proxy.lock", files)

    def test_debian_install_suppresses_package_default_start(self) -> None:
        task = named_task(
            load_tasks("install.yml"),
            "Install Debian distribution Caddy without starting package defaults",
        )

        self.assertEqual(
            {"name": "caddy", "state": "present", "policy_rc_d": 101},
            task["ansible.builtin.apt"],
        )
        self.assertEqual("ansible_facts['distribution'] == 'Debian'", task["when"])


    def test_reprovision_never_stops_or_restarts_healthy_caddy(self) -> None:
        for path in (ROLE_ROOT / "tasks").glob("*.yml"):
            for task in load_tasks(path.name):
                service = task.get("ansible.builtin.systemd_service")
                if not service:
                    continue
                with self.subTest(path=path.name, task=task["name"]):
                    self.assertNotIn(service.get("state"), {"stopped", "restarted"})

    def test_account_validation_rejects_shared_primary_group(self) -> None:
        tasks = load_tasks("install.yml")
        observation = named_task(tasks, "Read all effective host accounts")
        validation = named_task(tasks, "Verify compatible package-created Caddy identity")

        self.assertEqual(
            {"argv": ["/usr/bin/getent", "passwd"]},
            observation["ansible.builtin.command"],
        )
        conditions = "\n".join(validation["ansible.builtin.assert"]["that"])
        self.assertIn("reverse_proxy_all_passwd.stdout_lines", conditions)
        self.assertIn("| select(", conditions)
        self.assertIn("'match'", conditions)
        self.assertIn("| length == 1", conditions)


class SystemdRestrictionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        environment = Environment(
            loader=FileSystemLoader(ROLE_ROOT / "templates"),
            undefined=StrictUndefined,
        )
        cls.rendered = environment.get_template("caddy.service.conf.j2").render()
        cls.unit = configparser.ConfigParser(strict=False)
        cls.unit.read_file(io.StringIO(cls.rendered))

    def test_startup_waits_for_network_and_retries_with_a_finite_budget(self) -> None:
        unit = dict(self.unit.items("Unit")) if self.unit.has_section("Unit") else {}
        service = self.unit["Service"]
        self.assertIn("network-online.target", unit.get("wants", "").split())
        self.assertIn("network-online.target", unit.get("after", "").split())
        self.assertEqual("on-failure", service["Restart"])
        self.assertEqual("10s", service["RestartSec"])
        self.assertEqual("300s", unit["startlimitintervalsec"])
        self.assertEqual("12", unit["startlimitburst"])

    def test_unit_runs_caddy_with_minimum_required_capability(self) -> None:
        service = self.unit["Service"]

        self.assertEqual("caddy", service["User"])
        self.assertEqual("caddy", service["Group"])
        self.assertEqual("CAP_NET_BIND_SERVICE", service["CapabilityBoundingSet"])
        self.assertEqual("CAP_NET_BIND_SERVICE", service["AmbientCapabilities"])
        self.assertEqual("true", service["NoNewPrivileges"])
        self.assertEqual("0077", service["UMask"])
        self.assertEqual("", service["SupplementaryGroups"])
        self.assertLess(
            self.rendered.index("AmbientCapabilities=\n"),
            self.rendered.index("AmbientCapabilities=CAP_NET_BIND_SERVICE"),
        )
        self.assertLess(
            self.rendered.index("CapabilityBoundingSet=\n"),
            self.rendered.index("CapabilityBoundingSet=CAP_NET_BIND_SERVICE"),
        )

    def test_unit_has_root_recovery_and_reload_boundaries(self) -> None:
        service = self.unit["Service"]

        self.assertEqual(
            "+/usr/local/libexec/homelab-reverse-proxy recover",
            service["ExecStartPre"],
        )
        self.assertEqual(
            "+/usr/local/libexec/homelab-reverse-proxy reload",
            service["ExecReload"],
        )
        self.assertEqual(
            "/usr/bin/caddy run --config /etc/caddy/Caddyfile",
            service["ExecStart"],
        )

    def test_unit_removes_debian_package_group_before_recovery(self) -> None:
        group_reset = "ExecStartPre=+/usr/sbin/usermod --groups= caddy"
        recovery = (
            "ExecStartPre=+/usr/local/libexec/homelab-reverse-proxy recover"
        )

        self.assertIn(group_reset, self.rendered)
        self.assertLess(self.rendered.index(group_reset), self.rendered.index(recovery))

    def test_unit_limits_write_and_namespace_access(self) -> None:
        service = self.unit["Service"]

        self.assertEqual("strict", service["ProtectSystem"])
        self.assertEqual("true", service["ProtectHome"])
        self.assertEqual("true", service["PrivateTmp"])
        self.assertEqual("caddy", service["RuntimeDirectory"])
        self.assertEqual("0700", service["RuntimeDirectoryMode"])
        self.assertEqual("/var/lib/caddy /run/caddy", service["ReadWritePaths"])
        self.assertLess(
            self.rendered.index("ReadWritePaths=\n"),
            self.rendered.index("ReadWritePaths=/var/lib/caddy /run/caddy"),
        )


class ProvisioningFlowTests(unittest.TestCase):
    def test_all_managed_paths_are_lstat_checked_before_first_write(self) -> None:
        tasks = load_tasks("configure.yml")
        names = [task["name"] for task in tasks]
        first_write = names.index("Establish administrator-owned reverse proxy directories")
        checks = {
            task["name"]: task
            for task in tasks[:first_write]
            if "ansible.builtin.stat" in task
        }

        parents = checks["Inspect reverse proxy parent directories"]["loop"]
        directories = checks["Inspect managed reverse proxy directories"]["loop"]
        files = checks["Inspect managed reverse proxy files"]["loop"]
        self.assertEqual(
            [
                "/etc",
                "/etc/systemd",
                "/etc/systemd/system",
                "/etc/tmpfiles.d",
                "/usr",
                "/usr/local",
                "/usr/local/lib",
                "/var",
                "/var/lib",
                "/run",
                "/run/lock",
            ],
            parents,
        )
        self.assertIn("/run/caddy", directories)
        self.assertIn("/run/lock/homelab-reverse-proxy.lock", files)
        self.assertIn("/etc/caddy/Caddyfile.candidate", files)
        for check in checks.values():
            self.assertIs(check["ansible.builtin.stat"]["follow"], False)

    def test_initial_boot_is_admin_only_despite_nonempty_desired_fact(self) -> None:
        configure = load_tasks("configure.yml")
        task = named_task(configure, "Establish initial admin-only boot configuration")
        self.assertEqual("Caddyfile.admin.j2", task["ansible.builtin.template"]["src"])

        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / ".tmp") as directory:
            root = Path(directory)
            destination = root / "Caddyfile"
            playbook = root / "render.yml"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Render initial configuration with conflicting fact",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "tasks": [
                                {
                                    "name": "Set a nonempty desired configuration fact",
                                    "ansible.builtin.set_fact": {
                                        "reverse_proxy_config": {
                                            "bind_addresses": ["10.23.4.5"],
                                            "client_sources": ["10.23.4.0/24"],
                                            "routes": [
                                                {
                                                    "hostname": "notes.example.test",
                                                    "backend_port": 9080,
                                                    "certificate_name": "notes",
                                                }
                                            ],
                                        }
                                    },
                                },
                                {
                                    "name": "Render the dedicated initial configuration",
                                    "ansible.builtin.template": {
                                        "src": str(
                                            ROLE_ROOT
                                            / "templates/Caddyfile.admin.j2"
                                        ),
                                        "dest": str(destination),
                                        "mode": "0600",
                                    },
                                },
                            ],
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
                """\
{
\tadmin unix//run/caddy/admin.sock
\tauto_https off
\tservers {
\t\tprotocols h1 h2
\t}
}
""",
                destination.read_text(encoding="utf-8"),
            )

    def test_identity_is_made_private_only_after_managed_marker_exists(self) -> None:
        tasks = load_tasks("configure.yml")
        names = [task["name"] for task in tasks]

        self.assertLess(
            names.index("Mark reverse proxy as repository managed"),
            names.index("Remove package-added supplementary groups from Caddy"),
        )
        identity = named_task(
            tasks, "Remove package-added supplementary groups from Caddy"
        )
        self.assertEqual(
            {"name": "caddy", "groups": "", "append": False},
            identity["ansible.builtin.user"],
        )

    def test_installed_package_requires_exact_managed_marker(self) -> None:
        task = named_task(
            load_tasks("install.yml"), "Reject unmanaged reverse proxy installation"
        )
        expression = task["ansible.builtin.assert"]["that"][1]

        self.assertIn("stat.isreg", expression)
        self.assertIn("stat.uid == 0", expression)
        self.assertIn("stat.gid == 0", expression)
        self.assertIn("stat.mode == '0600'", expression)
        self.assertIn("stat.nlink == 1", expression)
        self.assertIn("stat.checksum", expression)
        self.assertIn("== ('managed by homelab-playbook", expression)

        initial = named_task(
            load_tasks("configure.yml"),
            "Establish initial admin-only boot configuration",
        )
        self.assertEqual(
            "'caddy' not in ansible_facts['packages']",
            initial["when"],
        )

    def test_initialization_uses_preinstall_package_state(self) -> None:
        condition = named_task(
            load_tasks("configure.yml"),
            "Establish initial admin-only boot configuration",
        )["when"]
        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / ".tmp") as directory:
            playbook = Path(directory) / "initialization.yml"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Exercise reverse proxy initialization guard",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "tasks": [
                                {
                                    "name": "Initialize when package was absent",
                                    "ansible.builtin.assert": {"that": [condition]},
                                    "vars": {
                                        "ansible_facts": {"packages": {}},
                                        "reverse_proxy_takeover_boundaries": {
                                            "results": [{"stat": {"exists": True}}]
                                        },
                                    },
                                },
                                {
                                    "name": "Preserve managed package configuration",
                                    "ansible.builtin.assert": {
                                        "that": [f"not ({condition})"]
                                    },
                                    "vars": {
                                        "ansible_facts": {
                                            "packages": {
                                                "caddy": [{"version": "fixture"}]
                                            }
                                        },
                                        "reverse_proxy_takeover_boundaries": {
                                            "results": [
                                                {
                                                    "stat": {
                                                        "exists": True,
                                                        "isreg": True,
                                                        "mode": "0600",
                                                    }
                                                }
                                            ]
                                        },
                                    },
                                },
                            ],
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

    def test_managed_marker_is_rediscovered_on_second_role_entry(self) -> None:
        ownership = named_task(
            load_tasks("install.yml"), "Reject unmanaged reverse proxy installation"
        )["ansible.builtin.assert"]["that"][1]
        ownership = ownership.replace("stat.uid == 0", f"stat.uid == {os.getuid()}")
        ownership = ownership.replace("stat.gid == 0", f"stat.gid == {os.getgid()}")
        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / ".tmp") as directory:
            root = Path(directory)
            marker = root / "managed"
            paths = [
                str(marker),
                str(root / "one"),
                str(root / "two"),
                str(root / "three"),
                str(root / "four"),
            ]
            observe = {
                "name": "Inspect marker for one role entry",
                "ansible.builtin.stat": {
                    "path": "{{ item }}",
                    "follow": False,
                    "checksum_algorithm": "sha256",
                },
                "loop": paths,
                "register": "reverse_proxy_takeover_boundaries",
            }
            role = root / "roles/ownership_probe/tasks"
            role.mkdir(parents=True)
            (role / "main.yml").write_text(
                yaml.safe_dump(
                    [
                        observe,
                        {
                            "name": "Accept only current managed state",
                            "ansible.builtin.assert": {"that": [ownership]},
                        },
                        {
                            "name": "Create the role marker",
                            "ansible.builtin.copy": {
                                "content": "managed by homelab-playbook\n",
                                "dest": str(marker),
                                "mode": "0600",
                            },
                        },
                    ],
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            includes = []
            for packages in ({}, {"caddy": [{"version": "fixture"}]}):
                includes.append(
                    {
                        "name": "Enter ownership probe role",
                        "ansible.builtin.include_role": {
                            "name": "ownership_probe",
                            "public": False,
                            "allow_duplicates": True,
                        },
                        "vars": {"ansible_facts": {"packages": packages}},
                    }
                )
            playbook = root / "double-entry.yml"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Exercise repeated role ownership discovery",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "tasks": includes,
                        }
                    ],
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment["ANSIBLE_ROLES_PATH"] = str(root / "roles")
            result = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(playbook)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_changed_unit_reloads_manager_then_restarts_service(self) -> None:
        configure = load_tasks("configure.yml")
        unit = named_task(configure, "Install hardened reverse proxy systemd override")
        handlers = yaml.safe_load(
            (ROLE_ROOT / "handlers/main.yml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            [
                "Reload systemd manager for reverse proxy",
                "Restart reverse proxy for systemd policy change",
            ],
            unit["notify"],
        )
        restart = named_task(
            handlers, "Restart reverse proxy for systemd policy change"
        )
        self.assertEqual(
            {"name": "caddy.service", "state": "restarted"},
            restart["ansible.builtin.systemd_service"],
        )

    def test_firewall_is_verified_before_candidate_activation(self) -> None:
        tasks = load_tasks("main.yml")
        names = [task["name"] for task in tasks]

        self.assertLess(
            names.index("Reconcile shared host firewall policy"),
            names.index("Verify shared host firewall policy"),
        )
        self.assertLess(
            names.index("Verify shared host firewall policy"),
            names.index("Install candidate desired reverse proxy manifest"),
        )
        self.assertLess(
            names.index("Start committed reverse proxy configuration"),
            names.index("Activate candidate reverse proxy configuration"),
        )

    def test_candidate_activation_reports_real_helper_change(self) -> None:
        task = named_task(
            load_tasks("main.yml"),
            "Activate candidate reverse proxy configuration",
        )

        self.assertEqual(
            {"argv": ["/usr/local/libexec/homelab-reverse-proxy", "apply-desired"]},
            task["ansible.builtin.command"],
        )
        self.assertEqual("reverse_proxy_activation.stdout | trim == 'changed'", task["changed_when"])
        self.assertTrue(task["no_log"])

    def test_firewall_roles_receive_complete_explicit_baseline_policy(self) -> None:
        tasks = load_tasks("main.yml")
        reconcile = named_task(tasks, "Reconcile shared host firewall policy")
        verify = named_task(tasks, "Verify shared host firewall policy")

        self.assertEqual(
            {"name": "security_baseline", "tasks_from": "firewall.yml", "public": False},
            reconcile["ansible.builtin.include_role"],
        )
        self.assertEqual(
            {"name": "os_baseline_verify", "tasks_from": "firewall.yml", "public": False},
            verify["ansible.builtin.include_role"],
        )
        self.assertEqual(
            "{{ reverse_proxy_expected_management_sources }}",
            verify["vars"]["os_baseline_verify_expected_management_sources"],
        )
        self.assertEqual(
            "{{ reverse_proxy_expected_firewall_services }}",
            verify["vars"]["os_baseline_verify_expected_firewall_services"],
        )


class ObservationalVerificationTests(unittest.TestCase):
    def test_verify_does_not_write_or_repair_host_state(self) -> None:
        mutation_modules = {
            "ansible.builtin.apt",
            "ansible.builtin.copy",
            "ansible.builtin.file",
            "ansible.builtin.package",
            "ansible.builtin.systemd_service",
            "ansible.builtin.template",
        }

        for task in load_tasks("verify.yml"):
            with self.subTest(task=task["name"]):
                self.assertTrue(mutation_modules.isdisjoint(task))
                if "ansible.builtin.command" in task:
                    self.assertIs(task.get("changed_when"), False)

    def test_verify_uses_helper_and_checks_effective_unit_properties(self) -> None:
        tasks = load_tasks("verify.yml")
        helper = named_task(tasks, "Verify active reverse proxy configuration")
        unit = named_task(tasks, "Read effective reverse proxy systemd properties")

        self.assertEqual(
            {"argv": ["/usr/local/libexec/homelab-reverse-proxy", "verify"]},
            helper["ansible.builtin.command"],
        )
        self.assertEqual(
            [
                "/usr/bin/systemctl",
                "show",
                "caddy.service",
                "--property=User",
                "--property=Group",
                "--property=SupplementaryGroups",
                "--property=AmbientCapabilities",
                "--property=CapabilityBoundingSet",
                "--property=NoNewPrivileges",
                "--property=PrivateTmp",
                "--property=ProtectHome",
                "--property=ProtectSystem",
                "--property=UMask",
                "--property=ReadWritePaths",
                "--property=ExecStartPre",
                "--property=ExecReload",
                "--property=Wants",
                "--property=After",
                "--property=Restart",
                "--property=RestartUSec",
                "--property=StartLimitIntervalUSec",
                "--property=StartLimitBurst",
            ],
            unit["ansible.builtin.command"]["argv"],
        )

    def test_verify_checks_exact_unit_paths_and_running_process_identity(self) -> None:
        tasks = load_tasks("verify.yml")
        unit_assertion = named_task(
            tasks, "Verify effective reverse proxy systemd restrictions"
        )
        pid = named_task(tasks, "Read reverse proxy service process identity")
        credentials = named_task(tasks, "Read running Caddy process credentials")
        process_assertion = named_task(tasks, "Verify running Caddy process identity")

        self.assertIn(
            "'ReadWritePaths=/var/lib/caddy /run/caddy' in reverse_proxy_verify_unit.stdout_lines",
            unit_assertion["ansible.builtin.assert"]["that"],
        )
        self.assertIn(
            "'SupplementaryGroups=' in reverse_proxy_verify_unit.stdout_lines",
            unit_assertion["ansible.builtin.assert"]["that"],
        )
        self.assertEqual(
            {
                "argv": [
                    "/usr/bin/systemctl",
                    "show",
                    "--property=MainPID",
                    "--value",
                    "caddy.service",
                ]
            },
            pid["ansible.builtin.command"],
        )
        self.assertEqual(
            "/proc/{{ reverse_proxy_verify_pid.stdout | trim }}/status",
            credentials["ansible.builtin.slurp"]["src"],
        )
        process_conditions = "\n".join(
            process_assertion["ansible.builtin.assert"]["that"]
        )
        self.assertIn("^Uid:", process_conditions)
        self.assertIn("^Gid:", process_conditions)
        self.assertIn("^Groups:", process_conditions)

    def test_verify_compares_committed_manifest_to_declared_routes_and_ingress(self) -> None:
        from ansible.plugins.filter.core import FilterModule

        task = named_task(
            load_tasks("verify.yml"),
            "Verify committed desired configuration matches declared routes",
        )
        conditions = task["ansible.builtin.assert"]["that"]
        environment = Environment(undefined=StrictUndefined)
        environment.filters.update(FilterModule().filters())
        spec = importlib.util.spec_from_file_location("manifest_filter", ROLE_ROOT / "filter_plugins/proxy.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        environment.filters.update(module.FilterModule().filters())
        config = {"bind_addresses": ["10.20.30.40"], "client_sources": ["10.20.0.0/16"],
                  "routes": [], "deferred_certificates": ["infra"]}
        manifest = json.dumps(config) + "\n"
        envelope = {"version": 1, "manifest": manifest, "ingress": {
            "version": 1, "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
            "listen_addresses": config["bind_addresses"],
            "https_client_networks": config["client_sources"],
        }}

        def accepted(value):
            return all(environment.compile_expression(condition)(
                reverse_proxy_verify_envelope=value, reverse_proxy_config=config
            ) for condition in conditions)

        self.assertTrue(accepted(envelope))
        for field, value in (("listen_addresses", ["10.20.30.41"]),
                             ("https_client_networks", ["10.0.0.0/8"]),
                             ("manifest_sha256", "0" * 64)):
            changed = copy.deepcopy(envelope)
            changed["ingress"][field] = value
            self.assertFalse(accepted(changed), field)
        changed = copy.deepcopy(envelope)
        changed_config = dict(config, deferred_certificates=[])
        changed["manifest"] = json.dumps(changed_config)
        changed["ingress"]["manifest_sha256"] = hashlib.sha256(changed["manifest"].encode()).hexdigest()
        self.assertFalse(accepted(changed))


if __name__ == "__main__":
    unittest.main()
