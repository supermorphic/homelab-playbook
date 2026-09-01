#!/usr/bin/python3
"""Read the SSH management peer and its routed interface without mutation."""

from __future__ import annotations

import ipaddress
import json
import os
import pathlib
import subprocess


def discover() -> dict[str, str | int]:
    process = os.getpid()
    connection = ""
    while process > 1 and not connection:
        try:
            for entry in pathlib.Path(f"/proc/{process}/environ").read_bytes().split(b"\0"):
                if entry.startswith(b"SSH_CONNECTION="):
                    connection = entry.split(b"=", 1)[1].decode()
            process = int(pathlib.Path(f"/proc/{process}/stat").read_text().rsplit(") ", 1)[1].split()[1])
        except (OSError, UnicodeDecodeError, ValueError, IndexError):
            break
    fields = connection.split()
    if len(fields) != 4:
        raise ValueError("SSH_CONNECTION is absent or invalid")
    peer = str(ipaddress.ip_address(fields[0]))
    local_address = str(ipaddress.ip_address(fields[2]))
    local_port = int(fields[3])
    if not 1 <= local_port <= 65535:
        raise ValueError("SSH_CONNECTION local port is invalid")
    route = subprocess.run(["/usr/sbin/ip", "-json", "route", "get", peer], check=True, capture_output=True, text=True)
    routes = json.loads(route.stdout)
    if not isinstance(routes, list) or len(routes) != 1 or not isinstance(routes[0].get("dev"), str):
        raise ValueError("management route is ambiguous")
    return {
        "peer": peer,
        "host": peer,
        "local_address": local_address,
        "local_port": local_port,
        "interface": routes[0]["dev"],
    }


if __name__ == "__main__":
    print(json.dumps(discover(), sort_keys=True))
