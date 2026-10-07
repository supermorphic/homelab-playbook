import importlib.util
from pathlib import Path
import unittest
import hashlib
from unittest.mock import patch


MODULE = Path(__file__).parents[2] / 'roles/forgejo_runner/files/registration.py'
spec = importlib.util.spec_from_file_location('deployment_registration', MODULE)
module = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Server:
    def __init__(self):
        self.rows = []
        self.posts = 0
        self.lost_response = False
        self.calls = []

    def __call__(self, path, *, method='GET', payload=None):
        self.calls.append((path, method))
        if path == '/repos/example/project':
            return {'id': 7}
        if method == 'GET':
            return {'runners': list(self.rows)}
        if method == 'POST':
            self.posts += 1
            row = dict(payload, id=23, repo_id=7)
            self.rows.append(row)
            if self.lost_response:
                raise TimeoutError('private-credential')
            return {'id': 23, 'uuid': 'synthetic-uuid', 'token': 'synthetic-private-token'}
        if method == 'DELETE':
            self.rows = [row for row in self.rows if str(row['id']) != path.rsplit('/', 1)[1]]
            return None
        raise AssertionError('Unexpected operation')


class RegistrationTests(unittest.TestCase):
    generation = 'a' * 32

    def test_pending_selection_skips_the_other_workers_reserved_job(self):
        rows = [{'handle': 'first-job'}, {'handle': 'second-job'}]
        client = module.RegistrationClient('https://forge.example', 'example/project',
                                          'synthetic', request=lambda *a, **kw: rows)
        first = hashlib.sha256(b'first-job').hexdigest()
        self.assertEqual('second-job', client.pending_job('homelab-podman-amd64', excluded={first}))
        second = hashlib.sha256(b'second-job').hexdigest()
        self.assertIsNone(client.pending_job('homelab-podman-amd64', excluded={first, second}))

    def setUp(self):
        self.server = Server()
        self.client = module.RegistrationClient('https://forge.example', 'example/project',
                                                'synthetic-authority', request=self.server)

    def test_ephemeral_repository_registration_and_retirement(self):
        registration = self.client.enroll('example/project', 'playbook', self.generation)
        self.assertEqual(registration.id, 23)
        self.assertEqual(registration.token, 'synthetic-private-token')
        self.assertNotIn(registration.token, repr(registration))
        self.assertNotIn('synthetic-authority', repr(self.client))
        self.assertEqual(self.server.rows[0]['ephemeral'], True)
        self.client.retire(registration)
        self.assertEqual(self.server.rows, [])
        self.client.retire(registration)

    def test_slow_pagination_stops_before_consuming_the_cleanup_deadline(self):
        elapsed = [0]
        calls = []
        def request(path, **keywords):
            calls.append(path)
            elapsed[0] += 10
            return [{'id': index + 1} for index in range(50)]
        client = module.RegistrationClient('https://forge.example', 'example/project', 'synthetic', request=request)
        with patch.object(module.time, 'monotonic', lambda: elapsed[0]):
            with self.assertRaises(module.RegistrationUncertain):
                client.rows()
        self.assertLessEqual(len(calls), 2)

    def test_wrong_repository_rejected_without_a_request(self):
        with self.assertRaises(ValueError):
            self.client.enroll('example/other', 'playbook', self.generation)
        self.assertEqual(self.server.calls, [])

    def test_lost_success_response_reconciles_without_retry(self):
        self.server.lost_response = True
        with self.assertRaises(module.RegistrationUncertain) as caught:
            self.client.enroll('example/project', 'playbook', self.generation)
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(self.server.rows, [])
        self.assertNotIn('private-credential', str(caught.exception))

    def test_ambiguous_identity_is_preserved(self):
        registration = self.client.enroll('example/project', 'playbook', self.generation)
        self.server.rows.append(dict(self.server.rows[0], id=24))
        with self.assertRaises(module.RegistrationUncertain):
            self.client.retire(registration)
        self.assertEqual(len(self.server.rows), 2)

    def test_wrong_owner_is_preserved(self):
        registration = self.client.enroll('example/project', 'playbook', self.generation)
        self.server.rows[0]['repo_id'] = 8
        with self.assertRaises(module.RegistrationUncertain):
            self.client.retire(registration)
        self.assertEqual(len(self.server.rows), 1)

    def test_stale_registration_blocks_new_admission(self):
        self.client.enroll('example/project', 'playbook', self.generation)
        with self.assertRaises(module.RegistrationUncertain):
            self.client.enroll('example/project', 'playbook', self.generation)
        self.assertEqual(self.server.posts, 1)

    def test_unsafe_url_and_names(self):
        for url in ['http://forge.example', 'https://secret@forge.example',
                    'https://forge.example?token=secret', 'https://forge.example/path']:
            with self.assertRaises(ValueError):
                module.RegistrationClient(url, 'example/project', 'synthetic')
        for slot in ['../../other', '', 'MixedCase']:
            with self.assertRaises(ValueError):
                self.client.enroll('example/project', slot, self.generation)
        self.assertEqual(self.server.calls, [])

    def test_queue_poll_is_observational_and_does_not_register_an_idle_slot(self):
        calls = []
        def request(path, *, method='GET', payload=None):
            calls.append((path, method, payload))
            return []
        client = module.RegistrationClient('https://forge.example', 'example/project',
                                           'synthetic-authority', request=request)
        self.assertIsNone(client.pending_job('homelab-podman-amd64'))
        self.assertEqual([('/repos/example/project/actions/runners/jobs?labels=homelab-podman-amd64',
                          'GET', None)], calls)
        with self.assertRaises(ValueError):
            client.pending_job('label&other=secret')
        self.assertEqual(1, len(calls))


if __name__ == '__main__':
    unittest.main()
