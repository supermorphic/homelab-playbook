"""Real upstream runtime, private initialization and persistence evidence."""

from __future__ import annotations

import base64
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import tempfile
import time
import urllib.error
import urllib.request

from jinja2 import Template

from scripts.forgejo.runtime import Run, defaults


def bootstrap_password(output: str) -> str:
    matched = re.search(r"^generated random password is '([^\r\n]+)'$", output, re.M)
    if matched is None:
        raise ValueError("initial administrator credential was not returned")
    return matched.group(1)


def database_sql(password: str) -> str:
    escaped = password.replace("'", "''")
    return (
        f"CREATE ROLE forgejo LOGIN PASSWORD '{escaped}' "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;\n"
        "CREATE DATABASE forgejo OWNER forgejo;\n"
        "REVOKE ALL ON DATABASE postgres FROM forgejo;\n"
    )


class Application:
    """A synthetic instance; every resource belongs to the supplied experiment."""

    def __init__(self, run: Run, directory: Path, *, suffix: str = "source", auth_dir: Path | None = None):
        self.run = run
        self.directory = directory
        self.suffix = suffix
        self.pins = defaults()
        self.user = "fixture-admin"
        self.password = secrets.token_urlsafe(32)
        self.db_password = secrets.token_urlsafe(32)
        self.network = run.create("network", f"{suffix}-network", [])
        self.database_volume = run.create("volume", f"{suffix}-database", [])
        self.data_volume = run.create("volume", f"{suffix}-data", [])
        self.config = directory / f"{suffix}-config"
        self.config.mkdir(mode=0o700)
        self.postgres_env = run.private_file(directory, f"{suffix}-postgres.env",
            "POSTGRES_USER=postgres\nPOSTGRES_DB=postgres\n"
            f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}\n")
        self.db = run.create("container", f"{suffix}-postgres", [
            "--network", self.network, "--network-alias", "forgejo-postgres",
            "--env-file", str(self.postgres_env),
            "--publish", "127.0.0.1::5432",
            "--volume", f"{self.database_volume}:/var/lib/postgresql/data",
            self.pins["forgejo_postgres_image"], "postgres", "-c", "log_connections=on",
        ])
        run.wait([run.podman, "exec", self.db, "pg_isready", "-U", "postgres"])
        self.sql(database_sql(self.db_password))
        self.ini = self.configuration()
        run.private_file(self.config, "app.ini", self.ini)
        extra_volumes = ["--volume", f"{auth_dir}:/test-auth:ro,U"] if auth_dir else []
        self.app = run.create("container", f"{suffix}-app", [
            "--userns=keep-id:uid=1000,gid=1000", "--user", "1000:1000",
            *extra_volumes,
            "--network", self.network,
            "--env", "GITEA_APP_INI=/etc/gitea/app.ini",
            "--publish", "127.0.0.1::3000",
            "--volume", f"{self.data_volume}:/var/lib/gitea:U",
            "--volume", f"{self.config}:/etc/gitea:U",
            self.pins["forgejo_image"],
        ])
        published = run.command([run.podman, "port", self.app, "3000/tcp"]).stdout.strip()
        port = published.rsplit(":", 1)[-1]
        if not port.isdigit():
            raise RuntimeError("application loopback port was not assigned")
        self.url = f"http://127.0.0.1:{port}"
        self.wait()

    def configuration(self) -> str:
        from scripts.forgejo.runtime import ROOT
        values = {
            "forgejo_hostname": "forgejo.infra.example.com",
            "forgejo_proxy_source": "127.0.0.1/32",
        }
        keys = {
            "database-password": self.db_password,
            "secret-key": secrets.token_hex(32),
            "internal-token": secrets.token_urlsafe(64),
            "lfs-jwt-secret": secrets.token_urlsafe(32),
            "oauth2-jwt-secret": secrets.token_urlsafe(32),
        }
        for name, value in keys.items():
            self.run.private_file(self.config, name, value)
        template = ROOT / "roles/forgejo/templates/app.ini.j2"
        return Template(template.read_text()).render(**values) + "\n"

    def sql(self, text: str) -> str:
        return self.run.command([
            self.run.podman, "exec", "-i", self.db, "psql", "-X", "-U", "postgres",
            "--set", "ON_ERROR_STOP=1", "--tuples-only", "--no-align",
        ], input_text=text).stdout.strip()

    def cli(self, *arguments: str) -> str:
        return self.run.command([
            self.run.podman, "exec", self.app, "forgejo", "--config",
            "/etc/gitea/app.ini", *arguments,
        ]).stdout

    def request(self, path: str, *, method: str = "GET", payload: dict | None = None,
                password: str | None = None):
        headers = {"Content-Type": "application/json"}
        credential = f"{self.user}:{password or self.password}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(credential).decode()
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.url + "/api/v1" + path,
                                         data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"application API returned HTTP {error.code}") from error

    def wait(self):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.url + "/api/healthz", timeout=5) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise RuntimeError("application HTTP readiness deadline exceeded")

    def initialize_admin(self):
        output = self.cli("admin", "user", "create", "--username", self.user,
                          "--email", "fixture@example.invalid", "--admin",
                          "--random-password", "--random-password-length", "32",
                          "--must-change-password=false")
        initial = bootstrap_password(output)
        self.request(f"/admin/users/{self.user}", method="PATCH", password=initial,
                     payload={"email": "fixture@example.invalid",
                              "password": self.password, "must_change_password": False})
        if not self.request("/user")["is_admin"]:
            raise RuntimeError("initial administrator authentication failed")


def run() -> int:
    experiment = Run()
    error = None
    phase = "create application"
    try:
        with tempfile.TemporaryDirectory(prefix="forgejo-compat-", dir=temp_root()) as scratch:
            application = Application(experiment, Path(scratch))
            phase = "database role privileges"
            flags = application.sql("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication "
                                    "FROM pg_roles WHERE rolname='forgejo';")
            if flags != "f|f|f|f":
                raise RuntimeError("application database role has excessive authority")
            denied = experiment.command([
                experiment.podman, "exec", application.db, "psql", "-X", "-U",
                "forgejo", "-d", "forgejo", "--set", "ON_ERROR_STOP=1", "-c",
                "CREATE DATABASE unrelated_fixture;",
            ], check=False)
            if denied.returncode == 0:
                raise RuntimeError("application role created an unrelated database")
            if not application.sql("SHOW server_version;").startswith("17."):
                raise RuntimeError("database did not report PostgreSQL major 17")
            phase = "protected administrator bootstrap"
            application.initialize_admin()
            phase = "persistent repository and application state"
            repo = application.request("/user/repos", method="POST",
                payload={"name": "compatibility", "auto_init": True, "private": True})
            application.request(f"/repos/{application.user}/compatibility/issues",
                                method="POST", payload={"title": "persisted fixture"})
            before = application.request(f"/repos/{application.user}/compatibility/branches")
            experiment.command([experiment.podman, "restart", application.app])
            application.wait()
            restored = application.request(f"/repos/{application.user}/compatibility")
            if repo["id"] != restored["id"] or not application.request(
                    f"/repos/{application.user}/compatibility/issues"):
                raise RuntimeError("application state did not persist across restart")
            after = application.request(f"/repos/{application.user}/compatibility/branches")
            if not before or [(b["name"], b["commit"]["id"]) for b in before] != [
                    (b["name"], b["commit"]["id"]) for b in after]:
                raise RuntimeError("Git branch contents did not persist across restart")
            phase = "Git and archive tools"
            application.cli("--version")
            experiment.command([experiment.podman, "exec", application.app,
                                "git", "--version"])
            experiment.command([experiment.podman, "exec", application.db, "/bin/sh", "-c",
                "pg_dump --version && pg_restore --version && command -v tar && command -v gzip && command -v sha256sum"])
            phase = "rootless forwarded client identity"
            probe_forwarded_source(experiment, application, Path(scratch))
    except (OSError, RuntimeError, ValueError, KeyError) as failure:
        error = f"{phase}: {failure}"
    finally:
        cleanup = experiment.cleanup()
        experiment.evidence(error, cleanup)
    if error or cleanup:
        print(json.dumps({"operation_error": error, "cleanup_errors": cleanup}))
        return 1
    print("Forgejo/PostgreSQL compatibility and protected initialization passed")
    return 0


def temp_root() -> Path:
    from scripts.forgejo.runtime import ROOT
    directory = ROOT / ".tmp/forgejo"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def probe_forwarded_source(experiment: Run, application: Application, directory: Path):
    """Measure rootless forwarding; the production address is an operator input."""
    port = experiment.command([experiment.podman, "port", application.db, "5432/tcp"]).stdout.strip().rsplit(":", 1)[-1]
    deadline = time.monotonic() + 30
    while True:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=5) as connection:
                connection.sendall(b"0000")
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("forwarding probe readiness failed") from None
            time.sleep(1)
    while True:
        logs = experiment.command([experiment.podman, "logs", application.db])
        source = re.findall(r"connection received: host=([0-9.]+) port=", logs.stdout + logs.stderr)
        if source:
            address = str(ipaddress.ip_address(source[-1]))
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("forwarded client identity was not recorded")
        time.sleep(0.1)
    experiment.private_file(directory, "forwarded-source.txt", address + "\n")
