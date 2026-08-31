from __future__ import annotations

import importlib.util
import json
import os
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES_PATH = REPO_ROOT / "scripts" / "dependencies.py"
TEST_ROLES = (
    ("example.bootstrap", "1.2.3"),
    ("example.service", "v4.5.6"),
    ("l3d.unbound", "v3.4.5"),
)
TEST_COLLECTIONS = (("example.utilities", "7.8.9"),)
REQUIREMENTS = "---\nroles:\n" + "".join(
    f"  - name: {role_name}\n    version: {role_version}\n"
    for role_name, role_version in TEST_ROLES
) + "collections:\n" + "".join(
    f"  - name: {collection_name}\n    version: {collection_version}\n"
    for collection_name, collection_version in TEST_COLLECTIONS
)


def load_dependencies() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dependencies", DEPENDENCIES_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {DEPENDENCIES_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dependencies = load_dependencies()


class RepositoryDependencyContractTests(unittest.TestCase):
    def test_molecule_controller_dependency_is_locked_and_bootstrapped(self) -> None:
        with (REPO_ROOT / "pyproject.toml").open("rb") as source:
            project = tomllib.load(source)

        groups = project["dependency-groups"]
        self.assertEqual(["molecule==26.6.0"], groups.get("molecule"))
        self.assertIn({"include-group": "molecule"}, groups["dev"])

    def test_podman_collection_is_exactly_pinned(self) -> None:
        collections = dependencies.required_collections(
            REPO_ROOT / "requirements.yml"
        )

        self.assertIn(("containers.podman", "1.20.2"), collections)

    def test_os_baseline_dependencies_are_exactly_pinned(self) -> None:
        roles = dependencies.required_roles(REPO_ROOT / "requirements.yml")
        collections = dependencies.required_collections(
            REPO_ROOT / "requirements.yml"
        )

        self.assertIn(("willshersystems.sshd", "v0.34.0"), roles)
        self.assertIn(("ansible.posix", "2.2.2"), collections)
        self.assertIn(("robertdebock.bootstrap", "7.1.5"), roles)
        self.assertIn(("geerlingguy.security", "3.0.0"), roles)

    def test_sshd_dependency_uses_canonical_git_source(self) -> None:
        requirements = (REPO_ROOT / "requirements.yml").read_text(encoding="utf-8")
        entry_match = re.search(
            r"(?ms)^  - name: willshersystems\.sshd\n(?P<entry>.*?)(?=^  - name:|^collections:)",
            requirements,
        )

        self.assertIsNotNone(entry_match)
        entry = entry_match.group("entry")
        src_match = re.search(r"^    src: (.+)$", entry, re.MULTILINE)
        scm_match = re.search(r"^    scm: (.+)$", entry, re.MULTILINE)
        self.assertIsNotNone(src_match)
        self.assertIsNotNone(scm_match)
        self.assertEqual(
            "https://github.com/willshersystems/ansible-sshd.git",
            src_match.group(1),
        )
        self.assertEqual("git", scm_match.group(1))


class DependencyVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary_directory.name)
        (self.repo_root / "requirements.yml").write_text(REQUIREMENTS)
        (self.repo_root / "uv.lock").write_text("version = 1\n")
        self.override_source = (
            self.repo_root
            / "overrides"
            / "ansible-galaxy"
            / "l3d.unbound"
            / "tasks"
            / "configure.yml"
        )
        self.override_source.parent.mkdir(parents=True)
        self.override_source.write_text("---\n# bootstrap-owned override\n")
        self.fake_bin = self.repo_root / "fake-bin"
        self.fake_bin.mkdir()
        self.write_fake_uv(0)
        self.original_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.fake_bin}{os.pathsep}{self.original_path}"

    def tearDown(self) -> None:
        os.environ["PATH"] = self.original_path
        self.temporary_directory.cleanup()

    def write_fake_uv(self, exit_code: int) -> None:
        fake_uv = self.fake_bin / "uv"
        fake_uv.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$#\" -eq 3 && \"$1\" == sync && "
            "\"$2\" == --frozen && \"$3\" == --check ]]; then\n"
            f"  exit {exit_code}\n"
            "fi\n"
            "exit 2\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)

    def create_virtualenv_executable(self) -> None:
        executable = self.repo_root / ".venv" / "bin" / "ansible-playbook"
        executable.parent.mkdir(parents=True)
        executable.touch()

    def create_roles(
        self,
        *,
        excluding: str | None = None,
        missing_metadata: str | None = None,
        wrong_metadata: str | None = None,
    ) -> None:
        for role_name, required_version in TEST_ROLES:
            if role_name != excluding:
                role_path = self.repo_root / ".ansible" / "roles" / role_name
                role_path.mkdir(parents=True)
                if role_name != missing_metadata:
                    metadata_path = role_path / "meta" / ".galaxy_install_info"
                    metadata_path.parent.mkdir()
                    version = (
                        "0.0.0" if role_name == wrong_metadata else required_version
                    )
                    metadata_path.write_text(f"version: {version}\n")

    def create_installed_override(self, content: str | None = None) -> None:
        override_target = (
            self.repo_root
            / ".ansible"
            / "roles"
            / "l3d.unbound"
            / "tasks"
            / "configure.yml"
        )
        override_target.parent.mkdir(parents=True, exist_ok=True)
        override_target.write_text(
            self.override_source.read_text() if content is None else content
        )

    def create_collections(
        self,
        *,
        excluding: str | None = None,
        wrong_metadata: str | None = None,
    ) -> None:
        for collection_name, required_version in TEST_COLLECTIONS:
            if collection_name == excluding:
                continue
            namespace, name = collection_name.split(".", maxsplit=1)
            manifest_path = (
                self.repo_root
                / ".ansible"
                / "collections"
                / "ansible_collections"
                / namespace
                / name
                / "MANIFEST.json"
            )
            manifest_path.parent.mkdir(parents=True)
            version = "0.0.0" if collection_name == wrong_metadata else required_version
            manifest_path.write_text(
                json.dumps(
                    {
                        "collection_info": {
                            "namespace": namespace,
                            "name": name,
                            "version": version,
                        }
                    }
                ),
                encoding="utf-8",
            )

    def create_current_environment(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles()
        self.create_collections()
        self.create_installed_override()

    def write_current_fingerprint(self) -> None:
        dependencies.write_fingerprint(self.repo_root)

    def test_verify_rejects_missing_virtualenv(self) -> None:
        self.create_roles()
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(
            any(".venv/bin/ansible-playbook" in error for error in errors), errors
        )

    def test_verify_rejects_missing_role(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles(excluding="example.service")
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("example.service" in error for error in errors), errors)

    def test_verify_rejects_stale_requirements_fingerprint(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles()
        self.create_installed_override()
        fingerprint = self.repo_root / ".ansible" / "requirements.sha256"
        fingerprint.write_text("stale\n")

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("stale" in error for error in errors), errors)

    def test_unreadable_fingerprint_returns_bootstrap_recovery(self) -> None:
        self.create_current_environment()
        fingerprint = self.repo_root / ".ansible" / "requirements.sha256"
        fingerprint.parent.mkdir(parents=True, exist_ok=True)
        fingerprint.write_bytes(b"\xff\xfe\xfd")

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(errors)
        self.assertTrue(
            all("mise run bootstrap" in error for error in errors), errors
        )
        self.assertTrue(any("fingerprint" in error for error in errors), errors)

    def test_verify_accepts_matching_roles_and_fingerprint(self) -> None:
        self.create_current_environment()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertEqual([], errors)

    def test_verify_rejects_missing_galaxy_install_version_metadata(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles(missing_metadata="example.bootstrap")
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(
            any("example.bootstrap" in error and "metadata" in error for error in errors),
            errors,
        )

    def test_verify_rejects_wrong_galaxy_install_version_metadata(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles(wrong_metadata="example.service")
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(
            any(
                "example.service" in error
                and "v4.5.6" in error
                and "0.0.0" in error
                for error in errors
            ),
            errors,
        )

    def test_verify_rejects_missing_collection(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles()
        self.create_collections(excluding="example.utilities")
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("example.utilities" in error for error in errors), errors)

    def test_verify_rejects_wrong_collection_version(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles()
        self.create_collections(wrong_metadata="example.utilities")
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(
            any(
                "example.utilities" in error
                and "7.8.9" in error
                and "0.0.0" in error
                for error in errors
            ),
            errors,
        )

    def test_verify_rejects_installed_override_that_differs_from_source(self) -> None:
        self.create_current_environment()
        self.write_current_fingerprint()
        self.create_installed_override("---\n# stale installed override\n")

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("installed override" in error for error in errors), errors)

    def test_verify_rejects_uv_lock_changed_after_bootstrap(self) -> None:
        self.create_current_environment()
        self.write_current_fingerprint()
        (self.repo_root / "uv.lock").write_text("version = 2\n")

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("fingerprint" in error for error in errors), errors)

    def test_verify_rejects_non_exact_galaxy_requirement_version(self) -> None:
        self.create_current_environment()
        requirements_path = self.repo_root / "requirements.yml"
        requirements_path.write_text(
            requirements_path.read_text().replace("version: 1.2.3", "version: latest")
        )
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("exact version" in error for error in errors), errors)

    def test_verify_rejects_non_exact_collection_version(self) -> None:
        self.create_current_environment()
        requirements_path = self.repo_root / "requirements.yml"
        requirements_path.write_text(
            requirements_path.read_text().replace("version: 7.8.9", "version: latest")
        )
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(
            any(
                "example.utilities" in error and "exact version" in error
                for error in errors
            ),
            errors,
        )

    def test_verify_rejects_uv_environment_that_fails_frozen_check(self) -> None:
        self.create_current_environment()
        self.write_current_fingerprint()
        self.write_fake_uv(1)

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("uv" in error and "current" in error for error in errors), errors)

    def test_every_verification_error_includes_bootstrap_recovery(self) -> None:
        self.create_roles()
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(errors)
        self.assertTrue(
            all("mise run bootstrap" in error for error in errors), errors
        )

    def test_missing_requirements_returns_bootstrap_recovery_instead_of_crashing(
        self,
    ) -> None:
        self.create_current_environment()
        self.write_current_fingerprint()
        (self.repo_root / "requirements.yml").unlink()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(errors)
        self.assertTrue(
            all("mise run bootstrap" in error for error in errors), errors
        )


if __name__ == "__main__":
    unittest.main()
