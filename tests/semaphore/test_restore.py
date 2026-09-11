from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_PATH = REPO_ROOT / "scripts" / "semaphore" / "restore.py"
FIXTURE_PATH = REPO_ROOT / "scripts" / "semaphore" / "fixture.py"


def load_restore():
    spec = importlib.util.spec_from_file_location("semaphore_restore", RESTORE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load restore module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_fixture():
    spec = importlib.util.spec_from_file_location("semaphore_fixture", FIXTURE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load fixture module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixtureContractTests(unittest.TestCase):
    def test_fixture_preregisters_owned_oneshot_before_ambiguous_failure(self) -> None:
        fixture_module = load_fixture()
        run = fixture_module.Fixture(
            "semaphore-20260910T031500Z-Ab12", Path("unused-destination")
        )
        run.command = mock.Mock(side_effect=fixture_module.subprocess.TimeoutExpired("podman", 300))

        with self.assertRaises(fixture_module.subprocess.TimeoutExpired):
            run.podman("run", "--rm", fixture_module.SAMBA_IMAGE)

        self.assertEqual(1, len(run.created))
        kind, name = run.created[0]
        self.assertEqual("container", kind)
        self.assertTrue(name.startswith("semaphore-fixture-step-"))
        command = run.command.call_args.args
        self.assertIn("--label", command)
        self.assertIn(run.owner_label, command)

    def test_native_seed_payload_binds_environment_secrets_to_bash_template(self) -> None:
        fixture = load_fixture()

        payloads = fixture.seed_payloads(7, 11, 13, "user-value", "password-value")

        self.assertEqual(
            [
                {
                    "name": "RECOVERY_USERNAME",
                    "secret": "user-value",
                    "type": "env",
                    "operation": "create",
                },
                {
                    "name": "RECOVERY_PASSWORD",
                    "secret": "password-value",
                    "type": "env",
                    "operation": "create",
                },
            ],
            payloads["environment"]["secrets"],
        )
        self.assertEqual([11], payloads["template"]["environment_ids"])
        self.assertEqual(13, payloads["template"]["repository_id"])
        self.assertEqual("bash", payloads["template"]["app"])
        self.assertFalse(payloads["template"]["allow_parallel_tasks"])

    def test_restore_download_can_join_only_the_fixture_smb_network(self) -> None:
        restore = load_restore()
        fields = restore.Options._fields
        self.assertIn("rclone_network", fields)


class ArchiveValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.restore = load_restore()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.archive = self.root / "semaphore-20260910T031500Z-Ab12"
        self.archive.mkdir()
        self.dump = b"PostgreSQL custom archive fixture"
        (self.archive / "database.dump").write_bytes(self.dump)
        self.digest = hashlib.sha256(self.dump).hexdigest()
        (self.archive / "SHA256SUMS").write_text(
            f"{self.digest}  database.dump\n", encoding="ascii"
        )
        (self.archive / "COMPLETE").write_text(
            f"SHA256={self.digest}\nBYTES={len(self.dump)}\n", encoding="ascii"
        )

    def test_accepts_exact_completed_archive_contract(self) -> None:
        self.restore.validate_archive(self.archive)

    def test_rejects_noncanonical_or_traversing_archive_identifiers(self) -> None:
        for identifier in (
            "../semaphore-20260910T031500Z-Ab12",
            "semaphore-20260910T031500Z-Ab12/other",
            "semaphore-latest",
            ".partial-semaphore-20260910T031500Z-Ab12",
            "semaphore-20260910T031500Z-a_b",
        ):
            with self.subTest(identifier=identifier):
                with self.assertRaisesRegex(ValueError, "archive identifier"):
                    self.restore.validate_archive_identifier(identifier)

    def test_rejects_missing_completion_marker(self) -> None:
        (self.archive / "COMPLETE").unlink()

        with self.assertRaisesRegex(ValueError, "COMPLETE"):
            self.restore.validate_archive(self.archive)

    def test_rejects_checksum_or_size_mismatch(self) -> None:
        (self.archive / "database.dump").write_bytes(self.dump + b"corrupt")

        with self.assertRaisesRegex(ValueError, "checksum"):
            self.restore.validate_archive(self.archive)

    def test_rejects_malformed_sidecar_and_completion_marker(self) -> None:
        invalid = (
            ("SHA256SUMS", f"{self.digest} database.dump\n"),
            ("SHA256SUMS", f"{self.digest}  other.dump\n"),
            ("COMPLETE", f"BYTES={len(self.dump)}\nSHA256={self.digest}\n"),
            ("COMPLETE", f"SHA256={self.digest}\nBYTES=01\n"),
        )
        for filename, contents in invalid:
            with self.subTest(filename=filename, contents=contents):
                original = (self.archive / filename).read_text()
                (self.archive / filename).write_text(contents)
                with self.assertRaisesRegex(ValueError, filename):
                    self.restore.validate_archive(self.archive)
                (self.archive / filename).write_text(original)

    def test_rejects_symlinked_archive_content(self) -> None:
        (self.archive / "database.dump").unlink()
        outside = self.root / "outside.dump"
        outside.write_bytes(self.dump)
        (self.archive / "database.dump").symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "symlink"):
            self.restore.validate_archive(self.archive)

    def test_subprocess_runner_applies_a_finite_timeout(self) -> None:
        with mock.patch.object(self.restore.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.restore.SubprocessRunner().run(["podman", "info"])

        self.assertEqual(run.call_args.kwargs["timeout"], 300)


class RestorePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.restore = load_restore()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = self.root / "settings.env"
        self.settings.write_text(
            "POSTGRES_USER=semaphore\n"
            "POSTGRES_PASSWORD=synthetic-db-password\n"
            "POSTGRES_DB=semaphore\n"
            "SEMAPHORE_DB_USER=semaphore\n"
            "SEMAPHORE_DB_PASS=synthetic-db-password\n"
            "SEMAPHORE_DB_NAME=semaphore\n"
            "SEMAPHORE_ADMIN=fixture-admin\n"
            "SEMAPHORE_ADMIN_PASSWORD=synthetic-admin-password\n"
            "SEMAPHORE_ACCESS_KEY_ENCRYPTION=synthetic-fixture-key\n"
        )
        self.settings.chmod(0o600)
        self.rclone = self.root / "rclone.conf"
        self.rclone.write_text("[nas]\ntype = local\n")
        self.rclone.chmod(0o600)

    def options(self, destination: Path | None = None):
        return self.restore.Options(
            archive_id="semaphore-20260910T031500Z-Ab12",
            destination=destination or self.root / "restore-run",
            remote="nas:semaphore",
            rclone_config=self.rclone,
            settings_env=self.settings,
            mode="fixture",
            target=None,
            confirmation=None,
            fixture_target_url="http://fixture-target/recovery.git",
            fixture_target_container="fixture-target",
            denied_url="http://managed-host/",
            expected_project_id=1,
            expected_project_name="Recovery fixture",
            expected_template_id=1,
            expected_template_name="Recovery credential probe",
            expected_environment_id=1,
            expected_history_id=1,
            expected_key_id=1,
            expected_output="RECOVERY_CREDENTIAL_ACCEPTED",
            semaphore_image=self.restore.SEMAPHORE_IMAGE,
            postgres_image=self.restore.POSTGRES_IMAGE,
            rclone_image=self.restore.RCLONE_IMAGE,
            rclone_network=None,
        )

    def test_preflight_rejects_existing_destination_before_commands(self) -> None:
        destination = self.root / "existing"
        destination.mkdir()
        runner = mock.Mock()

        with self.assertRaisesRegex(ValueError, "destination must not exist"):
            self.restore.RestoreExperiment(self.options(destination), runner).run()

        runner.run.assert_not_called()

    def test_preflight_rejects_symlinked_inputs_before_commands(self) -> None:
        real = self.root / "real-settings"
        real.write_text(self.settings.read_text())
        linked = self.root / "linked-settings"
        linked.symlink_to(real)
        options = self.options()
        options = options._replace(settings_env=linked)
        runner = mock.Mock()

        with self.assertRaisesRegex(ValueError, "symlink"):
            self.restore.RestoreExperiment(options, runner).run()

        runner.run.assert_not_called()

    def test_preflight_requires_immutable_image_references(self) -> None:
        options = self.options()._replace(semaphore_image="semaphoreui/semaphore:v2.19.12")

        with self.assertRaisesRegex(ValueError, "immutable"):
            self.restore.RestoreExperiment(options, mock.Mock()).run()

    def test_attended_mode_requires_exact_target_confirmation(self) -> None:
        options = self.options()._replace(
            mode="attended", target="nuc4", confirmation="different"
        )

        with self.assertRaisesRegex(ValueError, "confirmation"):
            self.restore.RestoreExperiment(options, mock.Mock()).run()

    def test_fixture_mode_requires_bounded_and_denied_targets(self) -> None:
        options = self.options()._replace(denied_url=None)

        with self.assertRaisesRegex(ValueError, "denied URL"):
            self.restore.RestoreExperiment(options, mock.Mock()).run()


class RecordingRunner:
    def __init__(self, restore, archive_bytes: bytes):
        self.restore = restore
        self.archive_bytes = archive_bytes
        self.calls: list[tuple[list[str], str | None]] = []
        self.removed: set[str] = set()
        self.api_responses = {
            "/api/projects": [{"id": 1, "name": "Recovery fixture"}],
            "/api/project/1/templates?sort=name&order=asc": [
                {
                    "id": 1,
                    "name": "Recovery credential probe",
                    "app": "bash",
                    "playbook": "probe.sh",
                    "environment_ids": [1],
                }
            ],
            "/api/project/1/keys?sort=name&order=asc": [
                {"id": 1, "name": "Fixture credential"}
            ],
            "/api/project/1/environment/1": {
                "id": 1,
                "name": "Recovery credentials",
                "secrets": [
                    {"id": 1, "name": "RECOVERY_USERNAME", "type": "env"},
                    {"id": 2, "name": "RECOVERY_PASSWORD", "type": "env"},
                ],
            },
            "/api/project/1/tasks": [
                {"id": 1, "template_id": 1, "status": "success"}
            ],
            "/api/project/1/tasks/2": {
                "id": 2,
                "template_id": 1,
                "status": "success",
            },
            "/api/project/1/tasks/2/output": [
                {"task_id": 2, "output": "RECOVERY_CREDENTIAL_ACCEPTED"}
            ],
        }

    def run(self, command, *, input_text=None, check=True, capture_output=True):
        command = list(command)
        self.calls.append((command, input_text))
        if len(command) >= 4 and (
            command[1:3] in (["volume", "rm"], ["network", "rm"])
            or command[1:3] == ["rm", "--force"]
        ):
            self.removed.add(command[-1])
        if "copyto" in command:
            target = Path(command[-1].replace("/archive/", self.mount(command)))
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.name == "database.dump":
                target.write_bytes(self.archive_bytes)
            elif target.name == "SHA256SUMS":
                digest = hashlib.sha256(self.archive_bytes).hexdigest()
                target.write_text(f"{digest}  database.dump\n")
            elif target.name == "COMPLETE":
                digest = hashlib.sha256(self.archive_bytes).hexdigest()
                target.write_text(
                    f"SHA256={digest}\nBYTES={len(self.archive_bytes)}\n"
                )
        stdout = ""
        if "network" in command and "inspect" in command:
            stdout = "true\n"
        fixture_inspect = any("io.homelab.semaphore.fixture" in value for value in command)
        if fixture_inspect:
            stdout = "true\n"
        if "inspect" in command and "--format" in command and not fixture_inspect and not (
            "network" in command and "{{.Internal}}" in command
        ):
            owner = re.search(
                r"semaphore-restore-(?:step|db|app|data|net)-([0-9a-f]+)",
                command[-1],
            )
            stdout = (owner.group(1) if owner else "") + "\n"
        if "/usr/bin/curl" in command:
            stdout = self.curl_response(command, input_text)
        returncode = 0
        if "inspect" in command and command[-1] in self.removed:
            returncode = 1
        if len(command) >= 4 and command[2] == "exists" and command[-1] in self.removed:
            returncode = 1
        if command[-1] == "http://fixture-target/recovery.git":
            returncode = 22
        if command[-1] == "http://managed-host/":
            returncode = 6
        return mock.Mock(returncode=returncode, stdout=stdout, stderr="")

    @staticmethod
    def mount(command: list[str]) -> str:
        mount = next(value for index, value in enumerate(command) if command[index - 1] == "-v" and value.endswith(":/archive:rw"))
        return mount.removesuffix(":/archive:rw") + "/"

    def curl_response(self, command: list[str], input_text: str | None) -> str:
        if command[-1] == "http://semaphore:3000/api/auth/login":
            return "Set-Cookie: semaphore=synthetic-session; Path=/; HttpOnly\r\n\r\n"
        url = command[-1]
        path = url.split("http://semaphore:3000", 1)[-1]
        if path == "/api/project/1/tasks" and "--request" in command:
            return '{"id": 2, "template_id": 1}'
        import json
        return json.dumps(self.api_responses.get(path, {}))


class RestoreOrchestrationTests(RestorePlanTests):
    def test_ambiguous_oneshot_failure_is_preowned_and_cleaned(self) -> None:
        options = self.options()
        runner = RecordingRunner(self.restore, b"custom archive")
        original = runner.run

        def timeout_after_create(command, **kwargs):
            result = original(command, **kwargs)
            if "copyto" in command:
                raise self.restore.CommandError("ambiguous timeout")
            return result

        runner.run = timeout_after_create
        with self.assertRaisesRegex(self.restore.RestoreFailure, "ambiguous timeout"):
            self.restore.RestoreExperiment(options, runner).run()

        rendered = [" ".join(call[0]) for call in runner.calls]
        created = next(value for value in rendered if "copyto" in value)
        self.assertIn("--name semaphore-restore-step-", created)
        self.assertIn("--label io.homelab.semaphore.restore=", created)
        self.assertTrue(any("podman rm --force semaphore-restore-step-" in value for value in rendered))

    def test_cleanup_does_not_treat_exists_runtime_error_as_absence(self) -> None:
        options = self.options()
        runner = RecordingRunner(self.restore, b"custom archive")
        original = runner.run

        def broken_exists(command, **kwargs):
            if len(command) >= 4 and command[1:3] == ["network", "exists"]:
                return mock.Mock(returncode=125, stdout="", stderr="runtime unavailable")
            return original(command, **kwargs)

        runner.run = broken_exists
        with self.assertRaisesRegex(self.restore.RestoreFailure, "cleanup failed.*determine"):
            self.restore.RestoreExperiment(options, runner).run()

    def test_history_must_be_successful_and_from_the_expected_template(self) -> None:
        options = self.options()
        runner = RecordingRunner(self.restore, b"custom archive")
        runner.api_responses["/api/project/1/tasks"] = [
            {"id": 1, "template_id": 99, "status": "success"}
        ]

        with self.assertRaisesRegex(self.restore.RestoreFailure, "history"):
            self.restore.RestoreExperiment(options, runner).run()

    def test_rejects_a_queued_task_for_a_different_template(self) -> None:
        options = self.options()
        runner = RecordingRunner(self.restore, b"custom archive")
        original_response = runner.curl_response

        def wrong_template(command, input_text):
            if command[-1] == "/api/project/1/tasks":
                return '{"id": 2, "template_id": 999}'
            response = original_response(command, input_text)
            if (
                command[-1] == "http://semaphore:3000/api/project/1/tasks"
                and "--request" in command
            ):
                return '{"id": 2, "template_id": 999}'
            return response

        runner.curl_response = wrong_template
        with self.assertRaisesRegex(self.restore.RestoreFailure, "template"):
            self.restore.RestoreExperiment(options, runner).run()

    def test_target_network_failure_does_not_count_as_authentication_rejection(self) -> None:
        options = self.options()
        runner = RecordingRunner(self.restore, b"custom archive")
        original = runner.run

        def unreachable(command, **kwargs):
            if command[-1] == options.fixture_target_url:
                return mock.Mock(returncode=6, stdout="", stderr="unresolved")
            return original(command, **kwargs)

        runner.run = unreachable
        with self.assertRaisesRegex(self.restore.RestoreFailure, "target.*reach"):
            self.restore.RestoreExperiment(options, runner).run()

    def test_denied_http_response_does_not_count_as_network_isolation(self) -> None:
        options = self.options()
        runner = RecordingRunner(self.restore, b"custom archive")
        original = runner.run

        def reachable(command, **kwargs):
            if command[-1] == options.denied_url:
                return mock.Mock(returncode=22, stdout="", stderr="HTTP 403")
            return original(command, **kwargs)

        runner.run = reachable
        with self.assertRaisesRegex(self.restore.RestoreFailure, "denied.*network"):
            self.restore.RestoreExperiment(options, runner).run()

    def test_attended_mode_attaches_only_the_confirmed_fixture_target(self) -> None:
        options = self.options()._replace(
            mode="attended",
            target="fixture-target",
            confirmation="fixture-target",
            fixture_target_container=None,
        )
        runner = RecordingRunner(self.restore, b"custom archive")

        self.restore.RestoreExperiment(options, runner).run()

        rendered = [" ".join(call[0]) for call in runner.calls]
        self.assertTrue(any("network connect" in value and "fixture-target" in value for value in rendered))
        self.assertTrue(any("network disconnect" in value and "fixture-target" in value for value in rendered))

    def test_restore_orders_validation_isolation_restore_probe_and_cleanup(self) -> None:
        options = self.options()
        runner = RecordingRunner(self.restore, b"custom archive")

        self.restore.RestoreExperiment(options, runner).run()

        commands = [call[0] for call in runner.calls]
        rendered = [" ".join(command) for command in commands]
        network_create = next(i for i, value in enumerate(rendered) if "network create" in value)
        database_start = next(i for i, value in enumerate(rendered) if "run --detach" in value and "restore-db" in value)
        app_start = next(i for i, value in enumerate(rendered) if "run --detach" in value and "restore-app" in value)
        self.assertLess(network_create, database_start)
        self.assertLess(database_start, app_start)
        schedule_disable = next(
            i for i, value in enumerate(rendered)
            if "UPDATE project__schedule SET active=false WHERE active=true" in value
        )
        self.assertLess(schedule_disable, app_start)
        self.assertTrue(any("network inspect --format {{.Internal}}" in value for value in rendered))
        self.assertTrue(any("network connect" in value and "fixture-target" in value for value in rendered))
        self.assertTrue(any("network disconnect" in value and "fixture-target" in value for value in rendered))
        self.assertTrue(any(
            "pg_restore --exit-on-error --single-transaction --no-owner --no-privileges" in value
            for value in rendered
        ))
        database_commands = [
            value for value in rendered
            if ("run --detach" in value and "restore-db" in value)
            or "pg_restore --exit-on-error" in value
        ]
        self.assertTrue(all("postgres.env" in value for value in database_commands))
        app_command = next(value for value in rendered if "run --detach" in value and "restore-app" in value)
        self.assertIn("semaphore.env", app_command)
        self.assertNotIn(str(options.settings_env), "\n".join(database_commands + [app_command]))
        curl_commands = [value for value in rendered if "/usr/bin/curl" in value]
        self.assertFalse(any(":/restore-state:rw" in value for value in curl_commands))
        self.assertTrue(any("curl-auth.conf:/restore-auth.conf:ro" in value for value in curl_commands))
        self.assertTrue(any("http://managed-host/" in value for value in rendered))
        self.assertTrue(any("/api/project/1/tasks/2/output" in value for value in rendered))
        self.assertTrue(any("/api/project/1/templates?sort=name&order=asc" in value for value in rendered))
        environment_calls = [
            value for value in rendered if "/api/project/1/environment/1" in value
        ]
        self.assertEqual(2, len(environment_calls))
        for kind in ("container", "volume", "network"):
            removals = [i for i, value in enumerate(rendered) if f"{kind} rm" in value or (kind == "container" and "podman rm --force" in value)]
            for removal in removals:
                self.assertTrue(
                    any(
                        i > removal
                        and (f"{kind} inspect" in value or f"{kind} exists" in value)
                        for i, value in enumerate(rendered)
                    ),
                    f"{kind} removal was not followed by an absence check",
                )
        self.assertFalse(any("delete" in value or "purge" in value for value in rendered if "rclone" in value))
        self.assertFalse(options.destination.exists())

    def test_primary_and_cleanup_failures_are_both_reported(self) -> None:
        options = self.options()
        runner = RecordingRunner(self.restore, b"custom archive")
        original = runner.run

        def fail(command, **kwargs):
            if "pg_isready" in command:
                raise self.restore.CommandError("database readiness failed")
            if "volume" in command and "rm" in command:
                raise self.restore.CommandError("volume cleanup failed")
            return original(command, **kwargs)

        runner.run = fail
        with self.assertRaises(self.restore.RestoreFailure) as raised:
            self.restore.RestoreExperiment(options, runner).run()

        self.assertIn("database readiness failed", str(raised.exception))
        self.assertIn("cleanup", str(raised.exception))
        self.assertIn("volume cleanup failed", str(raised.exception))

    def test_interrupt_still_runs_owned_cleanup(self) -> None:
        options = self.options()
        runner = RecordingRunner(self.restore, b"custom archive")
        original = runner.run

        def interrupt(command, **kwargs):
            if "pg_isready" in command:
                raise KeyboardInterrupt("fixture interruption")
            return original(command, **kwargs)

        runner.run = interrupt
        with self.assertRaisesRegex(self.restore.RestoreFailure, "fixture interruption"):
            self.restore.RestoreExperiment(options, runner).run()

        self.assertFalse(options.destination.exists())


if __name__ == "__main__":
    unittest.main()
