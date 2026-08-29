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
ANSIBLE_VALIDATION = REPOSITORY_ROOT / "scripts/ci/validate-ansible.sh"
ENCRYPTED_EXCLUSIONS = {
    "inventory/frozen/k3s/group_vars/k3s_cluster/vault.yml",
    "inventory/production/group_vars/pihole/vault.yml",
}


def candidate_paths(repository_root: Path = REPOSITORY_ROOT) -> set[str]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
    )
    return {
        encoded_path.decode()
        for encoded_path in result.stdout.split(b"\0")
        if encoded_path
    }


def has_symlink_component(repository_root: Path, relative_path: str) -> bool:
    candidate_prefix = repository_root
    for component in PurePosixPath(relative_path).parts:
        candidate_prefix /= component
        if candidate_prefix.is_symlink():
            return True
    return False


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
            if not has_symlink_component(REPOSITORY_ROOT, path)
            and (REPOSITORY_ROOT / path).is_file()
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

    def test_near_miss_inventory_vault_source_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            repository_root = Path(temporary_name)
            self.initialize_repository(repository_root)
            source_builder = self.isolated_source_builder(repository_root)
            relative_path = "inventory/staging/group_vars/example/vault.yml"
            near_miss_source = repository_root / relative_path
            near_miss_source.parent.mkdir(parents=True)
            near_miss_source.write_text("---\nfixture: public\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "scripts/ci/ansible-sources.sh", relative_path],
                cwd=repository_root,
                check=True,
            )

            result = subprocess.run(
                ["bash", str(source_builder)],
                cwd=repository_root,
                check=False,
                capture_output=True,
            )

            self.assertEqual(0, result.returncode, result.stderr.decode())
            self.assertEqual(f"{relative_path}\0".encode(), result.stdout)

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

    def replace_tracked_source_parent_with_symlink(
        self,
        repository_root: Path,
        target_directory: Path,
        *,
        source_name: str,
        target_bytes: bytes,
    ) -> tuple[Path, str]:
        source_builder = self.isolated_source_builder(repository_root)
        alias_directory = repository_root / "playbooks" / "alias"
        tracked_source = alias_directory / source_name
        tracked_source.parent.mkdir(parents=True)
        tracked_source.write_text("---\nfixture: tracked\n", encoding="utf-8")
        relative_path = tracked_source.relative_to(repository_root).as_posix()
        subprocess.run(
            ["git", "add", "scripts/ci/ansible-sources.sh", relative_path],
            cwd=repository_root,
            check=True,
        )

        shutil.rmtree(alias_directory)
        target_directory.mkdir(parents=True)
        (target_directory / source_name).write_bytes(target_bytes)
        alias_directory.symlink_to(target_directory, target_is_directory=True)
        return source_builder, relative_path

    def test_tracked_source_beneath_symlinked_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary_root = Path(temporary_name)
            repository_root = temporary_root / "repository"
            repository_root.mkdir()
            self.initialize_repository(repository_root)
            source_builder, relative_path = (
                self.replace_tracked_source_parent_with_symlink(
                    repository_root,
                    temporary_root / "alternate-source",
                    source_name="site.yml",
                    target_bytes=b"---\nfixture: alternate\n",
                )
            )

            result = subprocess.run(
                ["bash", str(source_builder)],
                cwd=repository_root,
                check=False,
                capture_output=True,
            )

            self.assertTrue(has_symlink_component(repository_root, relative_path))
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b"", result.stdout)
            self.assertEqual(
                f"refusing Ansible source symlink: {relative_path}\n".encode(),
                result.stderr,
            )

    def test_opaque_source_beneath_symlinked_directory_is_never_read_or_leaked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary_root = Path(temporary_name)
            repository_root = temporary_root / "repository"
            repository_root.mkdir()
            self.initialize_repository(repository_root)
            opaque_target_bytes = os.urandom(128)
            target_directory = temporary_root / "encrypted-target"
            source_builder, relative_path = (
                self.replace_tracked_source_parent_with_symlink(
                    repository_root,
                    target_directory,
                    source_name="vault.yml",
                    target_bytes=opaque_target_bytes,
                )
            )
            target_directory.chmod(0)
            try:
                result = subprocess.run(
                    ["bash", str(source_builder)],
                    cwd=repository_root,
                    check=False,
                    capture_output=True,
                )
            finally:
                target_directory.chmod(0o700)

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b"", result.stdout)
            self.assertEqual(
                f"refusing Ansible source symlink: {relative_path}\n".encode(),
                result.stderr,
            )

    def create_vault_alias_fixture(
        self, repository_root: Path, *, tracked_alias: bool
    ) -> Path:
        source_builder = self.isolated_source_builder(repository_root)
        vault_path = (
            repository_root
            / "inventory"
            / "production"
            / "group_vars"
            / "pihole"
            / "vault.yml"
        )
        vault_path.parent.mkdir(parents=True)
        vault_path.write_text("encrypted fixture\n", encoding="utf-8")
        vault_alias = repository_root / "playbooks" / "vault-alias.yml"
        vault_alias.parent.mkdir(parents=True)
        vault_alias.symlink_to(
            "../inventory/production/group_vars/pihole/vault.yml"
        )
        subprocess.run(
            ["git", "add", "scripts/ci/ansible-sources.sh", vault_path],
            cwd=repository_root,
            check=True,
        )
        if tracked_alias:
            subprocess.run(
                ["git", "add", vault_alias],
                cwd=repository_root,
                check=True,
            )
        return source_builder

    def assert_vault_symlink_alias_is_rejected(self, *, tracked_alias: bool) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            repository_root = Path(temporary_name)
            self.initialize_repository(repository_root)
            source_builder = self.create_vault_alias_fixture(
                repository_root, tracked_alias=tracked_alias
            )

            result = subprocess.run(
                ["bash", str(source_builder)],
                cwd=repository_root,
                check=False,
                capture_output=True,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b"", result.stdout)
            self.assertIn(b"symlink", result.stderr)

    def test_tracked_vault_symlink_alias_is_rejected_without_emitting_a_source(
        self,
    ) -> None:
        self.assert_vault_symlink_alias_is_rejected(tracked_alias=True)

    def test_untracked_vault_symlink_alias_is_rejected_without_emitting_a_source(
        self,
    ) -> None:
        self.assert_vault_symlink_alias_is_rejected(tracked_alias=False)

    def test_git_discovery_failure_is_propagated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            repository_root = Path(temporary_name)
            source_builder = self.isolated_source_builder(repository_root)
            valid_source = repository_root / "playbooks" / "valid.yml"
            valid_source.parent.mkdir(parents=True)
            valid_source.write_text("---\n", encoding="utf-8")

            fake_bin = repository_root / "fake-bin"
            fake_bin.mkdir()
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\0' 'playbooks/valid.yml'\n"
                "printf '%s\\n' 'forced git discovery failure' >&2\n"
                "exit 47\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = (
                f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}"
            )

            result = subprocess.run(
                ["bash", str(source_builder)],
                cwd=repository_root,
                env=environment,
                check=False,
                capture_output=True,
            )

            self.assertEqual(47, result.returncode)
            self.assertEqual(b"", result.stdout)
            self.assertIn(b"forced git discovery failure", result.stderr)

    def test_empty_explicit_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            repository_root = Path(temporary_name)
            self.initialize_repository(repository_root)
            source_builder = self.isolated_source_builder(repository_root)

            result = subprocess.run(
                ["bash", str(source_builder)],
                cwd=repository_root,
                check=False,
                capture_output=True,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b"", result.stdout)
            self.assertIn(b"no explicit Ansible sources", result.stderr)

    def test_ansible_validation_propagates_source_builder_failure_before_lint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            repository_root = Path(temporary_name)
            ansible_validation = (
                repository_root / "scripts" / "ci" / "validate-ansible.sh"
            )
            ansible_validation.parent.mkdir(parents=True)
            shutil.copy2(ANSIBLE_VALIDATION, ansible_validation)
            source_builder = (
                repository_root / "scripts" / "ci" / "ansible-sources.sh"
            )
            source_builder.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\0' 'playbooks/valid.yml'\n"
                "printf '%s\\n' 'forced source builder failure' >&2\n"
                "exit 53\n",
                encoding="utf-8",
            )
            valid_source = repository_root / "playbooks" / "valid.yml"
            valid_source.parent.mkdir(parents=True)
            valid_source.write_text("---\n", encoding="utf-8")

            tests_root = repository_root / "tests" / "ansible"
            tests_root.mkdir(parents=True)
            for fixture_name in ("inventory-test.sh", "vault-test.sh"):
                (tests_root / fixture_name).write_text(
                    "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
                )

            fake_bin = repository_root / "fake-bin"
            fake_bin.mkdir()
            fake_uv = fake_bin / "uv"
            fake_uv_log = repository_root / "fake-uv.log"
            fake_uv.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_UV_LOG\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = (
                f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}"
            )
            environment["FAKE_UV_LOG"] = os.fspath(fake_uv_log)

            result = subprocess.run(
                ["bash", str(ansible_validation)],
                cwd=repository_root,
                env=environment,
                check=False,
                capture_output=True,
            )

            self.assertEqual(53, result.returncode)
            self.assertIn(b"forced source builder failure", result.stderr)
            self.assertNotIn(
                "ansible-lint", fake_uv_log.read_text(encoding="utf-8")
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
            retained_source = repository_root / "playbooks" / "retained.yml"
            retained_source.write_text("---\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "add",
                    "playbooks/deleted.yml",
                    "playbooks/retained.yml",
                    "scripts/ci/ansible-sources.sh",
                ],
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
            self.assertIn("playbooks/retained.yml", selected_sources)


if __name__ == "__main__":
    unittest.main()
