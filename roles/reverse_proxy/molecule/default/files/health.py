"""Prove the edge contract only inside the disposable reverse proxy scenario."""

import http.client
import ipaddress
from pathlib import Path
import socket
import ssl
import subprocess
import sys


def request(address, hostname, method, path):
    context = ssl.create_default_context(cafile="/var/lib/reverse-proxy-molecule/ca.pem")
    with socket.create_connection((address, 443), timeout=5) as plain:
        with context.wrap_socket(plain, server_hostname=hostname) as connection:
            connection.sendall(
                f"{method} {path} HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n".encode()
            )
            response = http.client.HTTPResponse(connection, method=method)
            response.begin()
            return response.status, response.read()


def edge(address):
    for method, path, expected in (
        ("GET", "/healthz", (200, b"ok")),
        ("HEAD", "/healthz", (200, b"")),
        ("GET", "/healthz?probe=1", (200, b"ok")),
        ("GET", "/", (404, b"")),
        ("GET", "/healthz/", (404, b"")),
        ("GET", "/healthz/child", (404, b"")),
        ("GET", "/healthzz", (404, b"")),
        ("POST", "/healthz", (404, b"")),
        ("OPTIONS", "/healthz", (404, b"")),
    ):
        actual = request(address, "caddy.example.test", method, path)
        assert actual == expected, (method, path, actual)


def application(address):
    return request(address, "app.example.test", "GET", "/api/ping")


def main():
    address = str(ipaddress.ip_address(sys.argv[1]))
    assert Path("/run/.containerenv").is_file(), "requires disposable Podman target"
    assert Path("/var/lib/reverse-proxy-molecule/ca.pem").is_file()
    edge(address)
    assert application(address) == (200, b"fixture host=app.example.test path=/api/ping\n")
    # A health-looking path on an application hostname must still reach that app.
    assert request(address, "app.example.test", "GET", "/healthz") == (
        200, b"fixture host=app.example.test path=/healthz\n")
    if sys.argv[2:] == ["--stop-backend"]:
        unit = "reverse-proxy-molecule-backend.service"
        try:
            subprocess.run(["systemctl", "stop", unit], check=True)
            assert application(address)[0] == 502, "stopped backend still responds"
            edge(address)
        finally:
            subprocess.run(["systemctl", "start", unit], check=True)
        # Starting the unit does not guarantee that its HTTP listener is ready.
        import time
        for attempt in range(50):
            if application(address)[0] == 200:
                break
            time.sleep(0.1)
        assert application(address)[0] == 200, "fixture backend did not recover"
    else:
        assert not sys.argv[2:], "unexpected fixture arguments"
    print("PASS: trusted edge GET/HEAD, exact paths, and separate application route")


if __name__ == "__main__":
    main()
