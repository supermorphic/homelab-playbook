"""Missing and failed native mirror evidence must never be healthy."""
from datetime import datetime, timedelta, timezone
import importlib
import io
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(Path('scripts/forgejo/mirror.py').is_file(), 'native mirror fixture missing')
        self.mirror = importlib.import_module('scripts.forgejo.mirror')
        self.now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)

    def test_no_configuration_or_first_success_is_not_healthy(self):
        self.assertEqual('unconfigured', self.mirror.evaluate_status(False, None, None, self.now))
        self.assertEqual('pending', self.mirror.evaluate_status(True, None, None, self.now))

    def test_failure_wins_over_recent_success(self):
        self.assertEqual('failed', self.mirror.evaluate_status(True, self.now, 'synthetic failure', self.now))

    def test_stale_boundary_and_future_evidence(self):
        evaluate = self.mirror.evaluate_status
        self.assertEqual('healthy', evaluate(True, self.now - timedelta(hours=36), None, self.now))
        self.assertEqual('stale', evaluate(True, self.now - timedelta(hours=36, seconds=1), None, self.now))
        with self.assertRaises(ValueError):
            evaluate(True, self.now + timedelta(seconds=1), None, self.now)
        with self.assertRaises(ValueError):
            evaluate(True, self.now.replace(tzinfo=None), None, self.now)

    def test_fixture_timezone_moves_next_two_am_without_changing_clock(self):
        zone = ZoneInfo.from_file(io.BytesIO(self.mirror.fixture_timezone(self.now, seconds=20)))
        local = self.now.astimezone(zone)
        self.assertEqual((1, 59, 40), (local.hour, local.minute, local.second))
        at_scan = (self.now + timedelta(seconds=20)).astimezone(zone)
        self.assertEqual((2, 0, 0), (at_scan.hour, at_scan.minute, at_scan.second))
        self.assertEqual(self.now.timestamp(), local.timestamp())

    def test_native_rows_are_sanitized_and_policy_checked(self):
        row = {'id': 1, 'last_attempt': int(self.now.timestamp()), 'failed': False,
               'sync_on_commit': False, 'interval_seconds': 28800, 'https': True}
        status = self.mirror.summarize([row], self.now)
        self.assertEqual('healthy', status[0]['status'])
        self.assertNotIn('remote_address', status[0])
        for field, value in (('sync_on_commit', True), ('interval_seconds', 900), ('https', False)):
            with self.assertRaises(ValueError):
                self.mirror.summarize([dict(row, **{field: value})], self.now)

    def test_cron_metadata_uses_exact_fixture_offset_when_json_rounds_seconds(self):
        now = self.now + timedelta(seconds=17)
        tzif = self.mirror.fixture_timezone(now)
        # Go's RFC3339 JSON represents offsets only to whole minutes.
        parsed = self.mirror.cron_instant('2026-09-30T02:00:00-10:00', tzif)
        self.assertEqual(now + timedelta(seconds=20), parsed.astimezone(timezone.utc))


if __name__ == '__main__':
    unittest.main()
