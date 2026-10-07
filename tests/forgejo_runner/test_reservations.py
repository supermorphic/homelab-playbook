import copy
import importlib.util
from pathlib import Path
import unittest

from test_policy import slot

path = Path(__file__).parents[2] / 'roles/podman_foundation/filter_plugins/identity.py'
spec = importlib.util.spec_from_file_location('reserved_foundation', path)
foundation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(foundation)


class ReservationTests(unittest.TestCase):
    def reservation(self):
        return {'schema': 1, 'accounts': [slot()['worker'], slot()['controller']]}

    def test_future_foundation_accounts_cannot_adopt_reserved_runner_authority(self):
        reserved = self.reservation()
        unrelated = slot(1)['worker']
        self.assertEqual([unrelated], foundation.validate_reservations([unrelated], reserved))
        for field in ('name', 'uid', 'gid', 'subuid_start', 'subgid_start'):
            desired = copy.deepcopy(unrelated)
            desired[field] = reserved['accounts'][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                foundation.validate_reservations([desired], reserved)

    def test_reserved_mapping_cannot_overlap_a_new_primary_identity(self):
        account = slot(1)['worker']
        account['uid'] = self.reservation()['accounts'][0]['subuid_start'] + 1
        with self.assertRaises(ValueError):
            foundation.validate_reservations([account], self.reservation())

    def test_new_mapping_cannot_include_reserved_primary_identity(self):
        account = slot(1)['worker']
        account['subgid_start'] = self.reservation()['accounts'][0]['gid']
        with self.assertRaises(ValueError):
            foundation.validate_reservations([account], self.reservation())

    def test_invalid_or_duplicate_reservation_is_not_ignored(self):
        for reservation in ({}, {'schema': 2, 'accounts': []},
                            {'schema': 1, 'accounts': [slot()['worker']] * 2}):
            with self.subTest(reservation=reservation), self.assertRaises(ValueError):
                foundation.validate_reservations([], reservation)

    def test_no_reserved_identities_preserves_existing_foundation_behavior(self):
        desired = [slot()['worker']]
        self.assertEqual(desired, foundation.validate_reservations(desired, {'schema': 1, 'accounts': []}))
