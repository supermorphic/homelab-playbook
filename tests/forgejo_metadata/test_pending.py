"""Pending-create inspection never resolves uncertainty or publishes content."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from support import CONFIG, FakeDestination
from forgejo_metadata.model import MirrorError, SourceKey, enrollment_fingerprint, load_config
from forgejo_metadata.state import StateStore


class PendingPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.mapping = load_config(CONFIG)[0]
        self.store = StateStore(Path(temporary.name) / 'state.json',
                                enrollment_fingerprint([self.mapping]))
        self.store.bootstrap('initial', 'fixture')
        self.key = SourceKey('fixture', 7, 'comment', 24)
        self.store.begin_create(self.key)
        self.destination = FakeDestination()
        self.destination.data.issues = [{
            'id': 80, 'number': 80, 'user': {'id': 11},
            'body': 'private issue text\n<!-- forgejo-mirror:v=1;src=fixture;repo=7;kind=issue;id=23 -->'}]
        self.comment = {
            'id': 81, 'user': {'id': 11},
            'body': 'private comment text\n<!-- forgejo-mirror:v=1;src=fixture;repo=7;kind=comment;id=24 -->'}
        self.destination.data.comments = {80: [self.comment]}

    def plan(self):
        try:
            module = importlib.import_module('forgejo_metadata.pending')
        except ModuleNotFoundError:
            self.fail('read-only pending-create report is missing')
        return module.build_pending_plan([self.mapping], self.store,
                                         lambda mapping: (None, self.destination))

    def test_found_comment_is_reported_without_journal_or_destination_writes(self):
        before = self.store.path.read_bytes()
        plan = self.plan()
        self.assertEqual([{'key': {'instance': 'fixture', 'repository_id': 7,
                                  'kind': 'comment', 'object_id': 24},
                           'status': 'found', 'locator': '/issues/comments/81'}], plan['pending'])
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual([], self.destination.writes)
        self.assertNotIn('private', json.dumps(plan))

    def test_absence_is_not_permission_to_repeat_create(self):
        self.destination.data.comments = {}
        before = self.store.path.read_bytes()
        plan = self.plan()
        self.assertEqual('not_found', plan['pending'][0]['status'])
        self.assertNotIn('locator', plan['pending'][0])
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(1, len(self.store.load()['pending']))
        self.assertEqual([], self.destination.writes)

    def test_duplicate_or_wrong_actor_never_selects_a_destination(self):
        for rows in ([self.comment, dict(self.comment, id=82)],
                     [dict(self.comment, user={'id': 99})]):
            with self.subTest(rows=len(rows)):
                self.destination.data.comments = {80: copy.deepcopy(rows)}
                before = self.store.path.read_bytes()
                plan = self.plan()
                self.assertEqual('conflict', plan['pending'][0]['status'])
                self.assertNotIn('locator', plan['pending'][0])
                self.assertEqual(before, self.store.path.read_bytes())
                self.assertEqual([], self.destination.writes)

    def test_incomplete_inventory_cannot_establish_absence(self):
        self.destination.data.complete = False
        with self.assertRaisesRegex(MirrorError, 'incomplete_inventory'):
            self.plan()
        self.assertEqual(1, len(self.store.load()['pending']))

    def test_unenrolled_intent_is_not_hidden_by_current_mapping_selection(self):
        self.store.begin_create(SourceKey('fixture', 8, 'issue', 99))
        before = self.store.path.read_bytes()
        plan = self.plan()
        self.assertEqual(2, len(plan['pending']))
        self.assertEqual({'key': {'instance': 'fixture', 'repository_id': 8,
                                  'kind': 'issue', 'object_id': 99},
                          'status': 'not_enrolled'}, plan['pending'][1])
        self.assertEqual(before, self.store.path.read_bytes())

    def test_retry_deadline_is_observed_without_refreshing_it(self):
        self.store.defer(time.time() + 3600, 'destination')
        before = self.store.path.read_bytes()
        def forbidden(mapping):
            self.fail('throttled destination was contacted')
        self.destination.preflight = forbidden
        with self.assertRaisesRegex(MirrorError, 'rate_limited'):
            self.plan()
        self.assertEqual(before, self.store.path.read_bytes())
