"""Minimal stdlib support for the attended abrupt-loss controller."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

Runner = Callable[[list[str]], str]
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 1860
RECOVERY_RETURN_WAIT_SECONDS = 360 * 5
WORKLOAD_REPLACEMENT_WAIT_SECONDS = 60 * 5
VERIFIER_DEADLINE_SECONDS = 1800
RECOVERY_MARGIN_SECONDS = 600
RECOVERY_BRIDGE_TIMEOUT_SECONDS = (
    RECOVERY_RETURN_WAIT_SECONDS
    + WORKLOAD_REPLACEMENT_WAIT_SECONDS
    + VERIFIER_DEADLINE_SECONDS
    + RECOVERY_MARGIN_SECONDS
)
TERMINATION_GRACE_SECONDS = 5


class ScenarioFailure(RuntimeError):
    """A sanitized scenario failure safe to write to evidence."""


class InterruptedRun(ScenarioFailure):
    """The scenario was interrupted by an operator signal."""


def _supervisor_binding() -> tuple[Path, str, dict[str, Any]]:
    path = Path(os.environ.get("TALOS_LIFECYCLE_SUPERVISOR_PATH", ""))
    token = os.environ.get("TALOS_LIFECYCLE_SUPERVISOR_TOKEN", "")
    if (not path.is_absolute() or path.is_symlink() or not path.parent.is_dir()
            or path.parent.is_symlink() or len(token) != 64):
        raise ScenarioFailure("trusted lifecycle supervisor binding is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScenarioFailure("trusted lifecycle supervisor binding is unavailable") from error
    if (not isinstance(value, dict) or value.get("ownerToken") != token
            or not isinstance(value.get("lifecyclePgid"), int)
            or set(value) not in ({"lifecyclePgid", "ownerToken"},
                                  {"lifecyclePgid", "ownerToken", "bridgePgid"})):
        raise ScenarioFailure("trusted lifecycle supervisor binding is invalid")
    return path, token, value


def _register_bridge(group: int) -> None:
    path, _token, value = _supervisor_binding()
    value["bridgePgid"] = group
    atomic_write_json(path, value)


def _unregister_bridge(group: int) -> None:
    try:
        path, _token, value = _supervisor_binding()
    except ScenarioFailure:
        return
    if value.get("bridgePgid") != group:
        return
    value.pop("bridgePgid")
    atomic_write_json(path, value)


def _stop_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.communicate(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.communicate()
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _start_blocked_bridge(argv: list[str]) -> tuple[subprocess.Popen[str], int]:
    read_fd, release_fd = os.pipe()
    launcher = (
        "import os,sys; "
        "descriptor=int(sys.argv[1]); "
        "released=os.read(descriptor,1); "
        "os.close(descriptor); "
        "released == b'1' or sys.exit(125); "
        "os.execv(sys.argv[2],sys.argv[2:])"
    )
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", launcher, str(read_fd), *argv],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(read_fd,),
        )
    except BaseException:
        os.close(release_fd)
        raise
    finally:
        os.close(read_fd)
    return process, release_fd


def _command_timeout(argv: list[str]) -> float:
    if len(argv) >= 2 and Path(argv[0]).name == "abrupt-loss-bridge.sh" and argv[1] == "recover":
        return RECOVERY_BRIDGE_TIMEOUT_SECONDS
    return DEFAULT_BRIDGE_TIMEOUT_SECONDS


def run_command(argv: list[str], timeout_seconds: float | None = None) -> str:
    process, release_fd = _start_blocked_bridge(argv)
    try:
        _register_bridge(process.pid)
        os.write(release_fd, b"1")
        os.close(release_fd)
        release_fd = -1
        try:
            stdout, _stderr = process.communicate(
                timeout=_command_timeout(argv) if timeout_seconds is None else timeout_seconds
            )
        except subprocess.TimeoutExpired as error:
            raise ScenarioFailure(f"{Path(argv[0]).name} timed out") from error
        if process.returncode != 0:
            raise ScenarioFailure(f"{Path(argv[0]).name} exited with status {process.returncode}")
        return stdout
    finally:
        if release_fd >= 0:
            os.close(release_fd)
        _stop_group(process)
        _unregister_bridge(process.pid)


def tty_prompt(message: str, timeout_seconds: int = 900) -> str:
    terminal = os.environ.get("TALOS_LIFECYCLE_TTY_PATH", "/dev/tty")
    try:
        descriptor = os.open(terminal, os.O_RDWR | os.O_NOCTTY)
    except OSError as error:
        raise ScenarioFailure("a controlling terminal is required") from error
    try:
        os.write(descriptor, message.encode("utf-8"))
        ready, _, _ = select.select([descriptor], [], [], timeout_seconds)
        if not ready:
            raise ScenarioFailure("physical action prompt timed out")
        value = bytearray()
        while True:
            chunk = os.read(descriptor, 1)
            if not chunk:
                raise ScenarioFailure("controlling terminal closed during prompt")
            if chunk in (b"\n", b"\r"):
                return value.decode("utf-8", errors="replace")
            value.extend(chunk)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_recovery(run_dir: Path, status: str, reason: str) -> None:
    atomic_write_json(run_dir / "recovery.json", {"status": status, "reason": reason})


def install_interrupt_handlers() -> None:
    def interrupted(signum: int, _frame: object) -> None:
        raise InterruptedRun(f"interrupted by signal {signum}")

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
