#!/usr/bin/env python3
"""Run the disposable backup, SMB transfer, and restore fixture."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BACKUP_SCRIPT = ROOT / "roles" / "semaphore" / "files" / "backup.sh"
TRANSFER_SCRIPT = ROOT / "roles" / "semaphore" / "files" / "transfer.sh"
SAMBA_IMAGE = (
    "ghcr.io/servercontainers/samba:smbd-only-a3.24.1-s4.23.8-r0@"
    "sha256:0b8ce469f097a1afdd4f286c4313dae52ce18aad6a7e3b25ee3d93fd09200866"
)
TASK_OUTPUT_LINES = 20
TASK_OUTPUT_CHARACTERS = 4000


def _load_restore():
    spec = importlib.util.spec_from_file_location("semaphore_restore", HERE / "restore.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load restore implementation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_payloads(
    project_id: int,
    environment_id: int,
    repository_id: int,
    username: str,
    password: str,
) -> dict[str, dict]:
    """Return the native v2.19.12 records used by the fixture."""
    return {
        "environment": {
            "name": "Recovery credentials",
            "project_id": project_id,
            "json": "{}",
            "env": "{}",
            "secrets": [
                {"name": "RECOVERY_USERNAME", "secret": username, "type": "env", "operation": "create"},
                {"name": "RECOVERY_PASSWORD", "secret": password, "type": "env", "operation": "create"},
            ],
        },
        "template": {
            "project_id": project_id,
            "name": "Recovery credential probe",
            "app": "bash",
            "playbook": "probe.sh",
            "arguments": "[]",
            "repository_id": repository_id,
            "environment_ids": [environment_id],
            "allow_override_args_in_task": False,
            "allow_override_branch_in_task": False,
            "allow_parallel_tasks": False,
        },
    }


class FixtureFailure(RuntimeError):
    pass


class Fixture:
    def __init__(self, archive_id: str, destination: Path):
        self.restore = _load_restore()
        self.restore.validate_archive_identifier(archive_id)
        self.archive_id = archive_id
        self.destination = destination
        self.run_id = secrets.token_hex(6)
        self.owner_label = f"io.homelab.semaphore.fixture-run={self.run_id}"
        self.network = f"semaphore-fixture-net-{self.run_id}"
        self.denied_network = f"semaphore-fixture-denied-{self.run_id}"
        self.database = f"semaphore-fixture-db-{self.run_id}"
        self.application = f"semaphore-fixture-app-{self.run_id}"
        self.target = f"semaphore-fixture-target-{self.run_id}"
        self.denied = f"semaphore-fixture-managed-{self.run_id}"
        self.samba = f"semaphore-fixture-smb-{self.run_id}"
        self.volume = f"semaphore-fixture-data-{self.run_id}"
        self.created: list[tuple[str, str]] = []
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None
        self.settings: dict[str, str] = {}
        self.private_values: set[str] = set()
        self.cookie_config: Path | None = None
        self.oneshot_counter = 0

    def command(self, *args: str, input_text: str | None = None, check: bool = True):
        result = subprocess.run(
            list(args), input=input_text, text=True, capture_output=True,
            check=False, timeout=300,
        )
        if check and result.returncode != 0:
            parts = (result.stderr.strip(), result.stdout.strip())
            detail = " | ".join(part for part in parts if part) or "command failed"
            raise FixtureFailure(f"{args[0]} ({args[-1]}) failed: {detail}")
        return result

    def podman(self, *args: str, **kwargs):
        command = list(args)
        if command and command[0] == "run" and "--rm" in command and "--name" not in command:
            self.oneshot_counter += 1
            name = f"semaphore-fixture-step-{self.run_id}-{self.oneshot_counter}"
            command[1:1] = ["--name", name, "--label", self.owner_label]
            self._remember("container", name)
        return self.command("podman", *command, **kwargs)

    def _remember(self, kind: str, name: str) -> None:
        self.created.append((kind, name))

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    def _prepare_files(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semaphore-restore-fixture-")
        self.root = Path(self.temp.name)
        for name in ("backups", "nas", "repository"):
            (self.root / name).mkdir(mode=0o700)
        username = "fixture-user"
        password = secrets.token_urlsafe(24)
        database_password = secrets.token_urlsafe(24)
        admin_password = secrets.token_urlsafe(24)
        self.settings = {
            "POSTGRES_USER": "semaphore",
            "POSTGRES_PASSWORD": database_password,
            "POSTGRES_DB": "semaphore",
            "SEMAPHORE_DB_USER": "semaphore",
            "SEMAPHORE_DB_PASS": database_password,
            "SEMAPHORE_DB_NAME": "semaphore",
            "SEMAPHORE_ADMIN": "fixture-admin",
            "SEMAPHORE_ADMIN_PASSWORD": admin_password,
            "SEMAPHORE_ADMIN_NAME": "Fixture Admin",
            "SEMAPHORE_ADMIN_EMAIL": "fixture@example.invalid",
            "SEMAPHORE_COOKIE_HASH": base64.b64encode(os.urandom(32)).decode("ascii"),
            "SEMAPHORE_COOKIE_ENCRYPTION": base64.b64encode(os.urandom(32)).decode("ascii"),
            "SEMAPHORE_ACCESS_KEY_ENCRYPTION": base64.b64encode(os.urandom(32)).decode("ascii"),
            "SEMAPHORE_APPS": json.dumps({"bash": {"active": True}}),
        }
        basic_auth = f"{username}:{password}"
        self.private_values.update(
            {
                password,
                database_password,
                admin_password,
                self.settings["SEMAPHORE_COOKIE_HASH"],
                self.settings["SEMAPHORE_COOKIE_ENCRYPTION"],
                self.settings["SEMAPHORE_ACCESS_KEY_ENCRYPTION"],
                basic_auth,
                base64.b64encode(basic_auth.encode("utf-8")).decode("ascii"),
            }
        )
        self._write_private(
            self.root / "settings.env",
            "".join(f"{key}={value}\n" for key, value in self.settings.items()),
        )
        self._write_private(
            self.root / "postgres.env",
            "".join(f"{key}={self.settings[key]}\n" for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"))
            + f"PGUSER={self.settings['POSTGRES_USER']}\n"
            + f"PGPASSWORD={self.settings['POSTGRES_PASSWORD']}\n"
            + f"PGDATABASE={self.settings['POSTGRES_DB']}\n",
        )
        app = {key: value for key, value in self.settings.items() if key.startswith("SEMAPHORE_")}
        app.update({"SEMAPHORE_DB_DIALECT": "postgres", "SEMAPHORE_DB_HOST": "source-db", "SEMAPHORE_DB_PORT": "5432"})
        self._write_private(self.root / "semaphore.env", "".join(f"{key}={value}\n" for key, value in app.items()))
        self._write_private(self.root / "target.env", f"RECOVERY_USERNAME={username}\nRECOVERY_PASSWORD={password}\n")
        server = '''import base64, http.server, os\nfrom pathlib import Path\nclass Handler(http.server.SimpleHTTPRequestHandler):\n    def do_GET(self):\n        if self.path == "/probe":\n            want = "Basic " + base64.b64encode((os.environ["RECOVERY_USERNAME"] + ":" + os.environ["RECOVERY_PASSWORD"]).encode()).decode()\n            if self.headers.get("Authorization") != want:\n                self.send_response(401); self.end_headers(); return\n            self.send_response(200); self.end_headers(); self.wfile.write(b"RECOVERY_CREDENTIAL_ACCEPTED\\n"); return\n        super().do_GET()\nhttp.server.ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()\n'''
        (self.root / "server.py").write_text(server, encoding="utf-8")
        probe = '''#!/bin/sh\nset -eu\ncfg=$(mktemp)\ntrap 'rm -f "$cfg"' EXIT HUP INT TERM\numask 077\nprintf 'user = "%s:%s"\\n' "$RECOVERY_USERNAME" "$RECOVERY_PASSWORD" > "$cfg"\ncurl --fail --silent --show-error --config "$cfg" http://fixture-target:8000/probe\n'''
        repo = self.root / "repository"
        (repo / "probe.sh").write_text(probe, encoding="utf-8")
        (repo / "probe.sh").chmod(0o755)
        self.command("git", "init", "--initial-branch=main", str(repo))
        self.command("git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "add", "probe.sh")
        self.command("git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
        self.command("git", "clone", "--bare", str(repo), str(self.root / "www" / "recovery.git"))
        self.command("git", "--git-dir", str(self.root / "www" / "recovery.git"), "update-server-info")

    def _create_source(self) -> None:
        assert self.root is not None
        for network in (self.network, self.denied_network):
            self._remember("network", network)
            self.podman("network", "create", "--internal", "--label", self.owner_label, network)
        self._remember("volume", self.volume)
        self.podman("volume", "create", "--label", self.owner_label, self.volume)
        self._remember("container", self.target)
        self.podman("run", "-d", "--name", self.target, "--label", self.owner_label,
                    "--label", "io.homelab.semaphore.fixture=true", "--network", self.network,
                    "--network-alias", "fixture-target", "--env-file", str(self.root / "target.env"),
                    "-v", f"{self.root / 'server.py'}:/server.py:ro", "-v", f"{self.root / 'www'}:/www:ro",
                    "--workdir", "/www", "--entrypoint", "/opt/semaphore/apps/ansible/13.5.0/venv/bin/python3",
                    self.restore.SEMAPHORE_IMAGE, "/server.py")
        self._remember("container", self.denied)
        self.podman("run", "-d", "--name", self.denied, "--label", self.owner_label,
                    "--network", self.denied_network, "--network-alias", "managed-host",
                    "--env-file", str(self.root / "target.env"),
                    "-v", f"{self.root / 'server.py'}:/server.py:ro", "-v", f"{self.root / 'www'}:/www:ro",
                    "--workdir", "/www", "--entrypoint", "/opt/semaphore/apps/ansible/13.5.0/venv/bin/python3",
                    self.restore.SEMAPHORE_IMAGE, "/server.py")
        for _ in range(30):
            oracle = self.podman(
                "run", "--rm", "--network", self.denied_network,
                "--entrypoint", "/usr/bin/curl", self.restore.SEMAPHORE_IMAGE,
                "--fail", "--silent", "http://managed-host:8000/", check=False,
            )
            if oracle.returncode == 0:
                break
            time.sleep(1)
        else:
            raise FixtureFailure("managed-host reachability oracle failed before isolation")
        self._remember("container", self.database)
        self.podman("run", "-d", "--name", self.database, "--label", self.owner_label,
                    "--network", self.network, "--network-alias", "source-db",
                    "--env-file", str(self.root / "postgres.env"), "-v", f"{self.volume}:/var/lib/postgresql/data:rw",
                    self.restore.POSTGRES_IMAGE)
        for _ in range(60):
            if self.podman("exec", self.database, "pg_isready", "-q", "-U", "semaphore", check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            raise FixtureFailure("source PostgreSQL readiness timed out")
        self._remember("container", self.application)
        self.podman("run", "-d", "--name", self.application, "--label", self.owner_label,
                    "--network", self.network, "--network-alias", "source-semaphore",
                    "--env-file", str(self.root / "semaphore.env"), self.restore.SEMAPHORE_IMAGE)
        for _ in range(90):
            if self._curl("--fail", "http://source-semaphore:3000/api/ping", check=False).returncode == 0:
                return
            time.sleep(1)
        raise FixtureFailure("source Semaphore readiness timed out")

    def _curl(self, *args: str, input_text: str | None = None, check: bool = True):
        assert self.root is not None
        mounts: list[str] = []
        if self.cookie_config and self.cookie_config.exists():
            mounts = ["-v", f"{self.cookie_config}:/fixture-auth.conf:ro"]
        return self.podman("run", "--rm", "--interactive", "--network", self.network,
                           "--security-opt=no-new-privileges", "--cap-drop=all", "--user", "0:0",
                           *mounts, "--entrypoint", "/usr/bin/curl", self.restore.SEMAPHORE_IMAGE,
                           "--silent", "--show-error", *args, input_text=input_text, check=check)

    def _api(self, method: str, path: str, payload: dict | None = None):
        args = ["--fail", "--config", "/fixture-auth.conf"]
        if method != "GET": args += ["--request", method]
        body = None
        if payload is not None:
            args += ["--header", "Content-Type: application/json", "--data-binary", "@-"]
            body = json.dumps(payload)
        try:
            result = self._curl(*args, f"http://source-semaphore:3000{path}", input_text=body)
        except FixtureFailure as error:
            raise FixtureFailure(f"Semaphore API {method} {path} failed: {error}") from error
        return json.loads(result.stdout) if result.stdout.strip() else None

    def _seed(self) -> tuple[int, int, int, int, int, int]:
        assert self.root is not None
        login = self._curl("--fail-with-body", "--dump-header", "-", "--request", "POST", "--header", "Content-Type: application/json", "--data-binary", "@-", "http://source-semaphore:3000/api/auth/login", input_text=json.dumps({"auth": self.settings["SEMAPHORE_ADMIN"], "password": self.settings["SEMAPHORE_ADMIN_PASSWORD"]}))
        cookie = next((line.split(":", 1)[1].strip().split(";", 1)[0] for line in login.stdout.splitlines() if line.lower().startswith("set-cookie:")), None)
        if not cookie: raise FixtureFailure("fixture login returned no cookie")
        self.private_values.add(cookie)
        if "=" in cookie:
            cookie_value = cookie.split("=", 1)[1]
            if cookie_value:
                self.private_values.add(cookie_value)
        self.cookie_config = self.root / "api.conf"
        self._write_private(self.cookie_config, f"cookie = {json.dumps(cookie)}\n")
        project = self._api("POST", "/api/projects", {"name": "Recovery fixture", "alert": False, "max_parallel_tasks": 1})
        project_id = project["id"]
        no_key = self._api("POST", f"/api/project/{project_id}/keys", {"name": "Repository none", "project_id": project_id, "type": "none"})
        repository = self._api("POST", f"/api/project/{project_id}/repositories", {"name": "Recovery repository", "project_id": project_id, "git_url": "http://fixture-target:8000/recovery.git", "git_branch": "main", "ssh_key_id": no_key["id"]})
        creds = {"user": "fixture-user", "password": (self.root / "target.env").read_text().split("RECOVERY_PASSWORD=", 1)[1].strip()}
        env_payload = seed_payloads(project_id, 0, repository["id"], creds["user"], creds["password"])["environment"]
        environment = self._api("POST", f"/api/project/{project_id}/environment", env_payload)
        payload = seed_payloads(project_id, environment["id"], repository["id"], "", "")["template"]
        template = self._api("POST", f"/api/project/{project_id}/templates", payload)
        task = self._api("POST", f"/api/project/{project_id}/tasks", {"template_id": template["id"]})
        self._wait_task(project_id, task["id"])
        environment = self._api("GET", f"/api/project/{project_id}/environment/{environment['id']}")
        secret_ids = [item["id"] for item in environment["secrets"]]
        if len(secret_ids) != 2: raise FixtureFailure("native environment secrets were not stored")
        return project_id, repository["id"], environment["id"], template["id"], task["id"], secret_ids[0]

    def _wait_task(self, project: int, task: int) -> None:
        for _ in range(180):
            record = self._api("GET", f"/api/project/{project}/tasks/{task}")
            if record["status"] == "success": return
            if record["status"] in {"error", "stopped", "failed"}:
                detail = self._recent_task_output(project, task)
                raise FixtureFailure(
                    f"source task failed: {record['status']}; recent task output:\n{detail}"
                )
            time.sleep(1)
        raise FixtureFailure("source fixture task timed out")

    def _recent_task_output(self, project: int, task: int) -> str:
        try:
            records = self._api(
                "GET", f"/api/project/{project}/tasks/{task}/output"
            )
        except (FixtureFailure, TypeError, ValueError):
            return "task output unavailable"
        if not isinstance(records, list):
            return "task output unavailable"
        lines = [
            str(record.get("output", "")).strip()
            for record in records[-TASK_OUTPUT_LINES:]
            if isinstance(record, dict) and str(record.get("output", "")).strip()
        ]
        detail = "\n".join(lines) or "task output unavailable"
        for value in sorted(self.private_values, key=len, reverse=True):
            detail = detail.replace(value, "<redacted>")
        return detail[-TASK_OUTPUT_CHARACTERS:]

    def _backup_and_transfer(self) -> None:
        assert self.root is not None
        self.podman("run", "--rm", "--network", self.network, "--env-file", str(self.root / "postgres.env"),
                    "-e", "PGHOST=source-db", "-e", "BACKUP_ROOT=/backups",
                    "-v", f"{self.root / 'backups'}:/backups:rw", "-v", f"{BACKUP_SCRIPT}:/backup.sh:ro",
                    "--entrypoint", "/bin/sh", self.restore.POSTGRES_IMAGE, "/backup.sh")
        archives = [path for path in (self.root / "backups").iterdir() if path.is_dir()]
        if len(archives) != 1: raise FixtureFailure("backup did not create exactly one archive")
        archives[0].rename(self.root / "backups" / self.archive_id)
        selected = self.root / "backups" / self.archive_id
        for archive_name in (
            f"semaphore-20260908T120000Z-{self.run_id}",
            f"semaphore-20260909T120000Z-{self.run_id}",
        ):
            if archive_name != self.archive_id:
                shutil.copytree(selected, self.root / "backups" / archive_name)
        smb_password = secrets.token_urlsafe(20)
        samba_env = self.root / "samba.env"
        self._write_private(samba_env, f"ACCOUNT_fixture={smb_password}\nUID_fixture=1000\nAVAHI_DISABLE=1\nWSDD2_DISABLE=1\nNETBIOS_DISABLE=1\nSAMBA_VOLUME_CONFIG_semaphore=[semaphore]; path=/shares/semaphore; valid users = fixture; guest ok = no; read only = no; browsable = yes\n")
        obscured = self.podman("run", "--rm", "--interactive", "--network=none", "--entrypoint", "rclone", self.restore.RCLONE_IMAGE, "obscure", "-", input_text=smb_password + "\n").stdout.strip()
        config = self.root / "rclone.conf"
        self._write_private(config, f"[nas]\ntype = smb\nhost = nas\nuser = fixture\npass = {obscured}\n")
        transfer_base = [
            "run", "--rm", "--network", self.network,
            "-e", "BACKUP_ROOT=/backups", "-e", "RCLONE_REMOTE=nas:semaphore",
            "-e", "LOCAL_RETENTION_DAYS=99999", "-e", "REMOTE_RETENTION_DAYS=99999",
            "-v", f"{self.root / 'backups'}:/backups:rw",
            "-v", f"{config}:/config/rclone/rclone.conf:ro",
            "-v", f"{TRANSFER_SCRIPT}:/transfer.sh:ro",
            "--entrypoint", "/bin/sh", self.restore.RCLONE_IMAGE,
        ]
        outage = self.podman(*transfer_base, "-c", "timeout 8 /transfer.sh", check=False)
        if outage.returncode == 0:
            raise FixtureFailure("transfer unexpectedly succeeded while SMB was absent")
        self._remember("container", self.samba)
        # The authenticated SMB account must own the private host bind mount.
        # Keep the entrypoint root so it can create that account and start smbd.
        self.podman("run", "-d", "--name", self.samba, "--label", self.owner_label,
                    "--userns", "keep-id:uid=1000,gid=1000", "--user", "0:0",
                    "--network", self.network, "--network-alias", "nas", "--env-file", str(samba_env),
                    "-v", f"{self.root / 'nas'}:/shares/semaphore:rw", SAMBA_IMAGE)
        for _ in range(60):
            ready = self.podman("run", "--rm", "--network", self.network, "-v", f"{config}:/config/rclone/rclone.conf:ro", self.restore.RCLONE_IMAGE, "lsd", "nas:semaphore", check=False)
            if ready.returncode == 0: break
            time.sleep(1)
        else: raise FixtureFailure("SMB fixture readiness timed out")
        self.podman(*transfer_base, "/transfer.sh")
        complete = list((self.root / "nas").rglob("COMPLETE"))
        if len(complete) != len(list((self.root / "backups").iterdir())):
            raise FixtureFailure("SMB recovery did not transfer the complete archive backlog")
        instrument = self.root / "instrument"
        instrument.mkdir()
        trace = instrument / "commands"
        wrapper = instrument / "rclone"
        wrapper.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> /trace/commands\nexec /usr/local/bin/rclone "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        second = transfer_base.copy()
        second[second.index("--entrypoint"):second.index("--entrypoint")] = [
            "-e", "PATH=/instrument:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "-v", f"{instrument}:/instrument:ro", "-v", f"{instrument}:/trace:rw",
        ]
        self.podman(*second, "/transfer.sh")
        commands = trace.read_text(encoding="utf-8")
        if any(
            line.startswith("copyto ")
            or (line.startswith("cat ") and line.endswith("/database.dump"))
            for line in commands.splitlines()
        ):
            raise FixtureFailure("unchanged second transfer accessed a remote database dump")

    def _cleanup(self) -> list[str]:
        failures: list[str] = []
        for kind, name in reversed(self.created):
            try:
                exists = self.podman(kind, "exists", name, check=False)
                if exists.returncode == 1:
                    continue
                if exists.returncode != 0:
                    raise FixtureFailure(f"could not determine whether {kind} {name} exists")
                label_path = ".Config.Labels" if kind == "container" else ".Labels"
                inspected = self.podman(kind, "inspect", "--format", f'{{{{ index {label_path} "io.homelab.semaphore.fixture-run" }}}}', name)
                if inspected.stdout.strip() != self.run_id: raise FixtureFailure(f"refusing to remove unowned {kind} {name}")
                command = ("rm", "-f", name) if kind == "container" else (kind, "rm", name)
                self.podman(*command)
                absent = self.podman(kind, "exists", name, check=False)
                if absent.returncode != 1:
                    raise FixtureFailure(f"{kind} {name} absence could not be verified")
            except Exception as error: failures.append(str(error))
        if self.temp: self.temp.cleanup()
        return failures

    def run(self) -> None:
        if self.destination.exists() or self.destination.is_symlink(): raise ValueError("destination must not exist")
        primary: BaseException | None = None
        try:
            self._prepare_files(); self._create_source()
            project, _repository, environment, template, history, key = self._seed()
            self._backup_and_transfer()
            assert self.root is not None
            options = self.restore.Options(
                archive_id=self.archive_id, destination=self.destination, remote="nas:semaphore",
                rclone_config=self.root / "rclone.conf", settings_env=self.root / "settings.env",
                mode="fixture", target=None, confirmation=None,
                fixture_target_url="http://fixture-target:8000/probe", fixture_target_container=self.target,
                denied_url="http://managed-host:8000/", expected_project_id=project,
                expected_project_name="Recovery fixture", expected_template_id=template,
                expected_template_name="Recovery credential probe", expected_environment_id=environment,
                expected_history_id=history, expected_key_id=key,
                expected_output="RECOVERY_CREDENTIAL_ACCEPTED", semaphore_image=self.restore.SEMAPHORE_IMAGE,
                postgres_image=self.restore.POSTGRES_IMAGE, rclone_image=self.restore.RCLONE_IMAGE,
                rclone_network=self.network,
            )
            self.restore.RestoreExperiment(options, self.restore.SubprocessRunner()).run()
        except BaseException as error: primary = error
        cleanup = self._cleanup()
        if primary is not None or cleanup:
            details = ([f"fixture failed: {primary}"] if primary is not None else []) + (["cleanup failed: " + "; ".join(cleanup)] if cleanup else [])
            raise FixtureFailure("; ".join(details)) from primary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-id")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    if (args.archive_id is None) != (args.destination is None):
        parser.error("--archive-id and --destination must be supplied together")
    if args.archive_id is not None:
        archive_id = args.archive_id
        destination = args.destination
        assert destination is not None
        temporary = None
    else:
        temporary = tempfile.TemporaryDirectory(prefix="semaphore-restore-result-")
        archive_id = "semaphore-{}-{}".format(
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            secrets.token_hex(4),
        )
        destination = Path(temporary.name) / "restored"
    try:
        Fixture(archive_id, destination).run()
    except (ValueError, FixtureFailure) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.cleanup()
    print(f"Disposable restore fixture accepted archive {archive_id}; cleanup verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
