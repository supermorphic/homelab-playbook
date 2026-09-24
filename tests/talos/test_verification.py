from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import sys


FILES = Path(__file__).resolve().parents[2] / "roles/talos_lifecycle/files"
sys.path.insert(0, str(FILES))
from verification import VerificationError, invoke_verifier, validate_response  # noqa: E402


def request(mode: str = "recovery") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "requestId": "4e17c4dd-f983-4e2c-b261-b620916201a3",
        "mode": mode,
        "node": "node-a",
        "sourceRevision": "a" * 40,
        "apiServer": "https://192.0.2.20:6443",
        "nodes": {"node-a": "192.0.2.10", "node-b": "192.0.2.11", "node-c": "192.0.2.12"},
        "talosEndpoints": ["192.0.2.10", "192.0.2.11", "192.0.2.12"],
        "credentials": {
            "kubeconfig": "/operator/kubeconfig",
            "kubeContext": "fixture-kube",
            "talosconfig": "/operator/talosconfig",
            "talosContext": "fixture-talos",
        },
        "expectedContainment": None if mode != "recovery" else {
            "node": "node-a", "record": '{"schemaVersion":1,"kind":"reboot"}'
        },
        "timeoutSeconds": 1800,
    }


def response(value: dict[str, object]) -> dict[str, object]:
    mode = str(value["mode"])
    return {
        "schemaVersion": 1,
        "requestId": value["requestId"],
        "mode": mode,
        "node": value["node"],
        "sourceRevision": value["sourceRevision"],
        "kubeContext": value["credentials"]["kubeContext"],  # type: ignore[index]
        "talosContext": value["credentials"]["talosContext"],  # type: ignore[index]
        "checks": {
            "source": "passed",
            "cilium": "not-run" if mode == "prepare" else "passed",
            "foundation": "not-run" if mode == "prepare" else "passed",
        },
    }


class ResponseTests(unittest.TestCase):
    def assert_process_stopped(self, pid: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-o", "state=", "-p", str(pid)],
                text=True,
                capture_output=True,
                check=False,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                return
            time.sleep(0.05)
        self.fail(f"verifier descendant {pid} survived cleanup")

    def test_exact_prepare_baseline_and_recovery_results_are_accepted(self) -> None:
        for mode in ("prepare", "baseline", "recovery"):
            value = request(mode)
            validate_response(value, response(value), 0)

    def test_baseline_response_cannot_satisfy_recovery(self) -> None:
        value = request("baseline")
        result = response(value)
        value["mode"] = "recovery"
        with self.assertRaises(VerificationError):
            validate_response(value, result, 0)

    def test_nonzero_mismatch_missing_extra_and_incomplete_are_rejected(self) -> None:
        value = request()
        cases: list[tuple[dict[str, object], int]] = []
        cases.append((response(value), 7))
        for key in ("requestId", "mode", "node", "sourceRevision", "kubeContext", "talosContext"):
            result = response(value)
            result[key] = "wrong"
            cases.append((result, 0))
        result = response(value)
        del result["checks"]
        cases.append((result, 0))
        result = response(value)
        result["extra"] = True
        cases.append((result, 0))
        result = response(value)
        result["checks"]["foundation"] = "not-run"  # type: ignore[index]
        cases.append((result, 0))
        for result, returncode in cases:
            with self.subTest(result=result, returncode=returncode), self.assertRaises(VerificationError):
                validate_response(value, result, returncode)

    def test_fixed_invocation_rejects_malformed_multiple_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir()
            mise = root / "mise"
            source = {"verification_dir": str(root), "mise": str(mise), "bash": "/bin/bash",
                      "recovery_helm_cache": str(cache)}
            value = request("prepare")
            for stdout in ("not-json", "{}\n{}\n"):
                mise.write_text(
                    "#!/bin/sh\n"
                    f"printf '%b' {json.dumps(stdout)}\n",
                    encoding="utf-8",
                )
                mise.chmod(0o700)
                with self.assertRaises(VerificationError):
                    invoke_verifier(source, value, deadline_seconds=10)
            mise.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
            mise.chmod(0o700)
            with self.assertRaisesRegex(VerificationError, "timed out"):
                invoke_verifier(source, value, deadline_seconds=1)

    def test_timeout_stops_and_waits_for_owned_verifier_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir()
            pid_path = root / "observer.pid"
            mise = root / "mise"
            mise.write_text(
                "#!/usr/bin/env python3\n"
                "import os,pathlib,signal,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True)\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n"
                "def stop(_signum,_frame):\n"
                " os.killpg(child.pid,signal.SIGTERM); child.wait(); raise SystemExit(143)\n"
                "signal.signal(signal.SIGTERM,stop)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            mise.chmod(0o700)
            source = {
                "verification_dir": str(root),
                "mise": str(mise),
                "bash": "/bin/bash",
                "recovery_helm_cache": str(cache),
            }
            with self.assertRaisesRegex(VerificationError, "timed out"):
                invoke_verifier(source, request("prepare"), deadline_seconds=1)
            self.assert_process_stopped(int(pid_path.read_text(encoding="utf-8")))

    def test_cancellation_stops_and_waits_for_owned_verifier_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir()
            pid_path = root / "observer.pid"
            cancel_path = root / "cancel"
            mise = root / "mise"
            mise.write_text(
                "#!/usr/bin/env python3\n"
                "import os,pathlib,signal,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True)\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n"
                "def stop(_signum,_frame):\n"
                " os.killpg(child.pid,signal.SIGTERM); child.wait(); raise SystemExit(143)\n"
                "signal.signal(signal.SIGTERM,stop)\n"
                f"pathlib.Path({str(cancel_path)!r}).write_text('cancel')\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            mise.chmod(0o700)
            source = {
                "verification_dir": str(root),
                "mise": str(mise),
                "bash": "/bin/bash",
                "recovery_helm_cache": str(cache),
            }
            with mock.patch.dict(os.environ, {"TALOS_LIFECYCLE_CANCEL_PATH": str(cancel_path)}), self.assertRaisesRegex(
                VerificationError, "cancelled"
            ):
                invoke_verifier(source, request("prepare"), deadline_seconds=10)
            self.assert_process_stopped(int(pid_path.read_text(encoding="utf-8")))

    def test_signal_during_spawn_is_deferred_until_verifier_can_be_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir()
            child_pid = root / "verifier.pid"
            mise = root / "mise"
            mise.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
            mise.chmod(0o700)
            runner = (
                "import importlib.util,json,os,pathlib,signal,sys; "
                "spec=importlib.util.spec_from_file_location('verification_under_test',sys.argv[1]); "
                "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
                "real=module.subprocess.Popen; "
                "pid_path=pathlib.Path(sys.argv[2]); "
                "spawn=lambda *args,**kwargs: signal_after_spawn(real,pid_path,*args,**kwargs); "
                "module.subprocess.Popen=spawn; "
                "source=json.loads(sys.argv[3]); request=json.loads(sys.argv[4]); "
                "exec(\"try:\\n module.invoke_verifier(source,request,deadline_seconds=10)\\n"
                "except module.VerificationError as error:\\n"
                " raise SystemExit(0 if 'cancelled' in str(error) else 2)\\n"
                "raise SystemExit(3)\")"
            )
            helper = (
                "def signal_after_spawn(real,pid_path,*args,**kwargs):\n"
                " process=real(*args,**kwargs)\n"
                " pid_path.write_text(str(process.pid))\n"
                " os.kill(os.getpid(),signal.SIGTERM)\n"
                " return process\n"
            )
            source = {
                "verification_dir": str(root),
                "mise": str(mise),
                "bash": "/bin/bash",
                "recovery_helm_cache": str(cache),
            }
            process = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    helper + runner,
                    str(FILES / "verification.py"),
                    str(child_pid),
                    json.dumps(source),
                    json.dumps(request("prepare")),
                ],
                check=False,
                timeout=10,
            )
            pid = int(child_pid.read_text(encoding="utf-8"))
            if process.returncode != 0:
                try:
                    os.killpg(pid, __import__("signal").SIGKILL)
                except ProcessLookupError:
                    pass
            self.assertEqual(process.returncode, 0)
            self.assert_process_stopped(pid)


if __name__ == "__main__":
    unittest.main()
