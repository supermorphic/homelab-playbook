#!/usr/bin/env python3
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

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol


SELECTOR = "system_maintenance/default"


@dataclass(frozen=True)
class Platform:
    name: str
    base_image: str
    image: str
    container: str
    containerfile: Path


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


class PreflightError(RuntimeError):
    """A setup problem the operator can correct without a traceback."""


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


def run(arguments: Sequence[str] | None, runner: CommandRunner) -> int:
    parse_selector(arguments)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        preflight(repo_root, runner)
        with invocation_lock(repo_root, runner):
            pass
    except PreflightError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    return run(arguments, SubprocessCommandRunner())


if __name__ == "__main__":
    raise SystemExit(main())
