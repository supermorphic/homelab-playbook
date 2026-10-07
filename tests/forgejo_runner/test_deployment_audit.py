import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

path = Path(__file__).parents[2] / 'roles/forgejo_runner/files/deployment_audit.py'
spec = importlib.util.spec_from_file_location('deployment_audit', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeploymentAuditTests(unittest.TestCase):
    def test_unchanged_deployment_preserves_a_running_job_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_bytes(b'approved')
            path.chmod(0o600)
            manifest = [{'path': str(path), 'sha256': hashlib.sha256(b'approved').hexdigest(), 'mode': 0o600}]
            before = path.stat()
            result = module.audit_files(manifest, idle=False, authority_uid=os.getuid())
            self.assertFalse(result['changed'])
            self.assertEqual(before, path.stat())
            self.assertEqual(b'approved', path.read_bytes())

    def test_changed_deployment_requires_a_completed_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            path.write_bytes(b'old-synthetic')
            path.chmod(0o600)
            manifest = [{'path': str(path), 'sha256': hashlib.sha256(b'new-synthetic').hexdigest(), 'mode': 0o600}]
            with self.assertRaises(ValueError):
                module.audit_files(manifest, idle=False, authority_uid=os.getuid())
            self.assertEqual(b'old-synthetic', path.read_bytes())
            self.assertTrue(module.audit_files(manifest, idle=True, authority_uid=os.getuid())['changed'])

    def test_linked_or_shared_files_cannot_be_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'original'
            original.write_bytes(b'approved')
            original.chmod(0o600)
            alias = Path(directory) / 'alias'
            manifest = [{'path': str(alias), 'sha256': hashlib.sha256(b'approved').hexdigest(), 'mode': 0o600}]
            alias.symlink_to(original)
            with self.assertRaises(OSError):
                module.audit_files(manifest, idle=True, authority_uid=os.getuid())
            alias.unlink()
            os.link(original, alias)
            with self.assertRaises(ValueError):
                module.audit_files(manifest, idle=True, authority_uid=os.getuid())

    def test_fresh_install_reports_changes_without_creating_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'absent'
            manifest = [{'path': str(path), 'sha256': 'a'*64, 'mode': 0o600}]
            self.assertTrue(module.audit_files(manifest, idle=True, authority_uid=os.getuid())['changed'])
            self.assertFalse(path.exists())
