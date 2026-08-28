from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES_PATH = REPO_ROOT / "scripts" / "dependencies.py"
ROLE_NAMES = (
    "robertdebock.bootstrap",
    "geerlingguy.security",
    "geerlingguy.docker",
    "xanmanning.k3s",
    "r_pufky.pihole",
    "l3d.unbound",
)
ROLE_VERSIONS = {
    "robertdebock.bootstrap": "7.1.5",
    "geerlingguy.security": "3.0.0",
    "geerlingguy.docker": "7.6.0",
    "xanmanning.k3s": "v3.6.1",
    "r_pufky.pihole": "3.0.4",
    "l3d.unbound": "v1.1.0",
}
REQUIREMENTS = """---
roles:
  - name: robertdebock.bootstrap
    version: 7.1.5
  - name: geerlingguy.security
    version: 3.0.0
  - name: geerlingguy.docker
    version: 7.6.0
  - name: xanmanning.k3s
    version: v3.6.1
  - name: r_pufky.pihole
    version: 3.0.4
  - name: l3d.unbound
    version: v1.1.0
"""


def load_dependencies() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dependencies", DEPENDENCIES_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {DEPENDENCIES_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dependencies = load_dependencies()


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
        for role_name in ROLE_NAMES:
            if role_name != excluding:
                role_path = self.repo_root / ".ansible" / "roles" / role_name
                role_path.mkdir(parents=True)
                if role_name != missing_metadata:
                    metadata_path = role_path / "meta" / ".galaxy_install_info"
                    metadata_path.parent.mkdir()
                    version = (
                        "0.0.0"
                        if role_name == wrong_metadata
                        else ROLE_VERSIONS[role_name]
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

    def create_current_environment(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles()
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
        self.create_roles(excluding="geerlingguy.docker")
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(
            any("geerlingguy.docker" in error for error in errors), errors
        )

    def test_verify_rejects_stale_requirements_fingerprint(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles()
        self.create_installed_override()
        fingerprint = self.repo_root / ".ansible" / "requirements.sha256"
        fingerprint.write_text("stale\n")

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("stale" in error for error in errors), errors)

    def test_verify_accepts_matching_roles_and_fingerprint(self) -> None:
        self.create_current_environment()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertEqual([], errors)

    def test_verify_rejects_missing_galaxy_install_version_metadata(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles(missing_metadata="geerlingguy.security")
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(
            any("geerlingguy.security" in error and "metadata" in error for error in errors),
            errors,
        )

    def test_verify_rejects_wrong_galaxy_install_version_metadata(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles(wrong_metadata="r_pufky.pihole")
        self.create_installed_override()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(
            any(
                "r_pufky.pihole" in error
                and "3.0.4" in error
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
            requirements_path.read_text().replace("version: 7.1.5", "version: latest")
        )
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("exact version" in error for error in errors), errors)

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
