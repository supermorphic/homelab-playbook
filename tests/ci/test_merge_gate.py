from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MERGE_GATE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "merge_gate.py"
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def load_merge_gate() -> ModuleType:
    if not MERGE_GATE_PATH.is_file():
        raise RuntimeError(f"required implementation does not exist: {MERGE_GATE_PATH}")
    spec = importlib.util.spec_from_file_location("merge_gate", MERGE_GATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MERGE_GATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge_gate = load_merge_gate()


def indented_block(source: str, heading: str, indent: int) -> str:
    lines = source.splitlines()
    heading_line = f"{' ' * indent}{heading}:"
    try:
        start = lines.index(heading_line)
    except ValueError as error:
        raise AssertionError(f"missing YAML heading: {heading_line}") from error

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" "):
            line_indent = 0
        else:
            line_indent = len(line) - len(line.lstrip())
        if line.strip() and line_indent <= indent:
            end = index
            break
    return "\n".join(lines[start:end])


def named_step_block(source: str, name: str, indent: int = 6) -> str:
    lines = source.splitlines()
    heading_line = f"{' ' * indent}- name: {name}"
    try:
        start = lines.index(heading_line)
    except ValueError as error:
        raise AssertionError(f"missing workflow step: {name}") from error

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith(f"{' ' * indent}- name:"):
            end = index
            break
    return "\n".join(lines[start:end])


def direct_mapping_values(source: str, key: str, indent: int) -> list[str]:
    prefix = f"{' ' * indent}{key}:"
    return [
        line.removeprefix(prefix).strip()
        for line in source.splitlines()
        if line.startswith(prefix)
    ]


def direct_mapping_block(source: str, key: str, indent: int) -> str:
    lines = source.splitlines()
    prefix = f"{' ' * indent}{key}:"
    matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one direct mapping for {key!r}, found {len(matches)}"
        )

    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        line_indent = len(line) - len(line.lstrip())
        if line.strip() and line_indent <= indent:
            end = index
            break
    return "\n".join(lines[start:end])


def workflow_observability_errors(source: str) -> list[str]:
    errors: list[str] = []
    contracts = (
        ("Run pull request fast validation", "mise run check:fast"),
        ("Run full-history fast validation", "mise run check:fast"),
        ("Run offline Ansible validation", "mise run check:ansible"),
    )
    for step_name, command in contracts:
        try:
            step = named_step_block(source, step_name)
        except AssertionError:
            errors.append(f"{step_name}: step is missing")
            continue
        expected_fragments = (
            "started_at=$SECONDS",
            f"{command} || validation_status=$?",
            "elapsed_seconds=$((SECONDS - started_at))",
            f"'{command}'",
            "Validation duration: %s seconds",
            '>> "$GITHUB_STEP_SUMMARY"',
            'exit "$validation_status"',
        )
        for fragment in expected_fragments:
            if fragment not in step:
                errors.append(f"{step_name}: missing {fragment}")
    return errors


class MergeGateReconciliationTests(unittest.TestCase):
    def assert_accepted(
        self,
        depth: str,
        *,
        classify: str = "success",
        fast: str = "success",
        ansible: str = "success",
    ) -> None:
        self.assertEqual(
            [],
            merge_gate.reconcile(depth, classify, fast, ansible),
        )

    def test_fast_depth_accepts_skipped_ansible(self) -> None:
        self.assert_accepted("fast", ansible="skipped")

    def test_fast_depth_accepts_successful_shadow_ansible(self) -> None:
        self.assert_accepted("fast", ansible="success")

    def test_ansible_depth_requires_both_validation_jobs(self) -> None:
        self.assert_accepted("ansible")

    def test_full_depth_requires_both_validation_jobs(self) -> None:
        self.assert_accepted("full")

    def test_molecule_depth_fails_as_unimplemented(self) -> None:
        self.assertEqual(
            ["validation depth 'molecule' is not implemented"],
            merge_gate.reconcile("molecule", "success", "success", "success"),
        )

    def test_selected_fast_job_rejects_non_success_conclusions(self) -> None:
        for result in ("skipped", "failure", "cancelled"):
            with self.subTest(result=result):
                self.assertEqual(
                    [f"fast job result is '{result}', expected 'success'"],
                    merge_gate.reconcile("fast", "success", result, "skipped"),
                )

    def test_selected_ansible_job_rejects_non_success_conclusions(self) -> None:
        for depth in ("ansible", "full"):
            for result in ("skipped", "failure", "cancelled"):
                with self.subTest(depth=depth, result=result):
                    self.assertEqual(
                        [f"ansible job result is '{result}', expected 'success'"],
                        merge_gate.reconcile(depth, "success", "success", result),
                    )

    def test_non_selected_ansible_rejects_failure_or_cancellation(self) -> None:
        for result in ("failure", "cancelled"):
            with self.subTest(result=result):
                self.assertEqual(
                    [
                        f"ansible job result is '{result}', expected 'success' or "
                        "'skipped'"
                    ],
                    merge_gate.reconcile("fast", "success", "success", result),
                )

    def test_missing_or_unknown_depth_fails_closed(self) -> None:
        cases = {
            "": "validation depth is missing",
            "unexpected": "validation depth 'unexpected' is unknown",
        }
        for depth, expected in cases.items():
            with self.subTest(depth=depth):
                self.assertEqual(
                    [expected],
                    merge_gate.reconcile(depth, "success", "success", "success"),
                )

    def test_invalid_depths_still_report_classifier_and_fast_mismatches(self) -> None:
        cases = {
            "": "validation depth is missing",
            "molecule": "validation depth 'molecule' is not implemented",
            "unexpected": "validation depth 'unexpected' is unknown",
        }
        for depth, depth_error in cases.items():
            with self.subTest(depth=depth):
                self.assertEqual(
                    [
                        "classify job result is 'failure', expected 'success'",
                        "fast job result is 'cancelled', expected 'success'",
                        depth_error,
                    ],
                    merge_gate.reconcile(
                        depth,
                        "failure",
                        "cancelled",
                        "failure",
                    ),
                )

    def test_classifier_failure_fails_gate(self) -> None:
        self.assertEqual(
            ["classify job result is 'failure', expected 'success'"],
            merge_gate.reconcile("full", "failure", "success", "success"),
        )

    def test_cli_prints_every_mismatch_and_exits_one(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(MERGE_GATE_PATH),
                "--depth",
                "ansible",
                "--classify-result",
                "failure",
                "--fast-result",
                "cancelled",
                "--ansible-result",
                "skipped",
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual(
            "\n".join(
                [
                    "classify job result is 'failure', expected 'success'",
                    "fast job result is 'cancelled', expected 'success'",
                    "ansible job result is 'skipped', expected 'success'",
                    "",
                ]
            ),
            result.stderr,
        )

    def test_invalid_depth_cli_prints_classifier_fast_and_depth_mismatches(
        self,
    ) -> None:
        cases = {
            "": "validation depth is missing",
            "molecule": "validation depth 'molecule' is not implemented",
            "unexpected": "validation depth 'unexpected' is unknown",
        }
        for depth, depth_error in cases.items():
            with self.subTest(depth=depth):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(MERGE_GATE_PATH),
                        "--depth",
                        depth,
                        "--classify-result",
                        "failure",
                        "--fast-result",
                        "cancelled",
                        "--ansible-result",
                        "failure",
                    ],
                    cwd=REPOSITORY_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(1, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual(
                    "\n".join(
                        [
                            "classify job result is 'failure', expected 'success'",
                            "fast job result is 'cancelled', expected 'success'",
                            depth_error,
                            "",
                        ]
                    ),
                    result.stderr,
                )


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not WORKFLOW_PATH.is_file():
            raise RuntimeError(f"required workflow does not exist: {WORKFLOW_PATH}")
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.triggers = indented_block(cls.workflow, '"on"', 0)
        cls.classify = indented_block(cls.workflow, "classify", 2)
        cls.fast = indented_block(cls.workflow, "fast", 2)
        cls.ansible = indented_block(cls.workflow, "ansible", 2)
        cls.merge_gate = indented_block(cls.workflow, "merge-gate", 2)

    def test_triggers_permissions_and_concurrency_are_always_present(self) -> None:
        self.assertIn("  pull_request:\n    branches:\n      - main", self.triggers)
        self.assertIn("  workflow_dispatch:", self.triggers)
        self.assertIn("  schedule:\n    - cron:", self.triggers)
        self.assertNotIn("paths:", self.triggers)
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertIn(
            "group: ci-${{ github.workflow }}-${{ "
            "github.event.pull_request.number || github.ref }}",
            self.workflow,
        )
        self.assertIn("cancel-in-progress: true", self.workflow)

    def test_actions_are_sha_pinned_and_checkout_is_non_persisting(self) -> None:
        checkout = (
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        )
        mise = "jdx/mise-action@c2a87611a18de5b3828c5652fe268e992400cb5c"
        self.assertEqual(4, self.workflow.count(f"uses: {checkout}"))
        self.assertEqual(4, self.workflow.count(f"uses: {mise}"))
        self.assertEqual(4, self.workflow.count("persist-credentials: false"))
        self.assertEqual(4, self.workflow.count("fetch-depth: 0"))
        self.assertEqual(4, self.workflow.count("install: true"))
        self.assertEqual(4, self.workflow.count("cache: true"))
        for line in self.workflow.splitlines():
            if "uses:" in line:
                self.assertRegex(line, r"@[0-9a-f]{40}(?:\s+#.*)?$")

    def test_classifier_propagates_all_outputs_and_uses_event_specific_shas(self) -> None:
        expected_outputs = (
            "      depth: ${{ steps.classify.outputs.depth }}\n"
            "      run_fast: ${{ steps.classify.outputs.run_fast }}\n"
            "      run_ansible: ${{ steps.classify.outputs.run_ansible }}\n"
            "      run_molecule: ${{ steps.classify.outputs.run_molecule }}\n"
            "      paths: ${{ steps.classify.outputs.paths }}\n"
            "      reasons: ${{ steps.classify.outputs.reasons }}"
        )
        self.assertIn(expected_outputs, self.classify)
        self.assertIn("timeout-minutes: 2", self.classify)
        self.assertIn("EVENT_NAME: ${{ github.event_name }}", self.classify)
        self.assertIn(
            "PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}", self.classify
        )
        self.assertIn(
            "PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}", self.classify
        )
        self.assertIn('--base "$PR_BASE_SHA"', self.classify)
        self.assertIn('--head "$PR_HEAD_SHA"', self.classify)
        self.assertIn("--base HEAD", self.classify)
        self.assertIn("--head HEAD", self.classify)
        self.assertIn("--force-depth full", self.classify)
        self.assertIn("--format github", self.classify)
        self.assertIn('>> "$GITHUB_OUTPUT"', self.classify)
        self.assertNotIn("roles/", self.classify)
        self.assertNotIn("docs/", self.classify)

    def test_fast_starts_immediately_and_scopes_secret_scans_by_event(self) -> None:
        self.assertIn("timeout-minutes: 3", self.fast)
        self.assertNotIn("needs:", self.fast)
        sync = "mise exec -- uv sync --frozen --only-group fast"
        self.assertEqual(1, self.fast.count(sync))
        self.assertLess(self.fast.index(sync), self.fast.index("mise run check:fast"))
        self.assertEqual(
            2,
            self.fast.count("mise run check:fast || validation_status=$?"),
        )

        pull_request_step = named_step_block(
            self.fast, "Run pull request fast validation"
        )
        self.assertIn("if: github.event_name == 'pull_request'", pull_request_step)
        self.assertIn(
            "CI_BASE_SHA: ${{ github.event.pull_request.base.sha }}",
            pull_request_step,
        )
        self.assertIn(
            "CI_HEAD_SHA: ${{ github.event.pull_request.head.sha }}",
            pull_request_step,
        )
        self.assertNotIn("FULL_SECRET_SCAN", pull_request_step)

        full_history_step = named_step_block(
            self.fast, "Run full-history fast validation"
        )
        self.assertIn("if: github.event_name != 'pull_request'", full_history_step)
        self.assertIn('FULL_SECRET_SCAN: "1"', full_history_step)
        self.assertNotIn("CI_BASE_SHA", full_history_step)
        self.assertNotIn("CI_HEAD_SHA", full_history_step)

    def test_ansible_runs_only_for_exact_selected_depths_and_is_offline(self) -> None:
        self.assertIn("needs: classify", self.ansible)
        self.assertEqual(
            "\n".join(
                [
                    "    if: >-",
                    "      needs.classify.outputs.depth == 'ansible' ||",
                    "      needs.classify.outputs.depth == 'molecule' ||",
                    "      needs.classify.outputs.depth == 'full'",
                ]
            ),
            direct_mapping_block(self.ansible, "if", 4),
        )
        self.assertIn("timeout-minutes: 5", self.ansible)
        self.assertEqual(1, self.ansible.count("mise run bootstrap"))
        self.assertEqual(
            1,
            self.ansible.count("mise run check:ansible || validation_status=$?"),
        )
        self.assertLess(
            self.ansible.index("mise run bootstrap"),
            self.ansible.index("mise run check:ansible"),
        )
        lowered = self.ansible.lower()
        for forbidden in ("secrets.", "kubeconfig", "ssh", "inventory/production"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_validation_jobs_report_exact_commands_and_measured_durations(
        self,
    ) -> None:
        self.assertEqual([], workflow_observability_errors(self.workflow))

    def test_workflow_observability_contract_rejects_command_mutations(self) -> None:
        mutations = (
            self.workflow.replace("mise run check:fast", "mise run ci"),
            self.workflow.replace("mise run check:ansible", "mise run ci"),
        )

        for mutated_workflow in mutations:
            with self.subTest():
                self.assertTrue(workflow_observability_errors(mutated_workflow))

    def test_workflow_observability_contract_rejects_duration_mutation(self) -> None:
        mutated_workflow = self.workflow.replace(
            "elapsed_seconds=$((SECONDS - started_at))",
            "elapsed_seconds=0",
        )

        self.assertTrue(workflow_observability_errors(mutated_workflow))

    def test_merge_gate_always_reconciles_classifier_and_job_results(self) -> None:
        self.assertIn("name: merge-gate", self.merge_gate)
        self.assertEqual(
            ["always()"],
            direct_mapping_values(self.merge_gate, "if", 4),
        )
        self.assertIn("timeout-minutes: 2", self.merge_gate)
        self.assertIn(
            "needs:\n      - classify\n      - fast\n      - ansible", self.merge_gate
        )
        expected_environment = {
            "SELECTED_DEPTH: ${{ needs.classify.outputs.depth }}",
            "CLASSIFY_RESULT: ${{ needs.classify.result }}",
            "FAST_RESULT: ${{ needs.fast.result }}",
            "ANSIBLE_RESULT: ${{ needs.ansible.result }}",
        }
        for variable in expected_environment:
            with self.subTest(variable=variable):
                self.assertIn(variable, self.merge_gate)
        for argument in (
            '--depth "$SELECTED_DEPTH"',
            '--classify-result "$CLASSIFY_RESULT"',
            '--fast-result "$FAST_RESULT"',
            '--ansible-result "$ANSIBLE_RESULT"',
        ):
            with self.subTest(argument=argument):
                self.assertIn(argument, self.merge_gate)
        self.assertIn("REASONS: ${{ needs.classify.outputs.reasons }}", self.merge_gate)
        self.assertIn('>> "$GITHUB_STEP_SUMMARY"', self.merge_gate)
        self.assertNotIn("upload-artifact", self.workflow)
        self.assertNotIn("junit", self.workflow.lower())
        self.assertNotIn("allure", self.workflow.lower())


if __name__ == "__main__":
    unittest.main()
