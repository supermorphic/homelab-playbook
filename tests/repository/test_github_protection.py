from __future__ import annotations

import copy
import io
import unittest
from collections.abc import Callable
from typing import Any

from scripts.repository import github_protection


INTEGRATION_ID = 15368
RULESET_ID = 42
OTHER_RULESET_ID = 99

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
    f"{REPOSITORY_ENDPOINT}/commits/{'a' * 40}/check-runs?check_name=merge-gate"
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

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, endpoint, copy.deepcopy(payload)))
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
    ) -> None:
        self.repository = copy.deepcopy(repository or REPOSITORY)
        self.ruleset = copy.deepcopy(ruleset)
        self.effective_rules = copy.deepcopy(effective_rules or [])
        self.persist_mutations = persist_mutations
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, endpoint, copy.deepcopy(payload)))
        if method == "GET":
            if endpoint == REPOSITORY_ENDPOINT:
                return copy.deepcopy(self.repository)
            if endpoint == WORKFLOW_RUNS_ENDPOINT:
                return copy.deepcopy(WORKFLOW_RUNS)
            if endpoint == CHECK_RUNS_ENDPOINT:
                return copy.deepcopy(CHECK_RUNS)
            if endpoint == RULESETS_ENDPOINT:
                return [] if self.ruleset is None else [copy.deepcopy(RULESET_SUMMARY)]
            if endpoint == RULESET_ENDPOINT and self.ruleset is not None:
                return copy.deepcopy(self.ruleset)
            if endpoint == EFFECTIVE_RULES_ENDPOINT:
                return copy.deepcopy(self.effective_rules)
        if method == "PATCH" and endpoint == REPOSITORY_ENDPOINT:
            if self.persist_mutations:
                self.repository.update(copy.deepcopy(payload or {}))
            return copy.deepcopy(self.repository)
        if method == "POST" and endpoint == RULESETS_ENDPOINT:
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
            copy.deepcopy(WORKFLOW_RUNS) for _ in range(repetitions)
        ],
        ("GET", CHECK_RUNS_ENDPOINT): [
            copy.deepcopy(CHECK_RUNS) for _ in range(repetitions)
        ],
        ("GET", RULESETS_ENDPOINT): [
            [copy.deepcopy(RULESET_SUMMARY)] for _ in range(repetitions)
        ],
        ("GET", RULESET_ENDPOINT): [copy.deepcopy(RULESET) for _ in range(repetitions)],
        ("GET", EFFECTIVE_RULES_ENDPOINT): [
            copy.deepcopy(EFFECTIVE_RULES) for _ in range(repetitions)
        ],
    }


def collect_with_ruleset(ruleset: dict[str, Any]) -> Any:
    responses = exact_responses()
    responses[("GET", RULESET_ENDPOINT)] = [copy.deepcopy(ruleset)]
    return github_protection.collect_state(FakeAPI(responses))


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

    def test_exact_state_has_no_drift_blockers_or_actions(self) -> None:
        state = github_protection.collect_state(FakeAPI(exact_responses()))

        self.assertEqual([], github_protection.drift(state))
        self.assertEqual([], github_protection.blockers(state))
        self.assertEqual([], github_protection.plan_actions(state))

    def test_missing_ruleset_plans_creation(self) -> None:
        responses = exact_responses()
        responses[("GET", RULESETS_ENDPOINT)] = [[]]
        del responses[("GET", RULESET_ENDPOINT)]
        responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [[]]

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


class OwnershipAndVisibilityTests(unittest.TestCase):
    def test_duplicate_managed_rulesets_block_apply(self) -> None:
        responses = exact_responses()
        second_summary = {**copy.deepcopy(RULESET_SUMMARY), "id": 43}
        second_ruleset = {**copy.deepcopy(RULESET), "id": 43}
        responses[("GET", RULESETS_ENDPOINT)] = [
            [copy.deepcopy(RULESET_SUMMARY), second_summary]
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
        api = FakeAPI(responses)

        state = github_protection.collect_state(api)
        self.assertTrue(
            any("unmanaged effective" in item for item in github_protection.blockers(state))
        )

        apply_api = FakeAPI(responses=exact_responses())
        apply_api.responses[("GET", EFFECTIVE_RULES_ENDPOINT)] = [
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
    def test_integration_id_comes_from_recent_successful_github_actions_check(self) -> None:
        older_sha = "b" * 40
        runs = {
            "workflow_runs": [
                {
                    "id": 7001,
                    "head_sha": "a" * 40,
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
                },
                {
                    "id": 7000,
                    "head_sha": older_sha,
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
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
                },
                {
                    "name": "merge-gate",
                    "status": "completed",
                    "conclusion": "failure",
                    "app": {"id": 123, "slug": "github-actions"},
                },
            ]
        }
        older_endpoint = (
            f"{REPOSITORY_ENDPOINT}/commits/{older_sha}/check-runs"
            "?check_name=merge-gate&per_page=100"
        )
        api = FakeAPI(
            {
                ("GET", WORKFLOW_RUNS_ENDPOINT): [runs],
                ("GET", CHECK_RUNS_ENDPOINT): [first_checks],
                ("GET", older_endpoint): [CHECK_RUNS],
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

    def test_check_and_plan_perform_get_only(self) -> None:
        for mode, responses in (
            ("check", exact_responses()),
            (
                "plan",
                {
                    **exact_responses(),
                    ("GET", RULESETS_ENDPOINT): [[]],
                    ("GET", EFFECTIVE_RULES_ENDPOINT): [[]],
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
