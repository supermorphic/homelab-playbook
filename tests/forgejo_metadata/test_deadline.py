"""Slow backfill must stop before late writes and resume without duplicates."""
import copy
import importlib
from pathlib import Path
import tempfile
import unittest
from support import CONFIG, ISSUE, FakeSource, FakeDestination
from forgejo_metadata.model import load_config
from forgejo_metadata.reconcile import reconcile
from forgejo_metadata.state import StateStore


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = StateStore(Path(temporary.name) / 'state.json', 'fixture')
        self.store.bootstrap('initial', 'fixture')
        self.mapping = load_config(CONFIG)[0]
        self.source = FakeSource()
        self.source.data.labels = []
        self.source.data.milestones = []
        self.source.data.issues = [dict(copy.deepcopy(ISSUE), labels=[], milestone=None, state='open')]
        self.source.data.comments = {}
        self.destination = FakeDestination()
        self.now = 0

    def deadline(self):
        try:
            module = importlib.import_module('forgejo_metadata.deadline')
        except ModuleNotFoundError:
            self.fail('deadline-aware write admission is missing')
        return module.RunDeadline(self.now + 100, clock=lambda: self.now)

    def run_mirror(self):
        return reconcile(self.mapping, self.source, self.destination, self.store, 100,
                         deadline=self.deadline())

    def test_slow_initial_reads_do_not_start_a_create_near_deadline(self):
        inventory = self.destination.inventory
        def slow(mapping):
            self.now += 90
            return inventory(mapping)
        self.destination.inventory = slow
        result = self.run_mirror()
        self.assertIn('run_budget_exhausted', result.errors)
        self.assertIsNone(result.converged_at)
        self.assertEqual([], self.destination.writes)
        self.assertEqual({}, self.store.load()['pending'])

    def test_recheck_after_slow_prewrite_discovery_prevents_post(self):
        inventory = self.destination.inventory
        calls = [0]
        def slow(mapping):
            calls[0] += 1
            self.now += 1 if calls[0] == 1 else 90
            return inventory(mapping)
        self.destination.inventory = slow
        result = self.run_mirror()
        self.assertIn('run_budget_exhausted', result.errors)
        self.assertEqual([], self.destination.writes)
        self.assertEqual({}, self.store.load()['pending'])

    def test_slow_backfill_keeps_progress_then_resumes_without_duplicates(self):
        self.source.data.comments = {23: [
            {'id': identity, 'body': 'Fixture comment', 'user': {'login': 'fixture'},
             'created_at': '2026-01-02T00:00:00Z'} for identity in range(24, 32)]}
        for name, cost in (('preflight', 1), ('inventory', 2), ('create', 6), ('read', 1)):
            original = getattr(self.destination, name)
            def delayed(*args, original=original, cost=cost, **kwargs):
                self.now += cost
                return original(*args, **kwargs)
            setattr(self.destination, name, delayed)
        first = self.run_mirror()
        self.assertGreater(first.writes, 0)
        self.assertLess(first.writes, 9)
        self.assertGreater(first.backlog, 0)
        self.assertIsNone(first.converged_at)
        self.assertEqual({}, self.store.load()['pending'])
        second = self.run_mirror()
        self.assertEqual([], second.errors)
        self.assertIsNotNone(second.converged_at)
        self.assertEqual(1, len(self.destination.data.issues))
        self.assertEqual(8, sum(len(rows) for rows in self.destination.data.comments.values()))
        self.assertEqual(9, sum(method == 'POST' for method, _ in self.destination.writes))
        self.assertEqual({}, self.store.load()['pending'])

    def test_late_update_preserves_existing_destination_fields(self):
        self.assertEqual([], reconcile(self.mapping, self.source, self.destination,
                                       self.store, 100).errors)
        self.source.data.issues[0]['title'] = 'New title'
        before = copy.deepcopy(self.destination.data.issues)
        self.destination.writes.clear()
        inventory = self.destination.inventory
        calls = [0]
        def slow(mapping):
            calls[0] += 1
            self.now += 1 if calls[0] == 1 else 45
            return inventory(mapping)
        self.destination.inventory = slow
        result = self.run_mirror()
        self.assertIn('run_budget_exhausted', result.errors)
        self.assertEqual(before, self.destination.data.issues)
        self.assertEqual([], self.destination.writes)

    def test_accepted_create_with_lost_response_is_rediscovered_on_resume(self):
        self.destination.lost = True
        first = self.run_mirror()
        self.assertTrue(first.errors)
        self.assertEqual(1, len(self.store.load()['pending']))
        self.assertEqual([], self.run_mirror().errors)
        self.assertEqual(1, len(self.destination.data.issues))
        self.assertEqual(1, sum(method == 'POST' for method, _ in self.destination.writes))
        self.assertEqual({}, self.store.load()['pending'])
