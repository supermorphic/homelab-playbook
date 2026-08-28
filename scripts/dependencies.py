#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path


ROLE_NAME_PATTERN = re.compile(r"^\s*-\s+name:\s*['\"]?([^'\"\s]+)['\"]?\s*$")


def requirements_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def required_role_names(path: Path) -> list[str]:
    role_names: list[str] = []
    in_roles = False

    for line in path.read_text().splitlines():
        if line and not line[0].isspace():
            in_roles = line.strip() == "roles:"
            continue
        if in_roles and (match := ROLE_NAME_PATTERN.match(line)):
            role_names.append(match.group(1))

    return role_names


def verify(repo_root: Path) -> list[str]:
    errors: list[str] = []
    requirements_path = repo_root / "requirements.yml"
    ansible_playbook = repo_root / ".venv" / "bin" / "ansible-playbook"
    roles_path = repo_root / ".ansible" / "roles"
    fingerprint_path = repo_root / ".ansible" / "requirements.sha256"

    if not ansible_playbook.is_file():
        errors.append(f"missing controller executable: {ansible_playbook}")

    for role_name in required_role_names(requirements_path):
        role_path = roles_path / role_name
        if not role_path.is_dir():
            errors.append(f"missing Galaxy role: {role_path}")

    expected_fingerprint = requirements_sha256(requirements_path)
    if not fingerprint_path.is_file():
        errors.append(f"missing requirements fingerprint: {fingerprint_path}")
    elif fingerprint_path.read_text().strip() != expected_fingerprint:
        errors.append(f"stale requirements fingerprint: {fingerprint_path}")

    return errors


def write_fingerprint(repo_root: Path) -> None:
    requirements_path = repo_root / "requirements.yml"
    fingerprint_path = repo_root / ".ansible" / "requirements.sha256"
    fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint_path.write_text(f"{requirements_sha256(requirements_path)}\n")


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
