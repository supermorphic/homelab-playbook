#!/usr/bin/python3
"""Fail closed unless active repositories use distribution signature trust."""

from __future__ import annotations

import pathlib
import re
import shlex
import subprocess
import sys
import urllib.parse
from collections.abc import Mapping


DEBIAN_ALLOWED_HOSTS = {"deb.debian.org", "security.debian.org"}
DEBIAN_ARCHIVE_KEYRINGS = {
    "/usr/share/keyrings/debian-archive-keyring.gpg",
    "/usr/share/keyrings/debian-archive-keyring.pgp",
}
DEBIAN_GLOBAL_BYPASSES = {
    "Acquire::AllowInsecureRepositories",
    "Acquire::AllowWeakRepositories",
    "Acquire::AllowDowngradeToInsecureRepositories",
    "APT::Get::AllowUnauthenticated",
}
DEBIAN_SOURCE_BYPASSES = {
    "trusted",
    "allow-insecure",
    "allow-weak",
    "allow-downgrade-to-insecure",
}
TRUE_VALUES = {"1", "yes", "true", "on"}


def _rooted(root: pathlib.Path, target: str) -> pathlib.Path:
    path = pathlib.PurePosixPath(target)
    if path.is_absolute():
        return root / str(path).lstrip("/") if root != pathlib.Path("/") else pathlib.Path(path)
    return root / pathlib.Path(path)


def _parse_apt_config(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.endswith(";"):
            continue
        try:
            fields = shlex.split(line[:-1])
        except ValueError as error:
            raise ValueError("APT configuration dump is malformed") from error
        if len(fields) >= 2:
            values[fields[0].casefold()] = fields[1]
    return values


def _effective_apt_get_config(config: Mapping[str, str]) -> dict[str, str]:
    """Move apt-get's binary-specific subtree into the effective root."""
    effective = dict(config)
    prefix = "binary::apt-get::"
    for key, value in config.items():
        if key.startswith(prefix):
            effective[key.removeprefix(prefix)] = value
    return effective


def _apt_path(config: Mapping[str, str], leaf: str) -> str:
    root = pathlib.PurePosixPath(config.get("dir", "/"))
    etc = pathlib.PurePosixPath(config.get("dir::etc", "etc/apt"))
    if not etc.is_absolute():
        etc = root / etc
    value = pathlib.PurePosixPath(
        config.get(leaf.casefold(), leaf.rsplit("::", 1)[-1])
    )
    if not value.is_absolute():
        value = etc / value
    return str(value)


def _require_debian_archive_key(value: str, root: pathlib.Path) -> None:
    keys = value.split()
    if len(keys) != 1 or keys[0] not in DEBIAN_ARCHIVE_KEYRINGS:
        raise ValueError("Debian source does not use the distribution archive keyring")
    if not _rooted(root, keys[0]).is_file():
        raise ValueError("Debian distribution archive keyring is absent")


def _validate_debian_uri(uri: str) -> None:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in DEBIAN_ALLOWED_HOSTS:
        raise ValueError("enabled Debian source is not a distribution repository")


def _validate_one_line_source(line: str, root: pathlib.Path) -> int:
    try:
        fields = shlex.split(line, comments=True)
    except ValueError as error:
        raise ValueError("Debian one-line source is malformed") from error
    if not fields or fields[0] not in {"deb", "deb-src"}:
        return 0

    options: dict[str, str] = {}
    uri_index = 1
    if uri_index < len(fields) and fields[uri_index].startswith("["):
        option_fields: list[str] = []
        while uri_index < len(fields):
            field = fields[uri_index]
            option_fields.append(field)
            uri_index += 1
            if field.endswith("]"):
                break
        if not option_fields[-1].endswith("]"):
            raise ValueError("Debian source options are malformed")
        option_fields[0] = option_fields[0][1:]
        option_fields[-1] = option_fields[-1][:-1]
        for option in option_fields:
            if not option:
                continue
            key, separator, value = option.partition("=")
            if not separator:
                raise ValueError("Debian source option has no value")
            options[key.lower()] = value
    if uri_index >= len(fields):
        raise ValueError("Debian source has no URI")
    for bypass in DEBIAN_SOURCE_BYPASSES:
        if options.get(bypass, "").lower() in TRUE_VALUES:
            raise ValueError("Debian source enables an authentication bypass")
    _require_debian_archive_key(options.get("signed-by", ""), root)
    _validate_debian_uri(fields[uri_index])
    return 1


def _deb822_paragraphs(text: str) -> list[dict[str, str]]:
    paragraphs: list[dict[str, str]] = []
    for raw_paragraph in re.split(r"\n\s*\n", text):
        values: dict[str, str] = {}
        current = ""
        for raw_line in raw_paragraph.splitlines():
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            if raw_line[:1].isspace():
                if not current:
                    raise ValueError("Debian deb822 continuation has no field")
                values[current] += " " + raw_line.strip()
                continue
            key, separator, value = raw_line.partition(":")
            if not separator:
                raise ValueError("Debian deb822 source is malformed")
            current = key.strip().lower()
            if current in values:
                raise ValueError("Debian deb822 source repeats a field")
            values[current] = value.strip()
        if values:
            paragraphs.append(values)
    return paragraphs


def _validate_deb822_source(text: str, root: pathlib.Path) -> int:
    enabled = 0
    for values in _deb822_paragraphs(text):
        if values.get("enabled", "yes").lower() in {"0", "no", "false", "off"}:
            continue
        if not set(values.get("types", "").split()) & {"deb", "deb-src"}:
            continue
        for bypass in DEBIAN_SOURCE_BYPASSES:
            if values.get(bypass, "").lower() in TRUE_VALUES:
                raise ValueError("Debian source enables an authentication bypass")
        _require_debian_archive_key(values.get("signed-by", ""), root)
        uris = values.get("uris", "").split()
        if not uris:
            raise ValueError("Debian deb822 source has no URI")
        for uri in uris:
            _validate_debian_uri(uri)
        enabled += 1
    return enabled


def validate_debian_configuration(apt_config_text: str, root: pathlib.Path = pathlib.Path("/")) -> None:
    """Validate effective APT settings and only its authoritative source paths."""
    config = _effective_apt_get_config(_parse_apt_config(apt_config_text))
    for option in DEBIAN_GLOBAL_BYPASSES:
        if config.get(option.casefold(), "").lower() in TRUE_VALUES:
            raise ValueError("effective APT configuration enables an authentication bypass")

    source_files: list[pathlib.Path] = []
    source_list = _rooted(root, _apt_path(config, "Dir::Etc::sourcelist"))
    if source_list.is_file():
        source_files.append(source_list)
    source_parts = _rooted(root, _apt_path(config, "Dir::Etc::sourceparts"))
    if source_parts.is_dir():
        source_files.extend(sorted(source_parts.glob("*.list")))
        source_files.extend(sorted(source_parts.glob("*.sources")))

    enabled = 0
    for path in source_files:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".sources":
            enabled += _validate_deb822_source(text, root)
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                enabled += _validate_one_line_source(line, root)
    if not enabled:
        raise ValueError("APT has no enabled distribution source")



def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "Debian":
        print("usage: validate_repository_trust.py Debian", file=sys.stderr)
        return 2
    try:
        apt_config = subprocess.run(
            ["/usr/bin/apt-config", "dump"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        validate_debian_configuration(apt_config)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"repository trust validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
