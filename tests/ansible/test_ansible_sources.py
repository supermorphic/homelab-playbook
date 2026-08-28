"""Contracts for the explicit, credential-free ansible-lint source set."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_BUILDER = REPOSITORY_ROOT / "scripts/ci/ansible-sources.sh"
ENCRYPTED_EXCLUSIONS = {
    "inventory/frozen/k3s/group_vars/k3s_cluster/vault.yml",
    "inventory/production/group_vars/pihole/vault.yml",
}


def candidate_paths() -> set[str]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
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
    def source_builder_output(
        self,
        source_builder: Path = SOURCE_BUILDER,
        repository_root: Path = REPOSITORY_ROOT,
    ) -> list[str]:
        self.assertTrue(
            source_builder.is_file(),
            "the canonical NUL-safe Ansible source builder must exist",
        )
        result = subprocess.run(
            ["bash", str(source_builder)],
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
        return [
            encoded_path.decode()
            for encoded_path in result.stdout.split(b"\0")
            if encoded_path
        ]

    def test_every_candidate_ansible_yaml_is_selected_or_encrypted(self) -> None:
        """Tracked and untracked Ansible YAML enters lint or the exact denylist."""
        candidates = candidate_paths()
        expected_sources = {
            path
            for path in candidates
            if (REPOSITORY_ROOT / path).is_file()
            and is_ansible_yaml_source(path)
            and path not in ENCRYPTED_EXCLUSIONS
        }
        selected_sources = self.source_builder_output()

        self.assertEqual(len(selected_sources), len(set(selected_sources)))
        self.assertSetEqual(set(selected_sources), expected_sources)

    def test_encrypted_inventory_is_never_a_lint_source(self) -> None:
        """The explicit lint source list must never contain encrypted inputs."""
        candidates = candidate_paths()
        selected_sources = set(self.source_builder_output())

        self.assertTrue(ENCRYPTED_EXCLUSIONS.issubset(candidates))
        self.assertTrue(ENCRYPTED_EXCLUSIONS.isdisjoint(selected_sources))

    def isolated_source_builder(self, repository_root: Path) -> Path:
        source_builder = repository_root / "scripts" / "ci" / "ansible-sources.sh"
        source_builder.parent.mkdir(parents=True)
        shutil.copy2(SOURCE_BUILDER, source_builder)
        return source_builder

    def initialize_repository(self, repository_root: Path) -> None:
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main"],
            cwd=repository_root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Ansible Source Test"],
            cwd=repository_root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "ansible-source@example.invalid"],
            cwd=repository_root,
            check=True,
        )

    def test_untracked_invalid_module_is_selected_and_fails_semantic_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            repository_root = Path(temporary_name)
            self.initialize_repository(repository_root)
            source_builder = self.isolated_source_builder(repository_root)
            readme = repository_root / "README.md"
            readme.write_text("isolated repository\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "README.md", "scripts/ci/ansible-sources.sh"],
                cwd=repository_root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "base"],
                cwd=repository_root,
                check=True,
            )

            invalid_playbook = repository_root / "playbooks" / "invalid.yml"
            invalid_playbook.parent.mkdir(parents=True)
            invalid_playbook.write_text(
                "---\n"
                "- name: Reject an unknown module\n"
                "  hosts: localhost\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: Invoke an unknown module\n"
                "      example.invalid_module:\n",
                encoding="utf-8",
            )

            selected_sources = self.source_builder_output(
                source_builder, repository_root
            )

            self.assertIn("playbooks/invalid.yml", selected_sources)
            ansible_config = repository_root / "ansible.cfg"
            ansible_config.write_text(
                "[defaults]\nroles_path = ./.ansible/roles:./roles\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["ANSIBLE_CONFIG"] = os.fspath(ansible_config)
            for variable in (
                "ANSIBLE_ASK_VAULT_PASS",
                "ANSIBLE_VAULT_IDENTITY_LIST",
                "ANSIBLE_VAULT_PASSWORD_FILE",
            ):
                environment.pop(variable, None)
            lint_result = subprocess.run(
                [
                    os.fspath(REPOSITORY_ROOT / ".venv" / "bin" / "ansible-lint"),
                    "--profile",
                    "production",
                    *selected_sources,
                ],
                cwd=repository_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(0, lint_result.returncode)
            self.assertIn(
                "example.invalid_module", lint_result.stdout + lint_result.stderr
            )

    def test_deleted_cached_source_is_not_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            repository_root = Path(temporary_name)
            self.initialize_repository(repository_root)
            source_builder = self.isolated_source_builder(repository_root)
            deleted_source = repository_root / "playbooks" / "deleted.yml"
            deleted_source.parent.mkdir(parents=True)
            deleted_source.write_text("---\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "playbooks/deleted.yml", "scripts/ci/ansible-sources.sh"],
                cwd=repository_root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "base"],
                cwd=repository_root,
                check=True,
            )
            deleted_source.unlink()

            selected_sources = self.source_builder_output(
                source_builder, repository_root
            )

            self.assertNotIn("playbooks/deleted.yml", selected_sources)


if __name__ == "__main__":
    unittest.main()
