"""Controller pipe contract; identities exist only in registered test:secrets memory."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pty
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "roles/semaphore/files/identity-command.py"


class ControllerIdentityTests(unittest.TestCase):
    def remove_container(self, name):
        result = subprocess.run(["podman", "rm", "--force", "--ignore", name],
                                capture_output=True, timeout=60)
        self.assertEqual(0, result.returncode, "disposable identity container cleanup failed")

    def setUp(self):
        self.assertTrue(CLIENT.is_file(), "controller retrieval executable is missing")
        spec = importlib.util.spec_from_file_location("controller_identity", CLIENT)
        self.client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.client)
        self.temporary = tempfile.TemporaryDirectory(prefix="age-socket-")
        self.addCleanup(self.temporary.cleanup)
        self.path = str(Path(self.temporary.name) / "age.sock")

    def serve(self, payload):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.path)
        listener.listen(1)
        listener.settimeout(5)

        def send():
            with listener:
                connection, _ = listener.accept()
                with connection:
                    try:
                        connection.sendall(payload)
                    except BrokenPipeError:
                        pass

        thread = threading.Thread(target=send)
        thread.start()
        self.addCleanup(thread.join, 6)

    def test_retrieved_identity_decrypts_only_its_own_ciphertext(self):
        # A client that returns another key or truncates the response breaks decryption.
        identity = subprocess.run(["age-keygen"], capture_output=True, check=True).stdout
        recipient = subprocess.run(["age-keygen", "-y"], input=identity,
                                   capture_output=True, check=True).stdout.strip()
        encrypted = subprocess.run(["age", "-r", recipient.decode()], input=b"fixture",
                                   capture_output=True, check=True).stdout
        self.serve(identity)
        retrieved = self.client.read_identity(self.path)
        # age accepts a pipe descriptor as an identity; no private-key file is created.
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, retrieved)
            os.close(write_fd)
            write_fd = -1
            result = subprocess.run(["age", "-d", "-i", f"/dev/fd/{read_fd}"],
                                    input=encrypted, capture_output=True, pass_fds=(read_fd,))
            self.assertEqual(0, result.returncode, "retrieved fixture identity failed")
            self.assertTrue(result.stdout == b"fixture")
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def test_empty_response_fails_without_output(self):
        self.serve(b"")
        with self.assertRaises(ValueError):
            self.client.read_identity(self.path)

    def test_malformed_and_oversize_response_fail(self):
        self.serve(b"not-an-age-identity\n" * 1000)
        with self.assertRaises(ValueError):
            self.client.read_identity(self.path)

    def test_unavailable_socket_fails(self):
        with self.assertRaises(OSError):
            self.client.read_identity(self.path)

    def test_cli_rejects_terminal_output(self):
        primary, secondary = pty.openpty()
        try:
            result = subprocess.run([sys.executable, str(CLIENT)], stdout=secondary,
                                    stderr=subprocess.PIPE, timeout=5)
            self.assertNotEqual(0, result.returncode)
            self.assertIn(b"requires a SOPS pipe", result.stderr)
        finally:
            os.close(primary)
            os.close(secondary)

    def test_wrong_identity_cannot_decrypt(self):
        identity = subprocess.run(["age-keygen"], capture_output=True, check=True).stdout
        other = subprocess.run(["age-keygen"], capture_output=True, check=True).stdout
        recipient = subprocess.run(["age-keygen", "-y"], input=other,
                                   capture_output=True, check=True).stdout.strip()
        encrypted = subprocess.run(["age", "-r", recipient.decode()], input=b"fixture",
                                   capture_output=True, check=True).stdout
        self.serve(identity)
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, self.client.read_identity(self.path))
            os.close(write_fd)
            write_fd = -1
            result = subprocess.run(["age", "-d", "-i", f"/dev/fd/{read_fd}"],
                                    input=encrypted, capture_output=True, pass_fds=(read_fd,))
            self.assertNotEqual(0, result.returncode)
            self.assertFalse(result.stdout)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    @unittest.skipUnless(os.environ.get("SEMAPHORE_IDENTITY_TEST_IMAGE"),
                         "requires an explicit disposable Linux fixture image")
    def test_systemd_creds_delivers_authenticated_identity_over_socket(self):
        # This optional test is still dispatched through registered test:secrets.
        # The container receives the identity only on stdin and stores ciphertext only.
        identity = subprocess.run(["age-keygen"], capture_output=True, check=True).stdout
        name = "semaphore-identity-pipe-" + uuid.uuid4().hex
        self.addCleanup(self.remove_container, name)
        fixture = r'''
import json, pathlib, socket, subprocess, sys, tempfile, threading
payload = json.load(sys.stdin)
namespace = {"__name__": "fixture_client"}
exec(compile(payload["client"], "identity-command.py", "exec"), namespace)
identity = payload["identity"].encode()
with tempfile.TemporaryDirectory(prefix="credential-") as temporary:
    root = pathlib.Path(temporary)
    encrypted = root / "semaphore-age"
    command = ["systemd-creds", "encrypt", "--with-key=host", "--name=semaphore-age", "-", str(encrypted)]
    result = subprocess.run(command, input=identity, capture_output=True)
    if result.returncode:
        sys.exit("fixture encryption failed")
    for expected_name, success in [("semaphore-age", True), ("incorrect", False)]:
        path = str(root / "age.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        listener.settimeout(10)
        status = []
        def serve():
            with listener:
                connection, _ = listener.accept()
                with connection:
                    result = subprocess.run(["systemd-creds", "decrypt", "--name=" + expected_name,
                                             str(encrypted), "-"], stdin=connection,
                                            stdout=connection, stderr=subprocess.DEVNULL, timeout=10)
                    status.append(result.returncode)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            retrieved = namespace["read_identity"](path)
            valid = retrieved.strip() in identity.splitlines()
        except (ValueError, OSError):
            valid = False
        thread.join(12)
        pathlib.Path(path).unlink()
        if valid != success or not status or ((status[0] == 0) != success):
            sys.exit("fixture socket authentication failed")
    key = next(line for line in identity.splitlines() if line.startswith(b"AGE-SECRET-KEY-"))
    if any(key in path.read_bytes() for path in root.rglob("*") if path.is_file()):
        sys.exit("fixture stored a plaintext identity")
print("authenticated socket delivery and wrong-name rejection passed")
'''
        result = subprocess.run([
            "podman", "run", "--rm", "--name", name, "--network=none", "--read-only",
            "--tmpfs=/tmp", "--tmpfs=/var/lib/systemd", "-i",
            "--entrypoint=python3", os.environ["SEMAPHORE_IDENTITY_TEST_IMAGE"],
            "-c", fixture,
        ], input=json.dumps({"identity": identity.decode(), "client": CLIENT.read_text()}).encode(),
            capture_output=True, timeout=90)
        self.assertEqual(0, result.returncode, "disposable Linux credential/socket proof failed")
        self.assertIn(b"wrong-name rejection passed", result.stdout)

    @unittest.skipUnless(os.environ.get("SEMAPHORE_IDENTITY_TEST_IMAGE"),
                         "requires an explicit disposable Linux fixture image")
    def test_rendered_systemd_units_activate_and_restrict_socket(self):
        identity = subprocess.run(["age-keygen"], capture_output=True, check=True).stdout
        name = "semaphore-identity-test-" + uuid.uuid4().hex
        self.addCleanup(self.remove_container, name)
        renderer = Environment(undefined=StrictUndefined)
        units = {}
        for source, target in [("controller-identity.socket", "semaphore-age.socket"),
                               ("controller-identity@.service", "semaphore-age@.service")]:
            template = (ROOT / "roles/semaphore/templates" / (source + ".j2")).read_text()
            units[target] = renderer.from_string(template).render(
                ansible_managed="Disposable test", semaphore_account_name="svc-fixture",
                semaphore_controller_identity_credential_path="/etc/semaphore/controller-age.cred")
        started = subprocess.run([
            "podman", "run", "--detach", "--name", name, "--network=none",
            "--userns=keep-id:uid=24001,gid=24001", "--user=0",
            "--systemd=always", os.environ["SEMAPHORE_IDENTITY_TEST_IMAGE"],
            "/usr/lib/systemd/systemd",
        ], capture_output=True, timeout=60)
        self.assertEqual(0, started.returncode, "disposable systemd container failed to start")
        try:
            fixture = r'''
import json, os, pathlib, subprocess, sys
payload = json.load(sys.stdin)
def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, timeout=30, **kwargs)
    if result.returncode:
        sys.exit("fixture command failed: " + command[0])
    return result
run(["useradd", "--uid", "24001", "--no-create-home", "--shell", "/sbin/nologin", "svc-fixture"])
root = pathlib.Path("/etc/semaphore")
root.mkdir(mode=0o700, exist_ok=True)
run(["systemd-creds", "encrypt", "--with-key=host", "--name=semaphore-age", "-",
     str(root / "controller-age.cred")], input=payload["identity"].encode())
socket_root = pathlib.Path("/run/semaphore-identity")
socket_root.mkdir(mode=0o750, exist_ok=True)
socket_root.chmod(0o750)
os.chown(socket_root, 0, 24001)
client = pathlib.Path("/usr/local/bin/identity-command.py")
client.write_text(payload["client"])
client.chmod(0o755)
for name, contents in payload["units"].items():
    pathlib.Path("/etc/systemd/system", name).write_text(contents)
run(["systemctl", "daemon-reload"])
run(["systemctl", "start", "semaphore-age.socket"])
expected = next(line for line in payload["identity"].encode().splitlines()
                if line.startswith(b"AGE-SECRET-KEY-"))
for action in ("start", "restart"):
    run(["systemctl", action, "semaphore-age.socket"])
    result = run(["setpriv", "--reuid=24001", "--regid=24001", "--clear-groups",
                  "python3", str(client)])
    if result.stdout.strip() != expected:
        sys.exit("activated socket returned incorrect identity")
result = subprocess.run(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups",
                         "python3", str(client)], capture_output=True, timeout=15)
if result.returncode == 0 or result.stdout:
    sys.exit("unrelated uid was not denied")
print("systemd activation, socket restart, and unrelated uid rejection passed")
'''
            result = subprocess.run(["podman", "exec", "-i", name, "python3", "-c", fixture],
                                    input=json.dumps({"identity": identity.decode(), "client": CLIENT.read_text(),
                                                      "units": units}).encode(),
                                    capture_output=True, timeout=90)
            self.assertEqual(0, result.returncode, "disposable systemd activation proof failed: "
                             + result.stderr.decode(errors="replace"))
            self.assertIn(b"unrelated uid rejection passed", result.stdout)
        finally:
            cleanup = subprocess.run(["podman", "rm", "--force", name], capture_output=True, timeout=60)
            self.assertEqual(0, cleanup.returncode, "disposable identity container cleanup failed")

    @unittest.skipUnless(os.environ.get("SEMAPHORE_IDENTITY_SELINUX_TEST_IMAGE"),
                         "requires an explicit Rocky policy compiler fixture")
    def test_selinux_policy_compiles_without_broadening_stock_containers(self):
        policy_root = ROOT / "roles/semaphore/files"
        name = "semaphore-identity-policy-" + uuid.uuid4().hex
        self.addCleanup(self.remove_container, name)
        self.assertTrue((policy_root / "semaphore_controller.te").is_file(), "controller policy missing")  # codespell:ignore te
        fixture = r'''
import json, pathlib, subprocess, sys, tempfile
payload = json.load(sys.stdin)
def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, timeout=240, **kwargs)
    if result.returncode:
        sys.exit(result.stderr.decode(errors="replace") + result.stdout.decode(errors="replace"))
    return result
run(["dnf", "-y", "install", "selinux-policy-devel", "container-selinux", "setools-console"])
with tempfile.TemporaryDirectory(prefix="policy-") as directory:
    for suffix, content in payload.items():
        pathlib.Path(directory, "semaphore_controller." + suffix).write_text(content)
    run(["make", "-f", "/usr/share/selinux/devel/Makefile", "semaphore_controller.pp"], cwd=directory)
    # -N updates only this container's policy store; never load the host kernel policy.
    run(["semodule", "-N", "-i", str(pathlib.Path(directory, "semaphore_controller.pp"))])
    policy = sorted(pathlib.Path("/etc/selinux/targeted/policy").glob("policy.*"))[-1]
    def allowed(source, target, kind, permission):
        return run(["sesearch", "-A", "-s", source, "-t", target, "-c", kind,
                    "-p", permission, str(policy)]).stdout.strip()
    if not allowed("semaphore_controller_t", "init_t", "unix_stream_socket", "connectto"):
        sys.exit("controller socket connection missing")
    if not allowed("semaphore_controller_t", "semaphore_age_runtime_t", "sock_file", "write"):
        sys.exit("controller named socket access missing")
    if allowed("container_t", "semaphore_age_runtime_t", "sock_file", "write"):
        sys.exit("stock container gained identity access")
    if allowed("container_t", "init_t", "unix_stream_socket", "connectto"):
        sys.exit("stock container unexpectedly has host socket connection access")
    context = run(["matchpathcon", "-n", "/run/semaphore-identity/age.sock"]).stdout
    if b":semaphore_age_runtime_t:" not in context:
        sys.exit("socket pathname does not resolve to the dedicated label")
print("compiled policy grants only controller identity socket access")
'''
        payload = {suffix: (policy_root / ("semaphore_controller." + suffix)).read_text()
                   for suffix in ("te", "fc")}  # codespell:ignore te
        result = subprocess.run([
            "podman", "run", "--rm", "--name", name, "-i", "--entrypoint=python3",
            os.environ["SEMAPHORE_IDENTITY_SELINUX_TEST_IMAGE"], "-c", fixture,
        ], input=json.dumps(payload).encode(), capture_output=True, timeout=300)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
        self.assertIn(b"compiled policy grants only controller", result.stdout)


if __name__ == "__main__":
    unittest.main()
