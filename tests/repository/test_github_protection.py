from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.repository import github_protection


INTEGRATION_ID = 15368
RULESET_ID = 42
OTHER_RULESET_ID = 99
CHECK_SUITE_ID = 9001

REPOSITORY = {
    "visibility": "public",
    "allow_squash_merge": True,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
}
EXPECTED_RULES = [
    {"type": "deletion"},
    {"type": "required_linear_history"},
    {
        "type": "pull_request",
        "parameters": {
            "allowed_merge_methods": ["squash"],
            "dismiss_stale_reviews_on_push": False,
            "require_code_owner_review": False,
            "require_extra_approval_for_unattributed_changes": True,
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
                    "context": "merge-gate",
                    "integration_id": INTEGRATION_ID,
                }
            ],
            "strict_required_status_checks_policy": True,
        },
    },
    {"type": "non_fast_forward"},
]
RULESET = {
    "id": RULESET_ID,
    "name": "Protect main",
    "target": "branch",
    "source_type": "Repository",
    "source": "supermorphic/homelab-playbook",
    "enforcement": "active",
    "bypass_actors": [],
    "conditions": {
        "ref_name": {
            "exclude": [],
            "include": ["refs/heads/main"],
        }
    },
    "rules": EXPECTED_RULES,
}
RULESET_SUMMARY = {
    "id": RULESET_ID,
    "name": "Protect main",
    "target": "branch",
    "source_type": "Repository",
    "source": "supermorphic/homelab-playbook",
    "enforcement": "active",
}
EFFECTIVE_RULES = [
    {
        **copy.deepcopy(rule),
        "ruleset_id": RULESET_ID,
        "ruleset_source_type": "Repository",
        "ruleset_source": "supermorphic/homelab-playbook",
    }
    for rule in EXPECTED_RULES
]
WORKFLOW_RUNS = {
    "workflow_runs": [
        {
            "id": 7001,
            "head_sha": "a" * 40,
            "status": "completed",
            "conclusion": "success",
            "path": ".github/workflows/ci.yml",
            "check_suite_id": CHECK_SUITE_ID,
            "check_suite_url": (
                "https://api.github.com/repos/supermorphic/homelab-playbook/"
                f"check-suites/{CHECK_SUITE_ID}"
            ),
        }
    ]
}
CHECK_RUNS = {
    "check_runs": [
        {
            "id": 8001,
            "name": "merge-gate",
            "status": "completed",
            "conclusion": "success",
            "app": {"id": INTEGRATION_ID, "slug": "github-actions"},
            "check_suite": {"id": CHECK_SUITE_ID},
        }
    ]
}

REPOSITORY_ENDPOINT = "repos/supermorphic/homelab-playbook"
RULESETS_ENDPOINT = f"{REPOSITORY_ENDPOINT}/rulesets"
RULESET_ENDPOINT = f"{RULESETS_ENDPOINT}/{RULESET_ID}"
EFFECTIVE_RULES_ENDPOINT = f"{REPOSITORY_ENDPOINT}/rules/branches/main"
WORKFLOW_RUNS_ENDPOINT = (
    f"{REPOSITORY_ENDPOINT}/actions/workflows/ci.yml/runs?status=success&per_page=20"
)
CHECK_RUNS_ENDPOINT = (
    f"{REPOSITORY_ENDPOINT}/check-suites/{CHECK_SUITE_ID}/check-runs"
    "?check_name=merge-gate"
    "&per_page=100"
)


class FakeAPI:
    def __init__(
        self,
        responses: dict[tuple[str, str], list[Any]],
    ) -> None:
        self.responses = {
            key: [
                item if isinstance(item, Exception) or callable(item) else copy.deepcopy(item)
                for item in values
            ]
            for key, values in responses.items()
        }
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.pagination_calls: list[tuple[str, str, bool]] = []

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        paginate: bool = False,
    ) -> Any:
        self.calls.append((method, endpoint, copy.deepcopy(payload)))
        self.pagination_calls.append((method, endpoint, paginate))
        key = (method, endpoint)
        if key not in self.responses or not self.responses[key]:
            raise AssertionError(f"unexpected API request: {method} {endpoint}")
        response = self.responses[key].pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(method, endpoint, payload)
        return copy.deepcopy(response)


class StatefulAPI:
    def __init__(
        self,
        *,
        repository: dict[str, Any] | None = None,
        ruleset: dict[str, Any] | None = None,
        effective_rules: list[dict[str, Any]] | None = None,
        persist_mutations: bool = True,
        create_error: github_protection.ProtectionError | None = None,
    ) -> None:
        self.repository = copy.deepcopy(repository or REPOSITORY)
        self.ruleset = copy.deepcopy(ruleset)
        self.effective_rules = copy.deepcopy(effective_rules or [])
        self.persist_mutations = persist_mutations
        self.create_error = create_error
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.pagination_calls: list[tuple[str, str, bool]] = []

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        paginate: bool = False,
    ) -> Any:
        self.calls.append((method, endpoint, copy.deepcopy(payload)))
        self.pagination_calls.append((method, endpoint, paginate))
        if method == "GET":
            if endpoint == REPOSITORY_ENDPOINT:
                return copy.deepcopy(self.repository)
            if endpoint == WORKFLOW_RUNS_ENDPOINT:
                return [copy.deepcopy(WORKFLOW_RUNS)]
            if endpoint == CHECK_RUNS_ENDPOINT:
                return [copy.deepcopy(CHECK_RUNS)]
            if endpoint == RULESETS_ENDPOINT:
                return [
                    [] if self.ruleset is None else [copy.deepcopy(RULESET_SUMMARY)]
                ]
            if endpoint == RULESET_ENDPOINT and self.ruleset is not None:
                return copy.deepcopy(self.ruleset)
            if endpoint == EFFECTIVE_RULES_ENDPOINT:
                return [copy.deepcopy(self.effective_rules)]
        if method == "PATCH" and endpoint == REPOSITORY_ENDPOINT:
            if self.persist_mutations:
                self.repository.update(copy.deepcopy(payload or {}))
            return copy.deepcopy(self.repository)
        if method == "POST" and endpoint == RULESETS_ENDPOINT:
            if self.create_error is not None:
                raise self.create_error
            if self.persist_mutations:
                self.ruleset = {"id": RULESET_ID, **copy.deepcopy(payload or {})}
                self.ruleset.update(
                    {
                        "source_type": "Repository",
                        "source": "supermorphic/homelab-playbook",
                    }
                )
                self.effective_rules = copy.deepcopy(EFFECTIVE_RULES)
            return copy.deepcopy(self.ruleset or {})
        if method == "PUT" and endpoint == RULESET_ENDPOINT:
            if self.persist_mutations and self.ruleset is not None:
                ruleset_id = self.ruleset["id"]
                target = self.ruleset["target"]
                self.ruleset.update(copy.deepcopy(payload or {}))
                self.ruleset["id"] = ruleset_id
                self.ruleset["target"] = target
                self.effective_rules = copy.deepcopy(EFFECTIVE_RULES)
            return copy.deepcopy(self.ruleset or {})
        raise AssertionError(f"unexpected API request: {method} {endpoint}")


def exact_responses(repetitions: int = 1) -> dict[tuple[str, str], list[Any]]:
    return {
        ("GET", REPOSITORY_ENDPOINT): [copy.deepcopy(REPOSITORY) for _ in range(repetitions)],
        ("GET", WORKFLOW_RUNS_ENDPOINT): [
            [copy.deepcopy(WORKFLOW_RUNS)] for _ in range(repetitions)
        ],
        ("GET", CHECK_RUNS_ENDPOINT): [
            [copy.deepcopy(CHECK_RUNS)] for _ in range(repetitions)
        ],
        ("GET", RULESETS_ENDPOINT): [
            [[copy.deepcopy(RULESET_SUMMARY)]] for _ in range(repetitions)
        ],
        ("GET", RULESET_ENDPOINT): [copy.deepcopy(RULESET) for _ in range(repetitions)],
        ("GET", EFFECTIVE_RULES_ENDPOINT): [
            [copy.deepcopy(EFFECTIVE_RULES)] for _ in range(repetitions)
        ],
    }


def collect_with_ruleset(ruleset: dict[str, Any]) -> Any:
    responses = exact_responses()
    responses[("GET", RULESET_ENDPOINT)] = [copy.deepcopy(ruleset)]
    return github_protection.collect_state(FakeAPI(responses))


class GhAPIBoundaryTests(unittest.TestCase):
    def test_paginated_get_uses_paginate_and_slurp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            fake_gh = temporary_path / "gh"
            arguments_path = temporary_path / "arguments.json"
            fake_gh.write_text(
                """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

pathlib.Path(os.environ["GH_ARGUMENTS_PATH"]).write_text(
    json.dumps(sys.argv[1:]),
    encoding="utf-8",
)
sys.stdout.write('[[{"id":1}],[{"id":2}]]')
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            environment = {
                "PATH": f"{temporary_path}{os.pathsep}{os.environ['PATH']}",
                "GH_ARGUMENTS_PATH": str(arguments_path),
            }

            with patch.dict(os.environ, environment):
                response = github_protection.GhAPI().request(
                    "GET",
                    RULESETS_ENDPOINT,
                    paginate=True,
                )

            arguments = json.loads(arguments_path.read_text(encoding="utf-8"))

        self.assertEqual([[{"id": 1}], [{"id": 2}]], response)
        self.assertEqual(1, arguments.count("--paginate"))
        self.assertEqual(1, arguments.count("--slurp"))
        self.assertIn(RULESETS_ENDPOINT, arguments)


class DesiredStateTests(unittest.TestCase):
    def test_desired_rules_are_the_literal_contract(self) -> None:
        self.assertEqual(EXPECTED_RULES, github_protection.expected_rules(INTEGRATION_ID))

    def test_desired_ruleset_targets_only_main_with_no_bypass(self) -> None:
        expected = {
            "name": "Protect main",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {
                "ref_name": {
                    "exclude": [],
                    "include": ["refs/heads/main"],
                }
            },
            "rules": EXPECTED_RULES,
        }

        self.assertEqual(expected, github_protection.expected_ruleset(INTEGRATION_ID))

    def test_ruleset_normalization_is_public_and_ignores_api_metadata_and_order(
        self,
    ) -> None:
        remote = copy.deepcopy(RULESET)
        remote["rules"] = list(reversed(remote["rules"]))
        remote["_links"] = {"self": {"href": "https://api.example.invalid/ruleset"}}

        self.assertEqual(
            github_protection.expected_ruleset(INTEGRATION_ID),
            github_protection.normalize_ruleset(remote),
        )

    def test_effective_rule_normalization_compares_only_enforcement_semantics(
        self,
    ) -> None:
        remote = list(reversed(copy.deepcopy(EFFECTIVE_RULES)))
        for rule in remote:
            rule["_links"] = {"self": {"href": "https://api.example.invalid/rule"}}

        self.assertEqual(
            tuple(EFFECTIVE_RULES),
            github_protection.normalize_effective_rules(remote),
        )

    def test_exact_state_has_no_drift_blockers_or_actions(self) -> None:
        state = github_protection.collect_state(FakeAPI(exact_responses()))

        self.assertEqual([], github_protection.drift(state))
        self.assertEqual([], github_protection.blockers(state))
        self.assertEqual([], github_protection.plan_actions(state))

    def test_missing_ruleset_plans_creation(self) -> None:
        responses = exact_responses()
        responses[("GET", RULESETS_ENDPOINT)] = [[[]]]
        del responses[("GET", RULESET_ENDPOINT)]
        responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [[[]]]

        state = github_protection.collect_state(FakeAPI(responses))
        actions = github_protection.plan_actions(state)

        self.assertTrue(any("missing" in item for item in github_protection.drift(state)))
        self.assertEqual(1, len(actions))
        self.assertEqual("POST", actions[0].method)
        self.assertEqual(RULESETS_ENDPOINT, actions[0].endpoint)
        self.assertEqual(
            github_protection.expected_ruleset(INTEGRATION_ID), actions[0].payload
        )

    def test_repository_merge_method_drift_plans_only_merge_settings(self) -> None:
        for field, value in (
            ("allow_squash_merge", False),
            ("allow_merge_commit", True),
            ("allow_rebase_merge", True),
        ):
            with self.subTest(field=field):
                responses = exact_responses()
                repository = copy.deepcopy(REPOSITORY)
                repository[field] = value
                responses[("GET", REPOSITORY_ENDPOINT)] = [repository]

                state = github_protection.collect_state(FakeAPI(responses))
                actions = github_protection.plan_actions(state)

                self.assertTrue(any(field in item for item in github_protection.drift(state)))
                self.assertEqual(1, len(actions))
                self.assertEqual("PATCH", actions[0].method)
                self.assertEqual(REPOSITORY_ENDPOINT, actions[0].endpoint)
                self.assertEqual(
                    {
                        "allow_squash_merge": True,
                        "allow_merge_commit": False,
                        "allow_rebase_merge": False,
                    },
                    actions[0].payload,
                )


class DriftTests(unittest.TestCase):
    def assert_ruleset_drift(
        self,
        mutate: Callable[[dict[str, Any]], None],
        expected_fragment: str,
    ) -> None:
        ruleset = copy.deepcopy(RULESET)
        mutate(ruleset)

        messages = github_protection.drift(collect_with_ruleset(ruleset))

        self.assertTrue(
            any(expected_fragment in message for message in messages),
            messages,
        )

    def test_target_and_bypass_drift_are_detected(self) -> None:
        cases = {
            "target kind": (
                lambda ruleset: ruleset.__setitem__("target", "tag"),
                "target",
            ),
            "target ref": (
                lambda ruleset: ruleset["conditions"]["ref_name"].__setitem__(
                    "include", ["refs/heads/release"]
                ),
                "conditions.ref_name.include",
            ),
            "bypass": (
                lambda ruleset: ruleset.__setitem__(
                    "bypass_actors",
                    [
                        {
                            "actor_id": 1,
                            "actor_type": "OrganizationAdmin",
                            "bypass_mode": "always",
                        }
                    ],
                ),
                "bypass_actors",
            ),
        }

        for name, (mutate, fragment) in cases.items():
            with self.subTest(name=name):
                self.assert_ruleset_drift(mutate, fragment)

    def test_every_pull_request_parameter_is_compared(self) -> None:
        replacements = {
            "allowed_merge_methods": ["merge"],
            "dismiss_stale_reviews_on_push": True,
            "require_code_owner_review": True,
            "require_extra_approval_for_unattributed_changes": False,
            "require_last_push_approval": True,
            "required_approving_review_count": 1,
            "required_review_thread_resolution": True,
            "required_reviewers": [
                {
                    "repository_id": 1,
                    "reviewer_id": 2,
                    "reviewer_type": "Team",
                }
            ],
        }

        for parameter, replacement in replacements.items():
            with self.subTest(parameter=parameter):
                def mutate(ruleset: dict[str, Any]) -> None:
                    pull_request = next(
                        rule for rule in ruleset["rules"] if rule["type"] == "pull_request"
                    )
                    pull_request["parameters"][parameter] = replacement

                self.assert_ruleset_drift(mutate, parameter)

    def test_required_check_strictness_context_and_source_are_compared(self) -> None:
        replacements = {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": True,
            "context": "other-gate",
            "integration_id": 999,
        }

        for parameter, replacement in replacements.items():
            with self.subTest(parameter=parameter):
                def mutate(ruleset: dict[str, Any]) -> None:
                    status_rule = next(
                        rule
                        for rule in ruleset["rules"]
                        if rule["type"] == "required_status_checks"
                    )
                    if parameter in {
                        "strict_required_status_checks_policy",
                        "do_not_enforce_on_create",
                    }:
                        status_rule["parameters"][parameter] = replacement
                    else:
                        status_rule["parameters"]["required_status_checks"][0][
                            parameter
                        ] = replacement

                self.assert_ruleset_drift(mutate, parameter)

    def test_linear_history_deletion_and_non_fast_forward_are_required(self) -> None:
        for rule_type in (
            "required_linear_history",
            "deletion",
            "non_fast_forward",
        ):
            with self.subTest(rule_type=rule_type):
                def mutate(ruleset: dict[str, Any]) -> None:
                    ruleset["rules"] = [
                        rule for rule in ruleset["rules"] if rule["type"] != rule_type
                    ]

                self.assert_ruleset_drift(mutate, rule_type)

    def test_malformed_remote_rules_fail_closed(self) -> None:
        ruleset = copy.deepcopy(RULESET)
        pull_request = next(
            rule for rule in ruleset["rules"] if rule["type"] == "pull_request"
        )
        del pull_request["parameters"]

        with self.assertRaisesRegex(
            github_protection.ProtectionError,
            "malformed",
        ):
            collect_with_ruleset(ruleset)

    def test_missing_effective_rules_are_drift_even_when_ruleset_is_exact(self) -> None:
        responses = exact_responses()
        responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [[[]]]

        state = github_protection.collect_state(FakeAPI(responses))
        messages = github_protection.drift(state)

        for rule_type in (
            "deletion",
            "required_linear_history",
            "pull_request",
            "required_status_checks",
            "non_fast_forward",
        ):
            with self.subTest(rule_type=rule_type):
                self.assertTrue(
                    any(
                        f"effective_rules.{rule_type}" in message
                        and "missing" in message
                        for message in messages
                    ),
                    messages,
                )

    def test_one_missing_effective_rule_is_drift(self) -> None:
        responses = exact_responses()
        responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [
            [
                [
                    copy.deepcopy(rule)
                    for rule in EFFECTIVE_RULES
                    if rule["type"] != "non_fast_forward"
                ]
            ]
        ]

        messages = github_protection.drift(
            github_protection.collect_state(FakeAPI(responses))
        )

        self.assertTrue(
            any(
                "effective_rules.non_fast_forward" in message
                and "missing" in message
                for message in messages
            ),
            messages,
        )

    def test_effective_rule_enforcement_semantics_are_compared(self) -> None:
        replacements = {
            "strictness": (
                "required_status_checks",
                lambda rule: rule["parameters"].__setitem__(
                    "strict_required_status_checks_policy", False
                ),
            ),
            "approvals": (
                "pull_request",
                lambda rule: rule["parameters"].__setitem__(
                    "required_approving_review_count", 1
                ),
            ),
            "source": (
                "required_status_checks",
                lambda rule: rule.__setitem__("ruleset_source", "other/repository"),
            ),
        }

        for case, (rule_type, mutate) in replacements.items():
            with self.subTest(case=case):
                effective_rules = copy.deepcopy(EFFECTIVE_RULES)
                rule = next(
                    item for item in effective_rules if item["type"] == rule_type
                )
                mutate(rule)
                responses = exact_responses()
                responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [[effective_rules]]

                messages = github_protection.drift(
                    github_protection.collect_state(FakeAPI(responses))
                )

                self.assertTrue(
                    any(f"effective_rules.{rule_type}" in item for item in messages),
                    messages,
                )


class OwnershipAndVisibilityTests(unittest.TestCase):
    def test_collection_paginates_every_list_endpoint(self) -> None:
        api = FakeAPI(exact_responses())

        github_protection.collect_state(api)

        self.assertEqual(
            {
                ("GET", WORKFLOW_RUNS_ENDPOINT, True),
                ("GET", CHECK_RUNS_ENDPOINT, True),
                ("GET", RULESETS_ENDPOINT, True),
                ("GET", EFFECTIVE_RULES_ENDPOINT, True),
            },
            {call for call in api.pagination_calls if call[2]},
        )

    def test_duplicate_managed_ruleset_on_later_page_blocks_apply(self) -> None:
        responses = exact_responses()
        second_summary = {**copy.deepcopy(RULESET_SUMMARY), "id": 43}
        second_ruleset = {**copy.deepcopy(RULESET), "id": 43}
        responses[("GET", RULESETS_ENDPOINT)] = [
            [[copy.deepcopy(RULESET_SUMMARY)], [second_summary]]
        ]
        responses[("GET", f"{RULESETS_ENDPOINT}/43")] = [second_ruleset]
        api = FakeAPI(responses)

        with self.assertRaisesRegex(
            github_protection.ProtectionError,
            "multiple.*Protect main",
        ):
            github_protection.apply(api, github_protection.CONFIRMATION)

        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_unmanaged_effective_rule_on_later_page_blocks_apply(self) -> None:
        unmanaged = {
            "type": "deletion",
            "ruleset_id": OTHER_RULESET_ID,
            "ruleset_source_type": "Repository",
            "ruleset_source": "supermorphic/homelab-playbook",
        }
        responses = exact_responses()
        responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [
            [copy.deepcopy(EFFECTIVE_RULES), [unmanaged]]
        ]
        api = FakeAPI(responses)

        with self.assertRaisesRegex(
            github_protection.ProtectionError,
            "unmanaged effective",
        ):
            github_protection.apply(api, github_protection.CONFIRMATION)

        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_incomplete_same_name_ruleset_summary_is_ambiguous(self) -> None:
        mutations = {
            "missing id": lambda summary: summary.pop("id"),
            "malformed id": lambda summary: summary.__setitem__("id", "42"),
            "missing source type": lambda summary: summary.pop("source_type"),
            "malformed source type": lambda summary: summary.__setitem__(
                "source_type", 7
            ),
            "missing source": lambda summary: summary.pop("source"),
            "malformed source": lambda summary: summary.__setitem__("source", 7),
        }

        for case, mutate in mutations.items():
            with self.subTest(case=case):
                summary = copy.deepcopy(RULESET_SUMMARY)
                mutate(summary)
                responses = exact_responses()
                responses[("GET", RULESETS_ENDPOINT)] = [[[summary]]]
                responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [[[]]]
                api = FakeAPI(responses)

                with self.assertRaisesRegex(
                    github_protection.ProtectionError,
                    "ambiguous.*Protect main",
                ):
                    github_protection.apply(api, github_protection.CONFIRMATION)

                self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_duplicate_managed_rulesets_block_apply(self) -> None:
        responses = exact_responses()
        second_summary = {**copy.deepcopy(RULESET_SUMMARY), "id": 43}
        second_ruleset = {**copy.deepcopy(RULESET), "id": 43}
        responses[("GET", RULESETS_ENDPOINT)] = [
            [[copy.deepcopy(RULESET_SUMMARY), second_summary]]
        ]
        responses[("GET", f"{RULESETS_ENDPOINT}/43")] = [second_ruleset]
        api = FakeAPI(responses)

        with self.assertRaisesRegex(
            github_protection.ProtectionError,
            "multiple.*Protect main",
        ):
            github_protection.apply(api, github_protection.CONFIRMATION)

        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_unmanaged_effective_rule_blocks_apply(self) -> None:
        responses = exact_responses()
        responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [
            [
                copy.deepcopy(EFFECTIVE_RULES)
                + [
                    {
                        "type": "deletion",
                        "ruleset_id": OTHER_RULESET_ID,
                        "ruleset_source_type": "Repository",
                        "ruleset_source": "supermorphic/homelab-playbook",
                    }
                ]
            ]
        ]
        api = FakeAPI(responses)

        state = github_protection.collect_state(api)
        self.assertTrue(
            any("unmanaged effective" in item for item in github_protection.blockers(state))
        )

        apply_api = FakeAPI(responses=exact_responses())
        apply_api.responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [
            [
                copy.deepcopy(EFFECTIVE_RULES)
                + [
                    {
                        "type": "deletion",
                        "ruleset_id": OTHER_RULESET_ID,
                        "ruleset_source_type": "Repository",
                        "ruleset_source": "supermorphic/homelab-playbook",
                    }
                ]
            ]
        ]
        with self.assertRaisesRegex(
            github_protection.ProtectionError,
            "unmanaged effective",
        ):
            github_protection.apply(apply_api, github_protection.CONFIRMATION)
        self.assertTrue(all(method == "GET" for method, _, _ in apply_api.calls))

    def test_private_visibility_is_drift_and_blocks_apply(self) -> None:
        responses = exact_responses()
        repository = copy.deepcopy(REPOSITORY)
        repository["visibility"] = "private"
        responses[("GET", REPOSITORY_ENDPOINT)] = [repository]
        api = FakeAPI(responses)

        state = github_protection.collect_state(api)

        self.assertTrue(any("visibility" in item for item in github_protection.drift(state)))
        self.assertTrue(any("public" in item for item in github_protection.blockers(state)))

    def test_plan_403_is_actionable_and_has_no_traceback(self) -> None:
        api = FakeAPI(
            {
                ("GET", REPOSITORY_ENDPOINT): [
                    github_protection.GitHubAPIError(
                        status=403,
                        message="Resource not accessible",
                    )
                ]
            }
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        status = github_protection.run(
            "plan",
            api,
            environ={},
            stdout=stdout,
            stderr=stderr,
        )

        self.assertNotEqual(0, status)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("403", stderr.getvalue())
        self.assertIn("visibility", stderr.getvalue())
        self.assertIn("administration", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))


class IntegrationDiscoveryTests(unittest.TestCase):
    def test_successful_ci_workflow_run_on_second_page_is_discovered(self) -> None:
        other_workflow_run = copy.deepcopy(WORKFLOW_RUNS["workflow_runs"][0])
        other_workflow_run["path"] = ".github/workflows/other.yml"
        api = FakeAPI(
            {
                ("GET", WORKFLOW_RUNS_ENDPOINT): [
                    [
                        {"workflow_runs": [other_workflow_run]},
                        copy.deepcopy(WORKFLOW_RUNS),
                    ]
                ],
                ("GET", CHECK_RUNS_ENDPOINT): [[copy.deepcopy(CHECK_RUNS)]],
            }
        )

        try:
            integration_id = github_protection.discover_integration_id(api)
        except github_protection.ProtectionError:
            integration_id = None

        self.assertEqual(INTEGRATION_ID, integration_id)

    def test_successful_suite_check_on_second_page_is_discovered(self) -> None:
        failed_check = copy.deepcopy(CHECK_RUNS["check_runs"][0])
        failed_check["conclusion"] = "failure"
        api = FakeAPI(
            {
                ("GET", WORKFLOW_RUNS_ENDPOINT): [
                    [copy.deepcopy(WORKFLOW_RUNS)]
                ],
                ("GET", CHECK_RUNS_ENDPOINT): [
                    [
                        {"check_runs": [failed_check]},
                        copy.deepcopy(CHECK_RUNS),
                    ]
                ],
            }
        )

        try:
            integration_id = github_protection.discover_integration_id(api)
        except github_protection.ProtectionError:
            integration_id = None

        self.assertEqual(INTEGRATION_ID, integration_id)

    def test_same_sha_check_from_different_workflow_is_not_accepted(self) -> None:
        head_sha = "a" * 40
        check_suite_id = 9001
        check_suite_endpoint = (
            f"{REPOSITORY_ENDPOINT}/check-suites/{check_suite_id}/check-runs"
            "?check_name=merge-gate&per_page=100"
        )
        commit_endpoint = (
            f"{REPOSITORY_ENDPOINT}/commits/{head_sha}/check-runs"
            "?check_name=merge-gate&per_page=100"
        )
        run = {
            "id": 7001,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": "success",
            "path": ".github/workflows/ci.yml",
            "check_suite_id": check_suite_id,
            "check_suite_url": (
                "https://api.github.com/repos/supermorphic/homelab-playbook/"
                f"check-suites/{check_suite_id}"
            ),
        }
        other_workflow_check = {
            "id": 8001,
            "name": "merge-gate",
            "status": "completed",
            "conclusion": "success",
            "app": {"id": INTEGRATION_ID, "slug": "github-actions"},
            "check_suite": {"id": 9002},
        }
        api = FakeAPI(
            {
                ("GET", WORKFLOW_RUNS_ENDPOINT): [[{"workflow_runs": [run]}]],
                ("GET", commit_endpoint): [[{"check_runs": [other_workflow_check]}]],
                ("GET", check_suite_endpoint): [[{"check_runs": []}]],
            }
        )

        with self.assertRaisesRegex(
            github_protection.ProtectionError,
            "no recent successful merge-gate",
        ):
            github_protection.discover_integration_id(api)

        self.assertIn(("GET", check_suite_endpoint, True), api.pagination_calls)
        self.assertNotIn(("GET", commit_endpoint, True), api.pagination_calls)

    def test_workflow_run_check_suite_relationship_is_validated(self) -> None:
        check_suite_id = 9001
        valid_run = {
            "id": 7001,
            "head_sha": "a" * 40,
            "status": "completed",
            "conclusion": "success",
            "path": ".github/workflows/ci.yml",
            "check_suite_id": check_suite_id,
            "check_suite_url": (
                "https://api.github.com/repos/supermorphic/homelab-playbook/"
                f"check-suites/{check_suite_id}"
            ),
        }
        mutations = {
            "missing id": lambda run: run.pop("check_suite_id"),
            "malformed id": lambda run: run.__setitem__("check_suite_id", "9001"),
            "missing url": lambda run: run.pop("check_suite_url"),
            "mismatched url": lambda run: run.__setitem__(
                "check_suite_url",
                "https://api.github.com/repos/supermorphic/homelab-playbook/"
                "check-suites/9002",
            ),
        }

        for case, mutate in mutations.items():
            with self.subTest(case=case):
                run = copy.deepcopy(valid_run)
                mutate(run)
                api = FakeAPI(
                    {
                        ("GET", WORKFLOW_RUNS_ENDPOINT): [
                            [{"workflow_runs": [run]}]
                        ],
                        ("GET", CHECK_RUNS_ENDPOINT): [
                            [copy.deepcopy(CHECK_RUNS)]
                        ],
                    }
                )

                with self.assertRaisesRegex(
                    github_protection.ProtectionError,
                    "malformed.*check_suite",
                ):
                    github_protection.discover_integration_id(api)

    def test_check_run_must_report_the_selected_suite_id(self) -> None:
        check_suite_id = 9001
        endpoint = (
            f"{REPOSITORY_ENDPOINT}/check-suites/{check_suite_id}/check-runs"
            "?check_name=merge-gate&per_page=100"
        )
        run = {
            "id": 7001,
            "head_sha": "a" * 40,
            "status": "completed",
            "conclusion": "success",
            "path": ".github/workflows/ci.yml",
            "check_suite_id": check_suite_id,
            "check_suite_url": (
                "https://api.github.com/repos/supermorphic/homelab-playbook/"
                f"check-suites/{check_suite_id}"
            ),
        }
        check = copy.deepcopy(CHECK_RUNS["check_runs"][0])
        check["check_suite"] = {"id": 9002}
        api = FakeAPI(
            {
                ("GET", WORKFLOW_RUNS_ENDPOINT): [[{"workflow_runs": [run]}]],
                ("GET", endpoint): [[{"check_runs": [check]}]],
            }
        )

        with self.assertRaisesRegex(
            github_protection.ProtectionError,
            "malformed.*check_suite.id",
        ):
            github_protection.discover_integration_id(api)

    def test_integration_id_comes_from_recent_successful_github_actions_check(self) -> None:
        older_sha = "b" * 40
        older_suite_id = 9000
        runs = {
            "workflow_runs": [
                {
                    "id": 7001,
                    "head_sha": "a" * 40,
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
                    "check_suite_id": CHECK_SUITE_ID,
                    "check_suite_url": (
                        "https://api.github.com/repos/supermorphic/"
                        f"homelab-playbook/check-suites/{CHECK_SUITE_ID}"
                    ),
                },
                {
                    "id": 7000,
                    "head_sha": older_sha,
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
                    "check_suite_id": older_suite_id,
                    "check_suite_url": (
                        "https://api.github.com/repos/supermorphic/"
                        f"homelab-playbook/check-suites/{older_suite_id}"
                    ),
                },
            ]
        }
        first_checks = {
            "check_runs": [
                {
                    "name": "merge-gate",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": 999, "slug": "third-party"},
                    "check_suite": {"id": CHECK_SUITE_ID},
                },
                {
                    "name": "merge-gate",
                    "status": "completed",
                    "conclusion": "failure",
                    "app": {"id": 123, "slug": "github-actions"},
                    "check_suite": {"id": CHECK_SUITE_ID},
                },
            ]
        }
        older_endpoint = (
            f"{REPOSITORY_ENDPOINT}/check-suites/{older_suite_id}/check-runs"
            "?check_name=merge-gate&per_page=100"
        )
        older_checks = copy.deepcopy(CHECK_RUNS)
        older_checks["check_runs"][0]["check_suite"]["id"] = older_suite_id
        api = FakeAPI(
            {
                ("GET", WORKFLOW_RUNS_ENDPOINT): [[runs]],
                ("GET", CHECK_RUNS_ENDPOINT): [[first_checks]],
                ("GET", older_endpoint): [[older_checks]],
            }
        )

        integration_id = github_protection.discover_integration_id(api)

        self.assertEqual(INTEGRATION_ID, integration_id)
        self.assertEqual(
            [
                ("GET", WORKFLOW_RUNS_ENDPOINT),
                ("GET", CHECK_RUNS_ENDPOINT),
                ("GET", older_endpoint),
            ],
            [(method, endpoint) for method, endpoint, _ in api.calls],
        )


class CommandModeTests(unittest.TestCase):
    def test_confirmation_guard_is_exact_and_checked_before_observation(self) -> None:
        for confirmation in (
            None,
            "apply:github-protection",
            "apply:github-protection:supermorphic/other",
            f" {github_protection.CONFIRMATION}",
            f"{github_protection.CONFIRMATION} ",
        ):
            with self.subTest(confirmation=confirmation):
                api = FakeAPI({})
                with self.assertRaisesRegex(
                    github_protection.ProtectionError,
                    github_protection.CONFIRMATION,
                ):
                    github_protection.apply(api, confirmation)
                self.assertEqual([], api.calls)

    def test_apply_recollects_then_mutates_only_planned_state_and_reads_back(self) -> None:
        repository = copy.deepcopy(REPOSITORY)
        repository["allow_merge_commit"] = True
        api = StatefulAPI(repository=repository, ruleset=None)

        actions = github_protection.apply(api, github_protection.CONFIRMATION)

        self.assertEqual(2, len(actions))
        mutation_calls = [call for call in api.calls if call[0] != "GET"]
        self.assertEqual(
            [
                (
                    "PATCH",
                    REPOSITORY_ENDPOINT,
                    {
                        "allow_squash_merge": True,
                        "allow_merge_commit": False,
                        "allow_rebase_merge": False,
                    },
                ),
                (
                    "POST",
                    RULESETS_ENDPOINT,
                    github_protection.expected_ruleset(INTEGRATION_ID),
                ),
            ],
            mutation_calls,
        )
        for endpoint in (
            REPOSITORY_ENDPOINT,
            WORKFLOW_RUNS_ENDPOINT,
            CHECK_RUNS_ENDPOINT,
            RULESETS_ENDPOINT,
            EFFECTIVE_RULES_ENDPOINT,
        ):
            self.assertEqual(
                2,
                sum(
                    method == "GET" and called_endpoint == endpoint
                    for method, called_endpoint, _ in api.calls
                ),
                endpoint,
            )
        self.assertEqual(
            1,
            sum(
                method == "GET" and endpoint == RULESET_ENDPOINT
                for method, endpoint, _ in api.calls
            ),
        )

    def test_read_back_mismatch_fails(self) -> None:
        repository = copy.deepcopy(REPOSITORY)
        repository["allow_merge_commit"] = True
        api = StatefulAPI(
            repository=repository,
            ruleset=RULESET,
            effective_rules=EFFECTIVE_RULES,
            persist_mutations=False,
        )

        with self.assertRaisesRegex(
            github_protection.ProtectionError,
            "read-back mismatch",
        ):
            github_protection.apply(api, github_protection.CONFIRMATION)

        self.assertEqual(
            2,
            sum(
                method == "GET" and endpoint == REPOSITORY_ENDPOINT
                for method, endpoint, _ in api.calls
            ),
        )
        self.assertEqual(
            [("PATCH", REPOSITORY_ENDPOINT)],
            [
                (method, endpoint)
                for method, endpoint, _ in api.calls
                if method != "GET"
            ],
        )

    def test_apply_read_back_rejects_incomplete_effective_protection(self) -> None:
        api = StatefulAPI(
            repository=REPOSITORY,
            ruleset=RULESET,
            effective_rules=[],
            persist_mutations=False,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        status = github_protection.run(
            "apply",
            api,
            environ={"GITHUB_PROTECTION_CONFIRM": github_protection.CONFIRMATION},
            stdout=stdout,
            stderr=stderr,
        )

        self.assertNotEqual(0, status)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("read-back mismatch", stderr.getvalue())
        self.assertIn("effective_rules", stderr.getvalue())
        self.assertEqual(
            2,
            sum(
                method == "GET" and endpoint == EFFECTIVE_RULES_ENDPOINT
                for method, endpoint, _ in api.calls
            ),
        )

    def test_partial_apply_failure_still_reads_back_and_reports_observed_drift(
        self,
    ) -> None:
        repository = copy.deepcopy(REPOSITORY)
        repository["allow_merge_commit"] = True
        api = StatefulAPI(
            repository=repository,
            ruleset=None,
            create_error=github_protection.GitHubAPIError(
                status=500,
                message="ruleset creation failed",
            ),
        )

        with self.assertRaises(github_protection.ProtectionError) as raised:
            github_protection.apply(api, github_protection.CONFIRMATION)

        message = str(raised.exception)
        self.assertIn("write failed", message)
        self.assertIn("ruleset creation failed", message)
        self.assertIn("post-write", message)
        self.assertIn("ruleset 'Protect main': missing", message)
        self.assertIn("no rollback", message)
        self.assertFalse(api.repository["allow_merge_commit"])
        self.assertIsNone(api.ruleset)
        self.assertEqual(
            [("PATCH", REPOSITORY_ENDPOINT), ("POST", RULESETS_ENDPOINT)],
            [
                (method, endpoint)
                for method, endpoint, _ in api.calls
                if method != "GET"
            ],
        )
        for endpoint in (
            REPOSITORY_ENDPOINT,
            WORKFLOW_RUNS_ENDPOINT,
            CHECK_RUNS_ENDPOINT,
            RULESETS_ENDPOINT,
            EFFECTIVE_RULES_ENDPOINT,
        ):
            self.assertEqual(
                2,
                sum(
                    method == "GET" and called_endpoint == endpoint
                    for method, called_endpoint, _ in api.calls
                ),
                endpoint,
            )

    def test_check_and_plan_perform_get_only(self) -> None:
        for mode, responses in (
            ("check", exact_responses()),
            (
                "plan",
                {
                    **exact_responses(),
                    ("GET", RULESETS_ENDPOINT): [[[]]],
                    ("GET", EFFECTIVE_RULES_ENDPOINT): [[[]]],
                },
            ),
        ):
            with self.subTest(mode=mode):
                if mode == "plan":
                    del responses[("GET", RULESET_ENDPOINT)]
                api = FakeAPI(responses)
                stdout = io.StringIO()
                stderr = io.StringIO()

                status = github_protection.run(
                    mode,
                    api,
                    environ={},
                    stdout=stdout,
                    stderr=stderr,
                )

                self.assertEqual(0, status, stderr.getvalue())
                self.assertTrue(api.calls)
                self.assertTrue(all(method == "GET" for method, _, _ in api.calls))
                if mode == "plan":
                    self.assertIn("create", stdout.getvalue())
                    self.assertIn(
                        f"GITHUB_PROTECTION_CONFIRM={github_protection.CONFIRMATION}",
                        stdout.getvalue(),
                    )
                    self.assertIn("not authorization", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
