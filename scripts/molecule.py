from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Protocol


SELECTOR = "system_maintenance/default"


@dataclass(frozen=True)
class Platform:
    name: str
    base_image: str
    image: str
    container: str
    containerfile: Path


PLATFORMS = (
    Platform(
        name="debian13",
        base_image="docker.io/library/debian:13",
        image="localhost/homelab-playbook-system-maintenance-debian13:local",
        container="homelab-playbook-system-maintenance-debian13",
        containerfile=Path("Containerfile.debian13"),
    ),
    Platform(
        name="rockylinux9",
        base_image="docker.io/rockylinux/rockylinux:9",
        image="localhost/homelab-playbook-system-maintenance-rockylinux9:local",
        container="homelab-playbook-system-maintenance-rockylinux9",
        containerfile=Path("Containerfile.rockylinux9"),
    ),
    Platform(
        name="archlinux",
        base_image="docker.io/archlinux/archlinux:base",
        image="localhost/homelab-playbook-system-maintenance-archlinux:local",
        container="homelab-playbook-system-maintenance-archlinux",
        containerfile=Path("Containerfile.archlinux"),
    ),
)


@dataclass(frozen=True)
class HostPlan:
    podman_version: str
    host_architecture: str
    requested_architectures: dict[str, str]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def capture(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        raise NotImplementedError

    def stream(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        line_prefix: str,
    ) -> CommandResult:
        raise NotImplementedError


OUTPUT_LOCK = threading.Lock()


class SubprocessCommandRunner:
    def capture(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=None if env is None else dict(env),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def stream(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        line_prefix: str,
    ) -> CommandResult:
        if timeout is not None:
            result = self.capture(command, cwd=cwd, env=env, timeout=timeout)
            for line in (result.stdout + result.stderr).splitlines():
                with OUTPUT_LOCK:
                    print(f"{line_prefix} {line}", flush=True)
            return result

        output: list[str] = []
        with subprocess.Popen(
            command,
            cwd=cwd,
            env=None if env is None else dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process:
            if process.stdout is None:
                process.kill()
                raise WorkerError("could not capture worker output")
            for line in process.stdout:
                output.append(line)
                with OUTPUT_LOCK:
                    print(f"{line_prefix} {line.rstrip()}", flush=True)
            returncode = process.wait()
        return CommandResult(
            returncode=returncode,
            stdout="".join(output),
            stderr="",
        )

class PreflightError(RuntimeError):
    """A setup problem the operator can correct without a traceback."""


class WorkerError(RuntimeError):
    """A platform lifecycle failure that should appear in the summary."""


class StageError(WorkerError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PlatformResult:
    platform: str
    status: str
    message: str
    image_id: str | None
    repository_digest: str | None
    pull_seconds: float
    build_seconds: float
    molecule_seconds: float
    cleanup_seconds: float
    platform_seconds: float

    @property
    def success(self) -> bool:
        return self.status == "pass"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test system_maintenance in rootless Podman containers",
    )
    parser.add_argument(
        "selector",
        metavar=SELECTOR,
        help=f"exact role/scenario selector; supported value: {SELECTOR}",
    )
    return parser


def parse_selector(arguments: Sequence[str]) -> str:
    parser = _parser()
    parsed = parser.parse_args(arguments)
    if parsed.selector != SELECTOR:
        parser.error(f"unsupported selector {parsed.selector!r}; expected {SELECTOR}")
    return parsed.selector


def _runtime_recovery() -> str:
    if platform.system() == "Darwin":
        return "start the rootless Podman machine with: podman machine start"
    return "make rootless Podman available to the current user"


def _normalize_architecture(value: object) -> str:
    normalized = str(value).strip().lower()
    aliases = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "amd64",
        "x86_64": "amd64",
    }
    if normalized not in aliases:
        raise PreflightError(f"unsupported Podman host architecture: {normalized}")
    return aliases[normalized]


def preflight(repo_root: Path, runner: CommandRunner) -> HostPlan:
    dependency_result = runner.capture(
        [sys.executable, "scripts/dependencies.py", "verify"],
        cwd=repo_root,
    )
    if dependency_result.returncode != 0:
        raise PreflightError(
            "locked controller or Galaxy dependencies are unavailable; "
            "run mise run bootstrap"
        )

    podman_path = shutil.which("podman")
    if podman_path is None:
        raise PreflightError(
            "Podman is not available in PATH; install Podman for the current user"
        )

    version_result = runner.capture([podman_path, "--version"], cwd=repo_root)
    if version_result.returncode != 0 or not version_result.stdout.strip():
        raise PreflightError(f"Podman version inspection failed; {_runtime_recovery()}")

    try:
        info_result = runner.capture(
            [podman_path, "info", "--format", "json"],
            cwd=repo_root,
            timeout=15.0,
        )
    except subprocess.TimeoutExpired as error:
        raise PreflightError(
            f"Podman did not respond within 15 seconds; {_runtime_recovery()}"
        ) from error
    if info_result.returncode != 0:
        raise PreflightError(f"Podman is not reachable; {_runtime_recovery()}")

    try:
        info = json.loads(info_result.stdout)
        host = info["host"]
        rootless = host["security"]["rootless"]
        cgroup_version = str(host["cgroupVersion"]).lower().removeprefix("v")
        host_architecture = _normalize_architecture(host["arch"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PreflightError("Podman returned incomplete host information") from error

    if rootless is not True:
        raise PreflightError("Podman must run rootless as the current user")
    if cgroup_version != "2":
        raise PreflightError("Podman must provide cgroup v2")

    requested_architectures = {
        "debian13": host_architecture,
        "rockylinux9": host_architecture,
        "archlinux": "amd64",
    }
    return HostPlan(
        podman_version=version_result.stdout.strip(),
        host_architecture=host_architecture,
        requested_architectures=requested_architectures,
    )


def lock_path(repo_root: Path, runner: CommandRunner) -> Path:
    result = runner.capture(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo_root,
    )
    common_value = result.stdout.strip()
    if result.returncode != 0 or not common_value:
        raise PreflightError("Git could not resolve the repository invocation lock")
    common_directory = Path(common_value)
    if not common_directory.is_absolute():
        common_directory = repo_root / common_directory
    common_directory = common_directory.resolve()
    digest = hashlib.sha256(
        str(common_directory).encode("utf-8")
    ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"homelab-playbook-molecule-{digest}.lock"


@contextmanager
def invocation_lock(repo_root: Path, runner: CommandRunner) -> Iterator[None]:
    path = lock_path(repo_root, runner)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise PreflightError(f"could not open a safe lock file: {path}") from error

    lock_file = os.fdopen(descriptor, "r+")
    try:
        metadata = os.fstat(lock_file.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise PreflightError(f"refusing unsafe lock file: {path}")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PreflightError(
                "another Molecule test invocation is already running for this repository"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def ownership_labels(platform_definition: Platform) -> dict[str, str]:
    return {
        "io.supermorphic.homelab-playbook.repository": "homelab-playbook",
        "io.supermorphic.homelab-playbook.scenario": SELECTOR,
        "io.supermorphic.homelab-playbook.platform": platform_definition.name,
    }


def remove_owned_container(
    repo_root: Path,
    runner: CommandRunner,
    platform_definition: Platform,
) -> bool:
    exists_result = runner.capture(
        ["podman", "container", "exists", platform_definition.container],
        cwd=repo_root,
    )
    if exists_result.returncode == 1:
        return False
    if exists_result.returncode != 0:
        raise WorkerError(
            f"could not inspect container name {platform_definition.container}"
        )

    inspect_result = runner.capture(
        [
            "podman",
            "container",
            "inspect",
            platform_definition.container,
            "--format",
            "json",
        ],
        cwd=repo_root,
    )
    if inspect_result.returncode != 0:
        raise WorkerError(
            f"could not inspect container labels for {platform_definition.container}"
        )
    try:
        inspected = json.loads(inspect_result.stdout)
        labels = inspected[0]["Config"]["Labels"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise WorkerError(
            f"could not inspect container labels for {platform_definition.container}"
        ) from error

    expected_labels = ownership_labels(platform_definition)
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        raise WorkerError(
            f"container name collision: {platform_definition.container} is not "
            f"owned by {SELECTOR}/{platform_definition.name}"
        )

    remove_result = runner.capture(
        ["podman", "rm", "--force", platform_definition.container],
        cwd=repo_root,
    )
    if remove_result.returncode != 0:
        raise WorkerError(
            f"could not remove owned container {platform_definition.container}"
        )
    return True


def _image_provenance(output: str, base_image: str) -> tuple[str, str | None]:
    try:
        inspected = json.loads(output)
        image = inspected[0]
        image_id = image["Id"]
        repository_digests = image.get("RepoDigests") or []
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise StageError(
            "acquisition failure",
            f"could not read the pulled image identity for {base_image}",
        ) from error
    if not isinstance(image_id, str) or not image_id:
        raise StageError(
            "acquisition failure",
            f"could not read the pulled image identity for {base_image}",
        )

    repository = base_image.rsplit(":", maxsplit=1)[0]
    matching_digest = next(
        (
            digest
            for digest in repository_digests
            if isinstance(digest, str) and digest.startswith(f"{repository}@")
        ),
        None,
    )
    return image_id, matching_digest


def run_platform(
    platform_definition: Platform,
    host_plan: HostPlan,
    repo_root: Path,
    runner: CommandRunner,
    invocation_id: str,
    clock=time.monotonic,
) -> PlatformResult:
    platform_started = clock()
    pull_seconds = 0.0
    build_seconds = 0.0
    molecule_seconds = 0.0
    cleanup_seconds = 0.0
    image_id: str | None = None
    repository_digest: str | None = None
    status = "pass"
    message = "all Molecule phases passed"
    requested_architecture = host_plan.requested_architectures[
        platform_definition.name
    ]
    scenario_directory = (
        repo_root / "roles" / "system_maintenance" / "molecule" / "default"
    )
    role_directory = repo_root / "roles" / "system_maintenance"
    line_prefix = f"[{platform_definition.name}]"

    try:
        remove_owned_container(repo_root, runner, platform_definition)

        pull_started = clock()
        pull_result = runner.stream(
            [
                "podman",
                "pull",
                "--policy=always",
                "--retry=3",
                "--platform",
                f"linux/{requested_architecture}",
                platform_definition.base_image,
            ],
            cwd=repo_root,
            line_prefix=line_prefix,
        )
        if pull_result.returncode != 0:
            pull_seconds = clock() - pull_started
            raise StageError(
                "acquisition failure",
                f"image pull failed for {platform_definition.base_image}",
            )
        inspect_result = runner.capture(
            [
                "podman",
                "image",
                "inspect",
                platform_definition.base_image,
                "--format",
                "json",
            ],
            cwd=repo_root,
        )
        if inspect_result.returncode != 0:
            pull_seconds = clock() - pull_started
            raise StageError(
                "acquisition failure",
                f"image inspection failed for {platform_definition.base_image}",
            )
        image_id, repository_digest = _image_provenance(
            inspect_result.stdout,
            platform_definition.base_image,
        )
        pull_seconds = clock() - pull_started

        build_started = clock()
        build_result = runner.stream(
            [
                "podman",
                "build",
                "--pull=never",
                "--platform",
                f"linux/{requested_architecture}",
                "--tag",
                platform_definition.image,
                "--file",
                str(scenario_directory / platform_definition.containerfile),
                str(scenario_directory),
            ],
            cwd=repo_root,
            line_prefix=line_prefix,
        )
        build_seconds = clock() - build_started
        if build_result.returncode != 0:
            raise StageError(
                "build failure",
                f"test-image build failed for {platform_definition.name}",
            )

        molecule_started = clock()
        molecule_environment = os.environ.copy()
        molecule_environment["MOLECULE_EPHEMERAL_DIRECTORY"] = str(
            repo_root
            / ".tmp"
            / "molecule"
            / invocation_id
            / platform_definition.name
        )
        molecule_environment["ANSIBLE_ROLES_PATH"] = str(repo_root / "roles")
        molecule_environment["ANSIBLE_COLLECTIONS_PATH"] = str(
            repo_root / ".ansible" / "collections"
        )
        molecule_environment["HOMELAB_MOLECULE_PLATFORM"] = (
            platform_definition.name
        )
        molecule_result = runner.stream(
            [
                "molecule",
                "test",
                "--scenario-name",
                "default",
                "--platform-name",
                platform_definition.name,
                "--no-report",
                "--no-command-borders",
            ],
            cwd=role_directory,
            env=molecule_environment,
            line_prefix=line_prefix,
        )
        molecule_seconds = clock() - molecule_started
        if molecule_result.returncode != 0:
            raise StageError(
                "test failure",
                f"Molecule lifecycle failed for {platform_definition.name}",
            )
    except StageError as error:
        status = error.status
        message = str(error)
    except WorkerError as error:
        status = "cleanup failure"
        message = str(error)
    finally:
        cleanup_started = clock()
        try:
            remove_owned_container(repo_root, runner, platform_definition)
        except WorkerError as cleanup_error:
            primary = f"{status}: {message}"
            status = "cleanup failure"
            message = f"{primary}; cleanup failure: {cleanup_error}"
        cleanup_seconds = clock() - cleanup_started

    return PlatformResult(
        platform=platform_definition.name,
        status=status,
        message=message,
        image_id=image_id,
        repository_digest=repository_digest,
        pull_seconds=pull_seconds,
        build_seconds=build_seconds,
        molecule_seconds=molecule_seconds,
        cleanup_seconds=cleanup_seconds,
        platform_seconds=clock() - platform_started,
    )


def select_platforms(
    environment: Mapping[str, str],
    host_architecture: str,
) -> tuple[Platform, ...]:
    selected_name = environment.get("HOMELAB_MOLECULE_PLATFORM")
    if not selected_name:
        if host_architecture == "arm64":
            return tuple(
                platform_definition
                for platform_definition in PLATFORMS
                if platform_definition.name != "archlinux"
            )
        return PLATFORMS
    selected = tuple(
        platform_definition
        for platform_definition in PLATFORMS
        if platform_definition.name == selected_name
    )
    if not selected:
        raise PreflightError(f"unknown Molecule platform: {selected_name}")
    return selected


def run_platforms(
    platform_definitions: Sequence[Platform],
    worker: Callable[[Platform], PlatformResult],
) -> list[PlatformResult]:
    with ThreadPoolExecutor(max_workers=len(platform_definitions)) as executor:
        futures = [
            executor.submit(worker, platform_definition)
            for platform_definition in platform_definitions
        ]
        return [future.result() for future in futures]


def _platform_by_name(name: str) -> Platform:
    return next(
        platform_definition
        for platform_definition in PLATFORMS
        if platform_definition.name == name
    )


def _terminal_summary(
    host_plan: HostPlan,
    results: Sequence[PlatformResult],
    invocation_seconds: float,
    environment: Mapping[str, str],
) -> str:
    lines = [f"Podman: {host_plan.podman_version}"]
    for result in results:
        platform_definition = _platform_by_name(result.platform)
        requested = host_plan.requested_architectures[result.platform]
        execution_mode = (
            "native" if requested == host_plan.host_architecture else "emulated"
        )
        lines.append(
            f"{result.platform}: host={host_plan.host_architecture} "
            f"requested={requested} {execution_mode} "
            f"base={platform_definition.base_image} "
            f"image={result.image_id or 'unavailable'} "
            f"digest={result.repository_digest or 'unavailable'} "
            f"pull={result.pull_seconds:.2f}s "
            f"build={result.build_seconds:.2f}s "
            f"molecule={result.molecule_seconds:.2f}s "
            f"cleanup={result.cleanup_seconds:.2f}s "
            f"total={result.platform_seconds:.2f}s "
            f"result={result.status} message={result.message}"
        )
    if (
        not environment.get("HOMELAB_MOLECULE_PLATFORM")
        and host_plan.host_architecture == "arm64"
        and not any(result.platform == "archlinux" for result in results)
    ):
        lines.append(
            "archlinux: skipped on arm64; GitHub CI runs it on native amd64"
        )
    lines.append(f"Invocation total: {invocation_seconds:.2f}s")
    return "\n".join(lines)


def _github_summary(
    host_plan: HostPlan,
    results: Sequence[PlatformResult],
    invocation_seconds: float,
) -> str:
    lines = [
        "### Molecule platform summary",
        "",
        f"- Podman: `{host_plan.podman_version}`",
        f"- Invocation total: {invocation_seconds:.2f} seconds",
        "",
        "| Platform | Host | Requested | Mode | Pull | Build | Molecule | "
        "Cleanup | Total | Result |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in results:
        requested = host_plan.requested_architectures[result.platform]
        execution_mode = (
            "native" if requested == host_plan.host_architecture else "emulated"
        )
        lines.append(
            f"| {result.platform} | {host_plan.host_architecture} | {requested} | "
            f"{execution_mode} | {result.pull_seconds:.2f}s | "
            f"{result.build_seconds:.2f}s | {result.molecule_seconds:.2f}s | "
            f"{result.cleanup_seconds:.2f}s | {result.platform_seconds:.2f}s | "
            f"{result.status} |"
        )
        platform_definition = _platform_by_name(result.platform)
        lines.extend(
            [
                "",
                f"- {result.platform} base: `{platform_definition.base_image}`",
                f"- {result.platform} image ID: `{result.image_id or 'unavailable'}`",
                f"- {result.platform} digest: "
                f"`{result.repository_digest or 'unavailable'}`",
                f"- {result.platform} Pull: {result.pull_seconds:.2f} seconds; "
                f"Build: {result.build_seconds:.2f} seconds; "
                f"Molecule: {result.molecule_seconds:.2f} seconds",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def emit_summary(
    host_plan: HostPlan,
    results: Sequence[PlatformResult],
    invocation_seconds: float,
    environment: Mapping[str, str],
) -> None:
    print(_terminal_summary(host_plan, results, invocation_seconds, environment))

    summary_value = environment.get("GITHUB_STEP_SUMMARY")
    if not summary_value:
        return
    summary_path = Path(summary_value)
    try:
        metadata = summary_path.lstat()
    except OSError:
        return
    if summary_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return

    flags = os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(summary_path, flags)
    except OSError:
        return
    with os.fdopen(descriptor, "a", encoding="utf-8") as summary_file:
        summary_file.write(_github_summary(host_plan, results, invocation_seconds))


def run(arguments: Sequence[str] | None, runner: CommandRunner) -> int:
    parse_selector(arguments)
    repo_root = Path(__file__).resolve().parents[1]
    invocation_started = time.monotonic()
    try:
        selected_name = os.environ.get("HOMELAB_MOLECULE_PLATFORM")
        if selected_name and not any(
            platform_definition.name == selected_name
            for platform_definition in PLATFORMS
        ):
            raise PreflightError(f"unknown Molecule platform: {selected_name}")
        host_plan = preflight(repo_root, runner)
        platform_definitions = select_platforms(
            os.environ,
            host_plan.host_architecture,
        )
        with invocation_lock(repo_root, runner):
            invocation_id = uuid.uuid4().hex
            results = run_platforms(
                platform_definitions,
                lambda platform_definition: run_platform(
                    platform_definition,
                    host_plan,
                    repo_root,
                    runner,
                    invocation_id,
                ),
            )
    except PreflightError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    emit_summary(
        host_plan,
        results,
        time.monotonic() - invocation_started,
        os.environ,
    )
    return 0 if all(result.success for result in results) else 1


def main(arguments: Sequence[str] | None = None) -> int:
    return run(arguments, SubprocessCommandRunner())


if __name__ == "__main__":
    raise SystemExit(main())
