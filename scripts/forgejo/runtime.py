"""Bounded, private, run-owned resources for offline Forgejo experiments."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]


def defaults() -> dict:
    return yaml.safe_load((ROOT / "roles/forgejo/defaults/main.yml").read_text())


class Run:
    label = "io.homelab.forgejo.test"

    def __init__(self, *, podman: str = "podman", run_id: str | None = None):
        self.podman = podman
        self.run_id = run_id or uuid.uuid4().hex[:16]
        if re.fullmatch(r"[A-Za-z0-9]{8,32}", self.run_id) is None:
            raise ValueError("invalid experiment run identifier")
        self.resources: list[tuple[str, str]] = []

    def command(self, argv: list[str], *, input_text: str | None = None,
                check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(argv, input=input_text, capture_output=True,
                                    text=True, check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"{Path(argv[0]).name} execution failed") from error
        if check and result.returncode:
            # Application commands can return passwords/tokens, including on error.
            raise RuntimeError(f"{Path(argv[0]).name} exited {result.returncode}")
        return result

    def name(self, suffix: str) -> str:
        if re.fullmatch(r"[a-z][a-z0-9-]*", suffix) is None:
            raise ValueError("invalid resource suffix")
        return f"forgejo-test-{self.run_id}-{suffix}"

    def create(self, kind: str, suffix: str, arguments: list[str]) -> str:
        if kind not in ("container", "volume", "network"):
            raise ValueError("unsupported resource kind")
        name = self.name(suffix)
        exists = self.command([self.podman, kind, "exists", name], check=False)
        if exists.returncode != 1:
            raise RuntimeError("resource already exists or cannot be inspected")
        # Register before execution: interruption after creation still owns cleanup.
        self.resources.append((kind, name))
        if kind == "container":
            argv = [self.podman, "run", "--detach", "--name", name]
        else:
            argv = [self.podman, kind, "create"]
        positional = [] if kind == "container" else [name]
        self.command([*argv, "--label", f"{self.label}={self.run_id}",
                      *arguments, *positional])
        return name

    def private_file(self, directory: Path, name: str, value: str) -> Path:
        if Path(name).name != name:
            raise ValueError("private input requires a single filename")
        path = directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(value)
        return path

    def cleanup(self) -> list[str]:
        errors = []
        for kind, name in reversed(self.resources):
            try:
                exists = self.command([self.podman, kind, "exists", name], check=False)
                if exists.returncode == 1:
                    continue
                if exists.returncode:
                    raise RuntimeError("existence check failed")
                inspected = self.command([self.podman, kind, "inspect", name])
                document = json.loads(inspected.stdout)[0]
                labels = (document.get("Config", document).get("Labels")
                          or document.get("labels") or {})
                if labels.get(self.label) != self.run_id:
                    raise RuntimeError("ownership label differs")
                args = [self.podman, kind, "rm"]
                if kind == "container":
                    args.append("--force")
                self.command([*args, name], timeout=30)
            except (RuntimeError, ValueError, KeyError, IndexError) as error:
                errors.append(f"{kind} {name}: {error}")
        return errors

    def wait(self, argv: list[str], timeout: int = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.command(argv, check=False, timeout=10).returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError("readiness deadline exceeded")

    def evidence(self, operation_error: str | None, cleanup_errors: list[str]) -> None:
        directory = ROOT / ".tmp/forgejo"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{self.run_id}.json").write_text(json.dumps({
            "run_id": self.run_id, "operation_error": operation_error,
            "cleanup_errors": cleanup_errors,
        }, indent=2) + "\n")
