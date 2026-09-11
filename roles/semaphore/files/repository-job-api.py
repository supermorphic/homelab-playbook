#!/usr/bin/env python3
"""Reconcile the declared Semaphore project, repository, and task template."""

from __future__ import annotations

import argparse
from http import cookiejar
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib import error, request


class ApiError(RuntimeError):
    pass


class SemaphoreClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookies = cookiejar.CookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.cookies))

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None):
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        operation = request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(operation, timeout=15) as response:
                content = response.read()
        except (error.HTTPError, error.URLError, TimeoutError) as failure:
            status = getattr(failure, "code", "unavailable")
            raise ApiError(f"Semaphore API {method} {path} failed ({status})") from None
        if not content:
            return None
        try:
            return json.loads(content)
        except (UnicodeError, json.JSONDecodeError):
            raise ApiError(f"Semaphore API {method} {path} returned invalid JSON") from None

    def login(self, credentials: dict[str, str]) -> None:
        if set(credentials) != {"auth", "password"} or not all(
            isinstance(value, str) and value for value in credentials.values()
        ):
            raise ApiError("protected login input is incomplete")
        self.call("POST", "/api/auth/login", credentials)
        if not list(self.cookies):
            raise ApiError("Semaphore login returned no session cookie")


def _one(records: object, name: str, kind: str) -> dict[str, Any] | None:
    if not isinstance(records, list):
        raise ApiError(f"Semaphore {kind} response is not a list")
    matches = [record for record in records if isinstance(record, dict) and record.get("name") == name]
    if len(matches) > 1:
        raise ApiError(f"more than one Semaphore {kind} is named {name}")
    return matches[0] if matches else None


def _changed(record: dict[str, Any], desired: dict[str, Any]) -> bool:
    def actual(key: str, value: Any) -> Any:
        if key not in record and (value is False or value == "" or value == [] or value == {}):
            return value
        return record.get(key)

    return any(actual(key, value) != value for key, value in desired.items())


def _reconcile_record(
    client: SemaphoreClient,
    records_path: str,
    desired: dict[str, Any],
    kind: str,
    update_path: str | None = None,
) -> tuple[dict[str, Any], bool]:
    existing = _one(client.call("GET", records_path), desired["name"], kind)
    if existing is None:
        created = client.call("POST", records_path, desired)
        if not isinstance(created, dict) or not isinstance(created.get("id"), int):
            raise ApiError(f"Semaphore did not return the created {kind}")
        return created, True
    if not isinstance(existing.get("id"), int):
        raise ApiError(f"Semaphore {kind} has no numeric ID")
    if not _changed(existing, desired):
        return existing, False
    updated = dict(existing)
    updated.update(desired)
    client.call("PUT", f"{update_path or records_path}/{existing['id']}", updated)
    return updated, True


def validate_declaration(declaration: object) -> dict[str, dict[str, Any]]:
    if not isinstance(declaration, dict) or set(declaration) != {"project", "repository", "template"}:
        raise ApiError("controller API declaration has unexpected fields")
    project = declaration["project"]
    repository = declaration["repository"]
    template = declaration["template"]
    if not all(isinstance(value, dict) for value in (project, repository, template)):
        raise ApiError("controller API declaration objects are invalid")
    if set(project) != {"name", "max_parallel_tasks"} or project["max_parallel_tasks"] != 1:
        raise ApiError("project declaration must select one concurrent task")
    if set(repository) != {"name", "git_url", "git_branch"}:
        raise ApiError("repository declaration has unexpected fields")
    if set(template) != {"name", "playbook", "app"}:
        raise ApiError("template declaration has unexpected fields")
    if template.get("playbook") != "os-verify" or template.get("app") != "repository_verify":
        raise ApiError("template must use the fixed repository verification app and marker")
    string_fields = (
        project["name"], repository["name"], repository["git_url"],
        repository["git_branch"], template["name"], template["playbook"], template["app"],
    )
    if not all(isinstance(value, str) and value for value in string_fields):
        raise ApiError("controller API declaration contains an empty or invalid value")
    if re.fullmatch(r"https://\S+", repository["git_url"]) is None:
        raise ApiError("repository URL must use trusted HTTPS transport")
    repository_ref = repository["git_branch"]
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", repository_ref) is None
        or ".." in repository_ref.split("/")
    ):
        raise ApiError("repository ref is unsafe")
    return declaration  # type: ignore[return-value]


def reconcile(client: SemaphoreClient, raw_declaration: object) -> dict[str, Any]:
    declaration = validate_declaration(raw_declaration)
    project_desired = {
        "name": declaration["project"]["name"],
        "max_parallel_tasks": 1,
    }
    project, project_changed = _reconcile_record(
        client, "/api/projects", project_desired, "project", "/api/project"
    )
    project_id = project["id"]

    keys = client.call("GET", f"/api/project/{project_id}/keys")
    none_key = _one(keys, "None", "access key")
    if none_key is None or none_key.get("type") != "none" or not isinstance(none_key.get("id"), int):
        raise ApiError("declared project has no native None repository key")

    repository_desired = {
        "name": declaration["repository"]["name"],
        "project_id": project_id,
        "git_url": declaration["repository"]["git_url"],
        "git_branch": declaration["repository"]["git_branch"],
        "ssh_key_id": none_key["id"],
    }
    repository_path = f"/api/project/{project_id}/repositories"
    repository, repository_changed = _reconcile_record(
        client, repository_path, repository_desired, "repository"
    )

    template_desired = {
        "name": declaration["template"]["name"],
        "project_id": project_id,
        "repository_id": repository["id"],
        "playbook": "os-verify",
        "app": "repository_verify",
        "arguments": None,
        "git_branch": None,
        "survey_vars": [],
        "type": "",
        "autorun": False,
        "allow_override_args_in_task": False,
        "allow_override_branch_in_task": False,
        "allow_parallel_tasks": False,
        "environment_ids": [],
        "task_params": {},
    }
    template_path = f"/api/project/{project_id}/templates"
    template, template_changed = _reconcile_record(
        client, template_path, template_desired, "template"
    )
    return {
        "changed": project_changed or repository_changed or template_changed,
        "project_id": project_id,
        "repository_id": repository["id"],
        "template_id": template["id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--declaration", required=True, type=Path)
    args = parser.parse_args()
    try:
        document = json.loads(args.declaration.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or "api" not in document:
            raise ApiError("controller declaration has no api object")
        protected = json.load(sys.stdin)
        client = SemaphoreClient(args.base_url)
        client.login(protected)
        print(json.dumps(reconcile(client, document["api"]), sort_keys=True))
        return 0
    except (ApiError, OSError, UnicodeError, json.JSONDecodeError) as failure:
        print(f"Semaphore controller reconciliation failed: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
