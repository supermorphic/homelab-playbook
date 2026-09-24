#!/usr/bin/env python3
"""Strict validation for Talos lifecycle request references."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml


ACTIONS = {
    "maintenance-check",
    "maintenance-enter",
    "maintenance-exit",
    "reboot",
    "abrupt-loss-test",
}
BASE_KEYS = {
    "talos_node",
    "talos_kubeconfig",
    "talos_kube_context",
    "talos_talosconfig",
    "talos_talos_context",
    "talos_source_dir",
    "talos_source_revision",
}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
NAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?")


class InputError(ValueError):
    """The operator request is outside the fixed lifecycle interface."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate request key: {key}")
        result[key] = value
    return result


def _plain_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "{{" in value or "{%" in value:
        raise InputError(f"{name} must be a nonempty literal string")
    if any(character in value for character in ("\0", "\n", "\r")):
        raise InputError(f"{name} contains an invalid character")
    return value


def _absolute_existing_file(value: object, name: str) -> str:
    path = Path(_plain_string(value, name))
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise InputError(f"{name} must be an absolute regular file")
    return str(path.resolve())


def _absolute_directory(value: object, name: str, *, must_exist: bool = True) -> str:
    path = Path(_plain_string(value, name))
    if not path.is_absolute() or path.is_symlink():
        raise InputError(f"{name} must be an absolute directory path")
    if must_exist and not path.is_dir():
        raise InputError(f"{name} must be an existing directory")
    return str(path.resolve(strict=must_exist))


class _UniqueYamlLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise InputError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _read_yaml(path: Path, description: str) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueYamlLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise InputError(f"could not read {description}") from error
    if not isinstance(value, dict):
        raise InputError(f"{description} must contain a mapping")
    return value


def _require_kube_context(path: Path, context: str) -> None:
    config = _read_yaml(path, "Kubernetes configuration")
    contexts = config.get("contexts")
    if not isinstance(contexts, list) or not any(
        isinstance(item, dict) and item.get("name") == context for item in contexts
    ):
        raise InputError("selected Kubernetes context is absent")


def _require_talos_context(path: Path, context: str) -> None:
    config = _read_yaml(path, "Talos configuration")
    contexts = config.get("contexts")
    if not isinstance(contexts, dict) or context not in contexts:
        raise InputError("selected Talos context is absent")


def has_controlling_terminal() -> bool:
    terminal = os.environ.get("TALOS_LIFECYCLE_TTY_PATH", "/dev/tty")
    try:
        descriptor = os.open(terminal, os.O_RDONLY | os.O_NOCTTY)
    except OSError:
        return False
    os.close(descriptor)
    return True


def validate_request(value: dict, action: str) -> dict:
    if action not in ACTIONS:
        raise InputError("unsupported Talos lifecycle action")
    if not isinstance(value, dict):
        raise InputError("request must be an object")
    allowed = set(BASE_KEYS)
    if action != "maintenance-check":
        allowed.add("talos_confirmation")
    if action == "abrupt-loss-test":
        allowed.update({"talos_test_confirmation", "talos_evidence_dir"})
    unknown = set(value) - allowed
    missing = BASE_KEYS - set(value)
    if unknown:
        raise InputError(f"unsupported request key: {sorted(unknown)[0]}")
    if missing:
        raise InputError(f"missing request key: {sorted(missing)[0]}")

    node = _plain_string(value["talos_node"], "talos_node")
    if not NAME_PATTERN.fullmatch(node):
        raise InputError("talos_node is invalid")
    kubeconfig = _absolute_existing_file(value["talos_kubeconfig"], "talos_kubeconfig")
    talosconfig = _absolute_existing_file(value["talos_talosconfig"], "talos_talosconfig")
    kube_context = _plain_string(value["talos_kube_context"], "talos_kube_context")
    talos_context = _plain_string(value["talos_talos_context"], "talos_talos_context")
    _require_kube_context(Path(kubeconfig), kube_context)
    _require_talos_context(Path(talosconfig), talos_context)
    source_dir = _absolute_directory(value["talos_source_dir"], "talos_source_dir")
    revision = _plain_string(value["talos_source_revision"], "talos_source_revision")
    if not SHA_PATTERN.fullmatch(revision):
        raise InputError("talos_source_revision must be a full lowercase SHA")

    result = {
        "talos_node": node,
        "talos_kubeconfig": kubeconfig,
        "talos_kube_context": kube_context,
        "talos_talosconfig": talosconfig,
        "talos_talos_context": talos_context,
        "talos_source_dir": source_dir,
        "talos_source_revision": revision,
    }
    if action != "maintenance-check":
        result["talos_confirmation"] = _plain_string(
            value.get("talos_confirmation"), "talos_confirmation"
        )
    if action == "abrupt-loss-test":
        if value.get("talos_test_confirmation") != "chaos:node-abrupt-loss":
            raise InputError("talos_test_confirmation is invalid")
        if not has_controlling_terminal():
            raise InputError("abrupt-loss-test requires a controlling terminal")
        result["talos_test_confirmation"] = "chaos:node-abrupt-loss"
        if "talos_evidence_dir" in value:
            result["talos_evidence_dir"] = _absolute_directory(
                value["talos_evidence_dir"], "talos_evidence_dir", must_exist=False
            )
    return result


def load_request(path: Path, action: str) -> dict:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise InputError("request path must be an absolute regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InputError("request must contain one valid JSON object") from error
    return validate_request(value, action)
