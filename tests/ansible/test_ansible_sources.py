"""Contracts for the explicit, credential-free ansible-lint source set."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path, PurePosixPath


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_BUILDER = REPOSITORY_ROOT / "scripts/ci/ansible-sources.sh"
ENCRYPTED_EXCLUSIONS = {
    "inventory/frozen/k3s/group_vars/k3s_cluster/vault.yml",
    "inventory/production/group_vars/pihole/vault.yml",
}


def tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return {
        encoded_path.decode()
        for encoded_path in result.stdout.split(b"\0")
        if encoded_path
    }


def is_ansible_yaml_source(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    if path.suffix not in {".yml", ".yaml"}:
        return False

    parts = path.parts
    return (
        parts[0] in {"playbooks", "roles", "inventory"}
        or parts[:2] == ("overrides", "ansible-galaxy")
        or relative_path == "requirements.yml"
        or relative_path == "tests/fixtures/vault/playbook.yml"
    )


class AnsibleSourceContracts(unittest.TestCase):
    def source_builder_output(self) -> list[str]:
        self.assertTrue(
            SOURCE_BUILDER.is_file(),
            "the canonical NUL-safe Ansible source builder must exist",
        )
        result = subprocess.run(
            ["bash", str(SOURCE_BUILDER)],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
        )
        return [
            encoded_path.decode()
            for encoded_path in result.stdout.split(b"\0")
            if encoded_path
        ]

    def test_every_tracked_ansible_yaml_is_selected_or_encrypted(self) -> None:
        """A new tracked Ansible YAML path must enter lint or the exact denylist."""
        tracked = tracked_paths()
        expected_sources = {
            path
            for path in tracked
            if is_ansible_yaml_source(path) and path not in ENCRYPTED_EXCLUSIONS
        }
        selected_sources = self.source_builder_output()

        self.assertEqual(len(selected_sources), len(set(selected_sources)))
        self.assertSetEqual(set(selected_sources), expected_sources)

    def test_encrypted_inventory_is_never_a_lint_source(self) -> None:
        """The explicit lint source list must never contain encrypted inputs."""
        tracked = tracked_paths()
        selected_sources = set(self.source_builder_output())

        self.assertTrue(ENCRYPTED_EXCLUSIONS.issubset(tracked))
        self.assertTrue(ENCRYPTED_EXCLUSIONS.isdisjoint(selected_sources))


if __name__ == "__main__":
    unittest.main()
