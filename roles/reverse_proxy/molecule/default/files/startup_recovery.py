"""Exercise the installed Caddy unit only inside the disposable proxy scenario."""

import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import time


BOOT = Path("/etc/caddy/Caddyfile")
FAST_RETRY = Path("/etc/systemd/system/caddy.service.d/zz-molecule-retry.conf")
ADDRESS = "192.0.2.123"


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=45)


def properties():
    return dict(line.split("=", 1) for line in run(
        "systemctl", "show", "caddy.service", "-p", "ActiveState", "-p", "Result",
        "-p", "NRestarts", "-p", "RestartUSec", "-p", "StartLimitBurst",
    ).stdout.splitlines())


def wait_for(predicate, message, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = properties()
        if predicate(state):
            return state
        time.sleep(0.2)
    raise AssertionError(message + ": " + repr(properties()))


def main():
    original_address = str(ipaddress.ip_address(sys.argv[1]))
    original = BOOT.read_text()
    # Refuse to touch a host without the dedicated synthetic fixture material.
    assert Path("/var/lib/reverse-proxy-molecule/ca.pem").is_file()
    assert "app.example.test" in original and original_address in original
    assert not FAST_RETRY.exists()
    addresses = json.loads(run("ip", "-j", "address").stdout)
    assert not any(entry.get("local") == ADDRESS
                   for interface in addresses for entry in interface.get("addr_info", []))
    added_address = False
    try:
        run("systemctl", "stop", "caddy.service")
        run("systemctl", "reset-failed", "caddy.service")
        BOOT.write_text(original.replace(original_address, ADDRESS))
        first = run("systemctl", "start", "caddy.service", check=False)
        assert first.returncode != 0, "Caddy unexpectedly bound an absent address"
        log = run("journalctl", "-u", "caddy.service", "-n", "25", "--no-pager", "-o", "cat").stdout
        assert ADDRESS + ":443" in log and "cannot assign requested address" in log
        assert properties()["RestartUSec"] == "10s"

        # Model delayed DHCP without changing the controller connection address.
        run("ip", "address", "add", ADDRESS + "/32", "dev", "lo")
        added_address = True
        recovered = wait_for(lambda state: state["ActiveState"] == "active",
                             "Caddy did not recover after its address appeared")
        assert int(recovered["NRestarts"]) >= 1
        probe = run("/usr/local/libexec/reverse-proxy-molecule-probe.py",
                    "--address", ADDRESS, "--hostname", "app.example.test",
                    "--ca-file", "/var/lib/reverse-proxy-molecule/ca.pem", "https")
        assert json.loads(probe.stdout)["status"] == 200
        print("PASS: unavailable bind address recovered automatically with trusted HTTPS")

        run("systemctl", "stop", "caddy.service")
        run("ip", "address", "del", ADDRESS + "/32", "dev", "lo")
        added_address = False
        # Keep the installed retry count/window; shorten only the fixture delay.
        FAST_RETRY.write_text("[Service]\nRestartSec=100ms\n")
        run("systemctl", "daemon-reload")
        run("systemctl", "reset-failed", "caddy.service")
        run("systemctl", "start", "caddy.service", check=False)
        exhausted = wait_for(lambda state: state["Result"] == "start-limit-hit",
                             "Persistent bind failure did not exhaust the retry budget")
        assert exhausted["ActiveState"] == "failed"
        assert exhausted["StartLimitBurst"] == "12"
        print("PASS: persistent bind failure stops at the installed retry limit")
    finally:
        run("systemctl", "stop", "caddy.service")
        BOOT.write_text(original)
        FAST_RETRY.unlink(missing_ok=True)
        if added_address:
            run("ip", "address", "del", ADDRESS + "/32", "dev", "lo")
        run("systemctl", "daemon-reload")
        run("systemctl", "reset-failed", "caddy.service")
        run("systemctl", "start", "caddy.service")


if __name__ == "__main__":
    main()
