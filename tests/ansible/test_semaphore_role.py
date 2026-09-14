"""Behavioral contracts for rootless Semaphore deployment."""

from __future__ import annotations

import configparser
import base64
import copy
import getpass
import grp
import os
import io
import subprocess
import tempfile
from pathlib import Path
import unittest

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles/semaphore"


class SemaphoreTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.environment = Environment(
            loader=FileSystemLoader(ROLE / "templates"),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        cls.values = {
            "semaphore_image": "docker.io/semaphoreui/semaphore:v2.19.12@sha256:" + "a" * 64,
            "semaphore_postgres_image": "docker.io/library/postgres:17.6-alpine@sha256:" + "b" * 64,
            "semaphore_rclone_image": "docker.io/rclone/rclone:1.71.1@sha256:" + "c" * 64,
            "semaphore_backend_port": 13000,
            "semaphore_hostname": "semaphore.example.test",
            "semaphore_account_name": "svc-semaphore",
            "semaphore_foundation_account": {"uid": 2100, "gid": 2100},
            "semaphore_state_root": "/var/lib/svc-semaphore/semaphore",
            "semaphore_quadlet_root": "/etc/containers/systemd/users/2100",
            "semaphore_user_unit_root": "/var/lib/svc-semaphore/.config/systemd/user",
            "semaphore_helper_root": "/usr/local/libexec/semaphore",
            "semaphore_database_name": "semaphore",
            "semaphore_database_user": "semaphore",
            "semaphore_local_retention_days": 7,
            "semaphore_remote_retention_days": 90,
            "semaphore_backup_timeout": "2h",
            "semaphore_transfer_timeout": "2h",
            "semaphore_health_timeout_seconds": 60,
            "semaphore_rclone_remote": "nas:semaphore",
        }

    def render(self, name: str, **values: object) -> str:
        return self.environment.get_template(name).render(self.values | values)

    @staticmethod
    def unit(text: str) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read_file(io.StringIO(text))
        return parser

    def test_application_is_loopback_only_and_database_is_private(self) -> None:
        application = self.render("semaphore.container.j2")
        database = self.render("postgres.container.j2")

        self.assertIn("PublishPort=127.0.0.1:13000:3000", application)
        self.assertNotIn("PublishPort", database)
        self.assertIn("UserNS=keep-id:uid=1001,gid=0", application)
        self.assertIn("UserNS=keep-id:uid=999,gid=999", database)
        self.assertIn("Network=semaphore.network", application)
        self.assertIn("Network=semaphore.network", database)

    def test_service_template_map_resolves_every_source(self) -> None:
        services = yaml.safe_load((ROLE / "tasks/services.yml").read_text())
        self.assertEqual("definitions.yml", services[0]["ansible.builtin.import_tasks"])
        tasks = yaml.safe_load((ROLE / "tasks/definitions.yml").read_text())
        install = next(
            task
            for task in tasks
            if task["name"] == "Install Semaphore network and application Quadlets"
        )
        for item in install["loop"]:
            with self.subTest(source=item["src"]):
                self.assertTrue((ROLE / "templates" / f"{item['src']}.j2").is_file())

    def test_mounts_limit_writable_state_and_credential_consumers(self) -> None:
        application = self.render("semaphore.container.j2")
        database = self.render("postgres.container.j2")
        backup = self.render("backup.container.j2")
        transfer = self.render("transfer.container.j2")

        self.assertIn("EnvironmentFile=/var/lib/svc-semaphore/semaphore/credentials/semaphore.env", application)
        self.assertNotIn("/run/secrets/semaphore.env", application)
        self.assertNotIn("postgres.env", application)
        self.assertNotIn("rclone.conf", application)
        self.assertIn("EnvironmentFile=/var/lib/svc-semaphore/semaphore/credentials/postgres.env", database)
        self.assertNotIn("rclone.conf", database)
        self.assertIn("EnvironmentFile=/var/lib/svc-semaphore/semaphore/credentials/postgres.env", backup)
        self.assertNotIn("/run/secrets/postgres.env", backup)
        self.assertNotIn("semaphore.env", backup)
        self.assertIn("credentials/rclone.conf:/run/secrets/rclone.conf:ro", transfer)
        self.assertNotIn("postgres.env", transfer)
        self.assertNotIn("database:/", transfer)

    def test_executable_helpers_have_an_administrator_owned_mount_source(self) -> None:
        for template in (
            "semaphore.container.j2",
            "backup.container.j2",
            "transfer.container.j2",
        ):
            rendered = self.render(
                template,
                semaphore_controller_enabled=True,
                semaphore_controller_identity_enabled=True,
                ansible_facts={"os_family": "Debian"},
            )
            with self.subTest(template=template):
                self.assertNotIn(f"{self.values['semaphore_state_root']}/helpers", rendered)
                if "/opt/homelab/" in rendered:
                    self.assertIn(f"{self.values['semaphore_helper_root']}/", rendered)
                    helper_lines = [
                        line for line in rendered.splitlines()
                        if line.startswith(f"Volume={self.values['semaphore_helper_root']}/")
                    ]
                    self.assertTrue(helper_lines)
                    self.assertTrue(all(line.endswith(":ro") for line in helper_lines))

        application = self.render("semaphore.container.j2")
        for name in ("ssh_config", "known_hosts"):
            mount = next(line for line in application.splitlines() if f"/{name}:" in line)
            self.assertTrue(mount.endswith(":ro"), mount)

    def test_long_running_units_restart_and_wait_for_database(self) -> None:
        application = self.render("semaphore.container.j2")
        database = self.render("postgres.container.j2")

        self.assertEqual("on-failure", self.unit(application)["Service"]["Restart"])
        self.assertEqual("on-failure", self.unit(database)["Service"]["Restart"])
        self.assertIn("semaphore-postgres.service", application)
        self.assertIn("healthcheck run semaphore-postgres", application)
        self.assertEqual("90", self.unit(application)["Service"]["TimeoutStartSec"])

    def test_timers_use_exact_local_schedules_and_remain_repeatable(self) -> None:
        cases = {
            "backup.timer.j2": "*-*-* 03:00:00",
            "transfer.timer.j2": "*-*-* 00,04,08,12,16,20:15:00",
        }
        for template, schedule in cases.items():
            with self.subTest(template=template):
                rendered = self.render(template)
                unit = self.unit(rendered)
                self.assertEqual(schedule, unit["Timer"]["OnCalendar"])
                self.assertEqual("true", unit["Timer"]["Persistent"])
                self.assertNotIn("RandomizedDelaySec", unit["Timer"])
                self.assertNotIn("RemainAfterExit", rendered)

    def test_one_shots_have_finite_timeouts_and_independent_schedules(self) -> None:
        backup = self.render("backup.container.j2")
        transfer = self.render("transfer.container.j2")

        self.assertEqual("2h", self.unit(backup)["Service"]["TimeoutStartSec"])
        self.assertEqual("2h", self.unit(transfer)["Service"]["TimeoutStartSec"])
        self.assertEqual("no", self.unit(backup)["Service"]["RemainAfterExit"])
        self.assertEqual("no", self.unit(transfer)["Service"]["RemainAfterExit"])
        self.assertNotIn("backup.service", transfer)
        self.assertEqual("semaphore-transfer.service", self.unit(backup)["Unit"]["OnSuccess"])
        self.assertIn("LOCAL_RETENTION_DAYS=7", transfer)
        self.assertIn("REMOTE_RETENTION_DAYS=90", transfer)

    def test_runtime_environment_does_not_quote_secret_values(self) -> None:
        values = self.values | {
            "semaphore_database_password": "hash#$dollar=equals",
            "semaphore_admin_login": "operator",
            "semaphore_admin_password": "admin#$dollar=equals",
            "semaphore_admin_name": "Fixture Operator",
            "semaphore_admin_email": "operator@example.test",
            "semaphore_cookie_hash": "Y29va2llLWhhc2g=",
            "semaphore_cookie_encryption": "Y29va2llLWVuY3J5cHRpb24=",
            "semaphore_access_key_encryption": "fixture",
        }
        rendered = self.environment.get_template("semaphore.env.j2").render(values)
        environment = dict(
            line.split("=", 1) for line in rendered.splitlines() if line
        )

        self.assertEqual("hash#$dollar=equals", environment["SEMAPHORE_DB_PASS"])
        self.assertEqual("admin#$dollar=equals", environment["SEMAPHORE_ADMIN_PASSWORD"])
        self.assertEqual("/workspace", environment["SEMAPHORE_TMP_PATH"])


class SemaphorePlaybookTests(unittest.TestCase):
    PASSWORD_ALIASES = (
        "ansible_password",
        "ansible_ssh_pass",
        "ansible_ssh_password",
        "ansible_become_password",
        "ansible_become_pass",
    )

    def test_provision_and_verify_follow_guarded_serial_contract(self) -> None:
        for action in ("provision", "verify"):
            path = ROOT / "playbooks/semaphore" / f"{action}.yml"
            with self.subTest(action=action):
                play = yaml.safe_load(path.read_text())[0]
                self.assertEqual("semaphore", play["hosts"])
                self.assertEqual(1, play["serial"])
                self.assertIs(play["any_errors_fatal"], True)
                guard, connection, setup = play["pre_tasks"][:3]
                self.assertEqual(
                    [f"{name} is not defined" for name in self.PASSWORD_ALIASES],
                    guard["ansible.builtin.assert"]["that"],
                )
                self.assertIs(guard["no_log"], True)
                self.assertEqual(
                    {"name": "os_bootstrap", "tasks_from": "connection-preflight.yml"},
                    connection["ansible.builtin.import_role"],
                )
                self.assertIn("ansible.builtin.setup", setup)

    def test_verify_imports_only_observational_role_entrypoint(self) -> None:
        play = yaml.safe_load((ROOT / "playbooks/semaphore/verify.yml").read_text())[0]
        role = play["tasks"][0]["ansible.builtin.import_role"]
        self.assertEqual("semaphore", role["name"])
        self.assertEqual("verify.yml", role["tasks_from"])
        tasks = yaml.safe_load((ROLE / "tasks/verify.yml").read_text())
        self.assertEqual("preflight.yml", tasks[0]["ansible.builtin.import_tasks"])
        self.assertEqual("verify-definitions.yml", tasks[1]["ansible.builtin.import_tasks"])
        service_state = next(
            task for task in tasks if task["name"] == "Observe Semaphore user service state"
        )
        timer_state = next(
            task for task in tasks if task["name"] == "Observe Semaphore timer state"
        )
        self.assertEqual(
            ["semaphore-postgres.service", "semaphore.service"], service_state["loop"]
        )
        self.assertEqual(
            ["semaphore-backup.timer", "semaphore-transfer.timer"], timer_state["loop"]
        )

    def test_rootless_config_home_remains_account_owned(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/storage.yml").read_text())
        config_home = next(
            task
            for task in tasks
            if task["name"] == "Establish rootless Podman configuration directory"
        )
        managed_units = next(
            task
            for task in tasks
            if task["name"] == "Establish administrator-owned Semaphore user unit directories"
        )

        self.assertEqual("{{ semaphore_account_name }}", config_home["ansible.builtin.file"]["owner"])
        self.assertEqual("root", managed_units["ansible.builtin.file"]["owner"])
        self.assertNotIn('/var/lib/{{ semaphore_account_name }}/.config', managed_units["loop"])

    def test_application_restarts_when_an_individual_bind_source_is_replaced(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/configuration.yml").read_text())
        identity_helper = next(
            task
            for task in tasks
            if task["name"] == "Install controller identity command helper"
        )
        services = yaml.safe_load((ROLE / "tasks/services.yml").read_text())
        application_start = next(
            task
            for task in services
            if task["name"] == "Start or restart Semaphore after database readiness"
        )

        self.assertEqual("semaphore_identity_helper", identity_helper["register"])
        predicate = application_start["when"]
        self.assertIn("semaphore_identity_helper.changed", predicate)
        self.assertIn("semaphore_repository_job_wrapper.changed", predicate)
        self.assertIn("semaphore_ssh_configuration.changed", predicate)
        self.assertIn("semaphore_known_hosts.changed", predicate)

    def test_controller_override_staging_hashes_and_replaces_the_exact_tree(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/toolchain.yml").read_text())
        discovery = next(
            task
            for task in tasks
            if task["name"] == "Discover every controller Galaxy override input"
        )
        link_rejection = next(
            task
            for task in tasks
            if task["name"] == "Reject linked controller Galaxy override inputs"
        )
        reset = next(
            task
            for task in tasks
            if task["name"] == "Remove changed controller Galaxy override staging tree"
        )
        copy_definition = next(
            task
            for task in tasks
            if task["name"] == "Install controller Galaxy override inputs"
        )

        self.assertIs(discovery["ansible.builtin.find"]["get_checksum"], True)
        self.assertIs(discovery["ansible.builtin.find"]["hidden"], True)
        self.assertIs(discovery["ansible.builtin.find"]["follow"], False)
        self.assertEqual("localhost", discovery["delegate_to"])
        self.assertIn("semaphore_toolchain_override_links.matched == 0", link_rejection["ansible.builtin.assert"]["that"])
        self.assertEqual("absent", reset["ansible.builtin.file"]["state"])
        self.assertEqual(
            "{{ semaphore_state_root }}/tools/bootstrap/overrides/ansible-galaxy",
            reset["ansible.builtin.file"]["path"],
        )
        self.assertTrue(any("semaphore_toolchain_inputs_changed" in condition for condition in reset["when"]))
        self.assertTrue(
            any(
                "semaphore_toolchain_inputs_changed" in condition
                for condition in copy_definition["when"]
            )
        )

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            boundary = Path(directory)
            source = boundary / "source"
            state = boundary / "state"
            source.mkdir()
            (source / "kept.yml").write_text("kept\n")
            removed = source / "removed.yml"
            removed.write_text("removed\n")

            selected = {
                task["name"]: copy.deepcopy(task)
                for task in tasks
            }
            discovery_task = selected["Discover every controller Galaxy override input"]
            discovery_task["ansible.builtin.find"]["paths"] = str(source)
            initialize_task = selected["Initialize controller runtime input fingerprint material"]
            initialize_task["vars"]["semaphore_toolchain_fingerprint_inputs"] = ["fixed-input"]
            accumulate_task = selected[
                "Add every sorted override path and checksum to controller fingerprint material"
            ]
            accumulate_task["vars"]["semaphore_override_source_root"] = str(source)
            reset_task = selected["Remove changed controller Galaxy override staging tree"]
            recreate_task = selected["Recreate changed controller Galaxy override staging tree"]
            recreate_task["ansible.builtin.file"]["group"] = grp.getgrgid(os.getgid()).gr_name
            copy_task = selected["Install controller Galaxy override inputs"]
            copy_task["ansible.builtin.copy"]["src"] = f"{source}/"
            copy_task["ansible.builtin.copy"]["owner"] = getpass.getuser()
            copy_task["ansible.builtin.copy"]["group"] = grp.getgrgid(os.getgid()).gr_name
            fingerprint_task = selected["Compute current controller runtime input fingerprint"]
            record = {
                "name": "Expose staged fingerprint",
                "ansible.builtin.copy": {
                    "content": "{{ semaphore_toolchain_input_fingerprint }}\n",
                    "dest": str(boundary / "fingerprint"),
                    "mode": "0600",
                },
            }
            play = [{
                "name": "Exercise exact override staging",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "vars": {
                    "semaphore_controller_enabled": True,
                    "semaphore_toolchain_inputs_changed": True,
                    "semaphore_state_root": str(state),
                    "semaphore_account_name": getpass.getuser(),
                },
                "tasks": [
                    discovery_task,
                    initialize_task,
                    accumulate_task,
                    fingerprint_task,
                    reset_task,
                    recreate_task,
                    copy_task,
                    record,
                ],
            }]
            probe = boundary / "probe.yml"
            probe.write_text(yaml.safe_dump(play))

            first = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(probe)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            first_fingerprint = (boundary / "fingerprint").read_text()
            staged = state / "tools/bootstrap/overrides/ansible-galaxy"
            self.assertEqual({"kept.yml", "removed.yml"}, {path.name for path in staged.iterdir()})

            removed.unlink()
            second = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(probe)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
            self.assertNotEqual(first_fingerprint, (boundary / "fingerprint").read_text())
            self.assertEqual({"kept.yml"}, {path.name for path in staged.iterdir()})

    def test_runuser_commands_use_the_service_runtime_directory(self) -> None:
        for name in ("definitions.yml", "toolchain.yml", "verify.yml"):
            tasks = yaml.safe_load((ROLE / "tasks" / name).read_text())
            runuser_tasks = [
                task
                for task in tasks
                if task.get("ansible.builtin.command", {}).get("argv", [None])[0]
                == "/usr/sbin/runuser"
            ]
            self.assertTrue(runuser_tasks, name)
            for task in runuser_tasks:
                with self.subTest(file=name, task=task["name"]):
                    self.assertEqual(
                        "/run/user/{{ semaphore_foundation_account.uid }}",
                        task["environment"]["XDG_RUNTIME_DIR"],
                    )


class SemaphoreInputValidationTests(unittest.TestCase):
    @staticmethod
    def run_task(task: dict[str, object], variables: dict[str, object]) -> subprocess.CompletedProcess[str]:
        play = [{
            "name": "Exercise Semaphore validation",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": variables,
            "tasks": [task],
        }]
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            probe = Path(directory) / "probe.yml"
            probe.write_text(yaml.safe_dump(play))
            return subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(probe)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

    @staticmethod
    def public_variables() -> dict[str, object]:
        return {
            "semaphore_account_name": "svc-semaphore",
            "semaphore_foundation_account": {
                "name": "svc-semaphore",
                "uid": 2100,
                "gid": 2100,
                "subuid_start": 200000,
                "subuid_count": 65536,
                "subgid_start": 200000,
                "subgid_count": 65536,
            },
            "semaphore_image": "docker.io/semaphoreui/semaphore:v2.19.12@sha256:" + "a" * 64,
            "semaphore_postgres_image": "docker.io/library/postgres:17.11@sha256:" + "b" * 64,
            "semaphore_rclone_image": "docker.io/rclone/rclone:1.75.1@sha256:" + "c" * 64,
            "semaphore_postgres_major": "17",
            "semaphore_backend_port": 13000,
            "semaphore_hostname": "semaphore.example.test",
            "semaphore_certificate_name": "semaphore",
            "semaphore_database_name": "semaphore",
            "semaphore_database_user": "semaphore",
            "semaphore_state_root": "/var/lib/svc-semaphore/semaphore",
            "semaphore_quadlet_root": "/etc/containers/systemd/users/2100",
            "semaphore_user_unit_root": "/var/lib/svc-semaphore/.config/systemd/user",
            "semaphore_helper_root": "/usr/local/libexec/semaphore",
        }

    def test_shared_proxy_accepts_device_routes_and_rejects_local_conflicts(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/preflight.yml").read_text())
        validation = next(task for task in tasks if task["name"] == "Require declared shared proxy route")
        device = {
            "hostname": "device.example.test", "certificate_name": "infra",
            "backend": {"transport": "http", "address": "192.0.2.10", "port": 80},
        }
        application = {
            "hostname": "semaphore.example.test", "certificate_name": "semaphore",
            "backend_port": 13000,
        }
        cases = [
            ("mixed routes", [device, application], True),
            ("missing application", [device], False),
            ("duplicate hostname", [device, application, application], False),
            ("duplicate local port", [device, application, application | {"hostname": "other.example.test"}], False),
        ]
        for label, routes, accepted in cases:
            with self.subTest(label=label):
                result = self.run_task(validation, self.public_variables() | {"reverse_proxy_routes": routes})
                self.assertEqual(accepted, result.returncode == 0, result.stdout + result.stderr)

    def test_negative_foundation_identity_is_rejected_before_host_observation(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/preflight.yml").read_text())
        validation = next(
            task for task in tasks if task["name"] == "Validate public Semaphore inputs"
        )
        variables = self.public_variables()
        variables["semaphore_foundation_account"] = variables["semaphore_foundation_account"] | {"uid": -1}
        variables["semaphore_quadlet_root"] = "/etc/containers/systemd/users/-1"
        result = self.run_task(validation, variables)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_public_input_contract_rejects_each_unsafe_boundary(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/preflight.yml").read_text())
        validation = next(task for task in tasks if task["name"] == "Validate public Semaphore inputs")
        valid = self.run_task(validation, self.public_variables())
        self.assertEqual(0, valid.returncode, valid.stdout + valid.stderr)

        cases = {
            "privileged port": {"semaphore_backend_port": 443},
            "malformed digest": {"semaphore_image": "docker.io/semaphoreui/semaphore:v2.19.12"},
            "postgres major mismatch": {"semaphore_postgres_image": "docker.io/library/postgres:16@sha256:" + "b" * 64},
            "state traversal": {"semaphore_state_root": "/var/lib/svc-semaphore/../other"},
        }
        for label, changes in cases.items():
            with self.subTest(label=label):
                result = self.run_task(validation, self.public_variables() | changes)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_absent_protected_value_is_rejected_without_value_output(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/preflight.yml").read_text())
        validation = next(task for task in tasks if task["name"] == "Require protected Semaphore inputs without disclosing values")
        names = validation["loop"]
        variables = {name: "synthetic-sensitive-sentinel" for name in names}
        del variables["semaphore_cookie_hash"]
        result = self.run_task(validation, variables)

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("synthetic-sensitive-sentinel", result.stdout + result.stderr)

    def test_symlink_and_incompatible_foundation_record_are_rejected(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/preflight.yml").read_text())
        path_validation = next(task for task in tasks if task["name"] == "Reject unsafe existing Semaphore paths")
        path_validation["loop"] = [{
            "item": "/var/lib/svc-semaphore/semaphore",
            "stat": {"exists": True, "islnk": True, "isdir": True, "uid": 2100},
        }]
        path_result = self.run_task(path_validation, {
            "semaphore_state_root": "/var/lib/svc-semaphore/semaphore",
            "semaphore_foundation_account": {"uid": 2100},
        })
        self.assertNotEqual(0, path_result.returncode, path_result.stdout + path_result.stderr)

        record_validation = next(task for task in tasks if task["name"] == "Require exact foundation identity declaration")
        declared = self.public_variables()["semaphore_foundation_account"]
        encoded = base64.b64encode(b'{"name":"svc-other"}').decode()
        record_result = self.run_task(record_validation, {
            "semaphore_foundation_account": declared,
            "semaphore_foundation_record": {"content": encoded},
        })
        self.assertNotEqual(0, record_result.returncode, record_result.stdout + record_result.stderr)

    def test_effective_port_observer_rejects_extra_or_database_publication(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/verify.yml").read_text())
        assertion = next(
            task
            for task in tasks
            if task["name"] == "Verify effective Semaphore container port mappings"
        )
        variables = {
            "semaphore_backend_port": 13000,
            "semaphore_container_ports": {
                "results": [
                    {"stdout_lines": ["3000/tcp -> 127.0.0.1:13000"]},
                    {"stdout": ""},
                ]
            },
        }
        valid = self.run_task(assertion, variables)
        self.assertEqual(0, valid.returncode, valid.stdout + valid.stderr)

        variables["semaphore_container_ports"]["results"][0]["stdout_lines"].append(
            "3000/tcp -> 198.51.100.42:13000"
        )
        variables["semaphore_container_ports"]["results"][1]["stdout"] = (
            "5432/tcp -> 127.0.0.1:15432"
        )
        invalid = self.run_task(assertion, variables)
        self.assertNotEqual(0, invalid.returncode, invalid.stdout + invalid.stderr)

    def test_existing_database_major_must_match_declared_postgres(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/preflight.yml").read_text())
        assertion = next(
            task
            for task in tasks
            if task["name"] == "Require compatible existing PostgreSQL data"
        )
        variables = {
            "semaphore_postgres_major": "17",
            "semaphore_postgres_version_file": {
                "stat": {"exists": True, "isreg": True, "islnk": False}
            },
            "semaphore_existing_postgres_version": {
                "content": base64.b64encode(b"17\n").decode()
            },
        }
        valid = self.run_task(assertion, variables)
        self.assertEqual(0, valid.returncode, valid.stdout + valid.stderr)
        variables["semaphore_existing_postgres_version"]["content"] = base64.b64encode(
            b"16\n"
        ).decode()
        invalid = self.run_task(assertion, variables)
        self.assertNotEqual(0, invalid.returncode, invalid.stdout + invalid.stderr)


if __name__ == "__main__":
    unittest.main()
