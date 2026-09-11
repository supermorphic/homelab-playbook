#!/usr/bin/env python3
"""Retrieve one controller age identity into the SOPS pipe, without files."""

import re
import socket
import sys
import time

SOCKET_PATH = "/run/semaphore-identity/age.sock"
MAX_BYTES = 4096
TIMEOUT_SECONDS = 10


def read_identity(path=SOCKET_PATH):
    """Buffer a bounded complete response before exposing any identity bytes."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    response = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(TIMEOUT_SECONDS)
        connection.connect(path)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("identity retrieval timed out")
            connection.settimeout(remaining)
            chunk = connection.recv(MAX_BYTES + 1 - len(response))
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_BYTES:
                raise ValueError("identity response is too large")
    lines = [line.strip() for line in bytes(response).splitlines()
             if line.strip() and not line.lstrip().startswith(b"#")]
    if len(lines) != 1 or not re.fullmatch(b"AGE-SECRET-KEY-1[0-9A-Z]{58}", lines[0]):
        raise ValueError("identity response is invalid")
    return lines[0] + b"\n"


def main():
    if len(sys.argv) != 1 or sys.stdout.isatty():
        print("Controller identity requires a SOPS pipe and no arguments.", file=sys.stderr)
        return 1
    try:
        identity = read_identity()
        sys.stdout.buffer.write(identity)
        sys.stdout.buffer.flush()
    except (OSError, ValueError):
        print("Controller identity retrieval failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
