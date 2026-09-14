#!/usr/bin/python3
"""Exercise installed TLS publication against real disposable Caddy services."""

from datetime import datetime, timedelta, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


FIXTURE = Path("/var/lib/reverse-proxy-molecule")
HELPER = "/usr/local/libexec/homelab-reverse-proxy"
MANAGED = {"room-alert.infra.example.com", "modem.infra.example.com", "caddy.infra.example.com"}


def command(arguments, expected=0):
    result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=90, check=False)
    if result.returncode != expected:
        raise AssertionError("fixture operation failed: " + " ".join(arguments)
                             + "\n" + result.stderr[:1000])
    return result


def material(number):
    return ((FIXTURE / ("issued-" + str(number) + ".pem")).read_bytes(),
            (FIXTURE / ("issued-" + str(number) + ".key")).read_bytes())


def create_material(number):
    ca = x509.load_pem_x509_certificate((FIXTURE / "ca.pem").read_bytes())
    ca_key = serialization.load_pem_private_key((FIXTURE / "ca.key").read_bytes(), None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TLS integration fixture")]))
                   .issuer_name(ca.subject).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(days=2))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("*.infra.example.com")]), False)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
                   .sign(ca_key, hashes.SHA256()))
    public = certificate.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    for suffix, data in (("pem", public), ("key", private)):
        path = FIXTURE / ("issued-" + str(number) + "." + suffix)
        path.write_bytes(data)
        path.chmod(0o600)


def fingerprint(number):
    return x509.load_pem_x509_certificate(material(number)[0]).fingerprint(hashes.SHA256()).hex()


def served(address, hostname, request=False):
    with socket.create_connection((address, 443), timeout=10) as connection:
        with ssl.create_default_context().wrap_socket(connection, server_hostname=hostname) as tls:
            digest = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            health = hostname == "caddy.infra.example.com"
            if request or health:
                path = "/healthz" if health else "/ready"
                tls.sendall(("GET " + path + " HTTP/1.1\r\nHost: " + hostname
                             + "\r\nConnection: close\r\n\r\n").encode())
                response = http.client.HTTPResponse(tls)
                response.begin()
                body = response.read()
                if response.status != 200 or (body != b"ok" if health else b"device-fixture" not in body):
                    raise AssertionError("private device backend did not receive the proxy request")
            return digest


class AdminConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(10)
        self.sock.connect("/run/caddy/admin.sock")


def active_configuration():
    connection = AdminConnection("localhost")
    try:
        connection.request("GET", "/config/", headers={"Host": ""})
        response = connection.getresponse()
        if response.status != 200:
            raise AssertionError("could not observe active Caddy configuration")
        return json.loads(response.read())
    finally:
        connection.close()


def route_names(value):
    names = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"host", "sni"} and isinstance(item, list):
                names.update(item)
            else:
                names.update(route_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(route_names(item))
    return names


class DeviceHandler(BaseHTTPRequestHandler):
    def log_message(self, *_arguments):
        pass

    def do_GET(self):
        body = b"device-fixture\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_devices(address):
    servers = []
    for port in (80, 18443):
        server = ThreadingHTTPServer((address, port), DeviceHandler)
        if port == 18443:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain("/etc/caddy/tls/fixture/v1/fullchain.pem",
                                    "/etc/caddy/tls/fixture/v1/privkey.pem")
            server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    return servers


def fail_native_load(policy):
    """Reproduce the existing partial-listener regression on the real service."""
    address = policy.endpoints[0]["address"]
    candidate = Path("/etc/caddy/Caddyfile.fixture-failed-load")
    contents = Path("/etc/caddy/Caddyfile").read_text()
    changed = contents.replace("bind " + address, "bind " + address + " 192.168.255.254")
    if changed == contents:
        raise AssertionError("fixture failed to construct the unavailable listener")
    try:
        candidate.write_text(changed)
        os.chown(candidate, 0, policy.reader_gid)
        candidate.chmod(0o640)
        command(["/usr/sbin/runuser", "-u", "caddy", "--", "/usr/bin/caddy", "reload",
                 "--config", str(candidate), "--adapter", "caddyfile",
                 "--address", "unix//run/caddy/admin.sock", "--force"], expected=1)
    finally:
        candidate.unlink(missing_ok=True)


def caddy_pid():
    return command(["/usr/bin/systemctl", "show", "--property=MainPID", "--value",
                    "caddy.service"]).stdout.strip()


def publish(runtime, policy, number, reject_after_serving=False, partial_listener=False):
    original = runtime.verify_endpoints
    rejected = False

    def verify(selected_policy, digest):
        nonlocal rejected
        original(selected_policy, digest)
        if reject_after_serving and not rejected:
            rejected = True
            if partial_listener:
                fail_native_load(selected_policy)
            raise ValueError("disposable post-activation failure")

    with mock.patch.object(runtime, "verify_endpoints", verify):
        with runtime.publication_locked(policy) as deployment:
            try:
                return runtime.publisher_for(policy, deployment).publish(*material(number))
            except Exception as error:
                if reject_after_serving and not rejected:
                    raise AssertionError("activation failed before the intended served-certificate fault") from error
                raise


def crash_publication(runtime, policy, stage, number):
    from tls_runtime.publication import Publisher
    original = Publisher._write_journal

    def interrupt(publisher, record):
        original(publisher, record)
        if record["status"] == stage:
            os._exit(86)

    with mock.patch.object(Publisher, "_write_journal", interrupt):
        publish(runtime, policy, number)
    raise AssertionError("interruption point was not reached")


def run(runtime, policy):
    from tls_runtime.publication import PublicationError
    address = policy.endpoints[0]["address"]
    original_unrelated = served(address, "other.example.test")
    servers = start_devices(address)
    try:
        if runtime.PUBLISHED.joinpath("current").exists():
            raise AssertionError("first-publication fixture already has a certificate")
        if MANAGED & route_names(active_configuration()):
            raise AssertionError("unissued routes were activated")
        try:
            publish(runtime, policy, 1, reject_after_serving=True)
        except PublicationError as error:
            if error.restoration_failed:
                raise
        else:
            raise AssertionError("failed first publication unexpectedly succeeded")
        if os.path.lexists(runtime.PUBLISHED / "current") or runtime.JOURNAL.exists():
            raise AssertionError("failed first publication retained active or unfinished state")
        if MANAGED & route_names(active_configuration()):
            raise AssertionError("failed first publication retained affected routes")
        if served(address, "other.example.test") != original_unrelated:
            raise AssertionError("failed first publication changed unrelated TLS")
        print("PASS failed first publication preserves unrelated service", flush=True)

        if not publish(runtime, policy, 1):
            raise AssertionError("first publication was not recorded")
        for name in MANAGED:
            if served(address, name, request=True) != fingerprint(1):
                raise AssertionError("first served certificate differs from issued leaf")
        if publish(runtime, policy, 1):
            raise AssertionError("identical material unexpectedly created another generation")
        print("PASS first publication, private HTTP/HTTPS devices, identical-material reload", flush=True)

        publish(runtime, policy, 2)
        for name in MANAGED:
            if served(address, name) != fingerprint(2):
                raise AssertionError("renewal did not replace served leaf")
        try:
            publish(runtime, policy, 3, reject_after_serving=True)
        except PublicationError as error:
            if error.restoration_failed:
                raise
        else:
            raise AssertionError("forced renewal activation failure was ignored")
        if runtime.JOURNAL.exists() or served(address, next(iter(MANAGED))) != fingerprint(2):
            raise AssertionError("renewal rollback did not restore the old served leaf")
        print("PASS renewal, served-leaf replacement and rollback", flush=True)

        previous_pid = caddy_pid()
        try:
            publish(runtime, policy, 3, reject_after_serving=True, partial_listener=True)
        except PublicationError as error:
            if error.restoration_failed:
                raise
        else:
            raise AssertionError("partial listener failure unexpectedly succeeded")
        if caddy_pid() == previous_pid or runtime.JOURNAL.exists():
            raise AssertionError("automatic restart did not finish coupled rollback recovery")
        for name in MANAGED:
            if served(address, name) != fingerprint(2):
                raise AssertionError("automatic restart did not restore the previous certificate")
        if served(address, "other.example.test") != original_unrelated:
            raise AssertionError("automatic restart changed unrelated TLS")
        print("PASS automatic rollback restart outside deployment lock", flush=True)

        for stage, number in (("prepared", 3), ("switched", 4)):
            command([sys.executable, __file__, "crash", stage, str(number)], expected=86)
            if not runtime.JOURNAL.exists():
                raise AssertionError("interruption lost the durable transaction")
            envelope = Path("/var/lib/homelab-reverse-proxy/desired.json").read_bytes()
            command([HELPER, "apply-desired"], expected=1)
            if Path("/var/lib/homelab-reverse-proxy/desired.json").read_bytes() != envelope:
                raise AssertionError("concurrent configuration replaced recovery authority")
            command(["/usr/bin/systemctl", "restart", "caddy.service"])
            with runtime.publication_locked(policy) as deployment:
                runtime.publisher_for(policy, deployment).recover()
            if runtime.JOURNAL.exists():
                raise AssertionError("interrupted publication did not complete recovery")
            for name in MANAGED:
                if served(address, name) != fingerprint(number):
                    raise AssertionError("recovery did not activate retained issued material")
            print("PASS interrupted " + stage + " recovery and blocked configuration change", flush=True)

        process = None
        try:
            with runtime.publication_locked(policy) as deployment:
                process = subprocess.Popen([HELPER, "apply-desired"], stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                try:
                    process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    raise AssertionError("configuration operation did not wait for publication lock")
                runtime.publisher_for(policy, deployment).publish(*material(1))
            output, error = process.communicate(timeout=90)
            if process.returncode:
                raise AssertionError("serialized configuration failed: " + output + error)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        for name in MANAGED:
            if served(address, name, request=True) != fingerprint(1):
                raise AssertionError("concurrent operation changed selected certificate")
        if served(address, "other.example.test") != original_unrelated:
            raise AssertionError("publication changed unrelated certificate")
        command([HELPER, "verify"])
        print("PASS concurrent configuration/certificate serialization and final verification", flush=True)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def main():
    if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (0, 0, 0, 0):
        raise RuntimeError("TLS integration fixture requires the system manager's root identity")
    if not Path("/run/.containerenv").exists() or not (FIXTURE / "ca.key").is_file():
        raise RuntimeError("TLS integration fixture requires its disposable Podman target")
    sys.path.insert(0, "/usr/local/lib/homelab-tls")
    from tls_runtime import runtime
    policy = runtime.load_policy()
    if policy.namespace != "infra.example.com" or {item["hostname"] for item in policy.endpoints} != MANAGED:
        raise RuntimeError("TLS integration fixture refuses non-fixture policy")
    if sys.argv[1:] == ["prepare"]:
        for number in range(1, 5):
            create_material(number)
    elif len(sys.argv) == 4 and sys.argv[1] == "crash":
        crash_publication(runtime, policy, sys.argv[2], int(sys.argv[3]))
    elif len(sys.argv) == 1:
        run(runtime, policy)
    else:
        raise ValueError("invalid disposable fixture arguments")


if __name__ == "__main__":
    main()
