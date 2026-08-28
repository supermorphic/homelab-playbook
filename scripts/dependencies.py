#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from pathlib import Path


ROLE_NAME_PATTERN = re.compile(r"^\s*-\s+name:\s*['\"]?([^'\"\s]+)['\"]?\s*$")
ROLE_VERSION_PATTERN = re.compile(
    r"^\s+version:\s*['\"]?([^'\"\s]+)['\"]?\s*$"
)
EXACT_VERSION_PATTERN = re.compile(
    r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"
)
METADATA_VERSION_PATTERN = re.compile(
    r"^version:\s*['\"]?([^'\"\s]+)['\"]?\s*$"
)
BOOTSTRAP_FINGERPRINT_PATHS = (
    "uv.lock",
    "requirements.yml",
    "overrides/ansible-galaxy/l3d.unbound/tasks/configure.yml",
    ".ansible/roles/l3d.unbound/tasks/configure.yml",
)
OVERRIDE_PAIRS = (
    (
        "overrides/ansible-galaxy/l3d.unbound/tasks/configure.yml",
        ".ansible/roles/l3d.unbound/tasks/configure.yml",
    ),
)
RECOVERY = "run mise run bootstrap"


def requirements_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def required_roles(path: Path) -> list[tuple[str, str | None]]:
    roles: list[tuple[str, str | None]] = []
    in_roles = False
    pending_role: str | None = None

    for line in path.read_text().splitlines():
        if line and not line[0].isspace():
            if pending_role is not None:
                roles.append((pending_role, None))
                pending_role = None
            in_roles = line.strip() == "roles:"
            continue
        if not in_roles:
            continue
        if match := ROLE_NAME_PATTERN.match(line):
            if pending_role is not None:
                roles.append((pending_role, None))
            pending_role = match.group(1)
            continue
        if pending_role is not None and (match := ROLE_VERSION_PATTERN.match(line)):
            roles.append((pending_role, match.group(1)))
            pending_role = None

    if pending_role is not None:
        roles.append((pending_role, None))
    return roles


def required_role_names(path: Path) -> list[str]:
    return [role_name for role_name, _ in required_roles(path)]


def bootstrap_sha256(repo_root: Path) -> str:
    digest = hashlib.sha256()
    for relative_path in BOOTSTRAP_FINGERPRINT_PATHS:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((repo_root / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def installed_role_version(role_path: Path) -> str | None:
    metadata_path = role_path / "meta" / ".galaxy_install_info"
    try:
        for line in metadata_path.read_text(encoding="utf-8").splitlines():
            if match := METADATA_VERSION_PATTERN.match(line):
                return match.group(1)
    except (OSError, UnicodeDecodeError):
        return None
    return None


def recovery_error(message: str) -> str:
    return f"{message}; {RECOVERY}"


def verify(repo_root: Path) -> list[str]:
    errors: list[str] = []
    requirements_path = repo_root / "requirements.yml"
    ansible_playbook = repo_root / ".venv" / "bin" / "ansible-playbook"
    roles_path = repo_root / ".ansible" / "roles"
    fingerprint_path = repo_root / ".ansible" / "requirements.sha256"

    if not ansible_playbook.is_file():
        errors.append(
            recovery_error(f"missing controller executable: {ansible_playbook}")
        )

    try:
        roles = required_roles(requirements_path)
    except (OSError, UnicodeDecodeError):
        roles = []
        errors.append(
            recovery_error(
                f"missing or unreadable Galaxy requirements: {requirements_path}"
            )
        )
    for role_name, required_version in roles:
        if required_version is None or not EXACT_VERSION_PATTERN.fullmatch(
            required_version
        ):
            errors.append(
                recovery_error(
                    f"Galaxy role {role_name} must declare an exact version"
                )
            )
        role_path = roles_path / role_name
        if not role_path.is_dir():
            errors.append(recovery_error(f"missing Galaxy role: {role_path}"))
            continue
        installed_version = installed_role_version(role_path)
        if installed_version is None:
            errors.append(
                recovery_error(
                    f"missing Galaxy install version metadata: {role_path}"
                )
            )
        elif required_version is not None and installed_version != required_version:
            errors.append(
                recovery_error(
                    f"Galaxy role {role_name} installed version {installed_version} "
                    f"does not match required {required_version}"
                )
            )

    for source_name, target_name in OVERRIDE_PAIRS:
        source_path = repo_root / source_name
        target_path = repo_root / target_name
        try:
            matches_source = source_path.read_bytes() == target_path.read_bytes()
        except OSError:
            matches_source = False
        if not matches_source:
            errors.append(
                recovery_error(
                    f"installed override does not match bootstrap source: {target_path}"
                )
            )

    try:
        expected_fingerprint = bootstrap_sha256(repo_root)
    except OSError:
        expected_fingerprint = None
    if not fingerprint_path.is_file():
        errors.append(
            recovery_error(f"missing bootstrap fingerprint: {fingerprint_path}")
        )
    elif (
        expected_fingerprint is None
        or fingerprint_path.read_text().strip() != expected_fingerprint
    ):
        errors.append(
            recovery_error(f"stale bootstrap fingerprint: {fingerprint_path}")
        )

    try:
        uv_check = subprocess.run(
            ["uv", "sync", "--frozen", "--check"],
            cwd=repo_root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        uv_is_current = uv_check.returncode == 0
    except OSError:
        uv_is_current = False
    if not uv_is_current:
        errors.append(
            recovery_error("the frozen uv environment is not current")
        )

    return errors


def write_fingerprint(repo_root: Path) -> None:
    fingerprint_path = repo_root / ".ansible" / "requirements.sha256"
    fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint_path.write_text(f"{bootstrap_sha256(repo_root)}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify", "write-fingerprint"))
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    if args.action == "write-fingerprint":
        write_fingerprint(repo_root)
        return 0

    errors = verify(repo_root)
    if errors:
        for error in errors:
            print(error)
        return 1

    role_count = len(required_role_names(repo_root / "requirements.yml"))
    print(f"Locked controller environment and {role_count} Galaxy roles are current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
