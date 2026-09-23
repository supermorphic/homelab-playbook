from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "roles/talos_lifecycle/files/lib/node-lifecycle-state.sh"
FIXTURES = Path(__file__).with_name("fixtures") / "records"


class LifecycleRecordTests(unittest.TestCase):
    def validate(self, value: object, kind: str) -> subprocess.CompletedProcess[str]:
        command = (
            f"source {VALIDATOR!s}; "
            "validate_lifecycle_record \"$1\" \"$2\""
        )
        return subprocess.run(
            ["bash", "-c", command, "test", json.dumps(value), kind],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_independent_literal_fixtures_are_accepted(self) -> None:
        for name, kind in (
            ("maintenance.json", "maintenance"),
            ("reboot.json", "reboot"),
            ("abrupt-loss.json", "abrupt-loss"),
        ):
            with self.subTest(name=name):
                value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
                self.assertEqual(self.validate(value, kind).returncode, 0)

    def test_extra_fields_and_wrong_types_are_rejected(self) -> None:
        cases = [
            {"schemaVersion": 1, "kind": "reboot", "extra": True},
            {"schemaVersion": 9, "kind": "reboot"},
            {"schemaVersion": 1, "kind": "foreign"},
            {
                "schemaVersion": 1,
                "kind": "maintenance",
                "longhorn": {
                    "allowScheduling": {"before": 1, "during": False},
                    "evictionRequested": {"before": False, "during": True},
                },
            },
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertNotEqual(self.validate(value, str(value.get("kind"))).returncode, 0)


if __name__ == "__main__":
    unittest.main()
