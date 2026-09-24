"""Minimal stdlib support for the attended abrupt-loss controller."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

Runner = Callable[[list[str]], str]


class ScenarioFailure(RuntimeError):
    """A sanitized scenario failure safe to write to evidence."""


class InterruptedRun(ScenarioFailure):
    """The scenario was interrupted by an operator signal."""


def run_command(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=900)
    except subprocess.TimeoutExpired as error:
        raise ScenarioFailure(f"{Path(argv[0]).name} timed out") from error
    if result.returncode != 0:
        raise ScenarioFailure(f"{Path(argv[0]).name} exited with status {result.returncode}")
    return result.stdout


def tty_prompt(message: str, timeout_seconds: int = 900) -> str:
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
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
