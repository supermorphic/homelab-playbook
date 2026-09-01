"""Pure effective-state reducers for the read-only OS baseline verifier."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping


PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)
EMPTY_FIREWALL_FIELDS = (
    "sources", "services", "ports", "protocols", "source_ports", "forward_ports",
)


def _private_sources(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("firewall sources must be a non-empty list")
    result: list[str] = []
    for source in value:
        if not isinstance(source, str):
            raise ValueError("firewall source must be a CIDR string")
        try:
            network = ipaddress.ip_network(source, strict=True)
        except ValueError as error:
            raise ValueError("firewall source must be a strict CIDR") from error
        if not any(network.version == parent.version and network.subnet_of(parent) for parent in PRIVATE_NETWORKS):
            raise ValueError("firewall source must be private")
        result.append(source)
    return result


def os_baseline_verify_firewall_rules(payload: Mapping[str, object]) -> list[str]:
    """Independently validate inputs and create canonical rich rules."""
    if not isinstance(payload, Mapping):
        raise ValueError("firewall policy must be a mapping")
    rules: list[str] = []
    for source in _private_sources(payload.get("management_sources")):
        family = "ipv6" if ":" in source else "ipv4"
        rules.append(f'rule family="{family}" source address="{source}" service name="ssh" accept')
    services = payload.get("services")
    if not isinstance(services, list):
        raise ValueError("firewall service extensions must be a list")
    for extension in services:
        if not isinstance(extension, Mapping) or set(extension) != {"service", "sources"}:
            raise ValueError("firewall extension has an invalid shape")
        service = extension["service"]
        if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service) or service.lower() == "ssh":
            raise ValueError("firewall extension service is invalid")
        for source in _private_sources(extension["sources"]):
            family = "ipv6" if ":" in source else "ipv4"
            rules.append(f'rule family="{family}" source address="{source}" service name="{service}" accept')
    return rules


def os_baseline_verify_firewall_state_errors(state: Mapping[str, object], desired: list[str], interface: str) -> list[str]:
    """Return exact-policy differences without producer-role filters."""
    errors: list[str] = []
    if state.get("target") != "DROP": errors.append("target")
    if state.get("forward") is not False: errors.append("forward")
    if state.get("masquerade") is not False: errors.append("masquerade")
    if state.get("interface_zone") != "homelab": errors.append("interface-zone")
    if sorted(state.get("interfaces", [])) != [interface]: errors.append("interfaces")
    for field in EMPTY_FIREWALL_FIELDS:
        if state.get(field, []): errors.append(field)
    if sorted(state.get("rich_rules", [])) != sorted(desired): errors.append("rich-rules")
    return errors


def os_baseline_verify_conflicting_sources(text: str, peer: str) -> list[str]:
    """Find source-zone bindings outside homelab that include the management peer."""
    address = ipaddress.ip_address(peer)
    zone = ""
    conflicts: list[str] = []
    for raw in text.splitlines():
        if raw and not raw[:1].isspace():
            zone = raw.strip()
        elif zone != "homelab" and raw.strip().startswith("sources:"):
            for source in raw.partition(":")[2].split():
                try:
                    network = ipaddress.ip_network(source, strict=False)
                except ValueError:
                    conflicts.append(source)
                else:
                    if network.version == address.version and address in network:
                        conflicts.append(source)
    return conflicts


def os_baseline_verify_journald_values(text: str) -> dict[str, str]:
    """Return ordered [Journal] values, retaining an empty final reset."""
    values: dict[str, str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "Journal" and "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in {"Storage", "SystemMaxUse", "SystemKeepFree"}:
                values[key.strip()] = value.strip()
    return values


def os_baseline_verify_ini_values(text: str) -> dict[str, dict[str, str]]:
    """Parse INI text with section-aware, last-assignment precedence."""
    values: dict[str, dict[str, str]] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            values.setdefault(section, {})
        elif section and "=" in line:
            key, value = line.split("=", 1)
            values[section][key.strip()] = value.strip()
    return values


def os_baseline_verify_apt_policy(text: str) -> dict[str, object]:
    """Parse the selected APT policy and reject ambiguous origin blocks."""
    stripped = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    scalar = dict(re.findall(r'(?m)^\s*([^\s{]+)\s+"([^"]*)"\s*;', stripped))
    origin = re.search(r"Unattended-Upgrade::Origins-Pattern\s*\{(.*?)\};", stripped, re.S)
    if origin is None:
        return {"scalar": scalar, "origins": []}
    origins = re.findall(r'"([^"]*)"\s*;', origin.group(1))
    expected_origin = "origin=Debian,codename=${distro_codename}-security,label=Debian-Security"
    if origins != [expected_origin]:
        raise ValueError("APT security origins are not exact")
    return {"scalar": scalar, "origins": origins}


def os_baseline_verify_timer_values(text: str) -> dict[str, object]:
    """Parse ordered [Timer] assignments; an empty calendar resets prior ones."""
    values: dict[str, object] = {"OnCalendar": []}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "Timer" and "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            if key == "OnCalendar":
                if value == "": values[key] = []
                else: values[key] = [*values[key], value]
            else: values[key] = value
    return values


class FilterModule:
    def filters(self) -> dict[str, object]:
        return {
            "os_baseline_verify_firewall_rules": os_baseline_verify_firewall_rules,
            "os_baseline_verify_firewall_state_errors": os_baseline_verify_firewall_state_errors,
            "os_baseline_verify_conflicting_sources": os_baseline_verify_conflicting_sources,
            "os_baseline_verify_journald_values": os_baseline_verify_journald_values,
            "os_baseline_verify_ini_values": os_baseline_verify_ini_values,
            "os_baseline_verify_apt_policy": os_baseline_verify_apt_policy,
            "os_baseline_verify_timer_values": os_baseline_verify_timer_values,
        }
