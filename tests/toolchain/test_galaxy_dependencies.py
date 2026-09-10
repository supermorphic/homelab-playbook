from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import ModuleType
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
GALAXY_DEPENDENCIES_PATH = REPO_ROOT / "scripts" / "galaxy_dependencies.py"


def load_galaxy_dependencies() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "galaxy_dependencies", GALAXY_DEPENDENCIES_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {GALAXY_DEPENDENCIES_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


galaxy_dependencies = load_galaxy_dependencies()


class GalaxyPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repo_root = self.root / "checkout-one"
        self.cache_root = self.root / "persistent-cache"
        self.fake_bin = self.root / "fake-bin"
        self.install_log = self.root / "install.log"
        self.repo_root.mkdir()
        self.fake_bin.mkdir()
        self.write_checkout(self.repo_root)
        self.write_fake_ansible_galaxy()
        self.original_environment = os.environ.copy()
        os.environ["PATH"] = f"{self.fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["GALAXY_TEST_LOG"] = str(self.install_log)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_environment)
        self.temporary_directory.cleanup()

    def write_checkout(
        self,
        root: Path,
        *,
        role_version: str = "1.2.3",
        role_source: str = "https://example.invalid/example.role.git",
        override: str = "---\n# local policy one\n",
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        (root / "requirements.yml").write_text(
            "---\n"
            "roles:\n"
            "  - name: example.role\n"
            f"    version: {role_version}\n"
            f"    src: {role_source}\n"
            "    scm: git\n"
            "collections:\n"
            "  - name: example.utilities\n"
            "    version: 7.8.9\n",
            encoding="utf-8",
        )
        override_path = (
            root
            / "overrides"
            / "ansible-galaxy"
            / "example.role"
            / "tasks"
            / "configure.yml"
        )
        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.write_text(override, encoding="utf-8")

    def write_fake_ansible_galaxy(self) -> None:
        executable = self.fake_bin / "ansible-galaxy"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, re, sys\n"
            "kind = sys.argv[1]\n"
            "req_flag = '--role-file' if kind == 'role' else '--requirements-file'\n"
            "path_flag = '--roles-path' if kind == 'role' else '--collections-path'\n"
            "requirements = pathlib.Path(sys.argv[sys.argv.index(req_flag) + 1]).read_text()\n"
            "target = pathlib.Path(sys.argv[sys.argv.index(path_flag) + 1])\n"
            "with pathlib.Path(os.environ['GALAXY_TEST_LOG']).open('a') as log:\n"
            "    log.write(kind + '\\n')\n"
            "if kind == 'role':\n"
            "    name = re.search(r'roles:.*?name: ([^\\n]+)', requirements, re.S).group(1)\n"
            "    version = re.search(r'roles:.*?version: ([^\\n]+)', requirements, re.S).group(1)\n"
            "    metadata = target / name / 'meta' / '.galaxy_install_info'\n"
            "    metadata.parent.mkdir(parents=True, exist_ok=True)\n"
            "    metadata.write_text('version: ' + version + '\\n')\n"
            "    task = target / name / 'tasks' / 'configure.yml'\n"
            "    task.parent.mkdir(parents=True, exist_ok=True)\n"
            "    task.write_text('---\\n# upstream\\n')\n"
            "else:\n"
            "    match = re.search(r'collections:.*?name: ([^.\\n]+)\\.([^\\n]+).*?version: ([^\\n]+)', requirements, re.S)\n"
            "    namespace, name, version = match.groups()\n"
            "    manifest = target / 'ansible_collections' / namespace / name / 'MANIFEST.json'\n"
            "    manifest.parent.mkdir(parents=True, exist_ok=True)\n"
            "    manifest.write_text(json.dumps({'collection_info': {'namespace': namespace, 'name': name, 'version': version}}))\n"
            "if os.environ.get('GALAXY_TEST_FAIL') == kind:\n"
            "    raise SystemExit(23)\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    def calls(self) -> list[str]:
        if not self.install_log.exists():
            return []
        return self.install_log.read_text(encoding="utf-8").splitlines()

    def assert_selected(self, checkout: Path, generation: Path) -> None:
        self.assertEqual(
            (generation / "roles").resolve(),
            (checkout / ".ansible" / "roles").resolve(),
        )
        self.assertEqual(
            (generation / "collections").resolve(),
            (checkout / ".ansible" / "collections").resolve(),
        )

    def workspace_state(self) -> dict[str, object]:
        ansible_root = self.repo_root / ".ansible"
        state: dict[str, object] = {}
        if not ansible_root.exists():
            return state
        for path in sorted(ansible_root.rglob("*")):
            relative = path.relative_to(ansible_root).as_posix()
            if path.is_symlink():
                state[relative] = ("symlink", os.readlink(path))
            elif path.is_file():
                state[relative] = ("file", path.read_bytes())
            elif path.is_dir():
                state[relative] = ("directory",)
        return state

    def create_legacy_workspace(self) -> Path:
        seed_cache = self.root / "seed-cache"
        generation = galaxy_dependencies.prepare(self.repo_root, seed_cache)
        roles = self.repo_root / ".ansible" / "roles"
        collections = self.repo_root / ".ansible" / "collections"
        current = self.repo_root / ".ansible" / ".galaxy-current"
        fingerprint = self.repo_root / ".ansible" / "requirements.sha256"
        fingerprint_bytes = fingerprint.read_bytes()
        roles.unlink()
        collections.unlink()
        fingerprint.unlink()
        current.unlink()
        shutil.copytree(generation / "roles", roles)
        shutil.copytree(generation / "collections", collections)
        fingerprint.write_bytes(fingerprint_bytes)
        return generation

    def test_first_preparation_installs_and_publishes_verified_generation(self) -> None:
        generation = galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual(["role", "collection"], self.calls())
        self.assert_selected(self.repo_root, generation)
        self.assertEqual(
            Path(".galaxy-current/roles"),
            (self.repo_root / ".ansible" / "roles").readlink(),
        )
        self.assertEqual(
            Path(".galaxy-current/collections"),
            (self.repo_root / ".ansible" / "collections").readlink(),
        )
        self.assertEqual(
            "---\n# local policy one\n",
            (generation / "roles" / "example.role" / "tasks" / "configure.yml").read_text(),
        )
        self.assertTrue((generation / "success.json").is_file())

    def test_unchanged_preparation_reuses_generation_without_install(self) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        calls_before = self.calls()

        second = galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual(first, second)
        self.assertEqual(calls_before, self.calls())

    def test_fresh_checkout_reuses_persistent_generation_without_install(self) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        calls_before = self.calls()
        fresh_checkout = self.root / "checkout-two"
        self.write_checkout(fresh_checkout)

        second = galaxy_dependencies.prepare(fresh_checkout, self.cache_root)

        self.assertEqual(first, second)
        self.assertEqual(calls_before, self.calls())
        self.assert_selected(fresh_checkout, second)

    def test_changed_role_version_installs_a_new_requirements_base(self) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        self.write_checkout(self.repo_root, role_version="2.0.0")

        second = galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertNotEqual(first, second)
        self.assertEqual(["role", "collection", "role", "collection"], self.calls())
        metadata = second / "roles" / "example.role" / "meta" / ".galaxy_install_info"
        self.assertEqual("version: 2.0.0\n", metadata.read_text())

    def test_changed_source_url_installs_a_new_requirements_base(self) -> None:
        galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        self.write_checkout(
            self.repo_root,
            role_source="https://example.invalid/alternate/example.role.git",
        )

        galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual(["role", "collection", "role", "collection"], self.calls())

    def test_override_only_change_copies_base_without_install(self) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        calls_before = self.calls()
        self.write_checkout(self.repo_root, override="---\n# local policy two\n")

        second = galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertNotEqual(first, second)
        self.assertEqual(calls_before, self.calls())
        self.assertEqual(
            "---\n# local policy two\n",
            (second / "roles" / "example.role" / "tasks" / "configure.yml").read_text(),
        )

    def test_python_lock_only_change_reuses_generation_without_install(self) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        calls_before = self.calls()
        (self.repo_root / "uv.lock").write_text("version = 2\n", encoding="utf-8")

        second = galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual(first, second)
        self.assertEqual(calls_before, self.calls())

    def test_missing_metadata_reinstalls_without_mutating_selected_generation(self) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        (first / "roles" / "example.role" / "meta" / ".galaxy_install_info").unlink()

        second = galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertNotEqual(first, second)
        self.assertEqual(["role", "collection", "role", "collection"], self.calls())
        self.assert_selected(self.repo_root, second)

    def test_missing_role_directory_reinstalls(self) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        role = first / "roles" / "example.role"
        for path in sorted(role.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        role.rmdir()

        second = galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertNotEqual(first, second)
        self.assertEqual(["role", "collection", "role", "collection"], self.calls())

    def test_explicit_repair_installs_new_generation(self) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        second = galaxy_dependencies.prepare(self.repo_root, self.cache_root, repair=True)

        self.assertNotEqual(first, second)
        self.assertEqual(["role", "collection", "role", "collection"], self.calls())

    def test_failed_candidate_preserves_previous_selection_and_success(self) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        first_success = (first / "success.json").read_bytes()
        first_fingerprint = (
            self.repo_root / ".ansible" / "requirements.sha256"
        ).read_bytes()
        self.write_checkout(self.repo_root, role_version="2.0.0")
        os.environ["GALAXY_TEST_FAIL"] = "collection"

        with self.assertRaises(Exception):
            galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assert_selected(self.repo_root, first)
        self.assertEqual(first_success, (first / "success.json").read_bytes())
        self.assertEqual(
            first_fingerprint,
            (self.repo_root / ".ansible" / "requirements.sha256").read_bytes(),
        )
        errors = galaxy_dependencies.dependencies.verify(self.repo_root)
        self.assertTrue(
            any("stale bootstrap fingerprint" in error for error in errors), errors
        )

    def test_missing_requirements_fails(self) -> None:
        (self.repo_root / "requirements.yml").unlink()

        with self.assertRaises(FileNotFoundError):
            galaxy_dependencies.prepare(self.repo_root, self.cache_root)

    def test_unmanaged_workspace_directory_is_preserved(self) -> None:
        unmanaged = self.repo_root / ".ansible" / "roles"
        unmanaged.mkdir(parents=True)
        marker = unmanaged / "operator-content"
        marker.write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "unmanaged"):
            galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual("keep\n", marker.read_text(encoding="utf-8"))

    def test_verified_legacy_workspace_is_archived_during_migration(self) -> None:
        self.create_legacy_workspace()

        galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        archives = list((self.cache_root / "legacy").iterdir())
        self.assertEqual(1, len(archives))
        self.assertTrue((archives[0] / "roles" / "example.role").is_dir())
        self.assertTrue(
            (
                archives[0]
                / "collections"
                / "ansible_collections"
                / "example"
                / "utilities"
            ).is_dir()
        )

    def test_failed_second_legacy_move_restores_workspace_exactly(self) -> None:
        self.create_legacy_workspace()
        before = self.workspace_state()
        original_rename = Path.rename

        def fail_collections_move(path: Path, target: Path) -> Path:
            if path.name == "collections":
                raise OSError("injected second legacy move failure")
            return original_rename(path, target)

        with mock.patch.object(Path, "rename", autospec=True, side_effect=fail_collections_move):
            with self.assertRaisesRegex(OSError, "injected second legacy move failure"):
                galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual(before, self.workspace_state())

    def test_failed_workspace_link_creation_restores_legacy_workspace(self) -> None:
        self.create_legacy_workspace()
        before = self.workspace_state()
        original_symlink_to = Path.symlink_to

        def fail_collections_link(path: Path, target: Path, *args: object, **kwargs: object) -> None:
            if path.parent.name == ".ansible" and "collections" in path.name:
                raise OSError("injected workspace link failure")
            original_symlink_to(path, target, *args, **kwargs)

        with mock.patch.object(Path, "symlink_to", autospec=True, side_effect=fail_collections_link):
            with self.assertRaisesRegex(OSError, "injected workspace link failure"):
                galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual(before, self.workspace_state())

    def test_failed_current_pointer_swap_preserves_selection_and_fingerprint(self) -> None:
        galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        before = self.workspace_state()
        self.write_checkout(self.repo_root, override="---\n# replacement policy\n")
        original_replace = os.replace

        def fail_current_swap(source: Path, target: Path) -> None:
            if Path(target).name == ".galaxy-current":
                raise OSError("injected current pointer failure")
            original_replace(source, target)

        with mock.patch.object(os, "replace", side_effect=fail_current_swap):
            with self.assertRaisesRegex(OSError, "injected current pointer failure"):
                galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual(before, self.workspace_state())

    def test_failed_fingerprint_publication_preserves_selection(self) -> None:
        galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        before = self.workspace_state()
        self.write_checkout(self.repo_root, override="---\n# replacement policy\n")
        original_write_text = Path.write_text

        def fail_fingerprint(path: Path, data: str, *args: object, **kwargs: object) -> int:
            if path.name == "requirements.sha256":
                raise OSError("injected fingerprint publication failure")
            return original_write_text(path, data, *args, **kwargs)

        with mock.patch.object(Path, "write_text", autospec=True, side_effect=fail_fingerprint):
            with self.assertRaisesRegex(OSError, "injected fingerprint publication failure"):
                galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual(before, self.workspace_state())

    def test_unknown_current_directory_is_preserved_without_workspace_changes(self) -> None:
        ansible_root = self.repo_root / ".ansible"
        current = ansible_root / ".galaxy-current"
        current.mkdir(parents=True)
        (current / "operator-content").write_text("keep\n", encoding="utf-8")
        before = self.workspace_state()

        with self.assertRaisesRegex(RuntimeError, "unmanaged.*galaxy-current"):
            galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertEqual(before, self.workspace_state())

    def test_legacy_cleanup_failure_is_nonfatal_and_preserves_backup(self) -> None:
        self.create_legacy_workspace()
        original_remove = galaxy_dependencies._remove_workspace_entry

        def fail_legacy_fingerprint_cleanup(path: Path) -> None:
            if (
                path.parent.parent.name == "legacy"
                and path.name == "requirements.sha256"
            ):
                raise OSError("injected legacy cleanup failure")
            original_remove(path)

        stderr = io.StringIO()
        with mock.patch.object(
            galaxy_dependencies,
            "_remove_workspace_entry",
            side_effect=fail_legacy_fingerprint_cleanup,
        ):
            with redirect_stderr(stderr):
                generation = galaxy_dependencies.prepare(
                    self.repo_root, self.cache_root
                )

        self.assert_selected(self.repo_root, generation)
        self.assertIn("cleanup", stderr.getvalue())
        backups = list((self.cache_root / "legacy").iterdir())
        self.assertEqual(1, len(backups))
        self.assertTrue((backups[0] / "requirements.sha256").exists())

    def test_workspace_backup_cleanup_failure_is_nonfatal_and_preserves_backup(
        self,
    ) -> None:
        original_rmtree = shutil.rmtree

        def fail_workspace_backup_cleanup(
            path: Path, *args: object, **kwargs: object
        ) -> None:
            if Path(path).name.startswith(".workspace-"):
                raise OSError("injected workspace backup cleanup failure")
            original_rmtree(path, *args, **kwargs)

        stderr = io.StringIO()
        with mock.patch.object(
            shutil, "rmtree", side_effect=fail_workspace_backup_cleanup
        ):
            with redirect_stderr(stderr):
                generation = galaxy_dependencies.prepare(
                    self.repo_root, self.cache_root
                )

        self.assert_selected(self.repo_root, generation)
        self.assertIn("cleanup", stderr.getvalue())
        backups = list(self.cache_root.glob(".workspace-*"))
        self.assertEqual(1, len(backups))

    def test_stable_workspace_links_remain_available_during_pointer_change(
        self,
    ) -> None:
        first = galaxy_dependencies.prepare(self.repo_root, self.cache_root)
        stable_links = {
            name: (self.repo_root / ".ansible" / name).readlink()
            for name in ("roles", "collections", "requirements.sha256")
        }
        self.write_checkout(self.repo_root, override="---\n# replacement policy\n")
        original_rename = Path.rename

        def reject_stable_link_move(path: Path, target: Path) -> Path:
            if path.parent.name == ".ansible":
                raise OSError("stable workspace link became unavailable")
            return original_rename(path, target)

        with mock.patch.object(
            Path, "rename", autospec=True, side_effect=reject_stable_link_move
        ):
            second = galaxy_dependencies.prepare(self.repo_root, self.cache_root)

        self.assertNotEqual(first, second)
        self.assert_selected(self.repo_root, second)
        self.assertEqual(
            stable_links,
            {
                name: (self.repo_root / ".ansible" / name).readlink()
                for name in ("roles", "collections", "requirements.sha256")
            },
        )


if __name__ == "__main__":
    unittest.main()
