#!/usr/bin/env python3
"""Narrow local Ansible gateway for controller-side Talos lifecycle actions."""

from __future__ import annotations

import json
import os
import shutil
import signal
import time
import secrets
import subprocess
import sys
import tempfile
import stat
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "roles/talos_lifecycle/files"
sys.path.insert(0, str(FILES))
from inputs import InputError, _UniqueYamlLoader, validate_request  # noqa: E402

ACTIONS = {"maintenance-check", "maintenance-enter", "maintenance-exit", "reboot", "abrupt-loss-test"}
BASE_TOOLS = ("bash", "python3", "kubectl", "talosctl", "yq", "mise")
CANCELLATION_WAIT_SECONDS = 45
FALLBACK_GRACE_SECONDS = 5


class GatewayError(RuntimeError):
    """The requested command is outside the Talos operator interface."""


def _arguments(arguments: list[str]) -> tuple[Path, bool]:
    check = False
    request: Path | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--check":
            if check:
                raise GatewayError("check mode was specified more than once")
            check = True
            index += 1
            continue
        if argument in {"-e", "--extra-vars"}:
            if request is not None or index + 1 >= len(arguments):
                raise GatewayError("exactly one Talos input file is required")
            reference = arguments[index + 1]
            if not reference.startswith("@") or not Path(reference[1:]).is_absolute():
                raise GatewayError("Talos inputs must use @ABSOLUTE_FILE")
            request = Path(reference[1:])
            index += 2
            continue
        raise GatewayError(f"unsupported Talos gateway argument: {argument}")
    if request is None:
        raise GatewayError("one Talos input file is required")
    return request, check


def _load(path: Path, action: str) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise GatewayError("Talos input must be an absolute regular file")
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueYamlLoader)
        return validate_request(value, action)
    except (OSError, UnicodeError, yaml.YAMLError, InputError) as error:
        raise GatewayError(str(error)) from error


def _terminal_path() -> str:
    try:
        if not os.isatty(sys.stdin.fileno()):
            raise OSError("standard input is not the controlling terminal")
        path = os.ttyname(sys.stdin.fileno())
        metadata = os.fstat(sys.stdin.fileno())
    except OSError as error:
        raise GatewayError("abrupt-loss-test requires a controlling terminal") from error
    if not path.startswith("/dev/") or not stat.S_ISCHR(metadata.st_mode):
        raise GatewayError("controlling terminal path is invalid")
    return path


def _resolved_tools(action: str) -> dict[str, str]:
    names = (*BASE_TOOLS, *(('dig', 'curl') if action == 'abrupt-loss-test' else ()))
    resolved: dict[str, str] = {}
    for name in names:
        candidate = shutil.which(name)
        if candidate is None:
            raise GatewayError(f"required pinned tool is unavailable: {name}")
        path = Path(candidate).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise GatewayError(f"required pinned tool is not executable: {name}")
        resolved[name] = str(path)
    return resolved


def _automation_revision() -> str:
    git = Path("/usr/bin/git")
    if not git.is_file() or not os.access(git, os.X_OK):
        raise GatewayError("trusted Git executable is unavailable")
    result = subprocess.run(
        [str(git), "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
        capture_output=True, check=False, timeout=10,
        env={"PATH": "/usr/bin:/bin", "HOME": "/", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise GatewayError("automation revision is unavailable")
    return revision


def _group_exists(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _stop_lifecycle_fallback(supervisor_path: Path, owner_token: str) -> None:
    group: int | None = None
    bridge_group: int | None = None
    try:
        value = json.loads(supervisor_path.read_text(encoding="utf-8"))
        if (isinstance(value, dict)
                and set(value) in ({"lifecyclePgid", "ownerToken"},
                                   {"lifecyclePgid", "ownerToken", "bridgePgid"})
                and isinstance(value["lifecyclePgid"], int)
                and value["ownerToken"] == owner_token):
            group = value["lifecyclePgid"]
            if isinstance(value.get("bridgePgid"), int):
                bridge_group = value["bridgePgid"]
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    if group is None or group <= 1:
        return
    if bridge_group is not None and bridge_group > 1:
        try:
            os.killpg(bridge_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + FALLBACK_GRACE_SECONDS
        while _group_exists(bridge_group) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _group_exists(bridge_group):
            try:
                os.killpg(bridge_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        if os.getpgid(group) != group:
            return
    except ProcessLookupError:
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + FALLBACK_GRACE_SECONDS
    while _group_exists(group) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _group_exists(group):
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + FALLBACK_GRACE_SECONDS
    while _group_exists(group) and time.monotonic() < deadline:
        time.sleep(0.05)


def _run_ansible(argv: list[str], environment: dict[str, str], cancel_path: Path,
                 supervisor_path: Path, owner_token: str) -> int:
    process = subprocess.Popen(argv, cwd=ROOT, env=environment, start_new_session=True)
    received: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        if received:
            return
        received.append(signum)
        try:
            cancel_path.touch(mode=0o600, exist_ok=False)
        except (FileExistsError, OSError):
            pass

    previous = {signum: signal.signal(signum, forward) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        cancellation_deadline: float | None = None
        while True:
            try:
                status = process.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if received and cancellation_deadline is None:
                    cancellation_deadline = time.monotonic() + CANCELLATION_WAIT_SECONDS
                if cancellation_deadline is not None and time.monotonic() >= cancellation_deadline:
                    _stop_lifecycle_fallback(supervisor_path, owner_token)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    status = process.wait()
                    break
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if received:
        return 128 + received[0]
    return 128 - status if status < 0 else status


def run(action: str, inventory: str, arguments: list[str]) -> int:
    if action not in ACTIONS:
        raise GatewayError("unsupported Talos lifecycle action")
    if inventory != "production":
        raise GatewayError("Talos lifecycle supports production only")
    request_path, check = _arguments(arguments)
    if check and action != "maintenance-check":
        raise GatewayError("check mode is supported only for maintenance-check")
    request = _load(request_path, action)
    automation_revision = _automation_revision()
    terminal_path = _terminal_path() if action == "abrupt-loss-test" else ""
    tools = _resolved_tools(action)
    original_home = Path.home()
    mise_data = Path(os.environ.get("MISE_DATA_DIR", original_home / ".local/share/mise"))
    if not mise_data.is_absolute():
        raise GatewayError("Mise data directory must be absolute")
    with tempfile.TemporaryDirectory(prefix="talos-gateway-") as temporary:
        private = Path(temporary)
        os.chmod(private, 0o700)
        variables_path = private / "variables.json"
        cancel_path = private / "cancel"
        supervisor_path = private / "supervisor.json"
        owner_token = secrets.token_hex(32)
        variables_path.write_text(json.dumps({"talos_lifecycle_action": action, "talos_lifecycle_request": request,
                                               "talos_lifecycle_automation_revision": automation_revision,
                                               "talos_lifecycle_tty_path": terminal_path,
                                               "talos_lifecycle_tool_paths": tools,
                                               "talos_lifecycle_mise_data_dir": str(mise_data),
                                               "talos_lifecycle_cancel_path": str(cancel_path),
                                               "talos_lifecycle_supervisor_path": str(supervisor_path),
                                               "talos_lifecycle_supervisor_token": owner_token}, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(variables_path, 0o600)
        config = private / "ansible.cfg"
        config.write_text(
            "[defaults]\n"
            f"roles_path = {ROOT / 'roles'}\n"
            f"collections_path = {ROOT / '.ansible/collections'}\n"
            f"interpreter_python = {sys.executable}\n"
            "retry_files_enabled = false\n"
            "inject_facts_as_vars = false\n"
            "vars_plugins_enabled = host_group_vars\n",
            encoding="utf-8",
        )
        executable = Path(sys.executable).parent / "ansible-playbook"
        argv = [str(executable), "--inventory", "localhost,", "--connection", "local",
                str(ROOT / "playbooks/talos" / f"{action}.yml")]
        if check:
            argv.append("--check")
        argv.extend(["--extra-vars", f"@{variables_path}"])
        tool_path = os.pathsep.join(dict.fromkeys([*(str(Path(value).parent) for value in tools.values()), "/usr/bin", "/bin"]))
        environment = {"ANSIBLE_CONFIG": str(config), "HOME": str(private), "TMPDIR": str(private),
                       "PATH": tool_path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        return _run_ansible(argv, environment, cancel_path, supervisor_path, owner_token)


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) < 2:
        print("Usage: talos_gateway.py ACTION production [-e|--extra-vars @ABSOLUTE_FILE] [--check]", file=sys.stderr)
        return 2
    try:
        return run(values[0], values[1], values[2:])
    except GatewayError as error:
        print(f"Talos gateway rejected request: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
