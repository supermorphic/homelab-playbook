"""Offline images must retain their reviewed registry identity and native ABI."""

import importlib
import unittest


class NativeAssetTests(unittest.TestCase):
    def test_wrong_architecture_or_digest_is_rejected(self):
        module = importlib.import_module('scripts.forgejo_runner.native_assets')
        pin = 'example.invalid/image:version@sha256:' + 'a' * 64
        image = {'Id': 'b' * 64, 'Architecture': 'amd64',
                 'RepoDigests': ['example.invalid/image@sha256:' + 'a' * 64]}
        self.assertEqual('b' * 64, module.validate_image(image, pin, 'amd64'))
        for change in ({'Architecture': 'arm64'}, {'RepoDigests': []}, {'Id': 'unknown'},
                       {'RepoDigests': ['example.invalid/image@sha256:' + 'c' * 64]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.validate_image({**image, **change}, pin, 'amd64')


if __name__ == '__main__':
    unittest.main()
