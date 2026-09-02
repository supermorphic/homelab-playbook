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
    "icmp_blocks",
)
FIREWALL_DIRECT_READS = {
    "--get-all-chains",
    "--get-all-rules",
    "--get-all-passthroughs",
}


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
        f'source address="{source}" port port="22" protocol="tcp" accept'
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


def security_baseline_firewall_peer_is_covered(
    management_peer: str,
    management_sources: object,
) -> bool:
    """Return whether the active SSH peer is inside a desired source CIDR."""
    peer = ipaddress.ip_address(management_peer)
    return any(
        peer.version == network.version and peer in network
        for network in (
            ipaddress.ip_network(source, strict=True)
            for source in _validate_private_sources(management_sources)
        )
    )


def _firewall_zone_bindings(text: object) -> list[tuple[str, str, str]]:
    if not isinstance(text, str):
        raise ValueError("firewall zone bindings must be text")
    zone = ""
    seen: set[str] = set()
    bindings: list[tuple[str, str, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if not raw_line[:1].isspace():
            zone = raw_line.split()[0]
            if not SERVICE_NAME.fullmatch(zone) or zone in seen:
                raise ValueError("firewall zone output is malformed")
            seen.add(zone)
            continue
        field, separator, values = raw_line.strip().partition(":")
        if separator and field in {"interfaces", "sources"}:
            if not zone:
                raise ValueError("firewall binding has no zone")
            bindings.extend((zone, field, value) for value in values.split())
    return bindings


def _firewall_direct_openings(results: object) -> list[str]:
    if not isinstance(results, list):
        raise ValueError("firewall direct results must be a list")
    indexed: dict[str, str] = {}
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError("firewall direct result is malformed")
        item = result.get("item")
        stdout = result.get("stdout")
        if (
            not isinstance(item, str)
            or item in indexed
            or not isinstance(stdout, str)
        ):
            raise ValueError("firewall direct result is malformed")
        indexed[item] = stdout
    if set(indexed) != FIREWALL_DIRECT_READS:
        raise ValueError("firewall direct result layout is not exact")
    return [item for item, stdout in indexed.items() if stdout.strip()]


def _expected_firewalld_policy_lines(os_family: str) -> list[str]:
    rules = [
        "neighbour-advertisement",
        "neighbour-solicitation",
        "redirect",
        "router-advertisement",
    ]
    if os_family == "RedHat":
        rules.extend(
            (
                "mld-listener-done",
                "mld-listener-query",
                "mld-listener-report",
                "mld2-listener-report",
            )
        )
    elif os_family != "Debian":
        raise ValueError("firewalld policy platform is unsupported")
    return [
        "allow-host-ipv6",
        "priority: -15000",
        "target: CONTINUE",
        "ingress-zones: ANY",
        "egress-zones: HOST",
        "services:",
        "ports:",
        "protocols:",
        "masquerade: no",
        "forward-ports:",
        "source-ports:",
        "icmp-blocks:",
        "rich rules:",
        *[
            f'rule family="ipv6" icmp-type name="{name}" accept'
            for name in rules
        ],
    ]


def _firewalld_policy_is_exact(policy_text: str, os_family: str) -> bool:
    lines = [line.strip() for line in policy_text.splitlines() if line.strip()]
    if lines and lines[0] == "allow-host-ipv6 (active)":
        lines[0] = "allow-host-ipv6"
    return sorted(lines) == sorted(_expected_firewalld_policy_lines(os_family))


def security_baseline_firewall_global_surface_errors(
    binding_text: object,
    direct_results: object,
    policy_text: object,
    management_interface: str,
    allow_transition: bool,
    os_family: str,
) -> list[str]:
    """Reject global firewall surfaces outside the repository baseline."""
    if not isinstance(policy_text, str):
        raise ValueError("firewall policy objects must be text")
    bindings = _firewall_zone_bindings(binding_text)
    errors: list[str] = []
    sources = [binding for binding in bindings if binding[1] == "sources"]
    interfaces = [binding for binding in bindings if binding[1] == "interfaces"]
    if sources:
        errors.append("source bindings are unsupported")
    if allow_transition:
        if any(binding[2] != management_interface for binding in interfaces):
            errors.append("an unsupported interface binding is active")
        if len(interfaces) > 1:
            errors.append("the management interface has multiple bindings")
    else:
        expected = (
            [("homelab", "interfaces", management_interface)]
            if management_interface
            else []
        )
        if interfaces != expected:
            errors.append("interface bindings do not match policy")
    if _firewall_direct_openings(direct_results):
        errors.append("direct firewall openings are unsupported")
    if not _firewalld_policy_is_exact(policy_text, os_family):
        errors.append("firewalld policy objects do not match platform policy")
    return errors


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
        if state.get("icmp_block_inversion") is not False:
            errors.append(f"{mode} ICMP block inversion is enabled")
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
            "icmp_block_inversion",
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
            "security_baseline_firewall_peer_is_covered": security_baseline_firewall_peer_is_covered,
            "security_baseline_firewall_global_surface_errors": security_baseline_firewall_global_surface_errors,
            "security_baseline_firewall_reload_required": security_baseline_firewall_reload_required,
            "security_baseline_firewall_target_from_list_all": security_baseline_firewall_target_from_list_all,
            "security_baseline_firewall_policy_errors": security_baseline_firewall_policy_errors,
            "security_baseline_journal_size_is_valid": security_baseline_journal_size_is_valid,
            "security_baseline_journald_effective_values": security_baseline_journald_effective_values,
        }
