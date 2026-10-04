"""Explicit disposable Debian target validation and observational SSH preflight."""

import base64
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_COMMANDS = (
    "podman", "systemctl", "systemd-run", "ip", "nft", "mount", "umount",
    "newuidmap", "newgidmap",
)


def positive(value):
    return type(value) is int and value > 0


def overlaps(start, count, other_start, other_count):
    return start < other_start + other_count and other_start < start + count


def validate_target(target: dict) -> dict:
    """Reject undeclared authority and unsafe allocations before contacting SSH."""
    fields = {"ssh_alias", "host_key", "host_key_fingerprint", "ownership_marker", "state_root",
              "controller", "worker", "limits", "allowed_endpoints", "denied_endpoints"}
    try:
        if not isinstance(target, dict) or set(target) != fields:
            raise ValueError
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", target["ssh_alias"]) is None:
            raise ValueError
        marker = target["ownership_marker"]
        if re.fullmatch(r"[a-z][a-z0-9-]{0,47}", marker) is None:
            raise ValueError
        if target["state_root"] != f"/var/lib/forgejo-runner-fixture/{marker}":
            raise ValueError
        key_type, encoded = target["host_key"].split(" ")
        if key_type != "ssh-ed25519":
            raise ValueError
        key = base64.b64decode(encoded, validate=True)
        prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
        if len(key) != len(prefix) + 32 or not key.startswith(prefix):
            raise ValueError
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
        if target["host_key_fingerprint"] != fingerprint:
            raise ValueError
        account_fields = {"user", "uid", "gid", "subuid_start", "subgid_start", "subid_count"}
        for name in ("controller", "worker"):
            account = target[name]
            if not isinstance(account, dict) or set(account) != account_fields:
                raise ValueError
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", account["user"]) is None:
                raise ValueError
            if any(not positive(account[key]) for key in account_fields - {"user"}):
                raise ValueError
            if account["uid"] < 1000 or account["gid"] < 1000 or account["subid_count"] < 65536:
                raise ValueError
            if any(account[key] + account["subid_count"] > 2**32 for key in ("subuid_start", "subgid_start")):
                raise ValueError
        controller, worker = target["controller"], target["worker"]
        if any(controller[key] == worker[key] for key in ("user", "uid", "gid")):
            raise ValueError
        for key, identity in (("subuid_start", "uid"), ("subgid_start", "gid")):
            if overlaps(controller[key], controller["subid_count"], worker[key], worker["subid_count"]):
                raise ValueError
            for account in (controller, worker):
                for other in (controller, worker):
                    if account[key] <= other[identity] < account[key] + account["subid_count"]:
                        raise ValueError
        limits = target["limits"]
        if set(limits) != {"memory_bytes", "pids", "disk_bytes", "cpu_percent", "idle_seconds", "job_seconds"}:
            raise ValueError
        if any(not positive(value) for value in limits.values()):
            raise ValueError
        endpoints = []
        for key in ("allowed_endpoints", "denied_endpoints"):
            if not isinstance(target[key], list) or not target[key]:
                raise ValueError
            group = set()
            for endpoint in target[key]:
                if set(endpoint) != {"address", "port"}:
                    raise ValueError
                address = ipaddress.ip_address(endpoint["address"])
                if not positive(endpoint["port"]) or endpoint["port"] > 65535:
                    raise ValueError
                group.add((str(address), endpoint["port"]))
            endpoints.append(group)
        if endpoints[0] & endpoints[1]:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError, struct.error) as error:
        raise ValueError("Invalid disposable Debian fixture descriptor") from error
    return target


def validate_observation(target: dict, observed: dict) -> dict:
    """Require an independently observed empty, administrator-owned fixture."""
    validate_target(target)
    try:
        if observed["os_id"] != "debian" or observed["os_version"] != "13":
            raise ValueError
        architecture = {"x86_64": "amd64", "aarch64": "arm64"}[observed["architecture"]]
        if observed["effective_uid"] != 0 or observed["cgroup_v2"] is not True or observed["user_namespaces"] is not True:
            raise ValueError
        marker = observed["marker"]
        if (marker["value"] != target["ownership_marker"] or marker["uid"] != 0
                or marker["regular"] is not True or marker["mode"] & 0o022):
            raise ValueError
        if observed["state_root_exists"] is not False or observed["state_parent_secure"] is not True:
            raise ValueError
        if any(observed["commands"].get(command) is not True for command in REQUIRED_COMMANDS):
            raise ValueError
        for name in ("controller", "worker"):
            account = target[name]
            for user in observed["users"]:
                if (user["user"] == account["user"] or user["uid"] == account["uid"]
                        or account["subuid_start"] <= user["uid"] < account["subuid_start"] + account["subid_count"]):
                    raise ValueError
            for group in observed["groups"]:
                if (group["group"] == account["user"] or group["gid"] == account["gid"]
                        or account["subgid_start"] <= group["gid"] < account["subgid_start"] + account["subid_count"]):
                    raise ValueError
            for ranges, start, identity in (("subuids", "subuid_start", "uid"),
                                             ("subgids", "subgid_start", "gid")):
                for allocation in observed[ranges]:
                    if (not positive(allocation["start"]) or not positive(allocation["count"])
                            or allocation["user"] in (account["user"], str(account["uid"]))
                            or allocation["start"] <= account[identity] < allocation["start"] + allocation["count"]
                            or overlaps(account[start], account["subid_count"], allocation["start"], allocation["count"])):
                        raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("Disposable Debian fixture preflight failed; no setup was performed") from error
    return {**observed, "architecture": architecture}


def inspect_target(target: dict) -> dict:
    """Observe a pinned SSH host without setup, repair, registration or socket use."""
    validate_target(target)
    source = (ROOT / "scripts/forgejo_runner/host_probe.py").read_text()
    payload = source + "\nprint(json.dumps(observe(" + repr(target) + ")))\n"
    with tempfile.TemporaryDirectory(prefix="forgejo-runner-host-") as directory:
        known_hosts = Path(directory) / "known_hosts"
        known_hosts.write_text(target["ownership_marker"] + " " + target["host_key"] + "\n")
        known_hosts.chmod(0o600)
        argv = ["ssh", "-T"]
        for option in (
            "BatchMode=yes", "StrictHostKeyChecking=yes", "CheckHostIP=no",
            "GlobalKnownHostsFile=/dev/null", f"UserKnownHostsFile={known_hosts}",
            f"HostKeyAlias={target['ownership_marker']}", "ClearAllForwardings=yes",
            "PermitLocalCommand=no", "RequestTTY=no", "RemoteCommand=none", "ConnectTimeout=10",
            "ControlMaster=no", "ControlPath=none", "UpdateHostKeys=no",
            "ForwardAgent=no", "ForwardX11=no", "Tunnel=no", "HostKeyAlgorithms=ssh-ed25519",
        ):
            argv.extend(("-o", option))
        argv.extend((target["ssh_alias"], "sudo -n -- /usr/bin/python3 -"))
        try:
            result = subprocess.run(argv, input=payload, text=True, capture_output=True,
                                    timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Disposable fixture SSH observation failed") from error
    if result.returncode:
        raise RuntimeError("Disposable fixture SSH observation failed; check supplied access and host key")
    try:
        observed = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise ValueError("Disposable fixture returned invalid observation") from error
    return validate_observation(target, observed)


def run(images: Path | None, target: Path | None, *, preflight_only: bool = False) -> int:
    """Separate observational prerequisites from the unfinished containment gate."""
    if target is None:
        raise ValueError("An explicit disposable Debian host fixture descriptor is required")
    target = target.resolve()
    if not target.is_relative_to((ROOT / ".tmp").resolve()):
        raise ValueError("Disposable host descriptors must be local inputs under .tmp")
    try:
        if target.stat().st_size > 65536:
            raise ValueError("Host descriptor exceeds its bounded input size")
        descriptor = json.loads(target.read_text())
    except (OSError, ValueError) as error:
        raise ValueError("Cannot load disposable host descriptor") from error
    validate_target(descriptor)
    if not preflight_only:
        # Do not touch a target until combined acceptance is implemented. A
        # successful observation alone must never satisfy the host fixture gate.
        raise RuntimeError("Host containment acceptance is not implemented; use --preflight-only for observation")
    result = inspect_target(descriptor)
    print(f"Disposable Debian preflight passed ({result['architecture']}); containment was not tested")
    return 0
