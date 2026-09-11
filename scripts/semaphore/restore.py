#!/usr/bin/env python3
"""Run a bounded Semaphore restore experiment from one exact NAS archive."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import NamedTuple


SEMAPHORE_IMAGE = (
    "docker.io/semaphoreui/semaphore:v2.19.12@"
    "sha256:3996804607ebb63690528185bb9adc3507ee896851098e4453975f2ce7f8b435"
)
POSTGRES_IMAGE = (
    "docker.io/library/postgres:17.11@"
    "sha256:67f41722b7a8cbdb868a44a4995c846eddfdc2973bccb291ce937dce88ad5675"
)
RCLONE_IMAGE = (
    "docker.io/rclone/rclone:1.75.1@"
    "sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5"
)
OWNER_LABEL = "io.homelab.semaphore.restore"
ARCHIVE_PATTERN = re.compile(r"semaphore-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]+")
DIGEST_PATTERN = r"[0-9a-f]{64}"
REQUIRED_SETTINGS = {
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "SEMAPHORE_DB_USER",
    "SEMAPHORE_DB_PASS",
    "SEMAPHORE_DB_NAME",
    "SEMAPHORE_ADMIN",
    "SEMAPHORE_ADMIN_PASSWORD",
    "SEMAPHORE_ACCESS_KEY_ENCRYPTION",
}


class CommandError(RuntimeError):
    pass


class RestoreFailure(RuntimeError):
    pass


class Options(NamedTuple):
    archive_id: str
    destination: Path
    remote: str
    rclone_config: Path
    settings_env: Path
    mode: str
    target: str | None
    confirmation: str | None
    fixture_target_url: str | None
    fixture_target_container: str | None
    denied_url: str | None
    expected_project_id: int
    expected_project_name: str
    expected_template_id: int
    expected_template_name: str
    expected_environment_id: int
    expected_history_id: int
    expected_key_id: int
    expected_output: str
    semaphore_image: str
    postgres_image: str
    rclone_image: str
    rclone_network: str | None


class SubprocessRunner:
    def run(
        self,
        command: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                input=input_text,
                text=True,
                capture_output=capture_output,
                check=False,
                timeout=300,
            )
        except subprocess.TimeoutExpired as error:
            raise CommandError(f"{command[0]} timed out") from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or "command failed"
            raise CommandError(f"{command[0]} failed: {detail}")
        return result


def validate_archive_identifier(identifier: str) -> None:
    if ARCHIVE_PATTERN.fullmatch(identifier) is None:
        raise ValueError("archive identifier is not a completed Semaphore archive")


def _regular_file(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"archive content is a symlink: {path.name}")
    if not path.is_file():
        raise ValueError(f"archive is missing {path.name}")


def validate_archive(archive: Path) -> None:
    if archive.is_symlink() or not archive.is_dir():
        raise ValueError("selected archive must be a non-symlink directory")
    validate_archive_identifier(archive.name)
    expected_names = {"database.dump", "SHA256SUMS", "COMPLETE"}
    actual_names = {entry.name for entry in archive.iterdir()}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise ValueError(f"archive contents differ from contract; missing={missing}, extra={extra}")
    dump = archive / "database.dump"
    sidecar = archive / "SHA256SUMS"
    complete = archive / "COMPLETE"
    for path in (dump, sidecar, complete):
        _regular_file(path)
    try:
        sidecar_text = sidecar.read_text(encoding="ascii")
        complete_text = complete.read_text(encoding="ascii")
    except (UnicodeDecodeError, OSError) as error:
        raise ValueError("archive metadata is unreadable") from error
    sidecar_match = re.fullmatch(
        rf"({DIGEST_PATTERN})  database\.dump\n", sidecar_text
    )
    if sidecar_match is None:
        raise ValueError("SHA256SUMS does not match the archive contract")
    complete_match = re.fullmatch(
        rf"SHA256=({DIGEST_PATTERN})\nBYTES=(0|[1-9][0-9]*)\n", complete_text
    )
    if complete_match is None:
        raise ValueError("COMPLETE does not match the archive contract")
    expected_digest = sidecar_match.group(1)
    if complete_match.group(1) != expected_digest:
        raise ValueError("COMPLETE checksum differs from SHA256SUMS")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    if digest != expected_digest:
        raise ValueError("database.dump checksum does not match archive metadata")
    if dump.stat().st_size != int(complete_match.group(2)):
        raise ValueError("database.dump size does not match COMPLETE")


def _validate_input_file(path: Path, description: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{description} must not be a symlink")
    if not path.is_file():
        raise ValueError(f"{description} must be an existing regular file")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{description} must not be accessible to group or other users")


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"settings environment has invalid line {number}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not value:
            raise ValueError(f"settings environment has invalid line {number}")
        values[key] = value
    missing = sorted(REQUIRED_SETTINGS - values.keys())
    if missing:
        raise ValueError(f"settings environment is missing required names: {', '.join(missing)}")
    if values["POSTGRES_USER"] != values["SEMAPHORE_DB_USER"]:
        raise ValueError("PostgreSQL and Semaphore database users differ")
    if values["POSTGRES_PASSWORD"] != values["SEMAPHORE_DB_PASS"]:
        raise ValueError("PostgreSQL and Semaphore database passwords differ")
    if values["POSTGRES_DB"] != values["SEMAPHORE_DB_NAME"]:
        raise ValueError("PostgreSQL and Semaphore database names differ")
    return values


def _immutable(image: str) -> bool:
    return re.search(r"@sha256:[0-9a-f]{64}$", image) is not None


class RestoreExperiment:
    def __init__(self, options: Options, runner: SubprocessRunner):
        self.options = options
        self.runner = runner
        self.run_id = secrets.token_hex(6)
        self.label = f"{OWNER_LABEL}={self.run_id}"
        self.network = f"semaphore-restore-net-{self.run_id}"
        self.volume = f"semaphore-restore-data-{self.run_id}"
        self.database = f"semaphore-restore-db-{self.run_id}"
        self.application = f"semaphore-restore-app-{self.run_id}"
        self.created_destination = False
        self.created_network = False
        self.created_volume = False
        self.created_containers: list[str] = []
        self.connected_target: str | None = None
        self.oneshot_counter = 0
        self.settings: dict[str, str] = {}
        self.postgres_env = self.options.destination / "postgres.env"
        self.semaphore_env = self.options.destination / "semaphore.env"
        self.auth_config = self.options.destination / "curl-auth.conf"

    def _preflight(self) -> None:
        validate_archive_identifier(self.options.archive_id)
        if self.options.destination.exists() or self.options.destination.is_symlink():
            raise ValueError("destination must not exist")
        parent = self.options.destination.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("destination parent must be an existing non-symlink directory")
        _validate_input_file(self.options.settings_env, "settings environment")
        _validate_input_file(self.options.rclone_config, "rclone configuration")
        self.settings = _parse_env(self.options.settings_env)
        if not self.options.remote or self.options.remote.endswith("/"):
            raise ValueError("remote must name an exact rclone base without a trailing slash")
        for image in (
            self.options.semaphore_image,
            self.options.postgres_image,
            self.options.rclone_image,
        ):
            if not _immutable(image):
                raise ValueError("every restore image must use an immutable digest reference")
        if not self.options.fixture_target_url:
            raise ValueError("restore requires a bounded target URL")
        if not self.options.denied_url:
            raise ValueError("restore requires a denied URL")
        if self.options.mode == "attended":
            if not self.options.target or self.options.confirmation != self.options.target:
                raise ValueError("attended target and confirmation must match exactly")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.options.target) is None:
                raise ValueError("attended target is not a valid fixture container name")
        elif self.options.mode == "fixture":
            if not self.options.fixture_target_container:
                raise ValueError("fixture mode requires a fixture target container")
        else:
            raise ValueError("restore mode must be fixture or attended")

    def _podman(self, *arguments: str, **kwargs):
        command = list(arguments)
        if command and command[0] == "run" and "--rm" in command and "--name" not in command:
            self.oneshot_counter += 1
            name = f"semaphore-restore-step-{self.run_id}-{self.oneshot_counter}"
            command[1:1] = ["--name", name, "--label", self.label]
            self.created_containers.append(name)
        return self.runner.run(["podman", *command], **kwargs)

    @staticmethod
    def _write_env(path: Path, values: dict[str, str]) -> None:
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _write_runtime_env(self) -> None:
        self._write_env(
            self.postgres_env,
            {key: self.settings[key] for key in (
                "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"
            )},
        )
        application = {
            key: value
            for key, value in self.settings.items()
            if key.startswith("SEMAPHORE_")
        }
        application.update({
            "SEMAPHORE_DB_DIALECT": "postgres",
            "SEMAPHORE_DB_HOST": "restore-db",
            "SEMAPHORE_DB_PORT": "5432",
        })
        self._write_env(self.semaphore_env, application)

    def _download(self, archive: Path) -> None:
        config = str(self.options.rclone_config.resolve())
        mount = f"{archive.resolve()}:/archive:rw"
        for filename in ("database.dump", "SHA256SUMS", "COMPLETE"):
            source = f"{self.options.remote}/{self.options.archive_id}/{filename}"
            network = (
                ["--network", self.options.rclone_network]
                if self.options.rclone_network
                else []
            )
            self._podman(
                "run", "--rm",
                "--security-opt=no-new-privileges", "--cap-drop=all",
                *network,
                "-v", f"{config}:/config/rclone/rclone.conf:ro",
                "-v", mount,
                self.options.rclone_image,
                "copyto", source, f"/archive/{filename}",
            )

    def _check_archive_readability(self, archive: Path) -> None:
        self._podman(
            "run", "--rm", "--network=none",
            "--security-opt=no-new-privileges", "--cap-drop=all",
            "-v", f"{archive.resolve()}:/archive:ro",
            self.options.postgres_image,
            "pg_restore", "--file=/dev/null", "/archive/database.dump",
        )

    def _create_runtime(self) -> None:
        self.created_network = True
        self._podman(
            "network", "create", "--internal", "--label", self.label, self.network
        )
        internal = self._podman(
            "network", "inspect", "--format", "{{.Internal}}", self.network
        ).stdout.strip()
        if internal != "true":
            raise RestoreFailure("restore network is not internal")
        target_container = (
            self.options.fixture_target_container
            if self.options.mode == "fixture"
            else self.options.target
        )
        if target_container:
            fixture_label = self._podman(
                "container", "inspect", "--format",
                '{{ index .Config.Labels "io.homelab.semaphore.fixture" }}',
                target_container,
                check=False,
            )
            if fixture_label.returncode != 0 or fixture_label.stdout.strip() != "true":
                raise RestoreFailure("fixture target container is not owned by the test fixture")
            self.connected_target = target_container
            self._podman(
                "network", "connect", "--alias", "fixture-target",
                self.network, target_container,
            )
        self.created_volume = True
        self._podman("volume", "create", "--label", self.label, self.volume)
        self.created_containers.append(self.database)
        self._podman(
            "run", "--detach", "--name", self.database,
            "--label", self.label, "--network", self.network,
            "--network-alias", "restore-db", "--env-file", str(self.postgres_env),
            "-v", f"{self.volume}:/var/lib/postgresql/data:rw",
            self.options.postgres_image,
        )
        for _ in range(60):
            result = self._podman(
                "exec", self.database, "pg_isready", "--quiet",
                "--username", self.settings["POSTGRES_USER"],
                "--dbname", self.settings["POSTGRES_DB"], check=False,
            )
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RestoreFailure("database readiness timed out")

    def _restore_database(self, archive: Path) -> None:
        self._podman(
            "run", "--rm", "--network", self.network,
            "--security-opt=no-new-privileges", "--cap-drop=all",
            "--env-file", str(self.postgres_env),
            "-v", f"{archive.resolve()}:/archive:ro",
            "--entrypoint", "/bin/sh", self.options.postgres_image,
            "-ec",
            'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore '
            "--exit-on-error --single-transaction --no-owner --no-privileges "
            '--host=restore-db --username="$POSTGRES_USER" '
            '--dbname="$POSTGRES_DB" /archive/database.dump',
        )

    def _disable_restored_schedules(self) -> None:
        self._podman(
            "run", "--rm", "--network", self.network,
            "--security-opt=no-new-privileges", "--cap-drop=all",
            "--env-file", str(self.postgres_env),
            "--entrypoint", "/bin/sh", self.options.postgres_image,
            "-ec",
            'PGPASSWORD="$POSTGRES_PASSWORD" exec psql --set=ON_ERROR_STOP=1 '
            '--host=restore-db --username="$POSTGRES_USER" '
            '--dbname="$POSTGRES_DB" --command '
            "'UPDATE project__schedule SET active=false WHERE active=true'",
        )

    def _start_application(self) -> None:
        self.created_containers.append(self.application)
        self._podman(
            "run", "--detach", "--name", self.application,
            "--label", self.label, "--network", self.network,
            "--network-alias", "semaphore", "--env-file", str(self.semaphore_env),
            self.options.semaphore_image,
        )
        for _ in range(90):
            result = self._curl("--fail", "http://semaphore:3000/api/ping", check=False)
            if result.returncode == 0:
                return
            time.sleep(1)
        raise RestoreFailure("Semaphore readiness timed out")

    def _curl(self, *arguments: str, input_text: str | None = None, check: bool = True):
        mounts: list[str] = []
        if self.auth_config.is_file():
            mounts = ["-v", f"{self.auth_config.resolve()}:/restore-auth.conf:ro"]
        return self._podman(
            "run", "--rm", "--interactive", "--network", self.network,
            "--security-opt=no-new-privileges", "--cap-drop=all",
            "--user", "0:0",
            *mounts,
            "--entrypoint", "/usr/bin/curl", self.options.semaphore_image,
            "--silent", "--show-error", *arguments,
            input_text=input_text, check=check,
        )

    def _api(self, method: str, path: str, payload: dict | None = None):
        arguments = ["--fail", "--config", "/restore-auth.conf"]
        input_text = None
        if method != "GET":
            arguments.extend(["--request", method])
        if payload is not None:
            arguments.extend(["--header", "Content-Type: application/json", "--data-binary", "@-"])
            input_text = json.dumps(payload)
        arguments.append(f"http://semaphore:3000{path}")
        result = self._curl(*arguments, input_text=input_text)
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RestoreFailure(f"Semaphore returned invalid JSON for {path}") from error

    def _authenticate(self) -> None:
        payload = {
            "auth": self.settings["SEMAPHORE_ADMIN"],
            "password": self.settings["SEMAPHORE_ADMIN_PASSWORD"],
        }
        result = self._curl(
            "--fail", "--dump-header", "-", "--request", "POST",
            "--header", "Content-Type: application/json", "--data-binary", "@-",
            "http://semaphore:3000/api/auth/login",
            input_text=json.dumps(payload),
        )
        cookie = None
        for line in result.stdout.splitlines():
            if line.lower().startswith("set-cookie:"):
                cookie = line.split(":", 1)[1].strip().split(";", 1)[0]
                break
        if not cookie or "=" not in cookie:
            raise RestoreFailure("Semaphore login did not return a session cookie")
        self.auth_config.write_text(
            f"cookie = {json.dumps(cookie)}\n", encoding="utf-8"
        )
        self.auth_config.chmod(0o600)

    @staticmethod
    def _require_record(records, identifier: int, name: str, description: str):
        for record in records:
            if record.get("id") == identifier and record.get("name") == name:
                return record
        raise RestoreFailure(f"restored {description} {identifier} ({name}) was not found")

    def _validate_application(self) -> None:
        if self.options.fixture_target_url:
            denied = self._curl(
                "--fail", "--connect-timeout", "3", self.options.denied_url,
                check=False,
            )
            if denied.returncode not in {5, 6, 7, 28}:
                raise RestoreFailure(
                    "denied managed-host fixture was reachable from the restore network"
                )
            missing = self._curl("--fail", self.options.fixture_target_url, check=False)
            if missing.returncode == 0:
                raise RestoreFailure("fixture target accepted missing credentials")
            if missing.returncode != 22:
                raise RestoreFailure(
                    "fixture target could not be reached to prove missing-credential rejection"
                )
            wrong_config = "user = wrong:wrong\n"
            wrong = self._curl(
                "--fail", "--config", "-", self.options.fixture_target_url,
                input_text=wrong_config, check=False,
            )
            if wrong.returncode == 0:
                raise RestoreFailure("fixture target accepted incorrect credentials")
            if wrong.returncode != 22:
                raise RestoreFailure(
                    "fixture target could not be reached to prove wrong-credential rejection"
                )
        self._authenticate()
        project_id = self.options.expected_project_id
        self._require_record(
            self._api("GET", "/api/projects"), project_id,
            self.options.expected_project_name, "project",
        )
        template = self._require_record(
            self._api(
                "GET", f"/api/project/{project_id}/templates?sort=name&order=asc"
            ),
            self.options.expected_template_id, self.options.expected_template_name,
            "template",
        )
        if (
            template.get("app") != "bash"
            or template.get("playbook") != "probe.sh"
            or self.options.expected_environment_id
            not in template.get("environment_ids", [])
        ):
            raise RestoreFailure("restored template content or credential binding differs")
        environment_path = (
            f"/api/project/{project_id}/environment/"
            f"{self.options.expected_environment_id}"
        )
        environment_before = self._api("GET", environment_path)
        secrets_before = environment_before.get("secrets", [])
        if not any(
            record.get("id") == self.options.expected_key_id
            for record in secrets_before
        ):
            raise RestoreFailure("restored stored credential was not found")
        history = self._api("GET", f"/api/project/{project_id}/tasks")
        if not any(
            record.get("id") == self.options.expected_history_id
            and record.get("template_id") == self.options.expected_template_id
            and record.get("status") == "success"
            for record in history
        ):
            raise RestoreFailure("restored history record was not found")
        queued = self._api(
            "POST", f"/api/project/{project_id}/tasks",
            {"template_id": self.options.expected_template_id},
        )
        task_id = queued.get("id") if isinstance(queued, dict) else None
        if not isinstance(task_id, int):
            raise RestoreFailure("Semaphore did not return a restored task ID")
        if queued.get("template_id") != self.options.expected_template_id:
            raise RestoreFailure("Semaphore queued a task for a different template")
        for _ in range(180):
            task = self._api("GET", f"/api/project/{project_id}/tasks/{task_id}")
            status = task.get("status")
            if status == "success":
                break
            if status in {"error", "stopped", "failed"}:
                raise RestoreFailure(f"restored credential task ended with status {status}")
            time.sleep(1)
        else:
            raise RestoreFailure("restored credential task timed out")
        output = self._api("GET", f"/api/project/{project_id}/tasks/{task_id}/output")
        rendered = "\n".join(str(item.get("output", "")) for item in output)
        if self.options.expected_output not in rendered:
            raise RestoreFailure("restored task output did not contain the expected response")
        environment_after = self._api("GET", environment_path)
        if environment_after.get("secrets", []) != secrets_before:
            raise RestoreFailure("restored credential metadata changed during the drill")

    def _owned(self, kind: str, name: str) -> bool:
        labels = ".Config.Labels" if kind == "container" else ".Labels"
        result = self._podman(
            kind, "inspect", "--format",
            f"{{{{ index {labels} \"{OWNER_LABEL}\" }}}}", name,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == self.run_id

    def _exists(self, kind: str, name: str) -> bool:
        result = self._podman(kind, "exists", name, check=False)
        if result.returncode not in {0, 1}:
            raise RestoreFailure(f"could not determine whether {kind} {name} exists")
        return result.returncode == 0

    def _require_absent(self, kind: str, name: str) -> None:
        if self._exists(kind, name):
            raise RestoreFailure(f"{kind} {name} remains after cleanup")

    def _cleanup(self) -> list[str]:
        failures: list[str] = []
        if self.connected_target:
            try:
                self._podman(
                    "network", "disconnect", "--force", self.network,
                    self.connected_target,
                )
            except Exception as error:
                failures.append(str(error))
        for container in reversed(self.created_containers):
            try:
                if not self._exists("container", container):
                    continue
                if not self._owned("container", container):
                    raise RestoreFailure(f"refusing to remove unowned container {container}")
                self._podman("rm", "--force", container)
                self._require_absent("container", container)
            except Exception as error:  # cleanup must continue after an independent failure
                failures.append(str(error))
        if self.created_volume:
            try:
                if self._exists("volume", self.volume):
                    if not self._owned("volume", self.volume):
                        raise RestoreFailure(f"refusing to remove unowned volume {self.volume}")
                    self._podman("volume", "rm", self.volume)
                    self._require_absent("volume", self.volume)
            except Exception as error:
                failures.append(str(error))
        if self.created_network:
            try:
                if self._exists("network", self.network):
                    if not self._owned("network", self.network):
                        raise RestoreFailure(f"refusing to remove unowned network {self.network}")
                    self._podman("network", "rm", self.network)
                    self._require_absent("network", self.network)
            except Exception as error:
                failures.append(str(error))
        if self.created_destination:
            try:
                marker = self.options.destination / ".semaphore-restore-owner"
                if marker.read_text(encoding="ascii") != self.run_id + "\n":
                    raise RestoreFailure("refusing to remove an unowned restore destination")
                shutil.rmtree(self.options.destination)
                if self.options.destination.exists():
                    raise RestoreFailure("restore destination remains after cleanup")
            except Exception as error:
                failures.append(str(error))
        return failures

    def run(self) -> None:
        self._preflight()
        primary: BaseException | None = None
        try:
            self.options.destination.mkdir(mode=0o700)
            self.created_destination = True
            (self.options.destination / ".semaphore-restore-owner").write_text(
                self.run_id + "\n", encoding="ascii"
            )
            self._write_runtime_env()
            archive = self.options.destination / self.options.archive_id
            archive.mkdir(mode=0o700)
            self._download(archive)
            validate_archive(archive)
            self._check_archive_readability(archive)
            self._create_runtime()
            self._restore_database(archive)
            self._disable_restored_schedules()
            self._start_application()
            self._validate_application()
        except BaseException as error:
            primary = error
        cleanup = self._cleanup()
        if primary is not None or cleanup:
            details = []
            if primary is not None:
                details.append(f"restore failed: {primary}")
            if cleanup:
                details.append("cleanup failed: " + "; ".join(cleanup))
            raise RestoreFailure("; ".join(details)) from primary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--attended", action="store_true")
    parser.add_argument("--archive-id", required=True)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--rclone-config", required=True, type=Path)
    parser.add_argument("--settings-env", required=True, type=Path)
    parser.add_argument("--target")
    parser.add_argument("--confirm-restore-experiment", dest="confirmation")
    parser.add_argument("--fixture-target-url")
    parser.add_argument("--fixture-target-container")
    parser.add_argument("--denied-url")
    parser.add_argument("--expected-project-id", type=int, default=1)
    parser.add_argument("--expected-project-name", default="Recovery fixture")
    parser.add_argument("--expected-template-id", type=int, default=1)
    parser.add_argument("--expected-template-name", default="Recovery credential probe")
    parser.add_argument("--expected-environment-id", type=int, default=1)
    parser.add_argument("--expected-history-id", type=int, default=1)
    parser.add_argument("--expected-key-id", type=int, default=1)
    parser.add_argument("--expected-output", default="RECOVERY_CREDENTIAL_ACCEPTED")
    parser.add_argument("--semaphore-image", default=SEMAPHORE_IMAGE)
    parser.add_argument("--postgres-image", default=POSTGRES_IMAGE)
    parser.add_argument("--rclone-image", default=RCLONE_IMAGE)
    parser.add_argument("--rclone-network")
    return parser


def main() -> int:
    args = _parser().parse_args()
    options = Options(
        archive_id=args.archive_id,
        destination=args.destination,
        remote=args.remote,
        rclone_config=args.rclone_config,
        settings_env=args.settings_env,
        mode="fixture" if args.fixture else "attended",
        target=args.target,
        confirmation=args.confirmation,
        fixture_target_url=args.fixture_target_url,
        fixture_target_container=args.fixture_target_container,
        denied_url=args.denied_url,
        expected_project_id=args.expected_project_id,
        expected_project_name=args.expected_project_name,
        expected_template_id=args.expected_template_id,
        expected_template_name=args.expected_template_name,
        expected_environment_id=args.expected_environment_id,
        expected_history_id=args.expected_history_id,
        expected_key_id=args.expected_key_id,
        expected_output=args.expected_output,
        semaphore_image=args.semaphore_image,
        postgres_image=args.postgres_image,
        rclone_image=args.rclone_image,
        rclone_network=args.rclone_network,
    )
    try:
        RestoreExperiment(options, SubprocessRunner()).run()
    except (ValueError, RestoreFailure, CommandError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Restore experiment accepted archive {options.archive_id}; cleanup verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
