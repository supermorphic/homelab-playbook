from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class CommandLifecycleTests(unittest.TestCase):
    def test_local_validation_surface_uses_validate_without_check_aliases(
        self,
    ) -> None:
        result = subprocess.run(
            ["mise", "tasks", "--json"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        tasks = json.loads(result.stdout)
        task_names = {task["name"] for task in tasks}
        published_names = {
            name
            for task in tasks
            for name in (task["name"], *task["aliases"])
        }

        self.assertTrue(
            {"validate:fast", "validate:ansible", "ci:changed", "ci"}
            <= task_names
        )
        self.assertTrue(
            {
                "github-protection:check",
                "github-protection:plan",
                "github-protection:apply",
            }
            <= task_names
        )
        self.assertTrue(
            {"check:fast", "check:ansible", "check:molecule"}.isdisjoint(
                published_names
            )
        )

    def test_github_protection_tasks_publish_distinct_lifecycle_commands(
        self,
    ) -> None:
        result = subprocess.run(
            ["mise", "tasks", "--json"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        tasks = {task["name"]: task for task in json.loads(result.stdout)}

        for mode in ("check", "plan", "apply"):
            task = tasks[f"github-protection:{mode}"]
            command = "\n".join(task["run"])
            self.assertIn(
                f"scripts/repository/github_protection.py {mode}",
                command,
            )
            self.assertIn("uv run --frozen --no-sync python", command)


if __name__ == "__main__":
    unittest.main()
