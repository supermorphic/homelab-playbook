from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class InstalledRoleContractTests(unittest.TestCase):
    def test_pinned_sshd_role_validates_candidates_before_reload(self) -> None:
        source = (
            REPO_ROOT
            / ".ansible/roles/willshersystems.sshd/tasks/install_config.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("ansible.builtin.template:", source)
        self.assertIn("{{ sshd_binary | quote }} -t -f %s", source)
        self.assertLess(source.index("validate:"), source.index("notify: sshd_reload"))
