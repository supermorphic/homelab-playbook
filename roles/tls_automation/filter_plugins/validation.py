"""Validate TLS role inputs and refuse adoption of unmanaged identities."""

from __future__ import annotations

import json
import hashlib
import importlib.util
from pathlib import Path
import sys


ROLE_FILES = Path(__file__).resolve().parents[1] / "files"
if str(ROLE_FILES) not in sys.path:
    sys.path.insert(0, str(ROLE_FILES))

from tls_runtime.policy import parse_policy  # noqa: E402
from tls_runtime.unit_contract import (exec_start_matches, merge_service_arrays,
                                       service_array_query)  # noqa: E402


_ACCOUNT_FIELDS = {"name", "uid", "gid"}
_KEYED_LOOKUPS = {"user_name", "user_uid", "group_name", "group_gid"}
_UNITS = {
    "homelab-tls-issuer.service": {
        "FragmentPath": "/etc/systemd/system/homelab-tls-issuer.service",
        "User": "svc-acme",
        "Group": "svc-acme",
        "SupplementaryGroups": "",
        "Type": "oneshot",
        "ExecCondition": "",
        "ExecStartPre": "",
        "ExecStartPost": "",
        "LoadCredential": "cloudflare-token:/etc/homelab-tls/cloudflare-token",
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "PrivateTmp": "yes",
        "ReadWritePaths": "/var/lib/homelab-tls-issuer",
        "TimeoutStartUSec": "15min",
        "ExecStart": "/usr/local/libexec/homelab-tls-issue",
    },
    "homelab-tls-renew.service": {
        "FragmentPath": "/etc/systemd/system/homelab-tls-renew.service",
        "User": "root",
        "Group": "root",
        "SupplementaryGroups": "",
        "Type": "oneshot",
        "ExecCondition": "",
        "ExecStartPre": "",
        "ExecStartPost": "",
        "LoadCredential": "",
        "NoNewPrivileges": "yes",
        "AmbientCapabilities": "cap_setuid",
        "ProtectSystem": "strict",
        "PrivateTmp": "yes",
        "ReadWritePaths": "/var/lib/homelab-tls /etc/caddy /var/lib/homelab-reverse-proxy",
        "TimeoutStartUSec": "1h",
        "ExecStart": "/usr/local/libexec/homelab-tls-reconcile",
    },
    "homelab-tls-renew.timer": {
        "FragmentPath": "/etc/systemd/system/homelab-tls-renew.timer",
        "Unit": "homelab-tls-renew.service",
        "Persistent": "yes",
        "RandomizedDelayUSec": "1h",
    },
}


def _database(value: object, fields: int) -> list[list[str]]:
    if not isinstance(value, str):
        raise ValueError("host identity database must be text")
    entries = []
    for line in value.splitlines():
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != fields:
            raise ValueError("host identity database is malformed")
        entries.append(parts)
    return entries


def validate_policy(value: object) -> object:
    """Return a policy unchanged after the host parser accepts its JSON form."""
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        parse_policy(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("TLS public policy does not match the approved schema") from error
    return value


def _proxy_configuration(value: object) -> dict:
    source = ROLE_FILES.parents[1] / "reverse_proxy/filter_plugins/proxy.py"
    specification = importlib.util.spec_from_file_location("tls_proxy_configuration", source)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.validate(value)


def proxy_policy(value: object, namespace: str, email: str, reader_gid: int) -> dict:
    """Derive TLS targets from the same strict declaration used by the proxy."""
    configuration = _proxy_configuration(value)
    policy = {
        "namespace": namespace,
        "email": email,
        "reader_gid": reader_gid,
        "endpoints": [
            {"hostname": route["hostname"], "address": address, "port": 443}
            for route in configuration["routes"]
            if route["certificate_name"] == "infra"
            for address in configuration["bind_addresses"]
        ],
    }
    return validate_policy(policy)


def committed_proxy_policy(raw: str, declared: object, namespace: str,
                           email: str, reader_gid: int) -> dict:
    """Require the committed, ingress-bound declaration before installing policy."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate desired manifest field")
            result[key] = value
        return result

    if not isinstance(raw, str) or len(raw.encode()) > 1024 * 1024:
        raise ValueError("invalid committed desired manifest")
    envelope = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(envelope, dict) or set(envelope) != {"version", "manifest", "ingress"}
            or type(envelope["version"]) is not int or envelope["version"] != 1
            or not isinstance(envelope["manifest"], str)):
        raise ValueError("invalid committed desired envelope")
    config = _proxy_configuration(json.loads(envelope["manifest"], object_pairs_hook=unique))
    expected_ingress = {
        "version": 1, "manifest_sha256": hashlib.sha256(envelope["manifest"].encode()).hexdigest(),
        "listen_addresses": config["bind_addresses"],
        "https_client_networks": config["client_sources"],
    }
    ingress = envelope["ingress"]
    if (not isinstance(ingress, dict) or type(ingress.get("version")) is not int
            or ingress != expected_ingress or config != _proxy_configuration(declared)
            or config.get("deferred_certificates") != ["infra"]):
        raise ValueError("provision the declared Caddy routes and verified ingress before TLS")
    return proxy_policy(config, namespace, email, reader_gid)


def _keyed_records(value: object) -> dict[str, list[str] | None]:
    """Parse the four explicit NSS lookups used for collision detection."""
    if not isinstance(value, list) or len(value) != len(_KEYED_LOOKUPS):
        raise ValueError("keyed issuer identity lookups are incomplete")
    records: dict[str, list[str] | None] = {}
    for result in value:
        if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
            raise ValueError("keyed issuer identity lookup result is malformed")
        lookup = result["item"].get("name")
        if lookup not in _KEYED_LOOKUPS or lookup in records:
            raise ValueError("keyed issuer identity lookup names are invalid")
        return_code = result.get("rc")
        stdout = result.get("stdout")
        if return_code not in (0, 2) or isinstance(return_code, bool):
            raise ValueError("keyed issuer identity lookup did not complete safely")
        if not isinstance(stdout, str):
            raise ValueError("keyed issuer identity lookup output is malformed")
        if return_code == 2:
            if stdout:
                raise ValueError("missing keyed issuer identity lookup returned data")
            records[lookup] = None
            continue
        fields = 7 if lookup.startswith("user_") else 4
        parsed = _database(stdout, fields)
        if len(parsed) != 1:
            raise ValueError("keyed issuer identity lookup must return one record")
        records[lookup] = parsed[0]
    if set(records) != _KEYED_LOOKUPS:
        raise ValueError("keyed issuer identity lookups are incomplete")
    return records


def validate_identity(
    declaration: object,
    passwd: object,
    group: object,
    marker: object,
    keyed_lookups: object,
) -> str:
    """Return new or managed; reject collisions, drift and account adoption."""
    if not isinstance(declaration, dict) or set(declaration) != _ACCOUNT_FIELDS:
        raise ValueError("issuer identity declaration has unexpected fields")
    if declaration["name"] != "svc-acme":
        raise ValueError("issuer account name is fixed")
    for field in ("uid", "gid"):
        value = declaration[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < 2**31:
            raise ValueError("issuer UID and GID must be explicit positive integers")

    users = _database(passwd, 7)
    groups = _database(group, 4)
    keyed = _keyed_records(keyed_lookups)
    name = declaration["name"]
    uid = declaration["uid"]
    gid = declaration["gid"]
    user_matches = [entry for entry in users if entry[0] == name or int(entry[2]) == uid]
    group_matches = [entry for entry in groups if entry[0] == name or int(entry[2]) == gid]

    marker_value = None
    if marker:
        if not isinstance(marker, str):
            raise ValueError("issuer allocation marker must be JSON text")
        try:
            marker_value = json.loads(marker)
        except (TypeError, ValueError) as error:
            raise ValueError("issuer allocation marker is malformed") from error
        if marker_value != declaration:
            raise ValueError("issuer allocation marker differs from the declaration")

    keyed_records = [entry for entry in keyed.values() if entry is not None]
    if not keyed_records and not user_matches and not group_matches:
        if marker_value is not None:
            raise ValueError("orphan issuer allocation marker requires migration review")
        return "new"

    if marker_value is None or any(entry is None for entry in keyed.values()):
        raise ValueError("existing issuer identity requires migration review")
    user = keyed["user_name"]
    user_by_uid = keyed["user_uid"]
    primary_group = keyed["group_name"]
    group_by_gid = keyed["group_gid"]
    assert user is not None and user_by_uid is not None
    assert primary_group is not None and group_by_gid is not None
    if (
        user != user_by_uid
        or primary_group != group_by_gid
        or user[0] != name
        or int(user[2]) != uid
        or int(user[3]) != gid
        or user[5] != "/var/lib/homelab-tls-issuer"
        or user[6] not in ("/usr/sbin/nologin", "/sbin/nologin")
        or primary_group[0] != name
        or int(primary_group[2]) != gid
        or primary_group[3]
    ):
        raise ValueError("managed issuer identity differs from its declaration")
    if any(entry != user for entry in user_matches):
        raise ValueError("issuer name or UID resolves to a conflicting account")
    if any(entry != primary_group for entry in group_matches):
        raise ValueError("issuer name or GID resolves to a conflicting group")
    if any(name in entry[3].split(",") for entry in groups):
        raise ValueError("issuer account has supplementary groups")
    if any(entry[0] != name and int(entry[3]) == gid for entry in users):
        raise ValueError("issuer primary group is shared")
    return "managed"


def _properties(value: object) -> dict[str, str]:
    if not isinstance(value, str):
        raise ValueError("effective systemd properties must be text")
    properties: dict[str, str] = {}
    for line in value.splitlines():
        key, separator, item = line.partition("=")
        if not separator or not key or key in properties:
            raise ValueError("effective systemd properties are malformed")
        properties[key] = item
    return properties


def validate_units(value: object, allow_absent: object, array_value: object) -> bool:
    """Require the fixed effective unit fragments and security properties."""
    if not isinstance(allow_absent, bool):
        raise ValueError("effective unit absence policy must be boolean")
    if not isinstance(value, list) or len(value) != len(_UNITS):
        raise ValueError("effective TLS unit inspection is incomplete")
    units: dict[str, dict[str, str]] = {}
    for result in value:
        if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
            raise ValueError("effective TLS unit result is malformed")
        name = result["item"].get("name")
        if name not in _UNITS or name in units or result.get("rc") != 0:
            raise ValueError("effective TLS unit result is invalid")
        units[name] = _properties(result.get("stdout"))
    if set(units) != set(_UNITS):
        raise ValueError("effective TLS unit inspection is incomplete")

    if not isinstance(array_value, list) or len(array_value) != 2:
        raise ValueError("typed TLS service inspection is incomplete")
    arrays = {}
    for result in array_value:
        if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
            raise ValueError("typed TLS service result is malformed")
        name = result["item"].get("name")
        if name not in _UNITS or not name.endswith(".service") or name in arrays:
            raise ValueError("typed TLS service result is invalid")
        arrays[name] = result

    for name, expected in _UNITS.items():
        properties = units[name]
        if properties.get("DropInPaths") != "":
            raise ValueError(f"{name} has forbidden effective drop-ins")
        load_state = properties.get("LoadState")
        if load_state == "not-found" and allow_absent:
            if properties.get("FragmentPath") != "":
                raise ValueError(f"{name} has an unexpected absent fragment")
            if name in arrays and arrays[name].get("skipped") is not True:
                raise ValueError("absent TLS service has unexpected typed evidence")
            continue
        if load_state != "loaded":
            raise ValueError(f"{name} is not loaded from a regular unit")
        if name in arrays:
            result = arrays[name]
            if (result.get("skipped", False) is not False
                    or not isinstance(result.get("rc"), int) or isinstance(result.get("rc"), bool)
                    or result["rc"] != 0):
                raise ValueError("typed TLS service query did not complete")
            properties = merge_service_arrays(properties, name, result.get("stdout"))
        for key, expected_value in expected.items():
            if key == "ExecStart":
                if not exec_start_matches(properties.get(key), expected_value):
                    raise ValueError(f"{name} has an unexpected command")
            elif (allow_absent and name == "homelab-tls-renew.service"
                  and key == "ReadWritePaths"
                  and properties.get(key) == "/var/lib/homelab-tls"):
                # Preflight permits upgrading only the earlier canonical,
                # narrower sandbox. Installed-unit verification stays strict.
                continue
            elif (allow_absent and name == "homelab-tls-renew.service"
                  and key == "AmbientCapabilities" and properties.get(key) == ""):
                # Provision may upgrade the previous unit, but renewal and
                # post-install verification require the UID-switch capability.
                continue
            elif properties.get(key) != expected_value:
                raise ValueError(f"{name} has an unexpected {key} property")
        if name.endswith(".timer"):
            calendar = properties.get("TimersCalendar", "")
            if not calendar.startswith(
                "{ OnCalendar=*-*-* 00,12:00:00 ; next_elapse="
            ):
                raise ValueError(f"{name} has an unexpected calendar")
    return True


class FilterModule:
    def filters(self):
        return {
            "tls_automation_validate_policy": validate_policy,
            "tls_automation_proxy_policy": proxy_policy,
            "tls_automation_committed_proxy_policy": committed_proxy_policy,
            "tls_automation_validate_identity": validate_identity,
            "tls_automation_validate_units": validate_units,
            "tls_automation_service_array_query": service_array_query,
        }
