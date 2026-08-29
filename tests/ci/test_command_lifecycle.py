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
        task_names = {task["name"] for task in json.loads(result.stdout)}

        self.assertTrue(
            {"validate:fast", "validate:ansible", "ci:changed", "ci"}
            <= task_names
        )
        self.assertTrue({"check:fast", "check:ansible"}.isdisjoint(task_names))


if __name__ == "__main__":
    unittest.main()
