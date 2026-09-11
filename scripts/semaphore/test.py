#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".tmp" / "semaphore" / "runtime-evidence.md"
SEMAPHORE_IMAGE = "docker.io/semaphoreui/semaphore@sha256:3996804607ebb63690528185bb9adc3507ee896851098e4453975f2ce7f8b435"
POSTGRES_IMAGE = "docker.io/library/postgres@sha256:67f41722b7a8cbdb868a44a4995c846eddfdc2973bccb291ce937dce88ad5675"
RCLONE_IMAGE = "docker.io/rclone/rclone@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5"
MISE_VERSION = "2026.9.4"
DEFAULT_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 600
BOOTSTRAP_TIMEOUT = 900
CLEANUP_TIMEOUT = 30
HEALTH_ATTEMPTS = 45
PROVENANCE = {
    "Semaphore": "https://hub.docker.com/v2/repositories/semaphoreui/semaphore/tags/v2.19.12",
    "PostgreSQL": "https://hub.docker.com/v2/repositories/library/postgres/tags/17.11",
    "rclone": "https://hub.docker.com/v2/repositories/rclone/rclone/tags/1.75.1",
    "Mise": "https://api.github.com/repos/jdx/mise/releases/tags/v2026.9.4",
}
MISE_ASSETS = {
    "aarch64": (
        "arm64",
        "82d10b29ac668b31f937b788fd99feb305fc9f2f4955825baf57d714aac2e25d",
    ),
    "x86_64": (
        "x64",
        "4c53429e230c07588ae810b3b37f1541f1a763a1f6ac3a71c3fc98904ac3c333",
    ),
}
FIXTURE_FILES = (
    ".mise.toml",
    "mise.lock",
    "pyproject.toml",
    "uv.lock",
    "requirements.yml",
    "scripts/bootstrap.sh",
    "scripts/dependencies.py",
    "scripts/galaxy_dependencies.py",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run disposable Semaphore tests")
    parser.add_argument(
        "mode",
        choices=("unit", "compatibility", "fixture", "controller", "restore"),
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def _unit() -> int:
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests/semaphore",
                "-p",
                "test_*.py",
                "-v",
            ],
            cwd=ROOT,
            check=False,
            timeout=BOOTSTRAP_TIMEOUT,
        ).returncode
    except subprocess.TimeoutExpired:
        print("Semaphore unit tests timed out", file=sys.stderr)
        return 124


def _bounded_child(
    argv: list[str],
    label: str,
    *,
    timeout: float = BOOTSTRAP_TIMEOUT,
    env: dict[str, str] | None = None,
) -> int:
    process = subprocess.Popen(
        argv,
        cwd=ROOT,
        env=env,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"{label} timed out; interrupting it for cleanup", file=sys.stderr)
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=CLEANUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(
                f"{label} cleanup did not finish; forcing process-group termination",
                file=sys.stderr,
            )
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        return 124


def _fixture_test() -> int:
    return _bounded_child(
        [sys.executable, str(ROOT / "scripts" / "semaphore" / "fixture.py")],
        "Semaphore restore fixture",
    )


def _controller() -> int:
    environment = os.environ.copy()
    environment["SEMAPHORE_REPOSITORY_JOB_INTEGRATION"] = "1"
    environment.setdefault(
        "SEMAPHORE_REPOSITORY_JOB_TEST_IMAGE",
        "localhost/homelab-playbook-semaphore-debian13:local",
    )
    return _bounded_child(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests/semaphore",
            "-p",
            "test_repository_job_integration.py",
            "-v",
        ],
        "Semaphore controller integration",
        env=environment,
    )


def _restore(arguments: list[str]) -> int:
    return _bounded_child(
        [
            sys.executable,
            str(ROOT / "scripts" / "semaphore" / "restore.py"),
            *arguments,
        ],
        "Semaphore restore experiment",
    )


def _required_shell(*commands: str) -> str:
    return "set -eu\n" + "\n".join(commands)


class CompatibilityRun:
    def __init__(self) -> None:
        self.prefix = f"homelab-semaphore-compat-{uuid.uuid4().hex[:12]}"
        self.containers: set[str] = set()
        self.volumes: set[str] = set()
        self.networks: set[str] = set()
        self.results: list[tuple[str, int]] = []
        self.observations: list[str] = []

    def command(
        self,
        label: str,
        argv: list[str],
        *,
        capture: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                argv,
                cwd=ROOT,
                check=False,
                capture_output=capture,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            self.results.append((label, 124))
            raise RuntimeError(
                f"{label} timed out after {timeout:g} seconds"
            ) from error
        self.results.append((label, result.returncode))
        if capture and result.stdout:
            self.observations.append(f"{label}: {result.stdout.strip()}")
        return result

    def volume(self, suffix: str) -> str:
        name = f"{self.prefix}-{suffix}"
        self.volumes.add(name)
        if self.command(
            "create volume", ["podman", "volume", "create", name]
        ).returncode:
            raise RuntimeError(f"could not create volume {name}")
        return name

    def network(self, suffix: str) -> str:
        name = f"{self.prefix}-{suffix}"
        self.networks.add(name)
        if self.command(
            "create network", ["podman", "network", "create", name]
        ).returncode:
            raise RuntimeError(f"could not create network {name}")
        return name

    def name(self, suffix: str) -> str:
        name = f"{self.prefix}-{suffix}"
        self.containers.add(name)
        return name

    def discard_exited(self, name: str) -> None:
        self.containers.discard(name)

    @staticmethod
    def _cleanup_command(argv: list[str]) -> tuple[int | None, str | None]:
        try:
            result = subprocess.run(
                argv,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CLEANUP_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return None, f"timed out after {CLEANUP_TIMEOUT} seconds"
        except OSError as error:
            return None, str(error)
        return result.returncode, None

    def remove_container(self, name: str) -> str | None:
        exists_code, exists_error = self._cleanup_command(
            ["podman", "container", "exists", name]
        )
        if exists_error:
            return f"container existence check failed for {name}: {exists_error}"
        if exists_code == 1:
            self.containers.discard(name)
            return None
        if exists_code != 0:
            return f"container existence check failed for {name}: exit {exists_code}"
        stop_code, stop_error = self._cleanup_command(
            ["podman", "stop", "--time", "10", name],
        )
        remove_code, remove_error = self._cleanup_command(
            ["podman", "container", "rm", "--force", name]
        )
        if remove_code == 0:
            self.containers.discard(name)
            return None
        details = remove_error or f"exit {remove_code}"
        if stop_error:
            details = f"stop {stop_error}; remove {details}"
        elif stop_code:
            details = f"stop exit {stop_code}; remove {details}"
        return f"container remains: {name} ({details})"

    def cleanup(self) -> list[str]:
        errors: list[str] = []
        for name in sorted(self.containers):
            error = self.remove_container(name)
            if error:
                errors.append(error)
        for name in sorted(self.volumes):
            exists_code, exists_error = self._cleanup_command(
                ["podman", "volume", "exists", name]
            )
            if exists_code == 1:
                self.volumes.discard(name)
                continue
            if exists_error or exists_code != 0:
                errors.append(
                    f"volume existence check failed for {name} "
                    f"({exists_error or f'exit {exists_code}'})"
                )
                continue
            code, error = self._cleanup_command(["podman", "volume", "rm", name])
            if code == 0:
                self.volumes.discard(name)
            else:
                errors.append(f"volume remains: {name} ({error or f'exit {code}'})")
        for name in sorted(self.networks):
            exists_code, exists_error = self._cleanup_command(
                ["podman", "network", "exists", name]
            )
            if exists_code == 1:
                self.networks.discard(name)
                continue
            if exists_error or exists_code != 0:
                errors.append(
                    f"network existence check failed for {name} "
                    f"({exists_error or f'exit {exists_code}'})"
                )
                continue
            code, error = self._cleanup_command(["podman", "network", "rm", name])
            if code == 0:
                self.networks.discard(name)
            else:
                errors.append(f"network remains: {name} ({error or f'exit {code}'})")
        return errors

    def write_evidence(self, error: str | None, cleanup_errors: list[str]) -> None:
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        overall_error = error or ("cleanup failed" if cleanup_errors else None)
        lines = [
            "# Semaphore runtime compatibility evidence",
            "",
            f"- Overall experiment: {'failed: ' + overall_error if overall_error else 'passed'}",
            f"- Workstation architecture: {os.uname().machine}",
            f"- Semaphore v2.19.12: `{SEMAPHORE_IMAGE}` (linux/amd64, linux/arm64)",
            f"- PostgreSQL 17.11: `{POSTGRES_IMAGE}` (official multi-architecture image)",
            f"- rclone 1.75.1: `{RCLONE_IMAGE}` (linux/amd64, linux/arm64, linux/386, linux/arm)",
            f"- Mise: {MISE_VERSION}, official architecture-specific musl asset",
            "- Container runtime: Alpine 3.21.7 musl, UID 1001, GID 0, rootless Podman",
            "- Persistent mount: `/tools` for Mise data/cache/config/state, uv cache, and Galaxy cache",
            "- Required setting: `MISE_ALL_COMPILE=false` to select precompiled musl tools",
            "- Inventory, protected credentials, custom image builds, and package installation: none",
            "- Selected protected-storage adapter: root-owned systemd encrypted credential at `/etc/semaphore/controller-age.cred`, exposed only through the UID-scoped `/run/semaphore-identity/age.sock` command provider",
            "- Adapter restart: restart `semaphore-age.socket`; each connection starts a bounded `systemd-creds decrypt` service and writes only to its socket",
            "- Adapter recovery: restore or re-enroll the independently retained encrypted credential, verify root:root mode 0600, then restart the socket; never create an ambient or plaintext identity file",
            "",
            "## Pin provenance",
            "",
            "- Resolved from primary upstream release metadata on 2026-09-10:",
            *(f"  - {label}: {url}" for label, url in PROVENANCE.items()),
            "",
            "## Return codes",
            "",
            *(f"- {label}: {code}" for label, code in self.results),
            "",
            "## Observations",
            "",
            *(f"- {value}" for value in self.observations),
            "",
            "## Cleanup",
            "",
            (
                "- All run-owned containers, volumes, and networks removed"
                if not cleanup_errors
                else "- " + "; ".join(cleanup_errors)
            ),
        ]
        EVIDENCE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture(path: Path) -> None:
    for relative in FIXTURE_FILES:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    shutil.copytree(
        ROOT / "overrides" / "ansible-galaxy", path / "overrides" / "ansible-galaxy"
    )


def _container(
    run: CompatibilityRun,
    name: str,
    mounts: list[str],
    script: str,
    *,
    environment: dict[str, str] | None = None,
    image: str = SEMAPHORE_IMAGE,
    network: str | None = None,
) -> list[str]:
    argv = ["podman", "run", "--name", name, "--rm", "--entrypoint", "/bin/sh"]
    if image == SEMAPHORE_IMAGE:
        # Podman 4.9 generates an invalid range for keep-id:gid=0 (upstream #22080).
        # Owner access is sufficient for private fixtures; select process GID separately.
        argv.extend(("--userns", "keep-id:uid=1001", "--user", "1001:0"))
    if network:
        argv.extend(("--network", network))
    for mount in mounts:
        argv.extend(("--volume", mount))
    for key, value in (environment or {}).items():
        argv.extend(("--env", f"{key}={value}"))
    workdir = "/workspace" if any(":/workspace:" in mount for mount in mounts) else "/"
    return [*argv, "--workdir", workdir, image, "-c", script]


def _must(
    result: subprocess.CompletedProcess[str], message: str, *, include_output: bool = False
) -> None:
    if result.returncode:
        if include_output:
            message += f" (exit {result.returncode})"
            message += f"\nstdout:\n{result.stdout or ''}\nstderr:\n{result.stderr or ''}"
        raise RuntimeError(message)


def _wait_for_command(
    run: CompatibilityRun,
    label: str,
    argv: list[str],
    *,
    attempts: int = HEALTH_ATTEMPTS,
) -> None:
    last_code = 1
    for _ in range(attempts):
        try:
            last_code = subprocess.run(
                argv,
                cwd=ROOT,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
        except subprocess.TimeoutExpired:
            last_code = 124
        if last_code == 0:
            break
        time.sleep(1)
    run.results.append((label, last_code))
    if last_code:
        raise RuntimeError(f"{label} did not become ready after {attempts} attempts")


def _compatibility() -> int:
    run = CompatibilityRun()
    primary_error: str | None = None
    cleanup_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="homelab-semaphore-fixture-") as temporary:
        fixture = Path(temporary)
        _fixture(fixture)
        try:
            info = run.command(
                "podman rootless inspection",
                [
                    "podman",
                    "info",
                    "--format",
                    "{{.Host.Security.Rootless}} {{.Host.Arch}} {{.Version.Version}}",
                ],
                capture=True,
            )
            _must(info, "rootless Podman is unavailable")
            if not info.stdout.startswith("true "):
                raise RuntimeError("compatibility requires rootless Podman")
            images = (
                ("Semaphore", SEMAPHORE_IMAGE),
                ("PostgreSQL", POSTGRES_IMAGE),
                ("rclone", RCLONE_IMAGE),
            )
            for label, image in images:
                _must(
                    run.command(
                        f"pull pinned {label} image",
                        ["podman", "pull", image],
                        timeout=DOWNLOAD_TIMEOUT,
                    ),
                    f"could not pull pinned {label} image",
                )
                _must(
                    run.command(
                        f"{label} image inspection",
                        [
                            "podman",
                            "image",
                            "inspect",
                            image,
                            "--format",
                            "{{.Digest}} user={{.Config.User}} arch={{.Architecture}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}",
                        ],
                        capture=True,
                    ),
                    f"could not inspect pinned {label} image",
                )

            probe_name = run.name("probe")
            probe = run.command(
                "runtime identity, writable paths, and bundled tools",
                _container(
                    run,
                    probe_name,
                    [],
                    _required_shell(
                        "id",
                        "uname -m",
                        "cat /etc/alpine-release",
                        "ldd /bin/sh",
                        'for path in /home/semaphore /opt/semaphore /var/lib/semaphore /etc/semaphore /tmp/semaphore; do touch "$path/.compat"; rm "$path/.compat"; done',
                        'for tool in sh bash curl git python3 pip3 ansible ansible-galaxy ssh jq tar unzip; do command -v "$tool"; done',
                        "/usr/local/bin/semaphore version",
                        'test "$(id -u)" = 1001',
                        'test "$(id -g)" = 0',
                        "grep -F 'StrictHostKeyChecking no' /etc/ssh/ssh_config.d/semaphore.conf",
                    ),
                ),
                capture=True,
            )
            run.discard_exited(probe_name)
            # This probe contains only image identity, paths, and tool metadata.
            # Keep captured output private by default for all other commands.
            _must(probe, "Semaphore runtime probe failed", include_output=True)
            machine = next(
                line for line in probe.stdout.splitlines() if line in MISE_ASSETS
            )
            asset_arch, checksum = MISE_ASSETS[machine]

            tools = run.volume("tools")
            checkout_one = run.volume("checkout-one")
            install_name = run.name("mise-install")
            url = f"https://github.com/jdx/mise/releases/download/v{MISE_VERSION}/mise-v{MISE_VERSION}-linux-{asset_arch}-musl"
            run.observations.append(f"Mise musl asset: {url} sha256={checksum}")
            installed = run.command(
                "Mise musl installation",
                _container(
                    run,
                    install_name,
                    [f"{tools}:/tools:U"],
                    _required_shell(
                        "mkdir -p /tools/bin",
                        f"curl -fsSL {url} -o /tools/bin/mise",
                        f"printf '%s  %s\\n' {checksum} /tools/bin/mise | sha256sum -c -",
                        "chmod 0755 /tools/bin/mise",
                        "/tools/bin/mise --version",
                    ),
                ),
                timeout=DOWNLOAD_TIMEOUT,
            )
            run.discard_exited(install_name)
            _must(installed, "Mise musl installation failed")

            environment = {
                "PATH": "/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "MISE_DATA_DIR": "/tools/mise",
                "MISE_CACHE_DIR": "/tools/cache",
                "MISE_CONFIG_DIR": "/tools/config",
                "MISE_STATE_DIR": "/tools/state",
                "MISE_TRUSTED_CONFIG_PATHS": "/workspace",
                "MISE_ALL_COMPILE": "false",
                "MISE_LOCKED": "1",
                "UV_CACHE_DIR": "/tools/uv-cache",
                "UV_LINK_MODE": "copy",
                "HOMELAB_GALAXY_CACHE_DIR": "/tools/galaxy",
                "VIRTUAL_ENV": "",
            }

            def copy_checkout(volume: str, suffix: str) -> None:
                name = run.name(f"copy-{suffix}")
                copied = run.command(
                    f"fresh checkout {suffix}",
                    _container(
                        run,
                        name,
                        [f"{fixture}:/fixture:ro", f"{volume}:/workspace:U"],
                        _required_shell("cp -R /fixture/. /workspace/"),
                    ),
                )
                run.discard_exited(name)
                _must(copied, f"fresh checkout {suffix} failed")

            copy_checkout(checkout_one, "one")
            first_name = run.name("bootstrap-one")
            first = run.command(
                "first locked bootstrap and dependency verification",
                _container(
                    run,
                    first_name,
                    [f"{tools}:/tools:U", f"{checkout_one}:/workspace:U"],
                    _required_shell(
                        "mise run bootstrap",
                        "mise exec -- uv run --frozen --no-sync python scripts/dependencies.py verify",
                    ),
                    environment=environment,
                ),
                timeout=BOOTSTRAP_TIMEOUT,
            )
            run.discard_exited(first_name)
            _must(first, "first locked bootstrap failed")

            versions_name = run.name("tool-versions")
            versions = run.command(
                "executed locked tool versions",
                _container(
                    run,
                    versions_name,
                    [f"{tools}:/tools:U", f"{checkout_one}:/workspace:U"],
                    _required_shell(
                        "mise --version",
                        "mise exec -- python --version",
                        "mise exec -- uv --version",
                        "mise exec -- sops --version",
                        "mise exec -- age --version",
                    ),
                    environment=environment,
                ),
                capture=True,
            )
            run.discard_exited(versions_name)
            _must(versions, "could not execute locked tool versions")

            _must(
                run.command(
                    "remove first checkout", ["podman", "volume", "rm", checkout_one]
                ),
                "first checkout removal failed",
            )
            run.volumes.discard(checkout_one)

            checkout_two = run.volume("checkout-two")
            copy_checkout(checkout_two, "two")
            second_name = run.name("bootstrap-two")
            second = run.command(
                "network-disabled restart, persistent tools, and fresh checkout",
                _container(
                    run,
                    second_name,
                    [f"{tools}:/tools:U", f"{checkout_two}:/workspace:U"],
                    _required_shell(
                        "mise run bootstrap",
                        "mise exec -- uv run --frozen --no-sync python scripts/dependencies.py verify",
                        "test -f /tools/galaxy/generations/*/success.json",
                    ),
                    environment=environment,
                    network="none",
                ),
                timeout=BOOTSTRAP_TIMEOUT,
            )
            run.discard_exited(second_name)
            _must(second, "persistent tools failed from a fresh checkout")

            identity_name = run.name("identity-pipe")
            identity = run.command(
                "SOPS age command-provider pipe",
                _container(
                    run,
                    identity_name,
                    [f"{tools}:/tools:U", f"{checkout_two}:/workspace:U"],
                    _required_shell(
                        "identity=$(mise exec -- age-keygen 2>/dev/null)",
                        "recipient=$(printf '%s\\n' \"$identity\" | mise exec -- age-keygen -y)",
                        "printf 'value: disposable\\n' > /tmp/plain.yaml",
                        'mise exec -- sops --config /dev/null --encrypt --age "$recipient" /tmp/plain.yaml > /tmp/encrypted.yaml',
                        "mkfifo /tmp/identity.pipe",
                        "printf '%s\\n' \"$identity\" > /tmp/identity.pipe &",
                        "SOPS_AGE_KEY_CMD='cat /tmp/identity.pipe' mise exec -- sops --config /dev/null --decrypt /tmp/encrypted.yaml | grep -Fx 'value: disposable'",
                    ),
                    environment=environment,
                ),
            )
            run.discard_exited(identity_name)
            _must(identity, "SOPS command-provider pipe failed")

            rclone_name = run.name("rclone-probe")
            rclone = run.command(
                "rclone client shell and BusyBox tools",
                _container(
                    run,
                    rclone_name,
                    [],
                    _required_shell(
                        "rclone version",
                        'for tool in date mktemp sha256sum wc tr cat rm; do command -v "$tool"; done',
                        "date -D '%Y%m%dT%H%M%SZ' -d '20260910T010203Z' +%s >/dev/null",
                    ),
                    image=RCLONE_IMAGE,
                ),
                capture=True,
            )
            run.discard_exited(rclone_name)
            _must(rclone, "rclone client image probe failed")

            network = run.network("database")
            postgres_data = run.volume("postgres-data")
            postgres_name = run.name("postgres")
            database_password = secrets.token_urlsafe(24)
            postgres_start = run.command(
                "PostgreSQL 17 startup",
                [
                    "podman",
                    "run",
                    "--detach",
                    "--name",
                    postgres_name,
                    "--network",
                    network,
                    "--env",
                    "POSTGRES_USER=semaphore",
                    "--env",
                    f"POSTGRES_PASSWORD={database_password}",
                    "--env",
                    "POSTGRES_DB=semaphore",
                    "--volume",
                    f"{postgres_data}:/var/lib/postgresql/data:U",
                    POSTGRES_IMAGE,
                ],
            )
            _must(postgres_start, "PostgreSQL 17 startup failed")
            _wait_for_command(
                run,
                "PostgreSQL 17 readiness",
                [
                    "podman",
                    "exec",
                    postgres_name,
                    "pg_isready",
                    "--username",
                    "semaphore",
                    "--dbname",
                    "semaphore",
                ],
            )
            server_version = run.command(
                "PostgreSQL server major",
                [
                    "podman",
                    "exec",
                    postgres_name,
                    "psql",
                    "--username",
                    "semaphore",
                    "--dbname",
                    "semaphore",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SHOW server_version;",
                ],
                capture=True,
            )
            _must(server_version, "could not query PostgreSQL server version")
            if not server_version.stdout.strip().startswith("17."):
                raise RuntimeError("PostgreSQL server did not report major 17")
            client_version = run.command(
                "PostgreSQL dump client major",
                ["podman", "exec", postgres_name, "pg_dump", "--version"],
                capture=True,
            )
            _must(client_version, "could not execute PostgreSQL dump client")
            if "PostgreSQL) 17." not in client_version.stdout:
                raise RuntimeError("PostgreSQL dump client did not report major 17")
            restore_version = run.command(
                "PostgreSQL restore client major",
                ["podman", "exec", postgres_name, "pg_restore", "--version"],
                capture=True,
            )
            _must(restore_version, "could not execute PostgreSQL restore client")
            if "PostgreSQL) 17." not in restore_version.stdout:
                raise RuntimeError("PostgreSQL restore client did not report major 17")

            config_volume = run.volume("health-config")
            data_volume = run.volume("health-data")
            temp_volume = run.volume("health-tmp")
            health_name = run.name("health")
            access_key = secrets.token_hex(16)
            start = run.command(
                "Semaphore PostgreSQL startup",
                [
                    "podman",
                    "run",
                    "--detach",
                    "--name",
                    health_name,
                    "--network",
                    network,
                    "--env",
                    "SEMAPHORE_DB_DIALECT=postgres",
                    "--env",
                    f"SEMAPHORE_DB_HOST={postgres_name}",
                    "--env",
                    "SEMAPHORE_DB_PORT=5432",
                    "--env",
                    "SEMAPHORE_DB_USER=semaphore",
                    "--env",
                    f"SEMAPHORE_DB_PASS={database_password}",
                    "--env",
                    "SEMAPHORE_DB_NAME=semaphore",
                    "--env",
                    "SEMAPHORE_ADMIN=compatibility-admin",
                    "--env",
                    "SEMAPHORE_ADMIN_PASSWORD=compatibility-only",
                    "--env",
                    "SEMAPHORE_ADMIN_NAME=Compatibility",
                    "--env",
                    "SEMAPHORE_ADMIN_EMAIL=compatibility@example.invalid",
                    "--env",
                    f"SEMAPHORE_ACCESS_KEY_ENCRYPTION={access_key}",
                    "--volume",
                    f"{config_volume}:/etc/semaphore:U",
                    "--volume",
                    f"{data_volume}:/var/lib/semaphore:U",
                    "--volume",
                    f"{temp_volume}:/tmp/semaphore:U",
                    SEMAPHORE_IMAGE,
                ],
            )
            _must(start, "Semaphore startup against PostgreSQL 17 failed")
            _wait_for_command(
                run,
                "Semaphore/PostgreSQL /api/ping health",
                [
                    "podman",
                    "exec",
                    health_name,
                    "curl",
                    "-fsS",
                    "http://127.0.0.1:3000/api/ping",
                ],
            )
            for name in (health_name, postgres_name):
                remove_error = run.remove_container(name)
                if remove_error:
                    raise RuntimeError(remove_error)
        except (KeyError, OSError, RuntimeError, StopIteration, ValueError) as error:
            primary_error = str(error)
            print(f"compatibility failed: {primary_error}", file=sys.stderr)
        finally:
            cleanup_errors = run.cleanup()
            run.write_evidence(primary_error, cleanup_errors)
    if cleanup_errors:
        print(
            "compatibility cleanup failed: " + "; ".join(cleanup_errors),
            file=sys.stderr,
        )
    return 1 if primary_error or cleanup_errors else 0


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.mode == "restore":
        return _restore(args.arguments)
    if args.arguments:
        parser.error("unrecognized arguments: " + " ".join(args.arguments))
    dispatch = {
        "unit": _unit,
        "compatibility": _compatibility,
        "fixture": _fixture_test,
        "controller": _controller,
    }
    return dispatch[args.mode]()


if __name__ == "__main__":
    sys.exit(main())
