#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dependencies  # noqa: E402


SUCCESS_FILE = "success.json"


def _hash_parts(parts: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content in parts:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _override_files(repo_root: Path) -> list[tuple[Path, Path]]:
    root = repo_root / "overrides" / "ansible-galaxy"
    if not root.exists():
        return []
    if not root.is_dir():
        raise RuntimeError(f"Galaxy override root is not a directory: {root}")
    result: list[tuple[Path, Path]] = []
    for source in sorted(root.rglob("*")):
        if source.is_symlink():
            raise RuntimeError(f"Galaxy override must not be a symlink: {source}")
        if source.is_file():
            relative = source.relative_to(root)
            if len(relative.parts) < 2:
                raise RuntimeError(f"Galaxy override must name a role and path: {source}")
            result.append((source, relative))
    return result


def _generation_hash(repo_root: Path, requirements_hash: str) -> str:
    parts = [("requirements.sha256", requirements_hash.encode("ascii"))]
    parts.extend(
        (relative.as_posix(), source.read_bytes())
        for source, relative in _override_files(repo_root)
    )
    return _hash_parts(parts)


def _verification_errors(repo_root: Path, generation: Path) -> list[str]:
    errors: list[str] = []
    requirements_path = repo_root / "requirements.yml"
    roles = dependencies.required_roles(requirements_path)
    collections = dependencies.required_collections(requirements_path)
    roles_root = generation / "roles"
    collections_root = generation / "collections" / "ansible_collections"

    for name, version in roles:
        role_path = roles_root / name
        installed = dependencies.installed_role_version(role_path)
        if not role_path.is_dir():
            errors.append(f"missing Galaxy role: {name}")
        elif version is None or not dependencies.EXACT_VERSION_PATTERN.fullmatch(
            version
        ):
            errors.append(f"Galaxy role {name} must declare an exact version")
        elif installed != version:
            errors.append(f"Galaxy role {name} has invalid installed version metadata")

    for name, version in collections:
        try:
            namespace, collection = name.split(".", maxsplit=1)
        except ValueError:
            errors.append(f"Galaxy collection {name} must use namespace.name")
            continue
        collection_path = collections_root / namespace / collection
        installed = dependencies.installed_collection_version(collection_path)
        if not collection_path.is_dir():
            errors.append(f"missing Galaxy collection: {name}")
        elif version is None or not dependencies.EXACT_VERSION_PATTERN.fullmatch(
            version
        ):
            errors.append(f"Galaxy collection {name} must declare an exact version")
        elif installed != version:
            errors.append(f"Galaxy collection {name} has invalid installed version metadata")

    for source, relative in _override_files(repo_root):
        target = roles_root / relative
        try:
            matches = source.read_bytes() == target.read_bytes()
        except OSError:
            matches = False
        if not matches:
            errors.append(f"installed override does not match source: {relative}")
    return errors


def _success_matches(path: Path, requirements_hash: str, generation_hash: str) -> bool:
    try:
        value = json.loads((path / SUCCESS_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return value == {
        "generation_sha256": generation_hash,
        "requirements_sha256": requirements_hash,
    }


def _valid(
    repo_root: Path, path: Path, requirements_hash: str, generation_hash: str
) -> bool:
    try:
        return _success_matches(
            path, requirements_hash, generation_hash
        ) and not _verification_errors(repo_root, path)
    except (OSError, UnicodeDecodeError):
        return False


def _write_success(path: Path, requirements_hash: str, generation_hash: str) -> None:
    value = {
        "generation_sha256": generation_hash,
        "requirements_sha256": requirements_hash,
    }
    (path / SUCCESS_FILE).write_text(
        json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
    )


def _install_base(repo_root: Path, cache_root: Path, requirements_hash: str) -> Path:
    bases = cache_root / "bases"
    bases.mkdir(parents=True, exist_ok=True)
    published = bases / requirements_hash
    if published.is_dir():
        if not _verification_errors_without_overrides(repo_root, published):
            return published

    candidate = Path(tempfile.mkdtemp(prefix=".base-", dir=bases))
    try:
        roles = candidate / "roles"
        collections = candidate / "collections"
        roles.mkdir()
        collections.mkdir()
        requirements = repo_root / "requirements.yml"
        subprocess.run(
            [
                "ansible-galaxy",
                "role",
                "install",
                "--role-file",
                str(requirements),
                "--roles-path",
                str(roles),
                "--force",
            ],
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            [
                "ansible-galaxy",
                "collection",
                "install",
                "--requirements-file",
                str(requirements),
                "--collections-path",
                str(collections),
                "--force",
            ],
            cwd=repo_root,
            check=True,
        )
        errors = _verification_errors_without_overrides(repo_root, candidate)
        if errors:
            raise RuntimeError("; ".join(errors))
        if published.exists():
            shutil.rmtree(published)
        candidate.rename(published)
        return published
    except BaseException:
        shutil.rmtree(candidate, ignore_errors=True)
        raise


def _verification_errors_without_overrides(
    repo_root: Path, generation: Path
) -> list[str]:
    errors: list[str] = []
    requirements_path = repo_root / "requirements.yml"
    roles = dependencies.required_roles(requirements_path)
    collections = dependencies.required_collections(requirements_path)
    for name, version in roles:
        role_path = generation / "roles" / name
        if (
            not role_path.is_dir()
            or version is None
            or dependencies.installed_role_version(role_path) != version
        ):
            errors.append(f"invalid Galaxy role: {name}")
    for name, version in collections:
        try:
            namespace, collection = name.split(".", maxsplit=1)
        except ValueError:
            errors.append(f"invalid Galaxy collection name: {name}")
            continue
        collection_path = (
            generation / "collections" / "ansible_collections" / namespace / collection
        )
        if (
            not collection_path.is_dir()
            or version is None
            or dependencies.installed_collection_version(collection_path) != version
        ):
            errors.append(f"invalid Galaxy collection: {name}")
    return errors


def _publish_generation(
    repo_root: Path,
    cache_root: Path,
    base: Path,
    requirements_hash: str,
    generation_hash: str,
) -> Path:
    generations = cache_root / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix=".generation-", dir=generations))
    try:
        shutil.copytree(base / "roles", candidate / "roles")
        shutil.copytree(base / "collections", candidate / "collections")
        for source, relative in _override_files(repo_root):
            target = candidate / "roles" / relative
            if not target.parent.is_dir():
                raise RuntimeError(f"Galaxy override target parent is missing: {relative}")
            shutil.copyfile(source, target)
        errors = _verification_errors(repo_root, candidate)
        if errors:
            raise RuntimeError("; ".join(errors))
        _write_success(candidate, requirements_hash, generation_hash)
        published = generations / f"{generation_hash}-{uuid.uuid4().hex}"
        candidate.rename(published)
        return published
    except BaseException:
        shutil.rmtree(candidate, ignore_errors=True)
        raise


def _verified_legacy_workspace(repo_root: Path) -> bool:
    roles = repo_root / ".ansible" / "roles"
    collections = repo_root / ".ansible" / "collections"
    fingerprint = repo_root / ".ansible" / "requirements.sha256"
    if not roles.is_dir() or not collections.is_dir():
        return False
    try:
        return (
            fingerprint.read_text(encoding="utf-8").strip()
            == dependencies.bootstrap_sha256(repo_root)
            and not _verification_errors(repo_root, repo_root / ".ansible")
        )
    except (OSError, UnicodeDecodeError):
        return False


def _publish_selection(
    repo_root: Path, cache_root: Path, generation: Path
) -> Path:
    fingerprint = dependencies.bootstrap_sha256(repo_root, generation / "roles")
    selection_hash = hashlib.sha256(
        f"{fingerprint}\0{generation.name}".encode("utf-8")
    ).hexdigest()
    selections = cache_root / "selections"
    selections.mkdir(parents=True, exist_ok=True)
    published = selections / selection_hash
    if published.is_dir():
        try:
            if (
                (published / "roles").resolve() == (generation / "roles").resolve()
                and (published / "collections").resolve()
                == (generation / "collections").resolve()
                and (published / "requirements.sha256").read_text(
                    encoding="utf-8"
                ).strip()
                == fingerprint
            ):
                return published
        except (OSError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"invalid published Galaxy selection: {published}")

    candidate = Path(tempfile.mkdtemp(prefix=".selection-", dir=selections))
    try:
        (candidate / "roles").symlink_to(generation / "roles")
        (candidate / "collections").symlink_to(generation / "collections")
        (candidate / "requirements.sha256").write_text(
            f"{fingerprint}\n", encoding="utf-8"
        )
        candidate.rename(published)
        return published
    except BaseException:
        shutil.rmtree(candidate, ignore_errors=True)
        raise


def _remove_workspace_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _cleanup_warning(path: Path, error: OSError) -> None:
    print(
        f"Galaxy workspace cleanup incomplete at {path}: {error}; "
        "preserved for later cleanup",
        file=sys.stderr,
    )


def _stable_workspace_links(ansible_root: Path, targets: dict[Path, Path]) -> bool:
    current = ansible_root / ".galaxy-current"
    return current.is_symlink() and all(
        link.is_symlink() and link.readlink() == target
        for link, target in targets.items()
    )


def _select(repo_root: Path, cache_root: Path, selection: Path) -> None:
    ansible_root = repo_root / ".ansible"
    ansible_root.mkdir(parents=True, exist_ok=True)
    targets = {
        ansible_root / "roles": Path(".galaxy-current") / "roles",
        ansible_root / "collections": Path(".galaxy-current") / "collections",
        ansible_root / "requirements.sha256": Path(".galaxy-current")
        / "requirements.sha256",
    }
    current = ansible_root / ".galaxy-current"
    if current.exists() and not current.is_symlink():
        raise RuntimeError(f"refusing to replace unmanaged galaxy-current: {current}")
    if _stable_workspace_links(ansible_root, targets):
        temporary_current = (
            ansible_root / f".galaxy-current.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary_current.symlink_to(selection)
            os.replace(temporary_current, current)
        except BaseException:
            _remove_workspace_entry(temporary_current)
            raise
        return

    existing_directories = [
        link
        for link in (ansible_root / "roles", ansible_root / "collections")
        if link.exists() and not link.is_symlink()
    ]
    if existing_directories and not _verified_legacy_workspace(repo_root):
        paths = ", ".join(str(path) for path in existing_directories)
        raise RuntimeError(f"refusing to replace unmanaged Galaxy directories: {paths}")

    if existing_directories:
        backup = cache_root / "legacy" / uuid.uuid4().hex
        backup.mkdir(parents=True)
        retain_legacy = True
    else:
        backup = Path(tempfile.mkdtemp(prefix=".workspace-", dir=cache_root))
        retain_legacy = False
    workspace_entries = [*targets, current]
    moved: list[tuple[Path, Path]] = []
    temporary_entries: list[Path] = []
    published_entries: list[Path] = []
    try:
        for path in workspace_entries:
            if path.is_symlink() or path.exists():
                backup_path = backup / path.name
                path.rename(backup_path)
                moved.append((path, backup_path))

        for link, target in targets.items():
            temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
            temporary_entries.append(temporary)
            temporary.symlink_to(target)
            os.replace(temporary, link)
            published_entries.append(link)
        temporary_current = ansible_root / f".galaxy-current.{uuid.uuid4().hex}.tmp"
        temporary_entries.append(temporary_current)
        temporary_current.symlink_to(selection)
        os.replace(temporary_current, current)
        published_entries.append(current)
    except BaseException:
        for temporary in temporary_entries:
            _remove_workspace_entry(temporary)
        for path in published_entries:
            _remove_workspace_entry(path)
        for path, backup_path in reversed(moved):
            backup_path.rename(path)
        shutil.rmtree(backup, ignore_errors=True)
        raise

    if retain_legacy:
        for name in (".galaxy-current", "requirements.sha256"):
            try:
                _remove_workspace_entry(backup / name)
            except OSError as error:
                _cleanup_warning(backup, error)
                break
    else:
        try:
            shutil.rmtree(backup)
        except OSError as error:
            _cleanup_warning(backup, error)


def prepare(repo_root: Path, cache_root: Path, repair: bool = False) -> Path:
    repo_root = repo_root.resolve()
    cache_root = cache_root.resolve()
    requirements = repo_root / "requirements.yml"
    requirements_hash = dependencies.requirements_sha256(requirements)
    generation_hash = _generation_hash(repo_root, requirements_hash)
    cache_root.mkdir(parents=True, exist_ok=True)

    with (cache_root / "prepare.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        generations = cache_root / "generations"
        matches = (
            sorted(generations.glob(f"{generation_hash}-*"))
            if generations.exists()
            else []
        )
        generation = next(
            (
                path
                for path in matches
                if not repair
                and _valid(repo_root, path, requirements_hash, generation_hash)
            ),
            None,
        )
        if generation is None:
            if repair or matches:
                base = _install_base_fresh(repo_root, cache_root, requirements_hash)
            else:
                base = _install_base(repo_root, cache_root, requirements_hash)
            generation = _publish_generation(
                repo_root, cache_root, base, requirements_hash, generation_hash
            )
        selection = _publish_selection(repo_root, cache_root, generation)
        _select(repo_root, cache_root, selection)
        return generation


def _install_base_fresh(
    repo_root: Path, cache_root: Path, requirements_hash: str
) -> Path:
    published = cache_root / "bases" / requirements_hash
    if published.exists():
        shutil.rmtree(published)
    return _install_base(repo_root, cache_root, requirements_hash)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    cache_root = Path(
        os.environ.get("HOMELAB_GALAXY_CACHE_DIR", repo_root / ".cache" / "galaxy")
    )
    roles = repo_root / ".ansible" / "roles"
    previous = roles.resolve().parent if roles.is_symlink() else None
    generation = prepare(repo_root, cache_root, repair=args.repair)
    action = "Reused verified" if previous == generation else "Selected"
    print(f"{action} Galaxy generation: {generation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
