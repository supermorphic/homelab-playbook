"""Pure validation helpers for platform-native security controls."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping


PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)
SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
JOURNAL_SIZE = re.compile(r"^[1-9][0-9]*(?:[KMGTPE])?$")
FIREWALL_EMPTY_FIELDS = (
    "sources",
    "services",
    "ports",
    "protocols",
    "source_ports",
    "forward_ports",
)


def _validate_private_sources(values: object) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValueError("firewall sources must be a non-empty list")
    validated: list[str] = []
    for value in values:
        if not isinstance(value, str) or "/" not in value:
            raise ValueError("firewall source must be a CIDR string")
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as error:
            raise ValueError("firewall source must be a strict CIDR") from error
        if network.network_address.is_unspecified or network.is_multicast:
            raise ValueError("firewall source cannot be unspecified or multicast")
        if not any(
            network.version == parent.version and network.subnet_of(parent)
            for parent in PRIVATE_NETWORKS
        ):
            raise ValueError("firewall source must be within a private range")
        validated.append(value)
    return validated


def security_baseline_firewall_rules(payload: Mapping[str, object]) -> list[str]:
    """Validate firewall inputs and return canonical desired rich rules."""
    management_sources = _validate_private_sources(payload.get("management_sources"))
    extensions = payload.get("services")
    if not isinstance(extensions, list):
        raise ValueError("firewall service extensions must be a list")

    rules = [
        f'rule family="{"ipv6" if ":" in source else "ipv4"}" '
        f'source address="{source}" service name="ssh" accept'
        for source in management_sources
    ]
    for extension in extensions:
        if not isinstance(extension, Mapping) or set(extension) != {"service", "sources"}:
            raise ValueError("firewall extension has an invalid shape")
        service = extension["service"]
        if not isinstance(service, str) or not SERVICE_NAME.fullmatch(service):
            raise ValueError("firewall extension service is invalid")
        if service.lower() == "ssh":
            raise ValueError("SSH sources are owned only by management_sources")
        for source in _validate_private_sources(extension["sources"]):
            rules.append(
                f'rule family="{"ipv6" if ":" in source else "ipv4"}" '
                f'source address="{source}" service name="{service}" accept'
            )
    return rules


def security_baseline_firewall_reload_required(
    runtime: Mapping[str, object],
    desired_rules: list[str],
    permanent_changed: bool,
) -> bool:
    """Return whether runtime has not consumed the staged permanent baseline."""
    if permanent_changed or "homelab" not in runtime.get("zones", []):
        return True
    if runtime.get("target") != "DROP" or runtime.get("forward") is not False:
        return True
    return bool(set(desired_rules) - set(runtime.get("rich_rules", [])))


def security_baseline_firewall_target_from_list_all(text: str) -> str:
    """Read one zone target from supported firewall-cmd list-all output."""
    targets = [
        line.partition(":")[2].strip()
        for raw_line in text.splitlines()
        if (line := raw_line.strip()).partition(":")[0] == "target"
    ]
    if len(targets) != 1 or targets[0] not in {"default", "ACCEPT", "DROP", "REJECT"}:
        raise ValueError("firewall zone list output has no valid unique target")
    return targets[0]


def security_baseline_firewall_policy_errors(
    states: Mapping[str, object],
    desired_rules: list[str],
    management_interface: str,
) -> list[str]:
    """Compare relevant runtime and permanent zone state to exact policy."""
    errors: list[str] = []
    for mode in ("runtime", "permanent"):
        state = states.get(mode)
        if not isinstance(state, Mapping):
            errors.append(f"{mode} state is absent")
            continue
        if state.get("target") != "DROP":
            errors.append(f"{mode} target is not DROP")
        if state.get("forward") is not False:
            errors.append(f"{mode} forwarding is enabled")
        if state.get("masquerade") is not False:
            errors.append(f"{mode} masquerading is enabled")
        if state.get("interface_zone") != "homelab":
            errors.append(f"{mode} management interface is not in homelab")
        if sorted(state.get("interfaces", [])) != [management_interface]:
            errors.append(f"{mode} interfaces do not match policy")
        for field in FIREWALL_EMPTY_FIELDS:
            if state.get(field, []):
                errors.append(f"{mode} {field} contains an opening")
        if sorted(state.get("rich_rules", [])) != sorted(desired_rules):
            errors.append(f"{mode} rich rules do not match policy")

    runtime = states.get("runtime")
    permanent = states.get("permanent")
    if isinstance(runtime, Mapping) and isinstance(permanent, Mapping):
        scalar_fields = {
            "target",
            "forward",
            "masquerade",
            "interface_zone",
        }
        list_fields = {"interfaces", "rich_rules", *FIREWALL_EMPTY_FIELDS}
        scalar_difference = any(
            runtime.get(field) != permanent.get(field) for field in scalar_fields
        )
        list_difference = any(
            sorted(runtime.get(field, [])) != sorted(permanent.get(field, []))
            for field in list_fields
        )
        if scalar_difference or list_difference:
            errors.append("runtime and permanent policy differ")
    return errors


def security_baseline_firewall_conflicting_sources(
    text: str,
    management_peer: str,
) -> list[str]:
    """Return source bindings that can override this management connection."""
    peer = ipaddress.ip_address(management_peer)
    zone = ""
    conflicts: list[str] = []
    for raw_line in text.splitlines():
        if raw_line and not raw_line[:1].isspace():
            zone = raw_line.strip()
            continue
        line = raw_line.strip()
        if zone != "homelab" and line.startswith("sources:"):
            for source in line.partition(":")[2].split():
                try:
                    network = ipaddress.ip_network(source, strict=False)
                except ValueError:
                    conflicts.append(source)
                    continue
                if network.version == peer.version and peer in network:
                    conflicts.append(source)
    return conflicts


def security_baseline_journal_size_is_valid(value: object) -> bool:
    """Accept a conservative non-zero subset of systemd IEC size syntax."""
    return isinstance(value, str) and JOURNAL_SIZE.fullmatch(value) is not None


def security_baseline_journald_effective_values(text: str) -> dict[str, str]:
    """Reduce ordered cat-config scalar assignments with last-value precedence."""
    wanted = {"Storage", "SystemMaxUse", "SystemKeepFree"}
    effective: dict[str, str] = {}
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Journal" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in wanted:
            effective[key] = value.strip()
    return effective


class FilterModule:
    """Expose role-specific validation filters to Ansible."""

    def filters(self) -> dict[str, object]:
        return {
            "security_baseline_firewall_rules": security_baseline_firewall_rules,
            "security_baseline_firewall_reload_required": security_baseline_firewall_reload_required,
            "security_baseline_firewall_target_from_list_all": security_baseline_firewall_target_from_list_all,
            "security_baseline_firewall_policy_errors": security_baseline_firewall_policy_errors,
            "security_baseline_firewall_conflicting_sources": security_baseline_firewall_conflicting_sources,
            "security_baseline_journal_size_is_valid": security_baseline_journal_size_is_valid,
            "security_baseline_journald_effective_values": security_baseline_journald_effective_values,
        }
