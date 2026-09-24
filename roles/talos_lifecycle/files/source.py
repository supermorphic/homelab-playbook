#!/usr/bin/env python3
"""Prepare one private desired-state and verifier checkout without network use."""

from __future__ import annotations

import os
import hashlib
import json
import stat
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml


SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
CACHE_FILES = {"manifest.json", "cilium.tgz", "cert-manager.tgz", "metallb.tgz", "envoy-gateway.tgz", "external-dns.tgz"}


class SourceError(ValueError):
    """Desired source did not meet the fixed trust contract."""


class _UniqueYamlLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise SourceError(f"duplicate desired-data key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _git(source: Path, arguments: list[str], *, capture: bool = True) -> str:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(source),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
    }
    try:
        result = subprocess.run(
            ["git", "-C", str(source), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SourceError("desired source Git validation failed") from error
    return result.stdout.strip() if capture else ""


def _parse_desired(path: Path) -> tuple[str, dict[str, str], list[str]]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueYamlLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise SourceError("desired Talos input is invalid") from error
    if not isinstance(value, dict) or not isinstance(value.get("endpoint"), str):
        raise SourceError("desired Talos input lacks an endpoint")
    entries = value.get("nodes")
    if not isinstance(entries, list) or len(entries) != 3:
        raise SourceError("desired Talos input must contain exactly three nodes")
    nodes: dict[str, str] = {}
    addresses: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("controlPlane") is not True:
            raise SourceError("every desired node must be a control-plane node")
        name, address = entry.get("hostname"), entry.get("ipAddress")
        if not isinstance(name, str) or not isinstance(address, str) or not name or not address:
            raise SourceError("desired node identity is invalid")
        if name in nodes or address in addresses:
            raise SourceError("desired nodes and addresses must be unique")
        nodes[name] = address
        addresses.add(address)
    return value["endpoint"], nodes, list(nodes.values())


def _parse_probe_target(path: Path) -> tuple[str, str]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueYamlLoader)
        hostnames = value["spec"]["hostnames"]
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError) as error:
        raise SourceError("desired echo probe target is invalid") from error
    if (
        not isinstance(hostnames, list)
        or len(hostnames) != 1
        or not isinstance(hostnames[0], str)
        or not hostnames[0]
        or any(character in hostnames[0] for character in ("/", "\0", "\n", "\r"))
    ):
        raise SourceError("desired echo probe target must contain one DNS hostname")
    return hostnames[0], f"https://{hostnames[0]}/"


def _copy_recovery_cache(source: Path, destination: Path, revision: str) -> Path:
    if not source.is_dir() or source.is_symlink():
        raise SourceError("prepared recovery chart cache is missing")
    entries = list(source.iterdir())
    files = {entry.name for entry in entries if entry.is_file() and not entry.is_symlink()}
    if (
        files != CACHE_FILES
        or len(entries) != len(CACHE_FILES)
        or any(entry.is_symlink() for entry in entries)
        or any(not entry.is_file() for entry in entries)
    ):
        raise SourceError("prepared recovery chart cache layout is invalid")
    source_stats: dict[str, os.stat_result] = {}
    for name in CACHE_FILES:
        metadata = (source / name).lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SourceError("prepared recovery chart cache file is not private and regular")
        source_stats[name] = metadata
    destination.mkdir(parents=True, mode=0o700)
    for name in sorted(CACHE_FILES):
        source_path = source / name
        try:
            descriptor = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
            source_stat = os.fstat(descriptor)
            expected = source_stats[name]
            if ((source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns, source_stat.st_nlink) !=
                    (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns, expected.st_nlink)):
                raise SourceError("prepared recovery chart cache changed during private copy")
            with os.fdopen(descriptor, "rb") as source_handle:
                data = source_handle.read()
                final_stat = os.fstat(source_handle.fileno())
            if ((final_stat.st_size, final_stat.st_mtime_ns, final_stat.st_nlink) !=
                    (source_stat.st_size, source_stat.st_mtime_ns, source_stat.st_nlink)):
                raise SourceError("prepared recovery chart cache changed during private copy")
            destination_path = destination / name
            destination_path.write_bytes(data)
            os.chmod(destination_path, 0o600)
        except OSError as error:
            raise SourceError("prepared recovery chart cache changed during private copy") from error
    for name, expected in source_stats.items():
        current = (source / name).lstat()
        if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_nlink) !=
                (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns, expected.st_nlink)):
            raise SourceError("prepared recovery chart cache changed during private copy")
    try:
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SourceError("prepared recovery chart manifest is invalid") from error
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or manifest.get("sourceRevision") != revision:
        raise SourceError("prepared recovery chart cache has the wrong source revision")
    charts = manifest.get("charts")
    if not isinstance(charts, dict) or set(charts) != {name.removesuffix(".tgz") for name in CACHE_FILES if name.endswith(".tgz")}:
        raise SourceError("prepared recovery chart manifest entries are invalid")
    for logical, value in charts.items():
        if not isinstance(value, dict) or set(value) != {"file", "chartName", "version", "sha256"}:
            raise SourceError("prepared recovery chart manifest entry is invalid")
        archive = destination / str(value["file"])
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if archive.name != f"{logical}.tgz" or value["sha256"] != digest:
            raise SourceError("prepared recovery chart digest is invalid")
    return destination


def prepare_source(source_dir: Path, revision: str, destination: Path, trusted_origin: str) -> dict:
    source_dir = source_dir.resolve()
    if not source_dir.is_dir() or source_dir.is_symlink():
        raise SourceError("desired source must be a real directory")
    if not SHA_PATTERN.fullmatch(revision):
        raise SourceError("desired revision must be a full lowercase SHA")
    if _git(source_dir, ["remote", "get-url", "origin"]) != trusted_origin:
        raise SourceError("desired source origin is not trusted")
    resolved = _git(source_dir, ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"])
    if resolved != revision or _git(source_dir, ["rev-parse", "HEAD"]) != revision:
        raise SourceError("desired source HEAD does not match the requested revision")
    _git(source_dir, ["diff", "--quiet", "HEAD", "--"], capture=False)
    _git(source_dir, ["diff", "--cached", "--quiet", "HEAD", "--"], capture=False)
    wanted = "talos/talconfig.yaml"
    probe = "kubernetes/apps/testing/echo/app/httproute.yaml"
    for relative in (wanted, probe):
        if _git(source_dir, ["ls-files", "--error-unmatch", relative]) != relative:
            raise SourceError("required desired input is not tracked")
        source_file = source_dir / relative
        if not source_file.is_file() or source_file.is_symlink() or source_dir not in source_file.resolve().parents:
            raise SourceError("desired input escapes the source checkout")

    destination = destination.resolve()
    if destination.exists() or source_dir == destination or source_dir in destination.parents:
        raise SourceError("private snapshot destination is unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "git", "-c", "protocol.file.allow=always", "clone", "--quiet",
                "--no-local", "--no-hardlinks", "--no-checkout", str(source_dir), str(destination),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(destination.parent),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        _git(destination, ["config", "core.hooksPath", os.devnull], capture=False)
        _git(destination, ["remote", "set-url", "origin", trusted_origin], capture=False)
        _git(destination, ["checkout", "--quiet", "--detach", revision], capture=False)
        if _git(destination, ["rev-parse", "HEAD"]) != revision:
            raise SourceError("private snapshot revision mismatch")
        api_server, nodes, endpoints = _parse_desired(destination / wanted)
        probe_dns_name, probe_https_url = _parse_probe_target(destination / probe)
        recovery_cache = _copy_recovery_cache(
            source_dir / ".cache/recovery-helm",
            destination / ".cache/recovery-helm",
            revision,
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {
        "revision": revision,
        "snapshot_dir": str(destination),
        "verification_dir": str(destination),
        "api_server": api_server,
        "nodes": nodes,
        "talos_endpoints": endpoints,
        "probe_dns_name": probe_dns_name,
        "probe_https_url": probe_https_url,
        "recovery_helm_cache": str(recovery_cache),
    }
