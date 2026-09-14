"""Strict, deterministic inputs for the repository-owned reverse proxy."""

import ipaddress
import os
import re
from collections.abc import Mapping

_PRIVATE = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_CERTIFICATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_PATH_COMPONENT = re.compile(r"[A-Za-z0-9._-]+\Z")


def _private(value, network):
    if not isinstance(value, str) or "%" in value or (network and "/" not in value):
        raise ValueError("proxy addresses must be explicit private addresses or CIDRs")
    try:
        address = ipaddress.ip_network(value, strict=True) if network else ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("proxy address or CIDR is invalid") from None
    if not any(address.version == allowed.version and (
        address.subnet_of(allowed) if network else address in allowed
    ) for allowed in _PRIVATE):
        raise ValueError("proxy ingress must use RFC1918 or ULA addresses")
    return str(address)


def _hostname(value, description):
    if (not isinstance(value, str) or len(value) > 253 or "." not in value
            or any(not _LABEL.fullmatch(label) for label in value.lower().split("."))):
        raise ValueError(f"{description} must be an exact DNS hostname")
    result = value.lower()
    try:
        ipaddress.ip_address(result)
    except ValueError:
        return result
    raise ValueError(f"{description} must be a DNS name, not an IP literal")


def _component(value, description):
    if not isinstance(value, str) or not _CERTIFICATE.fullmatch(value):
        raise ValueError(f"{description} must be a restricted filesystem component")
    return value


def _port(value, minimum, description):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 65535:
        raise ValueError(f"{description} must be an integer from {minimum} through 65535")
    return int(value)


def _device_backend(value):
    if not isinstance(value, Mapping):
        raise ValueError("proxy device backend must be a mapping")
    transport = value.get("transport")
    fields = {"transport", "address", "port"}
    if transport == "https":
        fields |= {"server_name", "trust_name"}
    elif transport != "http":
        raise ValueError("proxy device transport must be http or https")
    if set(value) != fields:
        raise ValueError("proxy device backend fields do not match its transport")
    result = {
        "transport": transport,
        "address": _private(value["address"], False),
        "port": _port(value["port"], 1, "proxy device backend port"),
    }
    if transport == "https":
        result.update({
            "server_name": _hostname(value["server_name"], "proxy TLS server name"),
            "trust_name": _component(value["trust_name"], "proxy trust name"),
        })
    return result


def validate(value):
    """Return canonical fields without accepting arbitrary template fragments."""
    fields = {"bind_addresses", "client_sources", "routes"}
    allowed_fields = (fields, fields | {"deferred_certificates"})
    if not isinstance(value, Mapping) or set(value) not in allowed_fields:
        raise ValueError(
            "proxy configuration requires bind_addresses, client_sources and routes, "
            "with optional deferred_certificates"
        )
    if any(not isinstance(value[field], list) for field in fields):
        raise ValueError("proxy configuration fields must be lists")
    result = {"bind_addresses": sorted(set(_private(item, False) for item in value["bind_addresses"])),
              "client_sources": sorted(set(_private(item, True) for item in value["client_sources"])),
              "routes": []}
    if "deferred_certificates" in value:
        deferred = value["deferred_certificates"]
        if not isinstance(deferred, list) or deferred not in ([], ["infra"]):
            raise ValueError("deferred_certificates must be [] or ['infra']")
        result["deferred_certificates"] = list(deferred)
    if value["routes"] and (not result["bind_addresses"] or not result["client_sources"]):
        raise ValueError("configured proxy routes require bind addresses and client sources")
    hostnames = set()
    for route in value["routes"]:
        if not isinstance(route, Mapping):
            raise ValueError("proxy route must be a mapping")
        local_fields = {"hostname", "backend_port", "certificate_name"}
        device_fields = {"hostname", "backend", "certificate_name"}
        health_fields = {"hostname", "health", "certificate_name"}
        if set(route) == local_fields:
            backend = {"backend_port": _port(
                route["backend_port"], 1024, "proxy backend port"
            )}
        elif set(route) == device_fields:
            backend = {"backend": _device_backend(route["backend"])}
        elif set(route) == health_fields and route["health"] is True:
            backend = {"health": True}
        else:
            raise ValueError("proxy route requires one local backend, device backend, or health: true")
        hostname = _hostname(route["hostname"], "proxy hostname")
        if hostname in hostnames:
            raise ValueError("proxy hostname is declared more than once")
        hostnames.add(hostname)
        certificate = _component(route["certificate_name"], "proxy certificate name")
        result["routes"].append({
            "hostname": hostname,
            "certificate_name": certificate,
            **backend,
        })
    result["routes"].sort(key=lambda route: route["hostname"])
    return result


def _generation_directory(value):
    try:
        path = os.fspath(value)
    except TypeError:
        raise ValueError("certificate generation directories must be paths") from None
    if not isinstance(path, str) or not path.startswith("/") or path.endswith("/"):
        raise ValueError("certificate generation directories must be absolute paths")
    components = path.split("/")[1:]
    if (not components or any(component in ("", ".", "..")
                              or not _PATH_COMPONENT.fullmatch(component)
                              for component in components)):
        raise ValueError("certificate generation directory is unsafe")
    return path


def _upstream(address, port):
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def render(value, certificate_paths=None):
    """Render a validated effective configuration using fixed Caddy syntax."""
    config = validate(value)
    if certificate_paths is None:
        certificate_paths = {}
    if not isinstance(certificate_paths, Mapping):
        raise ValueError("certificate_paths must be a mapping")
    certificates = {route["certificate_name"] for route in config["routes"]}
    if set(certificate_paths) - certificates:
        raise ValueError("certificate_paths contains an unknown certificate name")
    selected_paths = {}
    for name, path in certificate_paths.items():
        _component(name, "certificate override name")
        selected_paths[name] = _generation_directory(path)

    lines = [
        "{",
        "\tadmin unix//run/caddy/admin.sock",
        "\tauto_https off",
        "\tservers {",
        "\t\tprotocols h1 h2",
        "\t}",
        "}",
    ]
    for route in config["routes"]:
        directory = selected_paths.get(
            route["certificate_name"],
            f"/etc/caddy/tls/{route['certificate_name']}/current",
        )
        lines.extend([
            "",
            f"https://{route['hostname']}:443 {{",
            f"\tbind {' '.join(config['bind_addresses'])}",
            f"\ttls {directory}/fullchain.pem {directory}/privkey.pem",
        ])
        if "health" in route:
            lines.extend([
                "\t@health {",
                "\t\tpath /healthz",
                "\t\tmethod GET HEAD",
                "\t}",
                '\trespond @health "ok" 200',
                "\trespond 404",
            ])
        elif "backend_port" in route:
            lines.append(f"\treverse_proxy 127.0.0.1:{route['backend_port']}")
        else:
            backend = route["backend"]
            upstream = _upstream(backend["address"], backend["port"])
            if backend["transport"] == "http":
                lines.append(f"\treverse_proxy http://{upstream}")
            else:
                lines.extend([
                    f"\treverse_proxy https://{upstream} {{",
                    "\t\ttransport http {",
                    f"\t\t\ttls_trusted_ca_certs /etc/caddy/trust/{backend['trust_name']}.pem",
                    f"\t\t\ttls_server_name {backend['server_name']}",
                    "\t\t}",
                    "\t}",
                ])
        lines.append("}")
    return "\n".join(lines) + "\n"


class FilterModule:
    def filters(self):
        return {"reverse_proxy_validate": validate, "reverse_proxy_render": render}
