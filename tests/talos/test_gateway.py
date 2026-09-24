from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import talos_gateway  # noqa: E402


class GatewayTests(unittest.TestCase):
    def request(self, root: Path, action: str = "maintenance-check") -> Path:
        kube = root / "kube.yaml"
        talos = root / "talos.yaml"
        source = root / "source"
        source.mkdir()
        kube.write_text("contexts:\n  - name: selected-kube\n", encoding="utf-8")
        talos.write_text("contexts:\n  selected-talos: {}\n", encoding="utf-8")
        value = {"talos_node": "node-a", "talos_kubeconfig": str(kube),
                 "talos_kube_context": "selected-kube", "talos_talosconfig": str(talos),
                 "talos_talos_context": "selected-talos", "talos_source_dir": str(source),
                 "talos_source_revision": "a" * 40}
        if action != "maintenance-check":
            value["talos_confirmation"] = f"{action}:node-a:192.0.2.10"
        path = root / "request.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_check_uses_fixed_local_ansible_argv_and_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = self.request(Path(temporary))
            captured: dict[str, object] = {}
            class Process:
                def __init__(self, argv: list[str], **kwargs: object) -> None:
                    captured.update(argv=argv, env=kwargs["env"])
                    variables = Path(argv[-1][1:])
                    captured["vars"] = json.loads(variables.read_text())
                    self.pid = 12345

                def wait(self, timeout: float | None = None) -> int:
                    return 0
            injected = {"PYTHONPATH": "/attacker", "PYTHONINSPECT": "1", "GIT_CONFIG_GLOBAL": "/attacker/git",
                        "MISE_CONFIG_FILE": "/attacker/mise", "ANSIBLE_INVENTORY": "/attacker/inventory",
                        "KUBECONFIG": "/attacker/kube", "TALOSCONFIG": "/attacker/talos", "SOPS_AGE_KEY": "secret",
                        "TALOS_LIFECYCLE_TTY_PATH": "/dev/attacker"}
            with (mock.patch.object(talos_gateway.subprocess, "Popen", Process),
                  mock.patch.object(talos_gateway, "_automation_revision", return_value="c" * 40),
                  mock.patch.dict(os.environ, injected)):
                self.assertEqual(talos_gateway.run("maintenance-check", "production", ["-e", f"@{request}", "--check"]), 0)
            argv = captured["argv"]
            self.assertEqual(argv[1:5], ["--inventory", "localhost,", "--connection", "local"])
            self.assertEqual(Path(argv[5]).name, "maintenance-check.yml")
            self.assertIn("--check", argv)
            self.assertTrue(argv[-1].startswith("@/"))
            self.assertNotIn("ANSIBLE_INVENTORY", captured["env"])
            for key in injected:
                self.assertNotIn(key, captured["env"])
            self.assertEqual(set(captured["env"]), {"ANSIBLE_CONFIG", "HOME", "TMPDIR", "PATH", "LANG", "LC_ALL"})
            self.assertNotIn("/attacker", captured["env"]["PATH"])
            self.assertEqual(captured["vars"]["talos_lifecycle_action"], "maintenance-check")
            self.assertEqual(set(captured["vars"]["talos_lifecycle_tool_paths"]),
                             {"bash", "python3", "kubectl", "talosctl", "yq", "mise"})

    def test_rejects_unsupported_controls_before_ansible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = self.request(Path(temporary))
            cases = [["--limit", "localhost"], ["-i", "/tmp/inventory"], ["--inventory=/tmp/x"],
                     ["--tags", "task"], ["--skip-tags=x"], ["--start-at-task", "x"],
                     ["--ask-pass"], ["-e", "talos_node=node-a"], ["-e", f"@{request}", "-e", f"@{request}"]]
            for arguments in cases:
                with self.subTest(arguments=arguments), mock.patch.object(talos_gateway.subprocess, "Popen") as process:
                    with self.assertRaises(talos_gateway.GatewayError):
                        talos_gateway.run("maintenance-check", "production", arguments)
                    process.assert_not_called()

    def test_rejects_inventory_action_check_mode_and_unknown_or_control_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self.request(root, "reboot")
            for action, inventory, arguments in (
                ("unknown", "production", ["-e", f"@{request}"]),
                ("reboot", "staging", ["-e", f"@{request}"]),
                ("reboot", "production", ["-e", f"@{request}", "--check"]),
            ):
                with self.subTest(action=action, inventory=inventory), self.assertRaises(talos_gateway.GatewayError):
                    talos_gateway.run(action, inventory, arguments)
            value = json.loads(request.read_text())
            for key in ("ansible_connection", "talos_lifecycle_action", "talos_lifecycle_tty_path",
                        "trusted_origin", "tool_path"):
                value[key] = "attacker-value"
                request.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(key=key), self.assertRaises(talos_gateway.GatewayError):
                    talos_gateway.run("reboot", "production", ["-e", f"@{request}"])
                del value[key]

    def test_fallback_requires_current_run_token_and_live_group_leader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "supervisor.json"
            path.write_text(json.dumps({"lifecyclePgid": 4321, "ownerToken": "a" * 64}))
            with mock.patch.object(talos_gateway.os, "killpg") as kill:
                talos_gateway._stop_lifecycle_fallback(path, "b" * 64)
                kill.assert_not_called()
            with (
                mock.patch.object(talos_gateway.os, "getpgid", return_value=9999),
                mock.patch.object(talos_gateway.os, "killpg") as kill,
            ):
                talos_gateway._stop_lifecycle_fallback(path, "a" * 64)
                kill.assert_not_called()
            with (
                mock.patch.object(talos_gateway.os, "getpgid", return_value=4321),
                mock.patch.object(talos_gateway.os, "killpg") as kill,
                mock.patch.object(talos_gateway, "_group_exists", return_value=False),
            ):
                talos_gateway._stop_lifecycle_fallback(path, "a" * 64)
                kill.assert_called_once_with(4321, talos_gateway.signal.SIGTERM)
            path.write_text(json.dumps({"lifecyclePgid": 4321, "bridgePgid": 5432,
                                        "ownerToken": "a" * 64}))
            with (
                mock.patch.object(talos_gateway.os, "getpgid", return_value=4321),
                mock.patch.object(talos_gateway.os, "killpg") as kill,
                mock.patch.object(talos_gateway, "_group_exists", return_value=False),
            ):
                talos_gateway._stop_lifecycle_fallback(path, "a" * 64)
                self.assertEqual(kill.call_args_list, [
                    mock.call(5432, talos_gateway.signal.SIGTERM),
                    mock.call(4321, talos_gateway.signal.SIGTERM),
                ])

    def test_cancel_deadline_stops_lifecycle_before_ansible_group(self) -> None:
        events: list[str] = []
        handlers: dict[int, object] = {}

        class Process:
            pid = 2468

            def __init__(self, _argv: list[str], **_kwargs: object) -> None:
                pass

            def wait(self, timeout: float | None = None) -> int:
                if timeout is None:
                    return -9
                if not handlers:
                    self.fail = True
                handler = handlers[talos_gateway.signal.SIGTERM]
                handler(talos_gateway.signal.SIGTERM, None)
                raise talos_gateway.subprocess.TimeoutExpired("ansible", timeout)

        def install(signum: int, handler: object) -> object:
            handlers[signum] = handler
            return talos_gateway.signal.SIG_DFL

        def fallback(_path: Path, _token: str) -> None:
            events.append("lifecycle")

        def killpg(_group: int, _signal: int) -> None:
            events.append("ansible")

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(talos_gateway.subprocess, "Popen", Process),
            mock.patch.object(talos_gateway.signal, "signal", side_effect=install),
            mock.patch.object(talos_gateway, "_stop_lifecycle_fallback", side_effect=fallback),
            mock.patch.object(talos_gateway.os, "killpg", side_effect=killpg),
            mock.patch.object(talos_gateway, "CANCELLATION_WAIT_SECONDS", 0),
        ):
            root = Path(temporary)
            status = talos_gateway._run_ansible([], {}, root / "cancel", root / "supervisor", "a" * 64)
        self.assertEqual(status, 143)
        self.assertEqual(events, ["lifecycle", "ansible"])


if __name__ == "__main__":
    unittest.main()
