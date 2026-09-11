#!/usr/bin/env python3
"""Run the one declared Semaphore repository verification job."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


MARKER = "os-verify"
REVISION = re.compile(r"[0-9a-f]{40}")
SELECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:,[A-Za-z0-9][A-Za-z0-9_.-]*)*")
INVENTORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
JOB_KEYS = {
    "work_root",
    "repository_url",
    "revision",
    "inventory",
    "host_selection",
    "mise_path",
    "tools_root",
    "galaxy_cache",
    "ssh_config",
    "age_identity_command",
    "lock_path",
    "lock_timeout_seconds",
}


class JobError(RuntimeError):
    pass


def _path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise JobError(f"{name} must be an absolute path")
    path = Path(value)
    if ".." in path.parts:
        raise JobError(f"{name} must not contain parent traversal")
    return path


def _trusted_file(path: Path, name: str, *, executable: bool = False) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise JobError(f"{name} is unavailable") from error
    if path.is_symlink() or not path.is_file() or metadata.st_mode & 0o022:
        raise JobError(f"{name} has an unsafe type or permissions")
    if executable and not os.access(path, os.X_OK):
        raise JobError(f"{name} is not executable")


def load_job(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise JobError("controller declaration is unavailable") from error
    wrapper_owner = Path(__file__).stat().st_uid
    if path.is_symlink() or not path.is_file():
        raise JobError("controller declaration must be a regular file")
    if metadata.st_uid != wrapper_owner or metadata.st_mode & 0o022:
        raise JobError("controller declaration has unsafe ownership or permissions")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise JobError("controller declaration is unreadable or invalid") from error
    if not isinstance(document, dict) or not isinstance(document.get("job"), dict):
        raise JobError("controller declaration has no job object")
    job = document["job"]
    if set(job) != JOB_KEYS:
        raise JobError("controller declaration has unexpected job fields")
    return job


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(cwd), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if result.returncode != 0:
        raise JobError("repository checkout validation failed")
    return result.stdout.strip()


def validate_checkout(job: dict[str, object], cwd: Path) -> tuple[Path, str]:
    work_root = _path(job["work_root"], "work_root").resolve(strict=True)
    checkout = cwd.resolve(strict=True)
    if checkout == work_root or work_root not in checkout.parents:
        raise JobError("checkout is outside the declared work root")
    if _git(checkout, "rev-parse", "--is-inside-work-tree") != "true":
        raise JobError("working directory is not a Git checkout")
    if Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
        raise JobError("working directory is not the checkout root")
    revision = job["revision"]
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise JobError("declared revision must be a full Git commit ID")
    head = _git(checkout, "rev-parse", "HEAD")
    if head != revision:
        raise JobError("checkout HEAD differs from the declared revision")
    repository_url = job["repository_url"]
    if not isinstance(repository_url, str) or _git(checkout, "remote", "get-url", "origin") != repository_url:
        raise JobError("checkout origin differs from the declared repository")
    if _git(checkout, "status", "--porcelain", "--untracked-files=no"):
        raise JobError("checkout has dirty tracked content")
    return checkout, head


def validate_job(job: dict[str, object]) -> tuple[str, str, Path, Path, Path, Path, int]:
    inventory = job["inventory"]
    selection = job["host_selection"]
    if not isinstance(inventory, str) or INVENTORY.fullmatch(inventory) is None:
        raise JobError("declared inventory is unsafe")
    if not isinstance(selection, str) or SELECTION.fullmatch(selection) is None:
        raise JobError("declared host selection is unsafe")
    tools_root = _path(job["tools_root"], "tools_root").resolve(strict=True)
    mise = _path(job["mise_path"], "mise_path").resolve(strict=True)
    galaxy = _path(job["galaxy_cache"], "galaxy_cache").resolve(strict=False)
    lock = _path(job["lock_path"], "lock_path").resolve(strict=False)
    ssh_config = _path(job["ssh_config"], "ssh_config")
    identity_command = _path(job["age_identity_command"], "age_identity_command")
    if tools_root not in mise.parents or tools_root not in galaxy.parents or tools_root not in lock.parents:
        raise JobError("tool, cache, and lock paths must be below the trusted tools root")
    if not mise.is_file() or not os.access(mise, os.X_OK):
        raise JobError("trusted Mise executable is unavailable")
    _trusted_file(ssh_config, "controller SSH configuration")
    _trusted_file(identity_command, "controller age identity command", executable=True)
    timeout = job["lock_timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0 or timeout > 300:
        raise JobError("job lock timeout is outside the supported range")
    return inventory, selection, mise, galaxy, ssh_config, identity_command, timeout


def acquire_lock(path: Path, timeout: int):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(descriptor, "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise JobError("repository job lock timed out")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def run(config: Path) -> int:
    job = load_job(config)
    inventory, selection, mise, galaxy, ssh_config, identity_command, timeout = validate_job(job)
    checkout, head = validate_checkout(job, Path.cwd())
    tools_root = Path(str(job["tools_root"])).resolve()
    home = tools_root / "home"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = {
        "PATH": f"{tools_root}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home),
        "LC_ALL": "C.UTF-8",
        "MISE_ALL_COMPILE": "false",
        "MISE_LOCKED": "1",
        "MISE_DATA_DIR": str(tools_root / "mise"),
        "MISE_CACHE_DIR": str(tools_root / "cache"),
        "MISE_CONFIG_DIR": str(tools_root / "config"),
        "MISE_STATE_DIR": str(tools_root / "state"),
        "MISE_TRUSTED_CONFIG_PATHS": str(checkout),
        "UV_CACHE_DIR": str(tools_root / "uv-cache"),
        "UV_LINK_MODE": "copy",
        "HOMELAB_GALAXY_CACHE_DIR": str(galaxy),
        "ANSIBLE_CONFIG": str(checkout / "ansible.cfg"),
        "ANSIBLE_SSH_ARGS": f"-F {ssh_config}",
        "ANSIBLE_SOPS_AGE_KEY_CMD": str(identity_command),
    }
    lock_path = Path(str(job["lock_path"]))
    with acquire_lock(lock_path, timeout):
        bootstrap = subprocess.run(
            [str(mise), "run", "bootstrap"], cwd=checkout, env=environment, check=False
        )
        if bootstrap.returncode != 0:
            return bootstrap.returncode
        playbook = subprocess.run(
            [str(mise), "run", "playbook", "--", "os", "verify", inventory, "--limit", selection],
            cwd=checkout,
            env=environment,
            check=False,
        )
        if playbook.returncode != 0:
            return playbook.returncode
    print(json.dumps({"commit": head, "inventory": inventory, "limit": selection}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("marker", choices=[MARKER])
    args = parser.parse_args()
    try:
        return run(args.config)
    except (JobError, OSError) as error:
        print(f"repository verification refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
