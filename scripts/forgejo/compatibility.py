"""Real upstream runtime, private initialization and persistence evidence."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import re
import secrets
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

    def __init__(self, run: Run, directory: Path, *, suffix: str = "source", auth_dir: Path | None = None,
                 extra_environment: tuple[str, ...] = (), additional_volumes: tuple[str, ...] = (),
                 access_log: bool = False):
        self.run = run
        self.directory = directory
        self.suffix = suffix
        self.access_log = access_log
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
            "--volume", f"{self.database_volume}:/var/lib/postgresql/data",
            self.pins["forgejo_postgres_image"],
        ])
        # The initialization server accepts Unix sockets before the final server starts.
        run.wait([run.podman, "exec", self.db, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"])
        self.sql(database_sql(self.db_password))
        self.ini = self.configuration()
        run.private_file(self.config, "app.ini", self.ini)
        extra_volumes = ["--volume", f"{auth_dir}:/test-auth:ro,U"] if auth_dir else []
        self.app = run.create("container", f"{suffix}-app", [
            "--userns=keep-id:uid=1000,gid=1000", "--user", "1000:1000",
            *extra_volumes,
            *[argument for value in extra_environment for argument in ('--env', value)],
            *[argument for value in additional_volumes for argument in ('--volume', value)],
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
        self.run.private_file(self.config, 'mirror-credentials', '')
        self.run.private_file(self.config, 'mirror-credential-helper.sh',
            (ROOT / 'roles/forgejo/files/mirror-credential-helper.sh').read_text()).chmod(0o700)
        template = ROOT / "roles/forgejo/templates/app.ini.j2"
        configuration = Template(template.read_text()).render(**values) + "\n"
        if self.access_log:
            # Synthetic-only evidence of the application's effective client identity.
            configuration = configuration.replace('LEVEL = Warn', 'LEVEL = Info')
            configuration += ('LOGGER_ACCESS_MODE = console\n'
                'ACCESS_LOG_TEMPLATE = forgejo-proxy-probe={{.Ctx.Req.URL.RawQuery}} peer={{.Ctx.RemoteHost}}\n')
        return configuration

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
                password: str | None = None, extra_headers: dict | None = None):
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers or {})
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
        # A stop/start can reallocate a randomly assigned fixture host port.
        published = self.run.command([self.run.podman, 'port', self.app, '3000/tcp']).stdout.strip()
        port = published.rsplit(':', 1)[-1]
        if not port.isdigit():
            raise RuntimeError('application loopback port was not assigned')
        self.url = 'http://127.0.0.1:' + port
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
            application = Application(experiment, Path(scratch), access_log=True)
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
            if repo['clone_url'] != f'https://forgejo.infra.example.com/{application.user}/compatibility.git':
                raise RuntimeError('repository clone URL differs from the canonical HTTPS URL')
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
            phase = "forwarded-header isolation"
            from scripts.forgejo.proxy import verify_headers
            verify_headers(experiment, application)
            phase = 'native HTTPS nightly mirror experiment'
            from scripts.forgejo.mirror import run_fixture
            run_fixture(experiment)
    except (OSError, RuntimeError, ValueError, KeyError) as failure:
        error = f"{phase}: {failure}"
    finally:
        cleanup = experiment.cleanup()
        experiment.evidence(error, cleanup)
    if error or cleanup:
        print(json.dumps({"operation_error": error, "cleanup_errors": cleanup}))
        return 1
    print("Forgejo/PostgreSQL compatibility, protected initialization and native HTTPS mirroring passed")
    return 0


def temp_root() -> Path:
    from scripts.forgejo.runtime import ROOT
    directory = ROOT / ".tmp/forgejo"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
