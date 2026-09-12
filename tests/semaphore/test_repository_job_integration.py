"""Disposable end-to-end proof for the Semaphore repository job wrapper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import importlib.util
import tempfile
import time
import unittest
import uuid
from urllib import request


ROOT = Path(__file__).resolve().parents[2]
SEMAPHORE_IMAGE = (
    "docker.io/semaphoreui/semaphore@"
    "sha256:3996804607ebb63690528185bb9adc3507ee896851098e4453975f2ce7f8b435"
)
DEFAULT_TARGET_IMAGE = "localhost/homelab-playbook-semaphore-debian13:local"
API_HELPER = ROOT / "roles" / "semaphore" / "files" / "repository-job-api.py"
MISE_VERSION = "2026.9.4"
MISE_ASSETS = {
    "aarch64": (
        "arm64",
        "82d10b29ac668b31f937b788fd99feb305fc9f2f4955825baf57d714aac2e25d",
    ),
    "x86_64": (
        "x64",
        "4c53429e230c07588ae810b3b37f1541f1a763a1f6ac3a71c3fc98904ac3c333",
    ),
}
SERVER = r"""
import os, pathlib, re, socket, sys
identity = sys.stdin.buffer.readline().strip()
if not re.fullmatch(b"AGE-SECRET-KEY-1[0-9A-Z]{58}", identity):
    raise SystemExit("invalid fixture identity")
root = pathlib.Path("/run/semaphore-identity")
path = root / "age.sock"
path.unlink(missing_ok=True)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(str(path))
os.chmod(path, 0o660)
listener.listen(4)
count = 0
while True:
    connection, _ = listener.accept()
    with connection:
        connection.sendall(identity + b"\n")
    count += 1
    (root / "served").write_text(str(count))
"""


def _run(
    argv: list[str],
    *,
    input_data: bytes | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        input=input_data,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _details(result: subprocess.CompletedProcess[bytes]) -> str:
    output = (result.stdout + result.stderr).decode(errors="replace")
    return output[-8000:]


def _load_api_helper():
    spec = importlib.util.spec_from_file_location("repository_job_integration_api", API_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load repository job API helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(
    os.environ.get("SEMAPHORE_REPOSITORY_JOB_INTEGRATION") == "1",
    "run through test:semaphore -- controller",
)
class RepositoryJobIntegrationTests(unittest.TestCase):
    """Use only synthetic credentials, inventory, containers, and addresses."""

    def setUp(self) -> None:
        self.prefix = f"homelab-repository-job-{uuid.uuid4().hex[:12]}"
        self.containers: list[str] = []
        self.volumes: list[str] = []
        self.network = self.prefix
        self.temporary = tempfile.TemporaryDirectory(prefix=self.prefix + "-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self._cleanup)

        result = _run(["podman", "network", "create", self.network])
        self.assertEqual(0, result.returncode, _details(result))
        for suffix in ("tools", "workspace", "config", "state", "ssh", "helper", "identity"):
            name = f"{self.prefix}-{suffix}"
            result = _run(["podman", "volume", "create", name])
            self.assertEqual(0, result.returncode, _details(result))
            self.volumes.append(name)

    def _cleanup(self) -> None:
        errors = []
        for name in reversed(self.containers):
            result = _run(["podman", "rm", "--force", "--ignore", name], timeout=30)
            if result.returncode:
                errors.append(f"container {name}: {_details(result)}")
        for name in reversed(self.volumes):
            result = _run(["podman", "volume", "rm", name], timeout=30)
            if result.returncode:
                errors.append(f"volume {name}: {_details(result)}")
        result = _run(["podman", "network", "rm", self.network], timeout=30)
        if result.returncode:
            errors.append(f"network {self.network}: {_details(result)}")
        self.temporary.cleanup()
        if errors:
            raise AssertionError("disposable fixture cleanup failed: " + "; ".join(errors))

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if process.stderr is not None:
            process.stderr.close()

    def _volume(self, suffix: str) -> str:
        return f"{self.prefix}-{suffix}"

    def _start(self, suffix: str, argv: list[str]) -> str:
        name = f"{self.prefix}-{suffix}"
        result = _run(["podman", "run", "--detach", "--name", name, *argv])
        self.assertEqual(0, result.returncode, _details(result))
        self.containers.append(name)
        return name

    def _exec(
        self,
        container: str,
        argv: list[str],
        *,
        environment: dict[str, str] | None = None,
        workdir: str | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["podman", "exec"]
        if workdir:
            command.extend(("--workdir", workdir))
        for key, value in (environment or {}).items():
            command.extend(("--env", f"{key}={value}"))
        command.extend((container, *argv))
        return _run(command, timeout=timeout)

    def _copy_checkout(self, destination: Path) -> None:
        destination.mkdir()
        for name in (
            ".mise.toml",
            "ansible.cfg",
            "mise.lock",
            "overrides",
            "playbooks",
            "pyproject.toml",
            "requirements.yml",
            "roles",
            "scripts",
            "uv.lock",
        ):
            source = ROOT / name
            target = destination / name
            if source.is_dir():
                shutil.copytree(
                    source,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                shutil.copy2(source, target)
        (destination / "inventory" / "production" / "group_vars" / "os_managed").mkdir(
            parents=True
        )

    def _identity(self) -> tuple[bytes, str]:
        generated = _run(["mise", "exec", "--", "age-keygen"])
        self.assertEqual(0, generated.returncode, "could not generate disposable age identity")
        identity = next(
            (line for line in generated.stdout.splitlines() if line.startswith(b"AGE-SECRET-KEY-")),
            b"",
        )
        self.assertRegex(identity, rb"^AGE-SECRET-KEY-1[0-9A-Z]{58}$")
        derived = _run(
            ["mise", "exec", "--", "age-keygen", "-y"], input_data=identity + b"\n"
        )
        self.assertEqual(0, derived.returncode, "could not derive disposable age recipient")
        return identity, derived.stdout.decode().strip()

    def _encrypt_inventory(self, checkout: Path, recipient: str, public_key: str) -> None:
        group = checkout / "inventory" / "production" / "group_vars" / "os_managed"
        (checkout / "inventory" / "production" / "hosts.yml").write_text(
            "---\n"
            "all:\n"
            "  children:\n"
            "    os_managed:\n"
            "      hosts:\n"
            "        fixture-target:\n"
            "          ansible_host: fixture-target\n"
            "          ansible_user: ansible\n"
            "          ansible_ssh_private_key_file: /etc/semaphore/controller-ssh\n",
            encoding="utf-8",
        )
        (group / "vars.yml").write_text(
            "---\n"
            "host_identity_hostname: fixture-target\n"
            "os_baseline_verify_runtime_controls: false\n"
            "os_reboot_boot_time_command: /usr/bin/stat -c %y /proc/1\n"
            "security_baseline_apply_audit_runtime: false\n"
            "security_baseline_apply_firewall_runtime: false\n"
            "security_baseline_apply_kernel_controls: false\n"
            "security_baseline_apply_time_runtime: false\n"
            "security_baseline_sshd_connection_context:\n"
            "  host: 127.0.0.1\n"
            "  peer: 127.0.0.1\n"
            "  local_address: 127.0.0.1\n"
            "  local_port: 22\n"
            "os_baseline_verify_sshd_connection_context:\n"
            "  host: 127.0.0.1\n"
            "  peer: 127.0.0.1\n"
            "  local_address: 127.0.0.1\n"
            "  local_port: 22\n"
            "system_maintenance_native_reboot_enabled: false\n",
            encoding="utf-8",
        )
        plaintext = self.root / "inventory-secrets.yml"
        plaintext.write_text(
            "host_identity_timezone: Etc/UTC\n"
            "security_baseline_authorized_keys:\n"
            f"  - {public_key}\n"
            "security_baseline_management_sources:\n"
            "  - 10.0.0.0/8\n",
            encoding="utf-8",
        )
        encrypted = _run(
            [
                "mise", "exec", "--", "sops", "--config", "/dev/null", "--encrypt",
                "--age", recipient, "--encrypted-regex", "^(.*)$", str(plaintext),
            ]
        )
        self.assertEqual(0, encrypted.returncode, _details(encrypted))
        (group / "secrets.sops.yml").write_bytes(encrypted.stdout)
        plaintext.unlink()

    def _initialize_volumes(self, checkout: Path, config: Path, private_key: Path) -> None:
        init_name = f"{self.prefix}-init"
        command = [
            "podman", "run", "--rm", "--name", init_name, "--user", "0",
            "--entrypoint", "/bin/sh",
            "--volume", f"{checkout}:/source:ro",
            "--volume", f"{config.parent}:/fixture:ro",
            "--volume", f"{self._volume('workspace')}:/workspace:U",
            "--volume", f"{self._volume('config')}:/config:U",
            "--volume", f"{self._volume('state')}:/state:U",
            "--volume", f"{self._volume('ssh')}:/ssh:U",
            "--volume", f"{self._volume('helper')}:/helper:U",
            "--volume", f"{self._volume('identity')}:/identity:U",
            SEMAPHORE_IMAGE,
            "-c",
            "set -eu; mkdir -p /workspace/job /state/.ssh; "
            "cp -R /source/. /workspace/job/; chown -R 1001:0 /workspace; "
            "cp /fixture/repository-job.json /config/; chmod 0644 /config/repository-job.json; "
            "cp /fixture/controller-ssh /config/; chown 1001:0 /config/controller-ssh; chmod 0600 /config/controller-ssh; "
            "chown 1001:0 /config; "
            "cp /source/roles/semaphore/files/repository-job.py /helper/; "
            "cp /source/roles/semaphore/files/identity-command.py /helper/; chmod 0755 /helper/*.py; "
            "cp /fixture/00-homelab.conf /ssh/; chmod 0644 /ssh/00-homelab.conf; "
            "cp /fixture/known_hosts /state/.ssh/; chown -R 1001:0 /state; chmod 0600 /state/.ssh/known_hosts; "
            "chown 1001:0 /identity; chmod 0750 /identity",
        ]
        result = _run(command)
        self.assertEqual(0, result.returncode, _details(result))

    def _executor(self, suffix: str) -> str:
        return self._start(
            suffix,
            [
                "--network", self.network,
                "--volume", f"{self._volume('tools')}:/tools:U",
                "--volume", f"{self._volume('workspace')}:/workspace:U",
                "--volume", f"{self._volume('config')}:/etc/semaphore:ro",
                "--volume", f"{self._volume('state')}:/var/lib/semaphore:U",
                "--volume", f"{self._volume('ssh')}:/etc/ssh/ssh_config.d:ro",
                "--volume", f"{self._volume('helper')}:/opt/homelab:ro",
                "--volume", f"{self._volume('identity')}:/run/semaphore-identity:U",
                "--entrypoint", "/bin/sh", SEMAPHORE_IMAGE, "-c", "exec sleep 1800",
            ],
        )

    def _environment(self) -> dict[str, str]:
        return {
            "PATH": "/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/tools/home",
            "LC_ALL": "C.UTF-8",
            "MISE_ALL_COMPILE": "false",
            "MISE_LOCKED": "1",
            "MISE_DATA_DIR": "/tools/mise",
            "MISE_CACHE_DIR": "/tools/cache",
            "MISE_CONFIG_DIR": "/tools/config",
            "MISE_STATE_DIR": "/tools/state",
            "MISE_TRUSTED_CONFIG_PATHS": "/workspace/job",
            "UV_CACHE_DIR": "/tools/uv-cache",
            "UV_LINK_MODE": "copy",
            "HOMELAB_GALAXY_CACHE_DIR": "/tools/galaxy",
            "ANSIBLE_CONFIG": "/workspace/job/ansible.cfg",
            "ANSIBLE_SSH_ARGS": "-F /etc/ssh/ssh_config.d/00-homelab.conf",
            "ANSIBLE_SOPS_AGE_KEY_CMD": "/opt/homelab/identity-command.py",
            "VIRTUAL_ENV": "",
        }

    def test_real_gateway_verifies_provisioned_host_and_rejects_wrong_host_key(self) -> None:
        target_image = os.environ.get(
            "SEMAPHORE_REPOSITORY_JOB_TEST_IMAGE", DEFAULT_TARGET_IMAGE
        )
        inspected = _run(["podman", "image", "inspect", target_image])
        self.assertEqual(
            0,
            inspected.returncode,
            f"controller fixture requires the preceding Semaphore Molecule image {target_image}",
        )

        key = self.root / "controller-ssh"
        generated = _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
        self.assertEqual(0, generated.returncode, _details(generated))
        public_key = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
        identity, recipient = self._identity()

        checkout = self.root / "checkout"
        self._copy_checkout(checkout)
        self._encrypt_inventory(checkout, recipient, public_key)
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Fixture"], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "config", "user.email", "fixture@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "fixture"], check=True)
        revision = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", str(checkout), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        repository = "https://example.invalid/homelab-playbook.git"
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "add", "origin", repository], check=True
        )

        target = self._start(
            "target",
            [
                "--network", self.network, "--network-alias", "fixture-target",
                "--hostname", "fixture-target", "--systemd=always", target_image,
                "/usr/lib/systemd/systemd",
            ],
        )
        for _ in range(30):
            ready = self._exec(target, ["systemctl", "is-system-running"])
            if ready.stdout.strip() in (b"running", b"degraded"):
                break
            time.sleep(1)
        else:
            self.fail("disposable target systemd did not become ready")
        result = self._exec(
            target,
            [
                "/bin/sh", "-c",
                "set -eu; install -d -o ansible -g ansible -m 0700 /home/ansible/.ssh; "
                "cat > /home/ansible/.ssh/authorized_keys; "
                "chown ansible:ansible /home/ansible/.ssh/authorized_keys; "
                "chmod 0600 /home/ansible/.ssh/authorized_keys; systemctl start ssh",
            ],
            timeout=30,
        )
        # podman exec does not expose stdin through _exec; copy the public key explicitly.
        copied = _run(["podman", "cp", str(key.with_suffix('.pub')), f"{target}:/tmp/controller.pub"])
        self.assertEqual(0, copied.returncode, _details(copied))
        result = self._exec(
            target,
            [
                "/bin/sh", "-c",
                "set -eu; install -d -o ansible -g ansible -m 0700 /home/ansible/.ssh; "
                "install -o ansible -g ansible -m 0600 /tmp/controller.pub /home/ansible/.ssh/authorized_keys; "
                "systemctl start ssh",
            ],
        )
        self.assertEqual(0, result.returncode, _details(result))
        host_key = self._exec(target, ["cat", "/etc/ssh/ssh_host_ed25519_key.pub"])
        self.assertEqual(0, host_key.returncode, _details(host_key))
        fields = host_key.stdout.decode().split()

        fixture = self.root / "mounted"
        fixture.mkdir()
        shutil.copy2(key, fixture / "controller-ssh")
        (fixture / "known_hosts").write_text(
            f"fixture-target {fields[0]} {fields[1]}\n", encoding="utf-8"
        )
        (fixture / "00-homelab.conf").write_text(
            "Host *\n"
            "    StrictHostKeyChecking yes\n"
            "    UserKnownHostsFile /var/lib/semaphore/.ssh/known_hosts\n"
            "    GlobalKnownHostsFile none\n"
            "    IdentityFile /etc/semaphore/controller-ssh\n"
            "    IdentitiesOnly yes\n"
            "    PasswordAuthentication no\n"
            "    KbdInteractiveAuthentication no\n",
            encoding="utf-8",
        )
        job = {
            "work_root": "/workspace",
            "repository_url": repository,
            "revision": revision,
            "inventory": "production",
            "host_selection": "fixture-target",
            "mise_path": "/tools/bin/mise",
            "tools_root": "/tools",
            "galaxy_cache": "/tools/galaxy",
            "ssh_config": "/etc/ssh/ssh_config.d/00-homelab.conf",
            "age_identity_command": "/opt/homelab/identity-command.py",
            "lock_path": "/tools/locks/repository-job.lock",
            "lock_timeout_seconds": 30,
        }
        (fixture / "repository-job.json").write_text(
            json.dumps({"job": job}), encoding="utf-8"
        )
        self._initialize_volumes(checkout, fixture / "repository-job.json", key)

        provider = self._start(
            "identity-provider",
            [
                "--network=none", "--volume", f"{self._volume('identity')}:/run/semaphore-identity:U",
                "--entrypoint", "/bin/sh", SEMAPHORE_IMAGE, "-c", "exec sleep 1800",
            ],
        )
        provider_process = subprocess.Popen(
            ["podman", "exec", "-i", provider, "python3", "-c", SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop_process, provider_process)
        assert provider_process.stdin is not None
        provider_process.stdin.write(identity + b"\n")
        provider_process.stdin.close()

        executor = self._executor("executor")
        machine = self._exec(executor, ["uname", "-m"])
        self.assertEqual(0, machine.returncode, _details(machine))
        asset_arch, checksum = MISE_ASSETS[machine.stdout.decode().strip()]
        url = (
            f"https://github.com/jdx/mise/releases/download/v{MISE_VERSION}/"
            f"mise-v{MISE_VERSION}-linux-{asset_arch}-musl"
        )
        installed = self._exec(
            executor,
            [
                "/bin/sh", "-c",
                "set -eu; mkdir -p /tools/bin; curl -fsSL \"$1\" -o /tools/bin/mise; "
                "printf '%s  %s\\n' \"$2\" /tools/bin/mise | sha256sum -c -; "
                "chmod 0755 /tools/bin/mise",
                "fixture", url, checksum,
            ],
            timeout=600,
        )
        self.assertEqual(0, installed.returncode, _details(installed))
        for _ in range(50):
            socket_ready = self._exec(provider, ["test", "-S", "/run/semaphore-identity/age.sock"])
            if socket_ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            self.fail("memory-only age identity provider did not become ready")

        bootstrap = self._exec(
            executor,
            ["/tools/bin/mise", "run", "bootstrap"],
            environment=self._environment(), workdir="/workspace/job", timeout=900,
        )
        self.assertEqual(0, bootstrap.returncode, _details(bootstrap))
        provision = self._exec(
            executor,
            [
                "/tools/bin/mise", "run", "playbook", "--", "os", "provision",
                "production", "--limit", "fixture-target",
            ],
            environment=self._environment(), workdir="/workspace/job", timeout=1200,
        )
        self.assertEqual(0, provision.returncode, _details(provision))
        checkout_status = self._exec(
            executor,
            ["git", "status", "--porcelain", "--untracked-files=no"],
            workdir="/workspace/job",
        )
        self.assertEqual(0, checkout_status.returncode, _details(checkout_status))
        self.assertEqual(b"", checkout_status.stdout, checkout_status.stdout.decode())

        verified = self._exec(
            executor,
            [
                "/opt/homelab/repository-job.py", "--config",
                "/etc/semaphore/repository-job.json", "os-verify",
            ],
            workdir="/workspace/job", timeout=1200,
        )
        self.assertEqual(0, verified.returncode, _details(verified))
        record = json.loads(verified.stdout.decode().splitlines()[-1])
        self.assertEqual(
            {"commit": revision, "inventory": "production", "limit": "fixture-target"},
            record,
        )
        served = self._exec(provider, ["cat", "/run/semaphore-identity/served"])
        self.assertEqual(0, served.returncode, _details(served))
        self.assertGreater(int(served.stdout), 0)

        wrong = self.root / "wrong-host-key"
        generated = _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(wrong)])
        self.assertEqual(0, generated.returncode, _details(generated))
        wrong_fields = wrong.with_suffix(".pub").read_text(encoding="utf-8").split()
        rewritten = self._exec(
            executor,
            [
                "/bin/sh", "-c",
                "printf '%s %s %s\\n' \"$1\" \"$2\" \"$3\" > "
                "/var/lib/semaphore/.ssh/known_hosts",
                "fixture", "fixture-target", wrong_fields[0], wrong_fields[1],
            ],
        )
        self.assertEqual(0, rewritten.returncode, _details(rewritten))
        rejected = self._exec(
            executor,
            [
                "/opt/homelab/repository-job.py", "--config",
                "/etc/semaphore/repository-job.json", "os-verify",
            ],
            workdir="/workspace/job", timeout=1200,
        )
        self.assertNotEqual(0, rejected.returncode, "wrong host key was accepted")
        self.assertRegex(
            _details(rejected),
            re.compile(r"host key verification failed", re.IGNORECASE),
        )

        corrected = self._exec(
            executor,
            [
                "/bin/sh", "-c",
                "printf '%s %s %s\\n' \"$1\" \"$2\" \"$3\" > "
                "/var/lib/semaphore/.ssh/known_hosts; "
                "rm -rf /workspace/source.git; "
                "git clone --bare /workspace/job /workspace/source.git",
                "fixture", "fixture-target", fields[0], fields[1],
            ],
            workdir="/workspace/job",
        )
        self.assertEqual(0, corrected.returncode, _details(corrected))
        fixture_repository = "file:///workspace/source.git"
        job["repository_url"] = fixture_repository
        replacement = json.dumps({"job": job}).encode()
        replaced = _run(
            [
                "podman", "run", "--rm", "--interactive", "--user", "0",
                "--entrypoint", "/bin/sh",
                "--volume", f"{self._volume('config')}:/config",
                SEMAPHORE_IMAGE, "-c",
                "cat > /config/repository-job.json; chmod 0644 /config/repository-job.json; "
                "chown 1001:0 /config",
            ],
            input_data=replacement,
        )
        self.assertEqual(0, replaced.returncode, _details(replaced))
        stopped = _run(["podman", "rm", "--force", executor], timeout=30)
        self.assertEqual(0, stopped.returncode, _details(stopped))

        apps = json.dumps(
            {
                "repository_verify": {
                    "active": True,
                    "title": "Repository verification",
                    "path": "/opt/homelab/repository-job.py",
                    "args": ["--config", "/etc/semaphore/repository-job.json"],
                }
            },
            separators=(",", ":"),
        )
        server = self._start(
            "native-app",
            [
                "--network", self.network, "--publish", "127.0.0.1::3000",
                "--volume", f"{self._volume('tools')}:/tools:U",
                "--volume", f"{self._volume('workspace')}:/workspace:U",
                "--volume", f"{self._volume('config')}:/etc/semaphore",
                "--volume", f"{self._volume('state')}:/var/lib/semaphore:U",
                "--volume", f"{self._volume('ssh')}:/etc/ssh/ssh_config.d:ro",
                "--volume", f"{self._volume('helper')}:/opt/homelab:ro",
                "--volume", f"{self._volume('identity')}:/run/semaphore-identity:U",
                "--env", "SEMAPHORE_DB_DIALECT=sqlite",
                "--env", "SEMAPHORE_DB_PATH=/var/lib/semaphore/semaphore.sqlite",
                "--env", "SEMAPHORE_ADMIN=fixture-admin",
                "--env", "SEMAPHORE_ADMIN_PASSWORD=synthetic-password",
                "--env", "SEMAPHORE_ADMIN_NAME=Fixture Administrator",
                "--env", "SEMAPHORE_ADMIN_EMAIL=fixture@example.invalid",
                "--env", "SEMAPHORE_ACCESS_KEY_ENCRYPTION=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "--env", "SEMAPHORE_TMP_PATH=/workspace",
                "--env", f"SEMAPHORE_APPS={apps}",
                SEMAPHORE_IMAGE,
            ],
        )
        port_result = _run(["podman", "port", server, "3000/tcp"])
        self.assertEqual(0, port_result.returncode, _details(port_result))
        if not port_result.stdout.strip():
            logs = _run(["podman", "logs", server])
            self.fail("native Semaphore port was not published: " + _details(logs))
        port = port_result.stdout.decode().strip().rsplit(":", 1)[1]
        base_url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                with request.urlopen(base_url + "/api/ping", timeout=1):
                    break
            except Exception:
                time.sleep(0.5)
        else:
            logs = _run(["podman", "logs", server])
            self.fail("native Semaphore server did not become ready: " + _details(logs))

        api = _load_api_helper()
        client = api.SemaphoreClient(base_url)
        client.login({"auth": "fixture-admin", "password": "synthetic-password"})
        native_apps = client.call("GET", "/api/apps")
        native_app = next(item for item in native_apps if item.get("id") == "repository_verify")
        self.assertEqual("/opt/homelab/repository-job.py", native_app["path"])
        self.assertEqual(
            ["--config", "/etc/semaphore/repository-job.json"], native_app["args"]
        )
        project = client.call(
            "POST", "/api/projects", {"name": "Controller fixture", "max_parallel_tasks": 1}
        )
        project_id = project["id"]
        none_key = next(
            item for item in client.call("GET", f"/api/project/{project_id}/keys")
            if item.get("type") == "none"
        )
        repository_record = client.call(
            "POST",
            f"/api/project/{project_id}/repositories",
            {
                "name": "Synthetic local repository",
                "project_id": project_id,
                "git_url": fixture_repository,
                "git_branch": branch,
                "ssh_key_id": none_key["id"],
            },
        )
        template = client.call(
            "POST",
            f"/api/project/{project_id}/templates",
            {
                "name": "OS verification",
                "project_id": project_id,
                "repository_id": repository_record["id"],
                "playbook": "os-verify",
                "app": "repository_verify",
                "type": "",
                "autorun": False,
                "allow_override_args_in_task": False,
                "allow_override_branch_in_task": False,
                "allow_parallel_tasks": False,
                "environment_ids": [],
                "task_params": {},
            },
        )
        for invocation in range(2):
            queued = client.call(
                "POST", f"/api/project/{project_id}/tasks", {"template_id": template["id"]}
            )
            for _ in range(600):
                task = client.call(
                    "GET", f"/api/project/{project_id}/tasks/{queued['id']}"
                )
                if task.get("status") == "success":
                    break
                if task.get("status") in {"error", "failed", "stopped"}:
                    output = client.call(
                        "GET", f"/api/project/{project_id}/tasks/{queued['id']}/output"
                    )
                    self.fail(
                        f"native repository_verify task {invocation + 1} failed: {output[-20:]}"
                    )
                time.sleep(1)
            else:
                self.fail(f"native repository_verify task {invocation + 1} timed out")
            output = client.call(
                "GET", f"/api/project/{project_id}/tasks/{queued['id']}/output"
            )
            rendered = "\n".join(str(item.get("output", "")) for item in output)
            self.assertIn("[bootstrap] $ ./scripts/bootstrap.sh", rendered)
            self.assertIn(
                "[playbook] $ ./scripts/playbook.sh os verify production --limit fixture-target",
                rendered,
            )
