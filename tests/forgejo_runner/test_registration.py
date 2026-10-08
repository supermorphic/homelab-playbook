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
        if path == '/user':
            return {'id': 7, 'login': 'example'}
        if path == '/orgs/example':
            return {'id': 7, 'username': 'example'}
        if not path.startswith(('/user/actions/runners', '/orgs/example/actions/runners')):
            raise AssertionError('Unexpected scope')
        if method == 'GET':
            return {'runners': list(self.rows)}
        if method == 'POST':
            self.posts += 1
            row = dict(payload, id=23, owner_id=7, repo_id=0)
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
        rows = [{'handle': 'first-job', 'owner_id': 7, 'repo_id': 18},
                {'handle': 'second-job', 'owner_id': 7, 'repo_id': 18}]
        client = module.RegistrationClient('https://forge.example', 'user:example',
            'synthetic', request=lambda path, **kw: {'id': 7, 'login': 'example'} if path == '/user' else rows)
        first = hashlib.sha256(b'first-job').hexdigest()
        self.assertEqual('second-job', client.pending_job('homelab-podman-amd64', excluded={first}))
        second = hashlib.sha256(b'second-job').hexdigest()
        self.assertIsNone(client.pending_job('homelab-podman-amd64', excluded={first, second}))

    def setUp(self):
        self.server = Server()
        self.client = module.RegistrationClient('https://forge.example', 'user:example',
                                                'synthetic-authority', request=self.server)

    def test_ephemeral_owner_registration_and_retirement(self):
        registration = self.client.enroll('user:example', 'playbook', self.generation)
        self.assertEqual(registration.id, 23)
        self.assertEqual(registration.token, 'synthetic-private-token')
        self.assertNotIn(registration.token, repr(registration))
        self.assertNotIn('synthetic-authority', repr(self.client))
        self.assertEqual(self.server.rows[0]['ephemeral'], True)
        self.assertEqual(7, self.server.rows[0]['owner_id'])
        self.assertEqual(0, self.server.rows[0]['repo_id'])
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
        client = module.RegistrationClient('https://forge.example', 'user:example', 'synthetic', request=request)
        with patch.object(module.time, 'monotonic', lambda: elapsed[0]):
            with self.assertRaises(module.RegistrationUncertain):
                client.rows()
        self.assertLessEqual(len(calls), 2)

    def test_wrong_scope_rejected_without_a_request(self):
        with self.assertRaises(ValueError):
            self.client.enroll('user:other', 'playbook', self.generation)
        self.assertEqual(self.server.calls, [])

    def test_lost_success_response_reconciles_without_retry(self):
        self.server.lost_response = True
        with self.assertRaises(module.RegistrationUncertain) as caught:
            self.client.enroll('user:example', 'playbook', self.generation)
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(self.server.rows, [])
        self.assertNotIn('private-credential', str(caught.exception))

    def test_ambiguous_identity_is_preserved(self):
        registration = self.client.enroll('user:example', 'playbook', self.generation)
        self.server.rows.append(dict(self.server.rows[0], id=24))
        with self.assertRaises(module.RegistrationUncertain):
            self.client.retire(registration)
        self.assertEqual(len(self.server.rows), 2)

    def test_wrong_owner_is_preserved(self):
        registration = self.client.enroll('user:example', 'playbook', self.generation)
        self.server.rows[0]['owner_id'] = 8
        with self.assertRaises(module.RegistrationUncertain):
            self.client.retire(registration)
        self.assertEqual(len(self.server.rows), 1)

    def test_stale_registration_blocks_new_admission(self):
        self.client.enroll('user:example', 'playbook', self.generation)
        with self.assertRaises(module.RegistrationUncertain):
            self.client.enroll('user:example', 'playbook', self.generation)
        self.assertEqual(self.server.posts, 1)

    def test_unsafe_url_and_names(self):
        for url in ['http://forge.example', 'https://secret@forge.example',
                    'https://forge.example?token=secret', 'https://forge.example/path']:
            with self.assertRaises(ValueError):
                module.RegistrationClient(url, 'user:example', 'synthetic')
        for slot in ['../../other', '', 'MixedCase']:
            with self.assertRaises(ValueError):
                self.client.enroll('user:example', slot, self.generation)
        self.assertEqual(self.server.calls, [])

    def test_queue_poll_is_observational_and_does_not_register_an_idle_slot(self):
        calls = []
        def request(path, *, method='GET', payload=None):
            calls.append((path, method, payload))
            if path == '/user':
                return {'id': 7, 'login': 'example'}
            return []
        client = module.RegistrationClient('https://forge.example', 'user:example',
                                           'synthetic-authority', request=request)
        self.assertIsNone(client.pending_job('homelab-podman-amd64'))
        self.assertEqual([('/user', 'GET', None),
                         ('/user/actions/runners/jobs?labels=homelab-podman-amd64',
                          'GET', None)], calls)
        with self.assertRaises(ValueError):
            client.pending_job('label&other=secret')
        self.assertEqual(2, len(calls))

    def test_future_repository_is_served_without_changing_the_owner_scope(self):
        rows = [{'handle': 'new-repo-job', 'repo_id': 902, 'owner_id': 7}]
        client = module.RegistrationClient('https://forge.example', 'user:example', 'synthetic',
            request=lambda path, **kw: {'id': 7, 'login': 'example'} if path == '/user' else rows)
        self.assertEqual('new-repo-job', client.pending_job('homelab-podman-amd64'))

    def test_empty_owner_queue_null_response_does_not_allocate_a_job(self):
        # Forgejo 15 serializes its empty pending-job slice as JSON null.
        client = module.RegistrationClient('https://forge.example', 'user:example', 'synthetic',
            request=lambda path, **kw: {'id': 7, 'login': 'example'} if path == '/user' else None)
        self.assertIsNone(client.pending_job('homelab-podman-amd64'))

    def test_malformed_queue_response_is_rejected_before_admission(self):
        for rows in ({'error': 'unavailable'}, 'unavailable', [None],
                     [{'handle': 'job', 'owner_id': True, 'repo_id': 7}],
                     [{'handle': 'job', 'owner_id': 7, 'repo_id': 0}]):
            client = module.RegistrationClient('https://forge.example', 'user:example', 'synthetic',
                request=lambda path, **kw: {'id': 7, 'login': 'example'} if path == '/user' else rows)
            with self.subTest(rows=rows), self.assertRaises(module.RegistrationUncertain):
                client.pending_job('homelab-podman-amd64')

    def test_foreign_owner_jobs_and_wrong_authenticated_user_are_denied(self):
        for identity, rows in (({'id': 7, 'login': 'other'}, []),
                               ({'id': 7, 'login': 'example'}, [{'handle': 'job', 'repo_id': 902, 'owner_id': 8}])):
            client = module.RegistrationClient('https://forge.example', 'user:example', 'synthetic',
                request=lambda path, **kw: identity if path == '/user' else rows)
            with self.subTest(identity=identity), self.assertRaises(module.RegistrationUncertain):
                client.pending_job('homelab-podman-amd64')

    def test_repository_registration_cannot_be_adopted_as_owner_registration(self):
        registration = self.client.enroll('user:example', 'playbook', self.generation)
        self.server.rows[0]['repo_id'] = 9
        with self.assertRaises(module.RegistrationUncertain):
            self.client.retire(registration)
        self.assertEqual(1, len(self.server.rows))

    def test_organization_scope_uses_organization_queue_and_registration_endpoints(self):
        client = module.RegistrationClient('https://forge.example', 'organization:example',
                                          'synthetic', request=self.server)
        registration = client.enroll('organization:example', 'playbook', self.generation)
        client.retire(registration)
        self.assertEqual([], self.server.rows)
        self.assertTrue(all(path.startswith('/orgs/example') for path, method in self.server.calls))


if __name__ == '__main__':
    unittest.main()
