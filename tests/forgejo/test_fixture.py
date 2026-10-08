"""Independent application oracle must fail on changed refs or user identity."""
import importlib
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'scripts/forgejo/fixture.py').is_file(), 'real NAS fixture is missing')
        self.fixture = importlib.import_module('scripts.forgejo.fixture')

    def test_fixture_uses_exact_seed_content_hashes(self):
        expected = self.fixture.seed_content()
        self.assertEqual(b'independent Forgejo attachment fixture\n', expected['attachment'])
        self.assertEqual(b'independent Forgejo LFS fixture\n', expected['lfs'])
        self.assertNotEqual(expected['attachment'], expected['lfs'])

    def test_reference_oracle_rejects_changed_revision(self):
        before = {'refs/heads/main': 'a' * 40, 'refs/tags/fixture': 'b' * 40}
        self.fixture.assert_refs(before, dict(before))
        with self.assertRaises(RuntimeError):
            self.fixture.assert_refs(before, dict(before, **{'refs/heads/main': 'c' * 40}))

    def test_actions_seed_records_independent_content_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class LocalClient:
                def command(self, argv, *, input_text):
                    argv = [value.replace('/var/lib/gitea/data', str(root)) for value in argv]
                    return subprocess.run(argv, input=input_text, text=True, check=True,
                                          capture_output=True)

            expected = self.fixture.seed_actions_data(LocalClient())
            content = {
                'actions_log': b'independent Forgejo Actions log fixture\n',
                'actions_artifacts': b'independent Forgejo Actions artifact fixture\n',
            }
            self.assertEqual(set(content), set(expected))
            for name, original in content.items():
                self.assertEqual(original, (root / name / 'recovery-fixture').read_bytes())
                self.assertEqual(hashlib.sha256(original).hexdigest(), expected[name])

    def test_outbound_oracle_rejects_traffic_or_removed_targets(self):
        self.fixture.assert_outbound(0, 0, 1, 1)
        for values in ((0, 1, 1, 1), (0, 0, 0, 1), (0, 0, 1, 0)):
            with self.assertRaises(RuntimeError):
                self.fixture.assert_outbound(*values)


if __name__ == '__main__':
    unittest.main()
