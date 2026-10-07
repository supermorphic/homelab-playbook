"""Admission is guarded by independently observed empty state and cleanup."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


source = Path(__file__).parents[2] / 'roles/forgejo_runner/files/lifecycle.py'
spec = importlib.util.spec_from_file_location('supervisor_lifecycle', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Backend:
    def __init__(self, events):
        self.events = events
        self.present = False
        self.fail_job = False
        self.fail_cleanup = False
        self.identity = {'device': 1, 'inode': 2, 'uid': 0, 'gid': 0,
                         'mode': 0o100600, 'nlink': 1, 'invocation': 'b'*32}

    def assert_empty(self):
        self.events.append('observe-empty')
        if self.present:
            raise RuntimeError('Resource remains')

    def prepare(self, generation):
        self.events.append('prepare')
        self.present = True
        return self.identity

    def run(self, registration):
        self.events.append('job')
        if self.fail_job:
            raise TimeoutError('synthetic private diagnostic')

    def destroy(self, generation, expected):
        self.events.append('destroy')
        if self.fail_cleanup:
            raise OSError('synthetic private cleanup diagnostic')
        self.present = False


class Enrollment:
    def __init__(self, events, backend):
        self.events, self.backend = events, backend
        self.present = None

    def enroll(self, repository, slot, generation):
        self.events.append('enroll')
        self.present = type('Registration', (), {'id': 7, 'name': slot+'-'+generation,
                                               'repository': repository})()
        return self.present

    def lookup(self, repository, slot, generation):
        return self.present

    def retire(self, registration):
        self.events.append('retire')
        # Registration retirement must precede destructive reuse.
        if not self.backend.present:
            raise AssertionError('Runtime was removed before admission authority')
        self.present = None


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.events = []
        self.backend = Backend(self.events)
        self.enrollment = Enrollment(self.events, self.backend)
        self.config = {'name': 'playbook', 'repository': 'example/project'}
        self.store = module.CheckpointStore(Path(self.directory.name), authority_uid=os.getuid())

    def run_cycle(self, **kwargs):
        with self.store as store:
            return module.run_slot(self.config, self.backend, self.enrollment, store, **kwargs)

    def test_success_then_reuse_requires_independent_absence(self):
        self.assertEqual(0, self.run_cycle())
        self.assertFalse(self.backend.present)
        self.assertIsNone(self.enrollment.present)
        with self.store as store:
            self.assertEqual('clean', store.read()['phase'])
        self.assertEqual(0, self.run_cycle())
        self.assertEqual(2, self.events.count('job'))
        first_destroy = self.events.index('destroy')
        second_prepare = self.events.index('prepare', first_destroy)
        self.assertIn('observe-empty', self.events[first_destroy+1:second_prepare])

    def test_job_failure_cleans_before_reuse(self):
        self.backend.fail_job = True
        self.assertEqual(1, self.run_cycle())
        self.assertFalse(self.backend.present)
        self.assertIsNone(self.enrollment.present)
        with self.store as store:
            state = store.read()
            self.assertEqual('clean', state['phase'])
            self.assertEqual('TimeoutError', state['primary_error'])
            self.assertEqual([], state['cleanup_errors'])
        self.backend.fail_job = False
        self.assertEqual(0, self.run_cycle())

    def test_failed_cleanup_quarantines_and_blocks_next_registration(self):
        self.backend.fail_job = True
        self.backend.fail_cleanup = True
        self.assertEqual(1, self.run_cycle())
        with self.store as store:
            state = store.read()
            self.assertEqual('quarantined', state['phase'])
            self.assertEqual('TimeoutError', state['primary_error'])
            self.assertEqual(['OSError'], state['cleanup_errors'])
        self.assertEqual(1, self.run_cycle())
        self.assertEqual(1, self.events.count('enroll'))
        self.backend.fail_cleanup = False
        self.assertEqual(0, self.run_cycle(recovery_only=True))
        self.assertEqual(1, self.events.count('enroll'))

    def test_unrecorded_resource_blocks_admission(self):
        self.backend.present = True
        self.assertEqual(1, self.run_cycle())
        self.assertNotIn('enroll', self.events)
        self.assertTrue(self.backend.present)
        self.events.clear()
        self.assertEqual(1, self.run_cycle(recovery_only=True))
        self.assertEqual([], self.events)

    def test_restart_cleans_recorded_generation_before_admission(self):
        generation = 'a'*32
        self.backend.present = True
        registration = self.enrollment.enroll('example/project', 'playbook', generation)
        with self.store as store:
            store.write({'phase': 'running', 'generation': generation,
                         'allocation': self.backend.identity,
                         'registration': {'id': registration.id, 'name': registration.name,
                                          'repository': registration.repository},
                         'primary_error': None, 'cleanup_errors': []})
        self.events.clear()
        self.assertEqual(0, self.run_cycle())
        self.assertLess(self.events.index('destroy'), self.events.index('prepare'))
        self.assertLess(self.events.index('retire'), self.events.index('destroy'))

    def test_foreign_checkpoint_is_preserved(self):
        with self.store as store:
            state = module.clean_checkpoint()
            state.update(phase='quarantined', generation='a'*32,
                         registration={'id': 7, 'name': 'other-'+'a'*32, 'repository': 'other/project'})
            store.write(state)
        self.assertEqual(1, self.run_cycle(recovery_only=True))
        self.assertEqual([], self.events)


if __name__ == '__main__':
    unittest.main()
