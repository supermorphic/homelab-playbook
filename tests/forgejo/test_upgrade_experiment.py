"""The registered rehearsal binds genuine cleanup and independent acceptance results."""
import importlib
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_archive
from test_archive import ROOT


class UpgradeExperimentTests(unittest.TestCase):
    def test_registered_upgrade_mode_is_available(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/forgejo/test.py'), '--help'],
                                capture_output=True, text=True, check=True)
        self.assertIn('upgrade', result.stdout)

    def test_evidence_cannot_claim_completion_after_failed_cleanup(self):
        self.assertTrue((ROOT / 'scripts/forgejo/upgrade.py').is_file(), 'upgrade rehearsal is missing')
        module = importlib.import_module('scripts.forgejo.upgrade')
        result = module.evidence_record('e' * 64, {name: True for name in
            ('migration', 'continuity', 'source_restore', 'isolation', 'cleanup', 'actions')},
            ['owned fixture cleanup failed'])
        self.assertFalse(result['completed'])
        self.assertFalse(result['checks']['cleanup'])

    def test_incomplete_api_acceptance_cannot_be_promoted_by_migration_success(self):
        self.assertTrue((ROOT / 'scripts/forgejo/upgrade.py').is_file(), 'upgrade rehearsal is missing')
        module = importlib.import_module('scripts.forgejo.upgrade')
        result = module.evidence_record('e' * 64, {'migration': True, 'source_restore': True}, [])
        self.assertFalse(result['completed'])

    def test_changed_rehearsal_files_invalidate_success(self):
        module = importlib.import_module('scripts.forgejo.upgrade')
        checks = {name: True for name in module.transition.REQUIRED_CHECKS}
        result = module.evidence_record('e' * 64, checks, [], oracle='f' * 64,
                                       current=('a' * 64, 'f' * 64))
        self.assertFalse(result['completed'])
        self.assertEqual('e' * 64, result['controller_sha256'])
        self.assertEqual('f' * 64, result['oracle_sha256'])

    def test_late_experiment_error_cannot_claim_success(self):
        module = importlib.import_module('scripts.forgejo.upgrade')
        result = module.evidence_record('e' * 64,
            {name: True for name in module.transition.REQUIRED_CHECKS}, [], error='owned filesystem cleanup')
        self.assertFalse(result['completed'])

    def test_interrupted_filesystem_cleanup_cannot_publish_success(self):
        module = importlib.import_module('scripts.forgejo.upgrade')
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_fixture = test_archive.ArchiveTests()
                archive_fixture.setUp()
                self.addCleanup(archive_fixture.doCleanups)
                config = root / 'config'
                config.mkdir()
                for name in module.restore.KEYS:
                    (config / name).write_text('synthetic upgrade fixture')

                def private_file(directory, name, content):
                    path = directory / name
                    path.write_text(content)
                    path.chmod(0o600)
                    return path

                # Replace container/API boundaries, retaining the real run orchestration,
                # archive validation, evidence serialization and filesystem writes.
                runtime = SimpleNamespace(run_id='interrupted', podman='unused-podman',
                    observe_volumes=lambda: None, cleanup=lambda: [], name=lambda name: name,
                    command=lambda *args, **kwargs: SimpleNamespace(stdout='lo:\n', returncode=1),
                    create=lambda *args: 'synthetic-container', wait=lambda *args, **kwargs: None,
                    private_file=private_file)
                client = SimpleNamespace(container='synthetic-container', base='/api/v1',
                    refs=lambda path: {}, command=runtime.command)
                source = SimpleNamespace(config=config, user='fixture', initialize_admin=lambda: None)
                capture = SimpleNamespace(backups=archive_fixture.path.parent,
                    settings={'recovery_generation': archive_fixture.manifest['recovery_generation']})
                directory = Mock()
                directory.__enter__ = Mock(return_value=str(root))
                directory.__exit__ = Mock(side_effect=interruption())
                patches = (
                    (module, 'ROOT', root),
                    (module, 'preflight', Mock(return_value=(runtime, {'architecture': 'amd64'}))),
                    (module, 'Run', Mock(return_value=runtime)),
                    (module, 'fingerprints', Mock(return_value=('e' * 64, 'f' * 64))),
                    (module, 'ActionsApplication', Mock(return_value=source)),
                    (module, 'source_workflow', Mock(return_value={})),
                    (module, 'check_host_runtime', Mock()),
                    (module, 'actions_acceptance', Mock(return_value={})),
                    (module, 'temp_root', Mock(return_value=root)),
                    (module.tempfile, 'TemporaryDirectory', Mock(return_value=directory)),
                    (module.fixture, 'source_auth', Mock(return_value=(client, root / 'token'))),
                    (module.fixture, 'seed', Mock(return_value={'refs': {}})),
                    (module.fixture, 'Sentinel', Mock()),
                    (module.fixture, 'Capture', Mock(return_value=capture)),
                    (module.backup, 'perform_capture', Mock(return_value={'archive_id': archive_fixture.path.name})),
                    (module.restore, 'checked_expected', Mock(return_value={'refs': {}})),
                    (module.restore, 'restore_local', Mock()),
                    (module.restore, 'Client', Mock(return_value=client)),
                    (module.restore, 'assert_expected', Mock()),
                )
                with ExitStack() as stack:
                    for owner, name, replacement in patches:
                        stack.enter_context(patch.object(owner, name, replacement))
                    with self.assertRaises(interruption):
                        module.run()
                record = json.loads((root / '.tmp/forgejo/interrupted.upgrade.json').read_text())
                self.assertTrue(all(record['checks'].values()))
                self.assertFalse(record['completed'])
                self.assertIsNotNone(record['failure_phase'])

    def test_rehearsal_fingerprint_changes_with_pinned_dependencies(self):
        module = importlib.import_module('scripts.forgejo.upgrade')
        before = module.oracle_digest()
        original = Path.read_bytes
        for name in ('uv.lock', '.mise.toml', 'roles/forgejo/files/mirror-credential-helper.sh'):
            def changed(path):
                value = original(path)
                return value + b'\n# synthetic dependency change\n' if path == ROOT / name else value
            with self.subTest(name=name), patch.object(Path, 'read_bytes', changed):
                self.assertNotEqual(before, module.oracle_digest())
