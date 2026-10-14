"""Helper publication rejects a pin change before changing active code."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_archive as fixtures


class HelperInstallTests(unittest.TestCase):
    def setUp(self):
        self.install = fixtures.load('install')
        root = fixtures.ROOT / '.tmp/forgejo'
        root.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.staged = self.root / 'staged'
        self.staged.mkdir(mode=0o700)
        self.destination = self.root / 'installed'
        self.destination.mkdir(mode=0o750)
        self.pins = {'image': 'source-image', 'postgres_image': 'same-database'}
        for name in self.install.FILES:
            path = self.staged / name
            path.write_text("VALUE = 'source'\nif __name__ == '__main__': print(VALUE)\n")
            path.chmod(0o600)

    def publish(self, installed=None):
        return self.install.publish_bundle(self.staged, self.destination,
            self.pins, self.pins if installed is None else installed,
            owner=os.getuid(), group=os.getgid())

    def test_rejected_runtime_change_does_not_publish_any_helper(self):
        with self.assertRaises(ValueError):
            self.publish({**self.pins, 'image': 'different-image'})
        self.assertEqual([], list(self.destination.iterdir()))

    def test_bundle_selection_is_idempotent_and_imports_one_generation(self):
        self.assertTrue(self.publish())
        entry = self.destination / 'service.py'
        before = entry.stat().st_ino
        self.assertFalse(self.publish())
        self.assertEqual(before, entry.stat().st_ino)
        result = subprocess.run([sys.executable, '-I', str(entry)],
            text=True, capture_output=True, check=True)
        self.assertEqual('source', result.stdout.strip())
        (self.staged / 'service.py').write_text("print('replacement')\n")
        self.assertTrue(self.publish())
        result = subprocess.run([sys.executable, '-I', str(entry)],
            text=True, capture_output=True, check=True)
        self.assertEqual('replacement', result.stdout.strip())

    def test_linked_candidate_is_rejected_before_publication(self):
        path = self.staged / 'service.py'
        path.unlink()
        path.symlink_to(self.staged / 'archive.py')
        with self.assertRaises((OSError, ValueError)):
            self.publish()
        self.assertEqual([], list(self.destination.iterdir()))

    def test_real_staged_modules_and_launchers_do_not_mutate_immutable_bundles(self):
        for name in self.install.FILES:
            (self.staged / name).write_bytes((fixtures.FILES / name).read_bytes())
        program = (
            'import install, os; from pathlib import Path; '
            'p={"image":"source","postgres_image":"database"}; '
            'install.publish_bundle(Path.cwd(),Path(' + repr(str(self.destination)) + '),p,p,'
            'owner=os.getuid(),group=os.getgid())')
        for _ in range(2):
            result = subprocess.run([sys.executable, '-B', '-c', program], cwd=self.staged,
                                    text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            result = subprocess.run([sys.executable, str(self.destination / 'upgrade_host.py'), '--help'],
                                    text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.staged / '__pycache__').exists())
        self.assertFalse(any(path.name == '__pycache__' for path in self.destination.rglob('*')))
