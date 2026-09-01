"""Independent exact-state controls for the complete baseline scenario."""

from __future__ import annotations

import re
from collections.abc import Mapping


EXPECTED_RICH_RULE = (
    'rule family="ipv4" source address="10.0.0.0/8" '
    'service name="ssh" accept'
)
EMPTY_ZONE_READS = {
    "--list-interfaces": "interfaces",
    "--list-sources": "sources",
    "--list-services": "services",
    "--list-ports": "ports",
    "--list-protocols": "protocols",
    "--list-source-ports": "source-ports",
    "--list-forward-ports": "forward-ports",
    "--list-icmp-blocks": "icmp-blocks",
}
EXPECTED_ZONE_READS = {
    "--list-all",
    *EMPTY_ZONE_READS,
    "--list-rich-rules",
    "--query-forward",
    "--query-masquerade",
}
EXPECTED_DIRECT_READS = {
    "--get-all-chains",
    "--get-all-rules",
    "--get-all-passthroughs",
}
EXPECTED_APT_ORIGIN = (
    "origin=Debian,codename=${distro_codename}-security,label=Debian-Security"
)
EXPECTED_TIMER = {
    "OnCalendar": ["*-*-* 04:00:00"],
    "RandomizedDelaySec": "0",
    "Persistent": "true",
}
EXPECTED_SSH_POLICY = {
    "allowusers": "allowusers ansible",
    "pubkeyauthentication": "pubkeyauthentication yes",
    "passwordauthentication": "passwordauthentication no",
    "kbdinteractiveauthentication": "kbdinteractiveauthentication no",
    "permitemptypasswords": "permitemptypasswords no",
    "permitrootlogin": "permitrootlogin no",
    "allowagentforwarding": "allowagentforwarding no",
    "allowtcpforwarding": "allowtcpforwarding no",
    "x11forwarding": "x11forwarding no",
    "permittunnel": "permittunnel no",
}


def system_maintenance_molecule_baseline_sshd_errors(
    lines: object,
) -> list[str]:
    """Return connection-specific SSH policy differences."""
    if not isinstance(lines, list) or not all(
        isinstance(line, str) for line in lines
    ):
        raise ValueError("effective SSH policy must be a list of lines")
    return [
        name
        for name, expected in EXPECTED_SSH_POLICY.items()
        if lines.count(expected) != 1
    ]


def _command_results(
    results: object,
    expected_items: set[str],
) -> dict[str, Mapping[str, object]]:
    if not isinstance(results, list):
        raise ValueError("command results must be a list")
    indexed: dict[str, Mapping[str, object]] = {}
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError("command result must be a mapping")
        item = result.get("item")
        if not isinstance(item, str) or item in indexed:
            raise ValueError("command result item is absent or duplicated")
        stdout = result.get("stdout")
        lines = result.get("stdout_lines")
        rc = result.get("rc")
        if (
            not isinstance(stdout, str)
            or not isinstance(lines, list)
            or not all(isinstance(line, str) for line in lines)
            or not isinstance(rc, int)
        ):
            raise ValueError("command result evidence is malformed")
        indexed[item] = result
    if set(indexed) != expected_items:
        raise ValueError("command result layout is not exact")
    return indexed


def system_maintenance_molecule_baseline_firewall_errors(
    configuration: object,
    zone_results: object,
    direct_results: object,
    binding_text: object,
    policy_text: object,
    os_family: object,
) -> list[str]:
    """Return exact permanent firewall policy differences."""
    if (
        not isinstance(configuration, str)
        or not isinstance(binding_text, str)
        or not isinstance(policy_text, str)
    ):
        raise ValueError("firewalld configuration must be text")
    zone = _command_results(zone_results, EXPECTED_ZONE_READS)
    direct = _command_results(direct_results, EXPECTED_DIRECT_READS)
    errors: list[str] = []

    default_zones = re.findall(
        r"(?m)^DefaultZone=([^\s#]+)\s*$",
        configuration,
    )
    if default_zones != ["homelab"]:
        errors.append("default-zone")

    target = re.search(
        r"(?m)^\s*target:\s*(\S+)\s*$",
        str(zone["--list-all"]["stdout"]),
    )
    if target is None or target.group(1) != "DROP":
        errors.append("target")
    if zone["--query-forward"]["rc"] != 1:
        errors.append("forward")
    if zone["--query-masquerade"]["rc"] != 1:
        errors.append("masquerade")

    for item, field in EMPTY_ZONE_READS.items():
        if str(zone[item]["stdout"]).split():
            errors.append(field)

    rich_rules = [
        line.strip()
        for line in zone["--list-rich-rules"]["stdout_lines"]
        if line.strip()
    ]
    if rich_rules != [EXPECTED_RICH_RULE]:
        errors.append("rich-rules")
    if any(str(result["stdout"]).strip() for result in direct.values()):
        errors.append("direct-openings")
    for raw in binding_text.splitlines():
        if raw[:1].isspace():
            field, separator, values = raw.strip().partition(":")
            if separator and field in {"interfaces", "sources"} and values.split():
                errors.append("zone-bindings")
                break
    if os_family not in {"Debian", "RedHat"}:
        raise ValueError("firewall policy platform is unsupported")
    policy_rules = [
        "neighbour-advertisement",
        "neighbour-solicitation",
        "redirect",
        "router-advertisement",
    ]
    if os_family == "RedHat":
        policy_rules.extend(
            (
                "mld-listener-done",
                "mld-listener-query",
                "mld-listener-report",
                "mld2-listener-report",
            )
        )
    expected_policy = [
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
            for name in policy_rules
        ],
    ]
    actual_policy = [line.strip() for line in policy_text.splitlines() if line.strip()]
    if actual_policy and actual_policy[0] == "allow-host-ipv6 (active)":
        actual_policy[0] = "allow-host-ipv6"
    if sorted(actual_policy) != sorted(expected_policy):
        errors.append("policy-objects")
    return errors


def _apt_assignments(text: object) -> list[tuple[str, str]]:
    if not isinstance(text, str):
        raise ValueError("APT configuration must be text")
    stripped = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    return re.findall(r'(?m)^\s*([^\s]+)\s+"([^"]*)"\s*;', stripped)


def _timer_values(text: object) -> dict[str, object]:
    if not isinstance(text, str):
        raise ValueError("timer configuration must be text")
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
                values[key] = [] if not value else [*values[key], value]
            else:
                values[key] = value
    return values


def _timer_errors(text: object, enabled: object) -> list[str]:
    errors: list[str] = []
    if _timer_values(text) != EXPECTED_TIMER:
        errors.append("timer")
    if not isinstance(enabled, str) or enabled.strip() != "enabled":
        errors.append("timer-enabled")
    return errors


def system_maintenance_molecule_baseline_debian_updater_errors(
    apt_configuration: object,
    timer_configuration: object,
    timer_enabled: object,
) -> list[str]:
    """Return exact Debian native security updater policy differences."""
    assignments = _apt_assignments(apt_configuration)
    expected_scalars = {
        "APT::Periodic::Update-Package-Lists": "1",
        "APT::Periodic::Unattended-Upgrade": "1",
        "Unattended-Upgrade::Automatic-Reboot": "false",
        "Unattended-Upgrade::Automatic-Reboot-WithUsers": "true",
        "Unattended-Upgrade::Automatic-Reboot-Time": "04:30",
        "Unattended-Upgrade::Remove-Unused-Dependencies": "false",
    }
    errors: list[str] = []
    for key, expected in expected_scalars.items():
        if [value for name, value in assignments if name == key] != [expected]:
            errors.append(key)
    origins = [
        value
        for key, value in assignments
        if key == "Unattended-Upgrade::Origins-Pattern::"
    ]
    if origins != [EXPECTED_APT_ORIGIN]:
        errors.append("origins")
    return [*errors, *_timer_errors(timer_configuration, timer_enabled)]


def _ini_values(text: object) -> dict[str, dict[str, str]]:
    if not isinstance(text, str):
        raise ValueError("DNF configuration must be text")
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


def system_maintenance_molecule_baseline_rocky_updater_errors(
    dnf_configuration: object,
    timer_configuration: object,
    timer_enabled: object,
) -> list[str]:
    """Return exact Rocky native security updater policy differences."""
    values = _ini_values(dnf_configuration)
    expected = {
        "commands": {
            "upgrade_type": "security",
            "download_updates": "yes",
            "apply_updates": "yes",
            "reboot": "never",
        },
        "emitters": {"emit_via": "stdio"},
    }
    errors = [] if values == expected else ["dnf-policy"]
    return [*errors, *_timer_errors(timer_configuration, timer_enabled)]


class FilterModule:
    def filters(self) -> dict[str, object]:
        return {
            "system_maintenance_molecule_baseline_sshd_errors": system_maintenance_molecule_baseline_sshd_errors,
            "system_maintenance_molecule_baseline_firewall_errors": system_maintenance_molecule_baseline_firewall_errors,
            "system_maintenance_molecule_baseline_debian_updater_errors": system_maintenance_molecule_baseline_debian_updater_errors,
            "system_maintenance_molecule_baseline_rocky_updater_errors": system_maintenance_molecule_baseline_rocky_updater_errors,
        }
