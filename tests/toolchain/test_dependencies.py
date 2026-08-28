from __future__ import annotations

import importlib.util
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

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_virtualenv_executable(self) -> None:
        executable = self.repo_root / ".venv" / "bin" / "ansible-playbook"
        executable.parent.mkdir(parents=True)
        executable.touch()

    def create_roles(self, *, excluding: str | None = None) -> None:
        for role_name in ROLE_NAMES:
            if role_name != excluding:
                (self.repo_root / ".ansible" / "roles" / role_name).mkdir(
                    parents=True
                )

    def write_current_fingerprint(self) -> None:
        dependencies.write_fingerprint(self.repo_root)

    def test_verify_rejects_missing_virtualenv(self) -> None:
        self.create_roles()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(
            any(".venv/bin/ansible-playbook" in error for error in errors), errors
        )

    def test_verify_rejects_missing_role(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles(excluding="l3d.unbound")
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("l3d.unbound" in error for error in errors), errors)

    def test_verify_rejects_stale_requirements_fingerprint(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles()
        fingerprint = self.repo_root / ".ansible" / "requirements.sha256"
        fingerprint.write_text("stale\n")

        errors = dependencies.verify(self.repo_root)

        self.assertTrue(any("stale" in error for error in errors), errors)

    def test_verify_accepts_matching_roles_and_fingerprint(self) -> None:
        self.create_virtualenv_executable()
        self.create_roles()
        self.write_current_fingerprint()

        errors = dependencies.verify(self.repo_root)

        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
