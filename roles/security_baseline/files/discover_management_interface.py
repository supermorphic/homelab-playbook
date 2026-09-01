#!/usr/bin/python3
"""Find the target interface and peer used by this SSH management process."""

from __future__ import annotations

import ipaddress
import json
import os
import pathlib
import subprocess
import sys


def _ssh_connection_from_ancestry() -> str:
    process = os.getpid()
    while process > 1:
        try:
            environ = pathlib.Path(f"/proc/{process}/environ").read_bytes()
            for entry in environ.split(b"\0"):
                if entry.startswith(b"SSH_CONNECTION="):
                    return entry.split(b"=", 1)[1].decode("utf-8")
            stat = pathlib.Path(f"/proc/{process}/stat").read_text(encoding="utf-8")
            process = int(stat.rsplit(") ", 1)[1].split()[1])
        except (OSError, UnicodeDecodeError, ValueError, IndexError):
            break
    raise ValueError("SSH_CONNECTION is absent from the management process ancestry")


def discover() -> dict[str, str]:
    fields = _ssh_connection_from_ancestry().split()
    if len(fields) != 4:
        raise ValueError("SSH_CONNECTION does not have four fields")
    peer = str(ipaddress.ip_address(fields[0]))
    route = subprocess.run(
        ["/usr/sbin/ip", "-json", "route", "get", peer],
        check=True,
        capture_output=True,
        text=True,
    )
    routes = json.loads(route.stdout)
    if not isinstance(routes, list) or len(routes) != 1:
        raise ValueError("management peer route is ambiguous")
    interface = routes[0].get("dev")
    if (
        not isinstance(interface, str)
        or not 1 <= len(interface) <= 15
        or any(character in interface for character in " /!*")
    ):
        raise ValueError("management route has an invalid interface")
    return {"peer": peer, "interface": interface}


def main() -> int:
    try:
        print(json.dumps(discover(), sort_keys=True))
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as error:
        print(f"management interface discovery failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
