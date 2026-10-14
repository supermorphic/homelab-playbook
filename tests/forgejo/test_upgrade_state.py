"""Pending upgrade state takes precedence over ordinary availability recovery."""
import json
import os
from pathlib import Path
import tempfile
import unittest

import test_archive as fixtures
from test_profiles import SOURCE, TARGET


def journal(phase='migration-started'):
    return {'schema': 1, 'id': 'a' * 32, 'phase': phase, 'source': dict(SOURCE), 'target': dict(TARGET),
            'source_generation': 'd' * 32, 'controller_sha256': 'e' * 64,
            'controller': {'pid': 42, 'boot': 'synthetic', 'ticks': '100'},
            'archive_id': 'forgejo-20261013T120000Z-abcdef12'}


class UpgradeStateTests(unittest.TestCase):
    def setUp(self):
        self.state = fixtures.load('upgrade_state')
        root = fixtures.ROOT / '.tmp/forgejo'
        root.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.journal = journal()

    def save(self, name, document):
        path = self.root / name
        path.write_text(json.dumps(document))
        path.chmod(0o640)
        return path

    def test_pending_upgrade_blocks_ordinary_activation_after_controller_death(self):
        self.save('upgrade-state.json', self.journal)
        with self.assertRaises(RuntimeError):
            self.state.require_idle(self.root, owner=os.getuid())

    def test_committed_or_aborted_transaction_allows_ordinary_operations(self):
        for phase in ('committed', 'aborted'):
            self.save('upgrade-state.json', {**self.journal, 'phase': phase})
            self.state.require_idle(self.root, owner=os.getuid())

    def test_malformed_or_linked_journal_cannot_be_treated_as_idle(self):
        path = self.save('upgrade-state.json', {**self.journal, 'phase': 'invented'})
        with self.assertRaises(ValueError):
            self.state.require_idle(self.root, owner=os.getuid())
        path.unlink()
        path.symlink_to(self.root / 'absent')
        with self.assertRaises((OSError, ValueError)):
            self.state.require_idle(self.root, owner=os.getuid())

    def test_terminal_state_without_its_recovery_identity_is_not_idle(self):
        for field in ('source', 'target', 'archive_id', 'source_generation', 'controller_sha256', 'controller'):
            document = {**self.journal, 'phase': 'committed'}
            document.pop(field)
            self.save('upgrade-state.json', document)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.state.require_idle(self.root, owner=os.getuid())

    def test_retention_references_are_validated_and_missing_is_empty(self):
        self.assertEqual(frozenset(), self.state.protected_archives(self.root, owner=os.getuid()))
        identifier = 'forgejo-20260930T033000Z-abcdef12'
        self.save('retained-archives.json', {'schema': 1, 'archives': [identifier]})
        self.assertEqual(frozenset([identifier]), self.state.protected_archives(self.root, owner=os.getuid()))
        for archives in (['../outside'], [identifier, identifier], 'not-an-array', [True]):
            self.save('retained-archives.json', {'schema': 1, 'archives': archives})
            with self.subTest(archives=archives), self.assertRaises(ValueError):
                self.state.protected_archives(self.root, owner=os.getuid())
