#!/usr/bin/env python3
"""Run one validated Talos lifecycle action in one foreground process tree."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from inputs import InputError, load_request
from source import SourceError, prepare_source
from verification import VerificationError, invoke_verifier, make_request


TRUSTED_ORIGIN = "https://github.com/supermorphic/homelab-talos.git"
FILES = Path(__file__).resolve().parent


class RuntimeFailure(RuntimeError):
    pass


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeFailure(f"required tool is unavailable: {name}")
    return str(Path(path).resolve())


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
    request = load_request(request_path, action)
    with tempfile.TemporaryDirectory(prefix="talos-lifecycle-") as temporary:
        private = Path(temporary)
        os.chmod(private, 0o700)
        source = prepare_source(
            Path(request["talos_source_dir"]),
            request["talos_source_revision"],
            private / "source",
            TRUSTED_ORIGIN,
        )
        node = request["talos_node"]
        nodes = source["nodes"]
        if node not in nodes:
            raise RuntimeFailure("selected node is absent from approved desired input")
        _confirmation(action, request, nodes[node])
        tool_names = ["bash", "python3", "kubectl", "talosctl", "yq", "mise"]
        if action == "abrupt-loss-test":
            tool_names.extend(["dig", "curl"])
        tools = {name: _tool(name) for name in tool_names}
        if _bash_major(tools["bash"]) < 5:
            raise RuntimeFailure("Talos lifecycle requires Bash 5 or newer")
        source["mise"] = tools["mise"]
        source["mise_data_dir"] = str(Path.home() / ".local/share/mise")
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
        tool_path = os.pathsep.join(dict.fromkeys(str(Path(value).parent) for value in tools.values()))
        environment = {
            "HOME": str(private),
            "PATH": tool_path,
            "TMPDIR": str(private),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        process = subprocess.Popen(
            [tools["bash"], str(FILES / "node/lifecycle.sh"), str(prepared_path)],
            start_new_session=False,
            env=environment,
        )
        try:
            status = process.wait()
            return 128 - status if status < 0 else status
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            status = process.wait()
            return 128 - status if status < 0 else status


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
