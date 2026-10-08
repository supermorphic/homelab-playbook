"""Exercise the Forgejo workflow's candidate and validation contracts."""

import os
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

from test_classify import TemporaryGitRepository

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".forgejo/workflows/ci.yml"


class ForgejoWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), "Forgejo CI workflow is required")
        self.workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.jobs = self.workflow["jobs"]

    def test_pr_manual_and_scheduled_validation_use_the_shared_pool(self):
        events = self.workflow["on"]
        self.assertEqual(["main"], events["pull_request"]["branches"])
        self.assertNotIn("paths", events["pull_request"])
        self.assertIn("workflow_dispatch", events)
        self.assertTrue(events["schedule"])
        self.assertEqual("bash", self.workflow["defaults"]["run"]["shell"])
        self.assertEqual("true", self.workflow["concurrency"]["cancel-in-progress"])
        self.assertEqual(
            {"classify", "fast", "ansible", "molecule", "merge-gate"}, set(self.jobs)
        )
        for job in self.jobs.values():
            self.assertEqual("homelab-podman-amd64", job["runs-on"])

    def test_checkout_selects_and_checks_the_declared_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=CI fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "--allow-empty",
                    "--quiet",
                    "-m",
                    "Candidate",
                ],
                check=True,
            )
            candidate = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
            ).strip()
            for name, job in self.jobs.items():
                steps = job["steps"]
                checkout = steps[0]
                self.assertRegex(
                    checkout["uses"],
                    r"^https://github\.com/actions/checkout@[a-f0-9]{40}$",
                )
                self.assertEqual("false", checkout["with"]["persist-credentials"])
                self.assertEqual("0", checkout["with"]["fetch-depth"])
                self.assertEqual(
                    "${{ github.event.pull_request.head.sha || github.sha }}",
                    checkout["with"]["ref"],
                )
                verify = steps[1]
                self.assertEqual(
                    "${{ github.event.pull_request.head.sha || github.sha }}",
                    verify["env"]["CANDIDATE_SHA"],
                )
                self.assertEqual(
                    "${{ github.event_name }}", verify["env"]["EVENT_NAME"]
                )
                self.assertEqual(
                    "${{ github.event.pull_request.base.sha }}",
                    verify["env"]["PR_BASE_SHA"],
                )
                for sha, expected in [(candidate, 0), ("0" * 40, 1), ("", 1)]:
                    with self.subTest(job=name, candidate=sha):
                        result = subprocess.run(
                            ["bash", "-e", "-o", "pipefail", "-c", verify["run"]],
                            cwd=root,
                            env={
                                **os.environ,
                                "CANDIDATE_SHA": sha,
                                "EVENT_NAME": "workflow_dispatch",
                            },
                            capture_output=True,
                        )
                        self.assertEqual(expected, result.returncode)

    def test_selected_jobs_and_gate_reuse_repository_validation(self):
        self.assertNotIn("needs", self.jobs["fast"])
        self.assertEqual("classify", self.jobs["ansible"]["needs"])
        self.assertEqual("classify", self.jobs["molecule"]["needs"])
        matrix = self.jobs["molecule"]["strategy"]
        self.assertEqual("false", matrix["fail-fast"])
        self.assertEqual(
            "${{ fromJSON(needs.classify.outputs.molecule_matrix) }}", matrix["matrix"]
        )
        gate = self.jobs["merge-gate"]
        self.assertEqual("always()", gate["if"])
        self.assertEqual(
            {"classify", "fast", "ansible", "molecule"}, set(gate["needs"])
        )
        reconcile = next(
            step
            for step in gate["steps"]
            if step["name"] == "Reconcile validation results"
        )
        self.assertIn("scripts/ci/merge_gate.py", reconcile["run"])
        self.assertEqual(
            "${{ needs.classify.outputs.molecule_plan }}",
            reconcile["env"]["MOLECULE_PLAN"],
        )
        for name in ("classify", "fast", "ansible", "molecule"):
            self.assertEqual(
                "${{ needs." + name + ".result }}",
                reconcile["env"][name.upper() + "_RESULT"],
            )
        for job in self.jobs.values():
            install = next(
                step for step in job["steps"] if step["name"] == "Install locked tools"
            )
            self.assertIn("mise install --locked", install["run"])
        runs = "\n".join(
            step.get("run", "") for job in self.jobs.values() for step in job["steps"]
        )
        for command in (
            "mise run validate:fast",
            "mise run validate:ansible",
            "mise run test:molecule",
            "mise run test:forgejo",
            "mise run test:semaphore",
            "mise run test:forgejo-metadata",
        ):
            self.assertIn(command, runs)

    def test_pr_candidate_must_include_the_current_target(self):
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        base = fixture.initialize_fixture()
        fixture.write("README.md", "candidate\n")
        candidate = fixture.commit_all("Candidate")
        target = subprocess.check_output(
            ["git", "-C", str(fixture.root), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
        ).strip()
        subprocess.run(
            [
                "git",
                "-C",
                str(fixture.root),
                "update-ref",
                "refs/remotes/origin/main",
                base,
            ],
            check=True,
        )
        for job in self.jobs.values():
            verify = job["steps"][1]
            env = {
                **os.environ,
                "EVENT_NAME": "pull_request",
                "CANDIDATE_SHA": candidate,
                "PR_BASE_SHA": base,
            }

            def run():
                return subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", verify["run"]],
                    cwd=fixture.root,
                    env=env,
                    capture_output=True,
                ).returncode

            self.assertEqual(0, run())
            # The fetched target advanced while this event was queued.
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.root),
                    "update-ref",
                    "refs/remotes/origin/main",
                    candidate,
                ],
                check=True,
            )
            self.assertNotEqual(0, run())
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.root),
                    "update-ref",
                    "refs/remotes/origin/main",
                    base,
                ],
                check=True,
            )
            # A target commit on a different branch is not in the PR head.
            subprocess.run(
                ["git", "-C", str(fixture.root), "checkout", "--detach", base],
                check=True,
                capture_output=True,
            )
            fixture.write("README.md", "target changes\n")
            advanced = fixture.commit_all("Target advances")
            subprocess.run(
                ["git", "-C", str(fixture.root), "checkout", target],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.root),
                    "update-ref",
                    "refs/remotes/origin/main",
                    advanced,
                ],
                check=True,
            )
            env["PR_BASE_SHA"] = advanced
            self.assertNotEqual(0, run())
            env["PR_BASE_SHA"] = ""
            self.assertNotEqual(0, run())
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.root),
                    "update-ref",
                    "refs/remotes/origin/main",
                    base,
                ],
                check=True,
            )

    def test_classifier_and_gate_execute_for_pr_manual_and_scheduled_events(self):
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        base = fixture.initialize_fixture()
        classify = next(
            step
            for step in self.jobs["classify"]["steps"]
            if step.get("id") == "classify"
        )
        gate = next(
            step
            for step in self.jobs["merge-gate"]["steps"]
            if step["name"] == "Reconcile validation results"
        )

        def command(script):
            return script.replace(
                "mise exec -- python", shlex.quote(sys.executable)
            ).replace("scripts/ci/", shlex.quote(str(ROOT / "scripts/ci")) + "/")

        cases = [
            ("pull_request", "README.md", "fast", set()),
            ("pull_request", "playbooks/example/provision.yml", "ansible", set()),
            (
                "pull_request",
                "roles/semaphore/tasks/main.yml",
                "molecule",
                {"semaphore/default"},
            ),
            (
                "pull_request",
                "roles/podman_foundation/tasks/main.yml",
                "molecule",
                {"forgejo/default", "semaphore/default", "system_maintenance/baseline"},
            ),
            ("pull_request", ".forgejo/workflows/ci.yml", "full", None),
            ("workflow_dispatch", "README.md", "full", None),
            ("schedule", "README.md", "full", None),
        ]
        for index, (event, path, depth, selectors) in enumerate(cases):
            fixture.write(path, f"candidate {index}\n")
            candidate = fixture.commit_all("Candidate")
            with self.subTest(event=event, path=path):
                output = fixture.root / ".git" / "workflow-outputs"
                output.write_text("")
                env = {
                    **os.environ,
                    "EVENT_NAME": event,
                    "PR_BASE_SHA": base,
                    "PR_HEAD_SHA": candidate,
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_STEP_SUMMARY": str(
                        fixture.root / ".git" / "workflow-summary"
                    ),
                }
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", command(classify["run"])],
                    cwd=fixture.root,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                values = dict(
                    line.split("=", 1) for line in output.read_text().splitlines()
                )
                self.assertEqual(depth, values["depth"])
                plan = json.loads(values["molecule_plan"])
                selected = {row["selector"] for row in plan["matrix"]["include"]}
                if selectors is not None:
                    self.assertEqual(selectors, selected)
                else:
                    self.assertEqual("full", plan["mode"])
                gate_env = {
                    **env,
                    "SELECTED_DEPTH": depth,
                    "MOLECULE_PLAN": values["molecule_plan"],
                    "CLASSIFY_RESULT": "success",
                    "FAST_RESULT": "success",
                    "ANSIBLE_RESULT": "success" if depth != "fast" else "skipped",
                    "MOLECULE_RESULT": (
                        "success" if depth in ("molecule", "full") else "skipped"
                    ),
                }
                required = [
                    key
                    for key in (
                        "CLASSIFY_RESULT",
                        "FAST_RESULT",
                        "ANSIBLE_RESULT",
                        "MOLECULE_RESULT",
                    )
                    if gate_env[key] == "success"
                ]
                for key in required:
                    for conclusion, expected in [
                        ("success", 0),
                        ("failure", 1),
                        ("cancelled", 1),
                        ("skipped", 1),
                        ("", 1),
                    ]:
                        with self.subTest(job=key, conclusion=conclusion):
                            result = subprocess.run(
                                [
                                    "bash",
                                    "-e",
                                    "-o",
                                    "pipefail",
                                    "-c",
                                    command(gate["run"]),
                                ],
                                cwd=fixture.root,
                                env={**gate_env, key: conclusion},
                                capture_output=True,
                                text=True,
                            )
                            self.assertEqual(expected, result.returncode, result.stderr)
            base = candidate


if __name__ == "__main__":
    unittest.main()
