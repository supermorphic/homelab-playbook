from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO


REPOSITORY = "supermorphic/homelab-playbook"
RULESET_NAME = "Protect main"
TARGET_REF = "refs/heads/main"
WORKFLOW = "ci.yml"
CHECK_NAME = "merge-gate"
CONFIRMATION = f"apply:github-protection:{REPOSITORY}"

REPOSITORY_ENDPOINT = f"repos/{REPOSITORY}"
RULESETS_ENDPOINT = f"{REPOSITORY_ENDPOINT}/rulesets"
EFFECTIVE_RULES_ENDPOINT = f"{REPOSITORY_ENDPOINT}/rules/branches/main"
WORKFLOW_RUNS_ENDPOINT = (
    f"{REPOSITORY_ENDPOINT}/actions/workflows/{WORKFLOW}/runs"
    "?status=success&per_page=20"
)
EXPECTED_REPOSITORY_SETTINGS = {
    "allow_squash_merge": True,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
}
RULE_ORDER = {
    "deletion": 0,
    "required_linear_history": 1,
    "pull_request": 2,
    "required_status_checks": 3,
    "non_fast_forward": 4,
}


class ProtectionError(RuntimeError):
    """An actionable repository-protection failure."""


class GitHubAPIError(ProtectionError):
    def __init__(self, *, status: int | None, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ProtectionState:
    repository: dict[str, Any]
    integration_id: int
    managed_rulesets: tuple[dict[str, Any], ...]
    effective_rules: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Action:
    summary: str
    method: str
    endpoint: str
    payload: dict[str, Any]


class GhAPI:
    """Small JSON boundary around the Mise-pinned GitHub CLI."""

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        paginate: bool = False,
    ) -> Any:
        method = method.upper()
        if paginate and (method != "GET" or payload is not None):
            raise ProtectionError("pagination is supported only for GET requests")
        arguments = [
            "gh",
            "api",
            "--method",
            method,
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        if paginate:
            arguments.extend(["--paginate", "--slurp"])
        arguments.append(endpoint)
        input_text = None
        if payload is not None:
            arguments.extend(["--input", "-"])
            input_text = json.dumps(payload, separators=(",", ":"))

        try:
            result = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                input=input_text,
            )
        except OSError as error:
            raise GitHubAPIError(
                status=None,
                message=f"could not execute gh: {error}",
            ) from error

        if result.returncode != 0:
            message = result.stderr.strip() or "gh api request failed"
            status_match = re.search(r"\bHTTP ([0-9]{3})\b", message)
            status = int(status_match.group(1)) if status_match else None
            raise GitHubAPIError(status=status, message=message)

        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubAPIError(
                status=None,
                message="gh api returned malformed JSON",
            ) from error


def expected_rules(integration_id: int) -> list[dict[str, Any]]:
    if not isinstance(integration_id, int) or isinstance(integration_id, bool):
        raise ValueError("integration ID must be an integer")
    return [
        {"type": "deletion"},
        {"type": "required_linear_history"},
        {
            "type": "pull_request",
            "parameters": {
                "allowed_merge_methods": ["squash"],
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_approving_review_count": 0,
                "required_review_thread_resolution": False,
                "required_reviewers": [],
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "do_not_enforce_on_create": False,
                "required_status_checks": [
                    {
                        "context": CHECK_NAME,
                        "integration_id": integration_id,
                    }
                ],
                "strict_required_status_checks_policy": True,
            },
        },
        {"type": "non_fast_forward"},
    ]


def expected_ruleset(integration_id: int) -> dict[str, Any]:
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "exclude": [],
                "include": [TARGET_REF],
            }
        },
        "rules": expected_rules(integration_id),
    }


def expected_effective_rules(
    integration_id: int,
    ruleset_id: int,
) -> list[dict[str, Any]]:
    _require_int(ruleset_id, "ruleset ID")
    return [
        {
            **rule,
            "ruleset_id": ruleset_id,
            "ruleset_source_type": "Repository",
            "ruleset_source": REPOSITORY,
        }
        for rule in expected_rules(integration_id)
    ]


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProtectionError(f"malformed GitHub response at {path}: expected object")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProtectionError(f"malformed GitHub response at {path}: expected list")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtectionError(f"malformed GitHub response at {path}: expected string")
    return value


def _require_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ProtectionError(
            f"malformed GitHub response at {path}: expected positive integer"
        )
    return value


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProtectionError(f"malformed GitHub response at {path}: expected boolean")
    return value


def _workflow_matches(path: Any) -> bool:
    return isinstance(path, str) and (path == WORKFLOW or path.endswith(f"/{WORKFLOW}"))


def _paginated_array(api: Any, endpoint: str, path: str) -> list[Any]:
    pages = _require_list(
        api.request("GET", endpoint, paginate=True),
        f"{path} pages",
    )
    if not pages:
        raise ProtectionError(f"malformed GitHub response at {path} pages: empty")
    items: list[Any] = []
    for page_index, raw_page in enumerate(pages):
        page = _require_list(raw_page, f"{path} pages[{page_index}]")
        items.extend(page)
    return items


def _paginated_object_items(
    api: Any,
    endpoint: str,
    field: str,
    path: str,
) -> list[Any]:
    pages = _require_list(
        api.request("GET", endpoint, paginate=True),
        f"{path} pages",
    )
    if not pages:
        raise ProtectionError(f"malformed GitHub response at {path} pages: empty")
    items: list[Any] = []
    for page_index, raw_page in enumerate(pages):
        page = _require_dict(raw_page, f"{path} pages[{page_index}]")
        page_items = _require_list(
            page.get(field),
            f"{path} pages[{page_index}].{field}",
        )
        items.extend(page_items)
    return items


def discover_integration_id(api: Any) -> int:
    runs = _paginated_object_items(
        api,
        WORKFLOW_RUNS_ENDPOINT,
        "workflow_runs",
        "workflow runs",
    )
    for index, raw_run in enumerate(runs):
        run = _require_dict(raw_run, f"workflow_runs[{index}]")
        if (
            run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or not _workflow_matches(run.get("path"))
        ):
            continue
        head_sha = _require_string(run.get("head_sha"), f"workflow_runs[{index}].head_sha")
        check_suite_id = _require_int(
            run.get("check_suite_id"),
            f"workflow_runs[{index}].check_suite_id",
        )
        check_suite_url = _require_string(
            run.get("check_suite_url"),
            f"workflow_runs[{index}].check_suite_url",
        )
        expected_check_suite_url = (
            f"https://api.github.com/repos/{REPOSITORY}/check-suites/{check_suite_id}"
        )
        if check_suite_url != expected_check_suite_url:
            raise ProtectionError(
                f"malformed GitHub response at workflow_runs[{index}].check_suite_url: "
                f"expected {expected_check_suite_url!r} for check_suite_id "
                f"{check_suite_id}, got {check_suite_url!r}"
            )
        endpoint = (
            f"{REPOSITORY_ENDPOINT}/check-suites/{check_suite_id}/check-runs"
            f"?check_name={CHECK_NAME}&per_page=100"
        )
        checks = _paginated_object_items(
            api,
            endpoint,
            "check_runs",
            f"check runs for workflow run {head_sha} suite {check_suite_id}",
        )
        for check_index, raw_check in enumerate(checks):
            check = _require_dict(raw_check, f"check_runs[{check_index}]")
            check_suite = _require_dict(
                check.get("check_suite"),
                f"check_runs[{check_index}].check_suite",
            )
            reported_check_suite_id = _require_int(
                check_suite.get("id"),
                f"check_runs[{check_index}].check_suite.id",
            )
            if reported_check_suite_id != check_suite_id:
                raise ProtectionError(
                    f"malformed GitHub response at check_runs[{check_index}]."
                    f"check_suite.id: expected selected check suite {check_suite_id}, "
                    f"got {reported_check_suite_id}"
                )
            app = check.get("app")
            if (
                check.get("name") != CHECK_NAME
                or check.get("status") != "completed"
                or check.get("conclusion") != "success"
                or not isinstance(app, dict)
                or app.get("slug") != "github-actions"
            ):
                continue
            return _require_int(app.get("id"), f"check_runs[{check_index}].app.id")

    raise ProtectionError(
        f"no recent successful {CHECK_NAME} check from github-actions was found for {WORKFLOW}"
    )


def _normalize_repository(value: Any) -> dict[str, Any]:
    repository = _require_dict(value, "repository")
    visibility = _require_string(repository.get("visibility"), "repository.visibility")
    normalized: dict[str, Any] = {"visibility": visibility}
    for field in EXPECTED_REPOSITORY_SETTINGS:
        normalized[field] = _require_bool(repository.get(field), f"repository.{field}")
    return normalized


def _normalize_json(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return copy.deepcopy(value)
    if isinstance(value, list):
        return [_normalize_json(item, f"{path}[]") for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {
            key: _normalize_json(item, f"{path}.{key}")
            for key, item in sorted(value.items())
        }
    raise ProtectionError(f"malformed GitHub response at {path}: invalid JSON value")


def _normalize_rule(value: Any, path: str) -> dict[str, Any]:
    rule = _require_dict(value, path)
    rule_type = _require_string(rule.get("type"), f"{path}.type")
    normalized = {
        key: _normalize_json(item, f"{path}.{key}")
        for key, item in sorted(rule.items())
    }

    if rule_type == "pull_request":
        parameters = _require_dict(rule.get("parameters"), f"{path}.parameters")
        for field in (
            "dismiss_stale_reviews_on_push",
            "require_code_owner_review",
            "require_last_push_approval",
            "required_review_thread_resolution",
        ):
            _require_bool(parameters.get(field), f"{path}.parameters.{field}")
        approvals = parameters.get("required_approving_review_count")
        if not isinstance(approvals, int) or isinstance(approvals, bool) or approvals < 0:
            raise ProtectionError(
                f"malformed GitHub response at {path}.parameters."
                "required_approving_review_count: expected nonnegative integer"
            )
        methods = _require_list(
            parameters.get("allowed_merge_methods"),
            f"{path}.parameters.allowed_merge_methods",
        )
        if any(not isinstance(method, str) for method in methods):
            raise ProtectionError(
                f"malformed GitHub response at {path}.parameters."
                "allowed_merge_methods: expected strings"
            )
        _require_list(
            parameters.get("required_reviewers"),
            f"{path}.parameters.required_reviewers",
        )
    elif rule_type == "required_status_checks":
        parameters = _require_dict(rule.get("parameters"), f"{path}.parameters")
        _require_bool(
            parameters.get("do_not_enforce_on_create"),
            f"{path}.parameters.do_not_enforce_on_create",
        )
        _require_bool(
            parameters.get("strict_required_status_checks_policy"),
            f"{path}.parameters.strict_required_status_checks_policy",
        )
        checks = _require_list(
            parameters.get("required_status_checks"),
            f"{path}.parameters.required_status_checks",
        )
        for index, raw_check in enumerate(checks):
            check = _require_dict(
                raw_check,
                f"{path}.parameters.required_status_checks[{index}]",
            )
            _require_string(
                check.get("context"),
                f"{path}.parameters.required_status_checks[{index}].context",
            )
            _require_int(
                check.get("integration_id"),
                f"{path}.parameters.required_status_checks[{index}].integration_id",
            )
    return normalized


def _rule_sort_key(rule: dict[str, Any]) -> tuple[int, str, str]:
    rule_type = str(rule.get("type", ""))
    return (
        RULE_ORDER.get(rule_type, len(RULE_ORDER)),
        rule_type,
        json.dumps(rule, sort_keys=True, separators=(",", ":")),
    )


def normalize_ruleset(value: Any, path: str = "ruleset") -> dict[str, Any]:
    ruleset = _require_dict(value, path)
    normalized: dict[str, Any] = {
        "name": _require_string(ruleset.get("name"), f"{path}.name"),
        "target": _require_string(ruleset.get("target"), f"{path}.target"),
        "enforcement": _require_string(
            ruleset.get("enforcement"), f"{path}.enforcement"
        ),
    }
    bypass_actors = _require_list(ruleset.get("bypass_actors"), f"{path}.bypass_actors")
    normalized["bypass_actors"] = sorted(
        (_normalize_json(actor, f"{path}.bypass_actors[]") for actor in bypass_actors),
        key=lambda actor: json.dumps(actor, sort_keys=True, separators=(",", ":")),
    )
    conditions = _require_dict(ruleset.get("conditions"), f"{path}.conditions")
    ref_name = _require_dict(
        conditions.get("ref_name"),
        f"{path}.conditions.ref_name",
    )
    for field in ("include", "exclude"):
        values = _require_list(
            ref_name.get(field),
            f"{path}.conditions.ref_name.{field}",
        )
        if any(not isinstance(item, str) for item in values):
            raise ProtectionError(
                f"malformed GitHub response at {path}.conditions.ref_name.{field}: "
                "expected strings"
            )
    normalized["conditions"] = _normalize_json(conditions, f"{path}.conditions")
    rules = _require_list(ruleset.get("rules"), f"{path}.rules")
    normalized["rules"] = sorted(
        (_normalize_rule(rule, f"{path}.rules[{index}]") for index, rule in enumerate(rules)),
        key=_rule_sort_key,
    )
    return normalized


def _managed_summaries(value: Any) -> list[dict[str, Any]]:
    summaries = _require_list(value, "rulesets")
    managed: list[dict[str, Any]] = []
    for index, raw_summary in enumerate(summaries):
        summary = _require_dict(raw_summary, f"rulesets[{index}]")
        name = _require_string(summary.get("name"), f"rulesets[{index}].name")
        if name != RULESET_NAME:
            continue
        try:
            ruleset_id = _require_int(summary.get("id"), f"rulesets[{index}].id")
            source_type = _require_string(
                summary.get("source_type"),
                f"rulesets[{index}].source_type",
            )
            source = _require_string(
                summary.get("source"),
                f"rulesets[{index}].source",
            )
        except ProtectionError as error:
            raise ProtectionError(
                f"ambiguous {RULESET_NAME} ruleset summary at rulesets[{index}]: "
                f"ownership metadata is incomplete or malformed ({error})"
            ) from error
        if name == RULESET_NAME and source_type == "Repository" and source == REPOSITORY:
            managed.append({"id": ruleset_id})
    return managed


def normalize_effective_rules(value: Any) -> tuple[dict[str, Any], ...]:
    rules = _require_list(value, "effective rules")
    normalized: list[dict[str, Any]] = []
    for index, raw_rule in enumerate(rules):
        rule = _require_dict(raw_rule, f"effective_rules[{index}]")
        path = f"effective_rules[{index}]"
        semantic_rule = {"type": rule.get("type")}
        if "parameters" in rule:
            semantic_rule["parameters"] = rule["parameters"]
        normalized_rule = _normalize_rule(semantic_rule, path)
        normalized_rule.update(
            {
                "ruleset_id": _require_int(rule.get("ruleset_id"), f"{path}.ruleset_id"),
                "ruleset_source_type": _require_string(
                    rule.get("ruleset_source_type"),
                    f"{path}.ruleset_source_type",
                ),
                "ruleset_source": _require_string(
                    rule.get("ruleset_source"),
                    f"{path}.ruleset_source",
                ),
            }
        )
        normalized.append(normalized_rule)
    return tuple(sorted(normalized, key=_rule_sort_key))


def collect_state(api: Any) -> ProtectionState:
    repository = _normalize_repository(api.request("GET", REPOSITORY_ENDPOINT))
    integration_id = discover_integration_id(api)
    summaries = _managed_summaries(
        _paginated_array(api, RULESETS_ENDPOINT, "rulesets")
    )
    managed_rulesets: list[dict[str, Any]] = []
    for index, summary in enumerate(summaries):
        ruleset_id = summary["id"]
        detail = _require_dict(
            api.request("GET", f"{RULESETS_ENDPOINT}/{ruleset_id}"),
            f"managed ruleset {ruleset_id}",
        )
        normalized = normalize_ruleset(detail, f"managed ruleset {ruleset_id}")
        normalized["id"] = ruleset_id
        managed_rulesets.append(normalized)
    effective_rules = normalize_effective_rules(
        _paginated_array(api, EFFECTIVE_RULES_ENDPOINT, "effective rules")
    )
    return ProtectionState(
        repository=repository,
        integration_id=integration_id,
        managed_rulesets=tuple(managed_rulesets),
        effective_rules=effective_rules,
    )


def _display(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _diff_values(path: str, expected: Any, actual: Any) -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        messages: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            child_path = f"{path}.{key}" if path else key
            if key not in actual:
                messages.append(f"{child_path}: missing")
            elif key not in expected:
                messages.append(f"{child_path}: unexpected {_display(actual[key])}")
            else:
                messages.extend(_diff_values(child_path, expected[key], actual[key]))
        return messages
    if expected != actual:
        return [f"{path}: expected {_display(expected)}, found {_display(actual)}"]
    return []


def _rules_drift(
    expected: Sequence[dict[str, Any]],
    actual: Sequence[dict[str, Any]],
    path_prefix: str = "rules",
) -> list[str]:
    expected_by_type = {rule["type"]: rule for rule in expected}
    actual_by_type: dict[str, list[dict[str, Any]]] = {}
    for rule in actual:
        actual_by_type.setdefault(str(rule["type"]), []).append(rule)

    messages: list[str] = []
    for rule_type in sorted(set(expected_by_type) | set(actual_by_type)):
        path = f"{path_prefix}.{rule_type}"
        matches = actual_by_type.get(rule_type, [])
        if rule_type not in expected_by_type:
            messages.append(f"{path}: unexpected")
        elif not matches:
            messages.append(f"{path}: missing")
        elif len(matches) > 1:
            messages.append(f"{path}: expected one, found {len(matches)}")
        else:
            messages.extend(_diff_values(path, expected_by_type[rule_type], matches[0]))
    return messages


def drift(state: ProtectionState) -> list[str]:
    messages = _diff_values(
        "repository",
        {"visibility": "public", **EXPECTED_REPOSITORY_SETTINGS},
        state.repository,
    )
    count = len(state.managed_rulesets)
    if count == 0:
        messages.append(f"ruleset {RULESET_NAME!r}: missing")
    elif count > 1:
        messages.append(f"ruleset {RULESET_NAME!r}: expected one, found {count}")
    else:
        actual = dict(state.managed_rulesets[0])
        actual.pop("id", None)
        expected = expected_ruleset(state.integration_id)
        actual_rules = actual.pop("rules")
        expected_rules_value = expected.pop("rules")
        messages.extend(_diff_values("ruleset", expected, actual))
        messages.extend(_rules_drift(expected_rules_value, actual_rules))
        managed_id = state.managed_rulesets[0]["id"]
        messages.extend(
            _rules_drift(
                expected_effective_rules(state.integration_id, managed_id),
                state.effective_rules,
                "effective_rules",
            )
        )
    return messages


def blockers(state: ProtectionState) -> list[str]:
    messages: list[str] = []
    if state.repository.get("visibility") != "public":
        messages.append(
            "repository must be public for Ruleset enforcement on the current plan"
        )

    managed_count = len(state.managed_rulesets)
    if managed_count > 1:
        messages.append(
            f"multiple repository-owned {RULESET_NAME!r} rulesets make ownership ambiguous"
        )
    if managed_count == 1 and state.managed_rulesets[0].get("target") != "branch":
        messages.append(
            f"managed {RULESET_NAME!r} has a non-branch target and cannot be safely updated"
        )

    managed_id = (
        state.managed_rulesets[0].get("id") if managed_count == 1 else None
    )
    unmanaged_sources: set[str] = set()
    for rule in state.effective_rules:
        ruleset_id = rule.get("ruleset_id")
        if ruleset_id != managed_id:
            unmanaged_sources.add(
                str(ruleset_id) if isinstance(ruleset_id, int) else "unattributed"
            )
    if unmanaged_sources:
        messages.append(
            "unmanaged effective protection applies to main from ruleset(s): "
            + ", ".join(sorted(unmanaged_sources))
        )
    return messages


def _ruleset_update_payload(integration_id: int) -> dict[str, Any]:
    payload = expected_ruleset(integration_id)
    payload.pop("target")
    return payload


def plan_actions(state: ProtectionState) -> list[Action]:
    actions: list[Action] = []
    if any(
        state.repository[field] != expected
        for field, expected in EXPECTED_REPOSITORY_SETTINGS.items()
    ):
        actions.append(
            Action(
                summary="update repository merge methods to squash only",
                method="PATCH",
                endpoint=REPOSITORY_ENDPOINT,
                payload=copy.deepcopy(EXPECTED_REPOSITORY_SETTINGS),
            )
        )

    if not state.managed_rulesets:
        actions.append(
            Action(
                summary=f"create {RULESET_NAME!r} for {TARGET_REF}",
                method="POST",
                endpoint=RULESETS_ENDPOINT,
                payload=expected_ruleset(state.integration_id),
            )
        )
    elif len(state.managed_rulesets) == 1:
        managed = state.managed_rulesets[0]
        if managed.get("target") == "branch":
            ruleset_messages = [
                message for message in drift(state) if not message.startswith("repository.")
            ]
            if ruleset_messages:
                actions.append(
                    Action(
                        summary=f"update {RULESET_NAME!r} for {TARGET_REF}",
                        method="PUT",
                        endpoint=f"{RULESETS_ENDPOINT}/{managed['id']}",
                        payload=_ruleset_update_payload(state.integration_id),
                    )
                )
    return actions


def _raise_blockers(messages: list[str]) -> None:
    if messages:
        raise ProtectionError("apply blocked: " + "; ".join(messages))


def apply(api: Any, confirmation: str | None) -> list[Action]:
    if confirmation != CONFIRMATION:
        raise ProtectionError(
            "apply requires exact confirmation "
            f"GITHUB_PROTECTION_CONFIRM={CONFIRMATION}"
        )

    current = collect_state(api)
    _raise_blockers(blockers(current))
    actions = plan_actions(current)
    write_error: ProtectionError | None = None
    try:
        for action in actions:
            api.request(action.method, action.endpoint, action.payload)
    except ProtectionError as error:
        write_error = error

    if actions:
        try:
            read_back = collect_state(api)
        except ProtectionError as read_back_error:
            if write_error is not None:
                raise ProtectionError(
                    f"write failed: {write_error}; post-write read-back failed: "
                    f"{read_back_error}; no rollback attempted"
                ) from write_error
            raise ProtectionError(
                f"post-write read-back failed: {read_back_error}"
            ) from read_back_error

        mismatches = [*blockers(read_back), *drift(read_back)]
        if write_error is not None:
            observed = (
                "; ".join(mismatches)
                if mismatches
                else "state matches the desired state"
            )
            raise ProtectionError(
                f"write failed: {write_error}; post-write state: {observed}; "
                "no rollback attempted"
            ) from write_error
        if mismatches:
            raise ProtectionError("read-back mismatch: " + "; ".join(mismatches))
    return actions


def _api_error_message(error: GitHubAPIError) -> str:
    if error.status == 403:
        return (
            "GitHub API returned 403; verify public repository visibility/current-plan "
            f"Ruleset support and authenticated administration access: {error.message}"
        )
    if error.status == 404:
        return (
            "GitHub API returned 404; verify the repository, public visibility/current-plan "
            f"Ruleset support, and authenticated administration access: {error.message}"
        )
    status = f" {error.status}" if error.status is not None else ""
    return f"GitHub API error{status}: {error.message}"


def _write_messages(prefix: str, messages: Sequence[str], stream: TextIO) -> None:
    for message in messages:
        print(f"{prefix}: {message}", file=stream)


def run(
    mode: str,
    api: Any,
    *,
    environ: Mapping[str, str],
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        if mode == "check":
            state = collect_state(api)
            blocked = blockers(state)
            differences = drift(state)
            if blocked or differences:
                _write_messages("blocker", blocked, stderr)
                _write_messages("drift", differences, stderr)
                return 1
            print(f"GitHub main protection is exact for {REPOSITORY}.", file=stdout)
            return 0

        if mode == "plan":
            state = collect_state(api)
            blocked = blockers(state)
            actions = plan_actions(state)
            if actions:
                for index, action in enumerate(actions, start=1):
                    print(f"{index}. {action.summary}", file=stdout)
            else:
                print("No changes planned.", file=stdout)
            print(
                f"Apply guard: GITHUB_PROTECTION_CONFIRM={CONFIRMATION} "
                "mise run github-protection:apply",
                file=stdout,
            )
            print("This plan and confirmation are not authorization to apply.", file=stdout)
            if blocked:
                _write_messages("blocker", blocked, stderr)
                return 1
            return 0

        if mode == "apply":
            actions = apply(api, environ.get("GITHUB_PROTECTION_CONFIRM"))
            if actions:
                for action in actions:
                    print(f"Applied: {action.summary}", file=stdout)
                print("Complete GitHub API read-back matches desired state.", file=stdout)
            else:
                print("GitHub main protection already matches desired state.", file=stdout)
            return 0

        raise ProtectionError(f"unsupported mode: {mode}")
    except GitHubAPIError as error:
        print(f"error: {_api_error_message(error)}", file=stderr)
        return 1
    except ProtectionError as error:
        print(f"error: {error}", file=stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check, plan, or apply guarded GitHub main protection"
    )
    parser.add_argument("mode", choices=("check", "plan", "apply"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return run(
        arguments.mode,
        GhAPI(),
        environ=os.environ,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
