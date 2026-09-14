from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class TimingTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "scripts/molecule_timing.py"
        self.assertTrue(path.exists(), "the runner needs a bounded timing collector")
        from scripts import molecule_timing

        self.timing = molecule_timing

    def test_phases_keep_repeated_destroy_failure_and_unstarted_phases(self):
        collector = self.timing.TimingCollector("default")
        events = [
            (1, "INFO [default > destroy] Executing"),
            (3, "INFO [default > destroy] Executed: Successful"),
            (4, "INFO [default > create] Executing"),
            (9, "INFO [default > create] Executed: Successful"),
            (10, "INFO [default > converge] Executing"),
            (17, "ERROR [default > converge] Executed: Failed"),
            (18, "INFO [default > cleanup] Executing"),
            (19, "INFO [default > cleanup] Executed: Successful"),
            (20, "INFO [default > destroy] Executing"),
        ]
        for at, line in events:
            collector.observe(line, at=at)
        collector.finish(23)
        report = collector.report()
        self.assertEqual(
            [
                ("destroy", 2, "successful"),
                ("create", 5, "successful"),
                ("converge", 7, "failed"),
                ("cleanup", 1, "successful"),
                ("destroy", 3, "incomplete"),
            ],
            [(p["phase"], p["seconds"], p["status"]) for p in report["phases"]],
        )
        self.assertEqual(["prepare", "idempotence", "verify"], report["not_run"])

    def test_colored_pinned_molecule_markers_ignore_task_text(self):
        collector = self.timing.TimingCollector("baseline")
        collector.observe("TASK [Executing converge]", at=0)
        collector.observe("INFO [other > verify] Executing", at=1)
        collector.observe("\x1b[34mINFO\x1b[0m baseline ➜ verify: Executing", at=2)
        collector.observe("INFO baseline ➜ verify: Executed: Successful", at=5)
        self.assertEqual(3, collector.report()["phases"][0]["seconds"])
        self.assertEqual(1, len(collector.report()["phases"]))

    def test_counted_completion_summary_closes_phase_without_inventing_success(self):
        collector = self.timing.TimingCollector("default")
        collector.observe("INFO [default > verify] Executing", at=2)
        collector.observe(
            "WARNING [default > verify] Executed: 2 successful, 1 failed", at=8
        )
        collector.finish(20)
        phase = collector.report()["phases"][0]
        self.assertEqual(6, phase["seconds"])
        self.assertEqual("failed", phase["status"])

    def test_tasks_are_aggregated_by_phase_source_and_role_execution(self):
        collector = self.timing.TimingCollector("default")
        collector.observe("INFO [default > converge] Executing", at=0)
        for duration, role_run in [(2, "one"), (3, "one"), (7, "two")]:
            event = {
                "source": "roles/proxy/tasks/main.yml:2",
                "role": "roles/proxy",
                "role_run": role_run,
                "seconds": duration,
            }
            self.assertTrue(
                collector.observe(self.timing.EVENT_PREFIX + json.dumps(event), at=1)
            )
        collector.observe("INFO [default > converge] Executed: Successful", at=15)
        report = collector.report()
        self.assertEqual(12, report["tasks"][0]["seconds"])
        self.assertEqual(3, report["tasks"][0]["count"])
        self.assertEqual(7, report["tasks"][0]["max_seconds"])
        self.assertEqual(2, report["roles"][0]["executions"])
        self.assertEqual(12, report["roles"][0]["seconds"])

    def test_collection_limits_and_malformed_events_are_visible(self):
        collector = self.timing.TimingCollector("default", max_tasks=2)
        collector.observe("INFO [default > verify] Executing", at=0)
        for i in range(4):
            collector.observe(
                self.timing.EVENT_PREFIX
                + json.dumps(
                    {
                        "source": f"roles/proxy/tasks/task{i}.yml:2",
                        "role": "roles/proxy",
                        "role_run": "one",
                        "seconds": i + 1,
                    }
                ),
                at=1,
            )
        collector.observe(self.timing.EVENT_PREFIX + '{"seconds": NaN}', at=1)
        report = collector.report()
        self.assertEqual(2, len(report["tasks"]))
        self.assertTrue(report["limited"])
        self.assertEqual(1, report["invalid_events"])

    def test_render_keeps_failed_phase_and_marks_missing_callback(self):
        collector = self.timing.TimingCollector("default")
        collector.observe("INFO [default > converge] Executing", at=2)
        collector.observe("ERROR [default > converge] Executed: Failed", at=8)
        text = self.timing.render(collector.report())
        self.assertIn("| converge | 1 | 6.00 | failed |", text)
        self.assertIn("| verify | — | — | not run |", text)
        self.assertIn("no callback records received", text)

    def test_git_provenance_marks_untracked_files_without_reporting_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True, text=True
                ).stdout.strip()

            git("init", "-q")
            git(
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "--allow-empty",
                "-qm",
                "fixture",
            )
            expected = git("rev-parse", "HEAD")
            self.assertEqual(
                {"commit": expected, "worktree": "clean"}, self.timing.provenance(root)
            )
            (root / "synthetic-private-name").touch()
            metadata = self.timing.provenance(root)
            self.assertEqual("uncommitted changes", metadata["worktree"])
            self.assertNotIn("synthetic-private-name", json.dumps(metadata))


if __name__ == "__main__":
    unittest.main()
