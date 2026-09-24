#!/usr/bin/env python3
"""Fixed adapter for the desired-source owner's observational verifier."""

from __future__ import annotations

import json
import argparse
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any


RESPONSE_KEYS = {
    "schemaVersion",
    "requestId",
    "mode",
    "node",
    "sourceRevision",
    "kubeContext",
    "talosContext",
    "checks",
}
CHECK_KEYS = {"source", "cilium", "foundation"}


class VerificationError(RuntimeError):
    """The fixed observational verifier did not return bound acceptance."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError("verifier returned duplicate JSON keys")
        result[key] = value
    return result


def make_request(
    source: dict,
    lifecycle_request: dict,
    mode: str,
    *,
    expected_record: str | None = None,
    timeout_seconds: int = 1800,
) -> dict:
    if mode not in {"prepare", "baseline", "recovery"}:
        raise VerificationError("unsupported verification mode")
    node = lifecycle_request["talos_node"]
    containment: dict[str, str] | None = None
    if mode == "recovery":
        if expected_record is None:
            raise VerificationError("recovery verification requires the exact record")
        containment = {"node": node, "record": expected_record}
    return {
        "schemaVersion": 1,
        "requestId": str(uuid.uuid4()),
        "mode": mode,
        "node": node,
        "sourceRevision": source["revision"],
        "apiServer": source["api_server"],
        "nodes": source["nodes"],
        "talosEndpoints": source["talos_endpoints"],
        "credentials": {
            "kubeconfig": lifecycle_request["talos_kubeconfig"],
            "kubeContext": lifecycle_request["talos_kube_context"],
            "talosconfig": lifecycle_request["talos_talosconfig"],
            "talosContext": lifecycle_request["talos_talos_context"],
        },
        "expectedContainment": containment,
        "timeoutSeconds": timeout_seconds,
    }


def validate_response(request: dict, response: dict, returncode: int) -> None:
    if returncode != 0:
        raise VerificationError("observational verifier failed")
    if not isinstance(response, dict) or set(response) != RESPONSE_KEYS:
        raise VerificationError("verifier response fields are invalid")
    if response.get("schemaVersion") != 1 or type(response.get("schemaVersion")) is not int:
        raise VerificationError("verifier response schema is invalid")
    credentials = request.get("credentials")
    if not isinstance(credentials, dict):
        raise VerificationError("verification request credentials are invalid")
    bindings = {
        "requestId": request.get("requestId"),
        "mode": request.get("mode"),
        "node": request.get("node"),
        "sourceRevision": request.get("sourceRevision"),
        "kubeContext": credentials.get("kubeContext"),
        "talosContext": credentials.get("talosContext"),
    }
    if any(response.get(key) != value for key, value in bindings.items()):
        raise VerificationError("verifier response does not match its request")
    checks = response.get("checks")
    if not isinstance(checks, dict) or set(checks) != CHECK_KEYS:
        raise VerificationError("verifier check results are invalid")
    expected = (
        {"source": "passed", "cilium": "not-run", "foundation": "not-run"}
        if request.get("mode") == "prepare"
        else {"source": "passed", "cilium": "passed", "foundation": "passed"}
    )
    if checks != expected:
        raise VerificationError("verifier did not complete every required check")


def invoke_verifier(source: dict, request: dict, *, deadline_seconds: int) -> dict:
    verification_dir = Path(source["verification_dir"])
    mise = Path(str(source.get("mise", "")))
    bash = Path(str(source.get("bash", "")))
    if not verification_dir.is_absolute() or not 1 <= deadline_seconds <= 3600:
        raise VerificationError("verification invocation is invalid")
    if not mise.is_absolute() or not mise.is_file() or not os.access(mise, os.X_OK):
        raise VerificationError("fixed Mise executable is unavailable")
    if not bash.is_absolute() or not bash.is_file() or not os.access(bash, os.X_OK):
        raise VerificationError("fixed Bash executable is unavailable")
    with tempfile.TemporaryDirectory(prefix="talos-verification-") as temporary:
        root = Path(temporary)
        request_path = root / "request.json"
        request_path.write_text(json.dumps(request, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(request_path, 0o600)
        environment = {
            "HOME": str(root),
            "PATH": os.pathsep.join(dict.fromkeys((str(bash.parent), str(mise.parent), "/usr/bin", "/bin"))),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "MISE_AUTO_INSTALL": "0",
        }
        if source.get("mise_data_dir"):
            environment["MISE_DATA_DIR"] = str(source["mise_data_dir"])
        recovery_cache = Path(str(source.get("recovery_helm_cache", "")))
        if not recovery_cache.is_absolute() or not recovery_cache.is_dir() or recovery_cache.is_symlink():
            raise VerificationError("prepared recovery chart cache is unavailable")
        environment["RECOVERY_HELM_CACHE"] = str(recovery_cache)
        try:
            result = subprocess.run(
                [str(mise), "exec", "--", "just", "kube", "recovery-verify", str(request_path)],
                cwd=verification_dir,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=deadline_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise VerificationError("observational verifier was unavailable or timed out") from error
    try:
        response = json.loads(result.stdout, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise VerificationError("verifier returned malformed structured output") from error
    validate_response(request, response, result.returncode)
    return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("prepare", "baseline", "recovery"))
    parser.add_argument("--record")
    arguments = parser.parse_args(argv)
    try:
        prepared = json.loads(arguments.prepared.read_text(encoding="utf-8"))
        verifier_request = make_request(
            prepared,
            prepared,
            arguments.mode,
            expected_record=arguments.record,
        )
        invoke_verifier(
            prepared,
            verifier_request,
            deadline_seconds=verifier_request["timeoutSeconds"],
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, VerificationError) as error:
        print(f"Platform verification failed: {error}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
