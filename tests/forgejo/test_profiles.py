"""Historical recovery selects reviewed images, independently of active defaults."""
import unittest

import test_archive as fixtures

SOURCE = {
    'image': 'codeberg.org/forgejo/forgejo:15.0.9-rootless@sha256:caf1bca332f95cdcf124227a4bfa3b49bbbfbbc8a5e4a97406921cb581165413',
    'postgres_image': 'docker.io/library/postgres:17.11@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f',
    'rclone_image': 'docker.io/rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5',
}
TARGET = {**SOURCE, 'image': 'codeberg.org/forgejo/forgejo:16.0.6-rootless@sha256:51b47921660e1ec7127f094208fe06901ba57df4fe6d59fcccc3a2419514a77f'}


class ProfileTests(unittest.TestCase):
    def test_historical_source_does_not_follow_current_default(self):
        profiles = fixtures.load('profiles')
        self.assertEqual(SOURCE, profiles.recovery_pins(SOURCE, TARGET))
        self.assertEqual(TARGET, profiles.recovery_pins(TARGET, SOURCE))

    def test_archive_cannot_choose_an_unreviewed_historical_digest(self):
        profiles = fixtures.load('profiles')
        for key in SOURCE:
            changed = {**SOURCE, key: SOURCE[key].split('@')[0] + '@sha256:' + 'a' * 64}
            with self.subTest(key=key), self.assertRaises(ValueError):
                profiles.recovery_pins(changed, TARGET)

    def test_current_exact_pins_remain_a_valid_recovery_profile(self):
        profiles = fixtures.load('profiles')
        self.assertEqual(TARGET, profiles.recovery_pins({**TARGET, 'schema': 1}, TARGET))

    def test_archive_contract_accepts_reviewed_major_without_weakening_pins(self):
        fixture = fixtures.ArchiveTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.manifest['image'] = TARGET['image']
        fixture.finish()
        self.assertEqual(fixture.manifest, fixture.archive.validate_archive(fixture.path, False))
        for pin in (TARGET['image'].split('@')[0], TARGET['image'].replace(':16.', ':17.'),
                    TARGET['image'].replace('-rootless', ''), 'docker.io/library/forgejo:16@sha256:' + 'a' * 64):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                fixture.archive.validate_manifest({**fixture.manifest, 'image': pin})
