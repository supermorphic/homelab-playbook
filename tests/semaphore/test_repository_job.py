from __future__ import annotations

import fcntl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib import request
import uuid


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "roles" / "semaphore" / "files" / "repository-job.py"
API_HELPER = ROOT / "roles" / "semaphore" / "files" / "repository-job-api.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepositoryJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work_root = self.root / "workspace"
        self.checkout = self.work_root / "project_1" / "repository_1_template_1"
        self.checkout.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.checkout)], check=True)
        subprocess.run(
            ["git", "-C", str(self.checkout), "config", "user.email", "fixture@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.checkout), "config", "user.name", "Fixture"],
            check=True,
        )
        (self.checkout / "tracked").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.checkout), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "commit", "-qm", "fixture"], check=True)
        self.revision = subprocess.run(
            ["git", "-C", str(self.checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.repository = "https://example.invalid/homelab-playbook.git"
        subprocess.run(
            ["git", "-C", str(self.checkout), "remote", "add", "origin", self.repository],
            check=True,
        )
        self.tools = self.root / "tools"
        (self.tools / "bin").mkdir(parents=True)
        self.calls = self.root / "mise-calls.jsonl"
        self.mise = self.tools / "bin" / "mise"
        self.mise.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$PWD|$MISE_ALL_COMPILE|$MISE_LOCKED|$MISE_DATA_DIR|$ANSIBLE_SOPS_AGE_KEY_CMD|$*\" >> {self.calls!s}\n"
            f"test ! -e {self.root / 'fail-bootstrap'!s} || test \"$*\" != 'run bootstrap' || exit 31\n"
            f"test ! -e {self.root / 'fail-playbook'!s} || test \"$*\" != 'run playbook -- os verify production --limit fixture-host' || exit 37\n",
            encoding="utf-8",
        )
        self.mise.chmod(self.mise.stat().st_mode | stat.S_IXUSR)
        (self.root / "ssh_config").write_text(
            "Host *\n    StrictHostKeyChecking yes\n    UserKnownHostsFile /fixture/known_hosts\n",
            encoding="utf-8",
        )
        (self.root / "ssh_config").chmod(0o640)
        identity_command = self.root / "identity-command.py"
        identity_command.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        identity_command.chmod(0o750)
        self.config = self.root / "repository-job.json"
        self.write_config()

    def write_config(self, **overrides) -> None:
        job = {
            "work_root": str(self.work_root),
            "repository_url": self.repository,
            "revision": self.revision,
            "inventory": "production",
            "host_selection": "fixture-host",
            "mise_path": str(self.mise),
            "tools_root": str(self.tools),
            "galaxy_cache": str(self.tools / "galaxy"),
            "ssh_config": str(self.root / "ssh_config"),
            "age_identity_command": str(self.root / "identity-command.py"),
            "lock_path": str(self.tools / "locks" / "repository-job.lock"),
            "lock_timeout_seconds": 1,
        }
        job.update(overrides)
        self.config.write_text(json.dumps({"job": job}), encoding="utf-8")
        self.config.chmod(0o600)

    def run_job(self, *extra: str):
        return subprocess.run(
            [os.environ.get("PYTHON", "python3"), str(WRAPPER), "--config", str(self.config), "os-verify", *extra],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            check=False,
        )

    def read_calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return self.calls.read_text(encoding="utf-8").splitlines()

    def test_runs_bootstrap_then_exact_gateway_with_trusted_environment(self) -> None:
        result = self.run_job()

        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.read_calls()
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[0].endswith("|run bootstrap"), calls)
        self.assertTrue(
            calls[1].endswith("|run playbook -- os verify production --limit fixture-host"),
            calls,
        )
        for call in calls:
            fields = call.split("|", 5)
            self.assertEqual(str(self.checkout.resolve()), fields[0])
            self.assertEqual("false", fields[1])
            self.assertEqual("1", fields[2])
            self.assertEqual(str((self.tools / "mise").resolve()), fields[3])
            self.assertEqual(str(self.root / "identity-command.py"), fields[4])
        record = json.loads(result.stdout)
        self.assertEqual(
            {"commit": self.revision, "inventory": "production", "limit": "fixture-host"},
            record,
        )

    def test_bootstrap_failure_prevents_playbook_and_propagates_status(self) -> None:
        (self.root / "fail-bootstrap").touch()

        result = self.run_job()

        self.assertEqual(31, result.returncode)
        self.assertEqual(1, len(self.read_calls()))
        self.assertTrue(self.read_calls()[0].endswith("|run bootstrap"))

    def test_playbook_failure_propagates_after_bootstrap(self) -> None:
        (self.root / "fail-playbook").touch()

        result = self.run_job()

        self.assertEqual(37, result.returncode)
        self.assertEqual(2, len(self.read_calls()))

    def test_rejects_extra_arguments_and_unsafe_selection_before_mise(self) -> None:
        result = self.run_job("unexpected=value")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.read_calls())

        result = self.run_job("--help")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.read_calls())

        self.write_config(host_selection="fixture-host:!other")
        result = self.run_job()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.read_calls())

    def test_rejects_wrong_revision_remote_dirty_checkout_and_outside_work_root(self) -> None:
        cases = (
            {"revision": "0" * 40},
            {"repository_url": "ssh://git@example.invalid/other.git"},
            {"work_root": str(self.root / "other-workspace")},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.write_config(**overrides)
                result = self.run_job()
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], self.read_calls())

        self.write_config()
        (self.checkout / "tracked").write_text("changed\n", encoding="utf-8")
        result = self.run_job()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.read_calls())

    def test_rejects_symlink_or_writable_configuration_and_does_not_echo_content(self) -> None:
        secret = "synthetic-controller-secret"
        self.config.write_text(json.dumps({"job": {"credential": secret}}), encoding="utf-8")
        self.config.chmod(0o666)

        result = self.run_job()

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual([], self.read_calls())

    def test_rejects_missing_ssh_policy_before_mise(self) -> None:
        (self.root / "ssh_config").unlink()

        result = self.run_job()

        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.read_calls())

    def test_lock_timeout_prevents_either_mise_call(self) -> None:
        lock_path = self.tools / "locks" / "repository-job.lock"
        lock_path.parent.mkdir(parents=True)
        with lock_path.open("w", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.write_config(lock_timeout_seconds=0)
            result = self.run_job()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("lock", result.stderr.lower())
        self.assertEqual([], self.read_calls())


class SemaphoreFixture(BaseHTTPRequestHandler):
    state = None

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _json(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def _send(self, status: int, payload=None):
        self.send_response(status)
        if payload is not None:
            data = json.dumps(payload).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
        else:
            data = b""
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        body = self._json()
        self.state["requests"].append(("POST", self.path, body))
        if self.path == "/api/auth/login":
            if body != self.state["login"]:
                self._send(HTTPStatus.UNAUTHORIZED)
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Set-Cookie", "semaphore=synthetic-session; Path=/; HttpOnly")
            self.end_headers()
            return
        if not self._authorized():
            return
        if self.path == "/api/projects":
            record = dict(body, id=self.state["next_project_id"], created="2026-09-10T00:00:00Z")
            self.state["next_project_id"] += 1
            self.state["projects"].append(record)
            self.state["keys"][record["id"]] = [{"id": 1, "name": "None", "type": "none"}]
            self.state["repositories"][record["id"]] = []
            self.state["templates"][record["id"]] = []
            self._send(HTTPStatus.CREATED, record)
            return
        for resource in ("repositories", "templates"):
            prefix = f"/api/project/1/{resource}"
            if self.path == prefix:
                records = self.state[resource][1]
                record = dict(body, id=len(records) + 1, project_id=1)
                records.append(record)
                self._send(HTTPStatus.CREATED, record)
                return
        self._send(HTTPStatus.NOT_FOUND)

    def do_GET(self):  # noqa: N802
        self.state["requests"].append(("GET", self.path, None))
        if not self._authorized():
            return
        if self.path == "/api/projects":
            self._send(HTTPStatus.OK, self.state["projects"])
            return
        for resource in ("keys", "repositories", "templates"):
            if self.path == f"/api/project/1/{resource}":
                self._send(HTTPStatus.OK, self.state[resource][1])
                return
        self._send(HTTPStatus.NOT_FOUND)

    def do_PUT(self):  # noqa: N802
        body = self._json()
        self.state["requests"].append(("PUT", self.path, body))
        if not self._authorized():
            return
        if self.path == "/api/project/1":
            index = next(
                index for index, record in enumerate(self.state["projects"])
                if record["id"] == 1
            )
            self.state["projects"][index] = body
            self._send(HTTPStatus.NO_CONTENT)
            return
        for resource in ("repositories", "templates"):
            prefix = f"/api/project/1/{resource}/"
            if self.path.startswith(prefix):
                identifier = int(self.path.removeprefix(prefix))
                records = self.state[resource][1]
                records[identifier - 1] = body
                self._send(HTTPStatus.NO_CONTENT)
                return
        self._send(HTTPStatus.NOT_FOUND)

    def _authorized(self):
        if self.headers.get("Cookie") != "semaphore=synthetic-session":
            self._send(HTTPStatus.UNAUTHORIZED)
            return False
        return True


class ApiReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = load_module("repository_job_api", API_HELPER)
        self.state = {
            "login": {"auth": "fixture-admin", "password": "synthetic-password"},
            "projects": [{"id": 90, "name": "Unrelated", "max_parallel_tasks": 8}],
            "keys": {},
            "repositories": {},
            "templates": {},
            "next_project_id": 1,
            "requests": [],
        }
        SemaphoreFixture.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SemaphoreFixture)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.declaration = {
            "project": {"name": "Homelab", "max_parallel_tasks": 1},
            "repository": {
                "name": "homelab-playbook",
                "git_url": "https://example.invalid/homelab-playbook.git",
                "git_branch": "a" * 40,
            },
            "template": {
                "name": "OS verification",
                "playbook": "os-verify",
                "app": "repository_verify",
            },
        }

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def reconcile(self):
        client = self.api.SemaphoreClient(self.base_url)
        client.login(self.state["login"])
        return self.api.reconcile(client, self.declaration)

    def test_creates_declared_native_objects_then_second_run_changes_nothing(self) -> None:
        first = self.reconcile()
        writes_after_first = [
            request for request in self.state["requests"]
            if request[0] in {"POST", "PUT"} and request[1] != "/api/auth/login"
        ]
        second = self.reconcile()
        writes_after_second = [
            request for request in self.state["requests"]
            if request[0] in {"POST", "PUT"} and request[1] != "/api/auth/login"
        ]

        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(writes_after_first, writes_after_second)
        self.assertEqual(["Unrelated", "Homelab"], [item["name"] for item in self.state["projects"]])
        template = self.state["templates"][1][0]
        self.assertEqual("repository_verify", template["app"])
        self.assertEqual("os-verify", template["playbook"])
        self.assertFalse(template["allow_override_args_in_task"])
        self.assertFalse(template["allow_override_branch_in_task"])
        self.assertFalse(template["allow_parallel_tasks"])
        self.assertIsNone(template["arguments"])
        self.assertIsNone(template["git_branch"])
        self.assertEqual([], template["survey_vars"])

    def test_clears_managed_template_arguments_branch_and_surveys(self) -> None:
        self.reconcile()
        template = self.state["templates"][1][0]
        template["arguments"] = '["--help"]'
        template["git_branch"] = "other"
        template["survey_vars"] = [
            {"name": "injected", "title": "Injected", "target": "env"}
        ]

        result = self.reconcile()

        self.assertTrue(result["changed"])
        template = self.state["templates"][1][0]
        self.assertIsNone(template["arguments"])
        self.assertIsNone(template["git_branch"])
        self.assertEqual([], template["survey_vars"])

    def test_reconciles_only_declared_fields_and_preserves_unrelated_objects(self) -> None:
        self.reconcile()
        self.state["projects"][1]["max_parallel_tasks"] = 4
        self.state["projects"][1]["alert"] = True
        self.state["repositories"][1].append(
            {"id": 2, "project_id": 1, "name": "Unrelated repository", "git_url": "/tmp/unrelated", "git_branch": "main", "ssh_key_id": 1}
        )

        result = self.reconcile()

        self.assertTrue(result["changed"])
        self.assertEqual(1, self.state["projects"][1]["max_parallel_tasks"])
        self.assertTrue(self.state["projects"][1]["alert"])
        self.assertEqual(2, len(self.state["repositories"][1]))

    def test_http_errors_are_sanitized(self) -> None:
        client = self.api.SemaphoreClient(self.base_url)

        with self.assertRaises(self.api.ApiError) as raised:
            client.login({"auth": "fixture-admin", "password": "do-not-print-this"})

        self.assertNotIn("do-not-print-this", str(raised.exception))

    def test_rejects_unsafe_repository_declaration_before_api_calls(self) -> None:
        self.declaration["repository"]["git_branch"] = "../main"
        client = mock.Mock()

        with self.assertRaises(self.api.ApiError):
            self.api.reconcile(client, self.declaration)

        client.call.assert_not_called()


class ControllerSourceContractTests(unittest.TestCase):
    def test_native_app_and_controller_tasks_keep_fixed_contract(self) -> None:
        environment = (ROOT / "roles/semaphore/templates/semaphore.env.j2").read_text()
        tasks = (ROOT / "roles/semaphore/tasks/controller.yml").read_text()
        declaration = (ROOT / "roles/semaphore/templates/repository-job.json.j2").read_text()
        ssh_config = (ROOT / "roles/semaphore/templates/ssh_config.j2").read_text()

        self.assertIn("SEMAPHORE_APPS", environment)
        self.assertIn("repository_verify", declaration)
        self.assertIn('"playbook": "os-verify"', declaration)
        self.assertIn('"mise_path": "/tools/bin/mise"', declaration)
        self.assertIn('"galaxy_cache": "/tools/galaxy"', declaration)
        self.assertNotIn("schedules", tasks)
        self.assertIn("no_log: true", tasks)
        self.assertIn("repository-job-api.py", tasks)
        self.assertIn("StrictHostKeyChecking yes", ssh_config)
        self.assertIn("UserKnownHostsFile /var/lib/semaphore/.ssh/known_hosts", ssh_config)
        self.assertIn("IdentityFile /etc/semaphore/controller-ssh", ssh_config)
        self.assertIn("IdentitiesOnly yes", ssh_config)


@unittest.skipUnless(
    os.environ.get("SEMAPHORE_ACTUAL_API_FIXTURE") == "1",
    "set SEMAPHORE_ACTUAL_API_FIXTURE=1 to run the pinned v2.19.12 API fixture",
)
class ActualSemaphoreApiTests(unittest.TestCase):
    IMAGE = (
        "docker.io/semaphoreui/semaphore:v2.19.12@"
        "sha256:3996804607ebb63690528185bb9adc3507ee896851098e4453975f2ce7f8b435"
    )

    def setUp(self) -> None:
        self.api = load_module("repository_job_api_actual", API_HELPER)
        self.container = f"semaphore-api-fixture-{uuid.uuid4().hex[:10]}"
        apps = json.dumps(
            {
                "repository_verify": {
                    "active": True,
                    "title": "Repository verification",
                    "path": "/bin/true",
                    "args": [],
                }
            },
            separators=(",", ":"),
        )
        result = subprocess.run(
            [
                "podman", "run", "--detach", "--name", self.container,
                "--publish", "127.0.0.1::3000",
                "--env", "SEMAPHORE_DB_DIALECT=sqlite",
                "--env", "SEMAPHORE_DB_PATH=/tmp/semaphore.sqlite",
                "--env", "SEMAPHORE_ADMIN=fixture-admin",
                "--env", "SEMAPHORE_ADMIN_PASSWORD=synthetic-password",
                "--env", "SEMAPHORE_ADMIN_NAME=Fixture Administrator",
                "--env", "SEMAPHORE_ADMIN_EMAIL=fixture@example.invalid",
                "--env", "SEMAPHORE_ACCESS_KEY_ENCRYPTION=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "--env", f"SEMAPHORE_APPS={apps}",
                self.IMAGE,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"could not start Semaphore API fixture: {result.stderr}")
        self.addCleanup(
            subprocess.run,
            ["podman", "rm", "--force", self.container],
            capture_output=True,
            text=True,
            check=False,
        )
        port = subprocess.run(
            ["podman", "port", self.container, "3000/tcp"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().rsplit(":", 1)[1]
        self.base_url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                with request.urlopen(self.base_url + "/api/ping", timeout=1):
                    break
            except Exception:
                time.sleep(0.5)
        else:
            logs = subprocess.run(
                ["podman", "logs", self.container], capture_output=True, text=True, check=False
            ).stderr
            self.fail(f"Semaphore API fixture did not become ready: {logs[-1000:]}")
        self.declaration = {
            "project": {"name": "Homelab", "max_parallel_tasks": 1},
            "repository": {
                "name": "homelab-playbook",
                "git_url": "https://example.invalid/homelab-playbook.git",
                "git_branch": "a" * 40,
            },
            "template": {
                "name": "OS verification",
                "playbook": "os-verify",
                "app": "repository_verify",
            },
        }

    def client(self):
        client = self.api.SemaphoreClient(self.base_url)
        client.login({"auth": "fixture-admin", "password": "synthetic-password"})
        return client

    def test_v21912_creation_and_second_run_no_change(self) -> None:
        client = self.client()
        unrelated = client.call("POST", "/api/projects", {"name": "Unrelated"})

        first = self.api.reconcile(client, self.declaration)
        second = self.api.reconcile(self.client(), self.declaration)

        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        verifier = self.client()
        projects = verifier.call("GET", "/api/projects")
        self.assertTrue(any(item["id"] == unrelated["id"] for item in projects))
        templates = verifier.call("GET", f"/api/project/{first['project_id']}/templates")
        declared = next(item for item in templates if item["id"] == first["template_id"])
        self.assertEqual("repository_verify", declared["app"])
        self.assertEqual("os-verify", declared["playbook"])
        self.assertFalse(declared.get("allow_override_args_in_task", False))
        self.assertFalse(declared.get("allow_override_branch_in_task", False))
        self.assertFalse(declared.get("allow_parallel_tasks", False))
        self.assertEqual(
            [], verifier.call("GET", f"/api/project/{first['project_id']}/schedules")
        )


if __name__ == "__main__":
    unittest.main()
