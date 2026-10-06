"""Missing and failed native mirror evidence must never be healthy."""
from datetime import datetime, timedelta, timezone
import importlib
import io
import itertools
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
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

    def test_scan_waits_for_eligible_attempt_completion(self):
        before = [{'id': 1, 'last_attempt': int(self.now.timestamp()) - 9 * 3600,
                   'failed': False, 'interval_seconds': 28800}]
        completed = [dict(before[0], last_attempt=int(self.now.timestamp()))]
        snapshots = iter([before, before, before, completed])
        observed = None

        def current_rows(application, **kwargs):
            nonlocal observed
            observed = next(snapshots)
            return observed

        run = Mock(podman='podman')
        application = SimpleNamespace(app='synthetic-app', wait=lambda: None)
        with tempfile.TemporaryDirectory() as directory:
            zone_file = Path(directory).resolve() / 'MirrorFixture'
            cron_calls = 0

            def current_cron(application):
                nonlocal cron_calls
                cron_calls += 1
                zone = ZoneInfo.from_file(io.BytesIO(zone_file.read_bytes()))
                return {'schedule': '0 0 2 * * *', 'exec_times': int(cron_calls > 1),
                        'next': (self.now + timedelta(seconds=20)).astimezone(zone).isoformat()}

            with patch.object(self.mirror, 'rows', side_effect=current_rows), \
                    patch.object(self.mirror, 'cron', side_effect=current_cron), \
                    patch.object(self.mirror, 'datetime', wraps=datetime) as clock, \
                    patch.object(self.mirror.time, 'sleep'):
                clock.now.return_value = self.now
                self.mirror.scan(run, application, zone_file)
        self.assertEqual(completed, observed)

    def test_eligible_attempt_completion_has_a_deadline(self):
        before = [{'id': 1, 'last_attempt': int(self.now.timestamp()) - 9 * 3600,
                   'failed': False, 'interval_seconds': 28800}]
        with patch.object(self.mirror, 'rows', return_value=before), \
                patch.object(self.mirror.time, 'monotonic', side_effect=itertools.count(0, 16)), \
                patch.object(self.mirror.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'completion deadline'):
                self.mirror.wait_for_attempts(object(), before)

    def test_new_failure_is_a_completed_attempt(self):
        before = [{'id': 1, 'last_attempt': int(self.now.timestamp()) - 9 * 3600,
                   'failed': False, 'interval_seconds': 28800}]
        failed = [dict(before[0], failed=True, last_attempt=int(self.now.timestamp()))]
        with patch.object(self.mirror, 'rows', return_value=failed):
            self.mirror.wait_for_attempts(object(), before)


if __name__ == '__main__':
    unittest.main()
