#!/usr/bin/env python3
"""Run one validated Talos lifecycle action in one foreground process tree."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import uuid
import time
from pathlib import Path

from inputs import InputError, load_request
from source import SourceError, prepare_source
from verification import VerificationError, invoke_verifier, make_request


FILES = Path(__file__).resolve().parent


class RuntimeFailure(RuntimeError):
    pass


def _record_phase(phase: str) -> None:
    allowed = {"preflight-pending", "preflight-confirmed", "containment-confirmed", "completed"}
    if phase not in allowed:
        raise RuntimeFailure("invalid lifecycle result phase")
    raw_path = os.environ.get("TALOS_LIFECYCLE_RESULT_PATH", "")
    path = Path(raw_path)
    if not path.is_absolute() or path.is_symlink() or not path.parent.is_dir() or path.parent.is_symlink():
        raise RuntimeFailure("trusted lifecycle result path is missing or invalid")
    descriptor, temporary = tempfile.mkstemp(prefix=".talos-result-", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"last_confirmed_phase": phase}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _record_supervisor(group: int) -> Path:
    raw_path = os.environ.get("TALOS_LIFECYCLE_SUPERVISOR_PATH", "")
    path = Path(raw_path)
    token = os.environ.get("TALOS_LIFECYCLE_SUPERVISOR_TOKEN", "")
    if (group <= 1 or len(token) != 64 or any(character not in "0123456789abcdef" for character in token)
            or not path.is_absolute() or path.is_symlink()
            or not path.parent.is_dir() or path.parent.is_symlink()):
        raise RuntimeFailure("trusted lifecycle supervisor path is missing or invalid")
    descriptor, temporary = tempfile.mkstemp(prefix=".talos-supervisor-", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"lifecyclePgid": group, "ownerToken": token}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def _process_group_exists(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _registered_bridge_group() -> int | None:
    path = Path(os.environ.get("TALOS_LIFECYCLE_SUPERVISOR_PATH", ""))
    token = os.environ.get("TALOS_LIFECYCLE_SUPERVISOR_TOKEN", "")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (not isinstance(value, dict) or value.get("ownerToken") != token
            or set(value) != {"lifecyclePgid", "ownerToken", "bridgePgid"}
            or not isinstance(value.get("bridgePgid"), int)):
        return None
    group = value["bridgePgid"]
    return group if group > 1 else None


def _stop_group_id(group: int, initial_signal: int = signal.SIGTERM) -> None:
    try:
        os.killpg(group, initial_signal)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 5
    while _process_group_exists(group) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _process_group_exists(group):
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _stop_process_group(
    process: subprocess.Popen[object], timeout: float = 30, initial_signal: int = signal.SIGTERM
) -> int:
    bridge_group = _registered_bridge_group()
    if bridge_group is not None:
        _stop_group_id(bridge_group, initial_signal)
    try:
        os.killpg(process.pid, initial_signal)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        status = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        status = process.wait()
    deadline = time.monotonic() + 5
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return status


def _tools(action: str) -> dict[str, str]:
    expected = {"bash", "python3", "kubectl", "talosctl", "yq", "mise"}
    if action == "abrupt-loss-test":
        expected.update({"dig", "curl"})
    try:
        value = json.loads(os.environ["TALOS_LIFECYCLE_TOOL_PATHS"])
    except (KeyError, json.JSONDecodeError) as error:
        raise RuntimeFailure("trusted tool paths are missing or invalid") from error
    if not isinstance(value, dict) or set(value) != expected:
        raise RuntimeFailure("trusted tool paths do not match the action")
    tools: dict[str, str] = {}
    for name, raw in value.items():
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise RuntimeFailure(f"trusted tool path is invalid: {name}")
        path = Path(raw)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeFailure(f"trusted tool is unavailable: {name}")
        tools[name] = str(path)
    return tools


def _confirmation(action: str, request: dict[str, object], address: str) -> None:
    value = request.get("talos_confirmation")
    node = request["talos_node"]
    expected = {
        "maintenance-enter": f"enter:{node}:{address}",
        "reboot": f"reboot:{node}:{address}",
        "abrupt-loss-test": f"remove-power:{node}:{address}",
    }.get(action)
    if expected is not None and value != expected:
        raise RuntimeFailure("confirmation does not match the action and resolved target")
    if action == "maintenance-exit" and (
        not isinstance(value, str) or not value.startswith(f"accept:{node}:")
    ):
        raise RuntimeFailure("confirmation does not match maintenance exit")


def _bash_major(bash: str) -> int:
    output = subprocess.check_output([bash, "-c", "printf '%s' \"${BASH_VERSINFO[0]}\""], text=True)
    return int(output)


def _make_evidence_dir(request: dict[str, object]) -> Path:
    root = Path(str(request.get("talos_evidence_dir", FILES.parents[2] / ".tmp/talos-evidence")))
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise RuntimeFailure("Talos evidence root must be a real directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    evidence = Path(tempfile.mkdtemp(prefix="abrupt-loss-", dir=root))
    os.chmod(evidence, 0o700)
    return evidence


def run(action: str, request_path: Path) -> int:
    _record_phase("preflight-pending")
    request = load_request(request_path, action)
    with tempfile.TemporaryDirectory(prefix="talos-lifecycle-") as temporary:
        private = Path(temporary)
        os.chmod(private, 0o700)
        trusted_origin = os.environ.get("TALOS_LIFECYCLE_TRUSTED_ORIGIN", "")
        if trusted_origin != "https://github.com/supermorphic/homelab-talos.git":
            raise RuntimeFailure("trusted source origin is missing or invalid")
        source = prepare_source(
            Path(request["talos_source_dir"]),
            request["talos_source_revision"],
            private / "source",
            trusted_origin,
        )
        node = request["talos_node"]
        nodes = source["nodes"]
        if node not in nodes:
            raise RuntimeFailure("selected node is absent from approved desired input")
        _confirmation(action, request, nodes[node])
        tools = _tools(action)
        if _bash_major(tools["bash"]) < 5:
            raise RuntimeFailure("Talos lifecycle requires Bash 5 or newer")
        source["mise"] = tools["mise"]
        source["bash"] = tools["bash"]
        mise_data_dir = Path(os.environ.get("TALOS_LIFECYCLE_MISE_DATA_DIR", ""))
        if not mise_data_dir.is_absolute():
            raise RuntimeFailure("trusted Mise data directory is missing or invalid")
        source["mise_data_dir"] = str(mise_data_dir)
        prepare_request = make_request(source, request, "prepare")
        invoke_verifier(source, prepare_request, deadline_seconds=prepare_request["timeoutSeconds"])
        if action != "maintenance-exit":
            baseline_request = make_request(source, request, "baseline")
            invoke_verifier(
                source,
                baseline_request,
                deadline_seconds=baseline_request["timeoutSeconds"],
            )
        prepared = {
            **request,
            **source,
            "action": action,
            "node": node,
            "address": nodes[node],
            "tools": tools,
            "run_id": str(uuid.uuid4()),
            "holder": f"talos-lifecycle-{uuid.uuid4()}",
            "runtime_dir": str(private),
        }
        prepared_path = private / "prepared.json"
        prepared["prepared_path"] = str(prepared_path)
        if action == "abrupt-loss-test":
            prepared["evidence_dir"] = str(_make_evidence_dir(request))
        prepared_path.write_text(json.dumps(prepared, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(prepared_path, 0o600)
        tool_path = os.pathsep.join(
            dict.fromkeys([*(str(Path(value).parent) for value in tools.values()), "/usr/bin", "/bin"])
        )
        environment = {
            "HOME": str(private),
            "PATH": tool_path,
            "TMPDIR": str(private),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TALOS_LIFECYCLE_RESULT_PATH": os.environ["TALOS_LIFECYCLE_RESULT_PATH"],
            "TALOS_LIFECYCLE_SUPERVISOR_PATH": os.environ["TALOS_LIFECYCLE_SUPERVISOR_PATH"],
            "TALOS_LIFECYCLE_SUPERVISOR_TOKEN": os.environ["TALOS_LIFECYCLE_SUPERVISOR_TOKEN"],
        }
        if action == "abrupt-loss-test":
            environment["TALOS_LIFECYCLE_TTY_PATH"] = os.environ.get(
                "TALOS_LIFECYCLE_TTY_PATH", "/dev/tty"
            )
        cancel_path = Path(os.environ.get("TALOS_LIFECYCLE_CANCEL_PATH", ""))
        if not cancel_path.is_absolute() or cancel_path.exists():
            raise RuntimeFailure("trusted cancellation path is missing or invalid")
        process = subprocess.Popen(
            [tools["bash"], str(FILES / "node/lifecycle.sh"), str(prepared_path)],
            start_new_session=True,
            env=environment,
        )
        supervisor_path: Path | None = None
        try:
            supervisor_path = _record_supervisor(process.pid)
            while True:
                status = process.poll()
                if status is not None:
                    _stop_process_group(process, timeout=5)
                    if status == 0:
                        _record_phase("completed")
                    return 128 - status if status < 0 else status
                if cancel_path.exists():
                    _stop_process_group(process)
                    return 143
                time.sleep(0.1)
        except KeyboardInterrupt:
            status = _stop_process_group(process, initial_signal=signal.SIGINT)
            return 128 - status if status < 0 else status
        except BaseException:
            _stop_process_group(process)
            raise
        finally:
            if supervisor_path is not None:
                supervisor_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--request", required=True, type=Path)
    try:
        arguments = parser.parse_args(argv)
        if not arguments.request.is_absolute():
            raise InputError("request path must be absolute")
        return run(arguments.action, arguments.request)
    except (
        InputError,
        SourceError,
        VerificationError,
        RuntimeFailure,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Talos lifecycle failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
