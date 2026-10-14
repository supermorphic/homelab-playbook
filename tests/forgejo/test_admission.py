"""Maintenance changes only the exact Forgejo route and preserves concurrent changes."""
from copy import deepcopy
import unittest

import test_archive as fixtures

HOST = 'forgejo.infra.example.com'
ORIGINAL = {'match': [{'host': [HOST]}], 'handle': [
    {'handler': 'subroute', 'routes': [{'handle': [
        {'handler': 'reverse_proxy', 'upstreams': [{'dial': '127.0.0.1:18081'}]}]}]}],
    'terminal': True}
OTHER = {'match': [{'host': ['other.infra.example.com']}],
         'handle': [{'handler': 'static_response', 'body': 'independent other service'}]}


class Client:
    def __init__(self):
        self.routes = [deepcopy(OTHER), deepcopy(ORIGINAL)]
        self.version = 1
        self.saved = False

    def get(self, path):
        if path == '/config/apps/http/servers':
            result = {'private': {'routes': self.routes}}
        else:
            result = self.routes[int(path.rsplit('/', 1)[-1])]
        return deepcopy(result), str(self.version)

    def replace(self, path, body, etag):
        if not self.saved:
            raise AssertionError('original route was not durable before mutation')
        if etag != str(self.version):
            raise RuntimeError('concurrent route change')
        self.routes[int(path.rsplit('/', 1)[-1])] = deepcopy(body)
        self.version += 1


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.admission = fixtures.load('admission')
        self.client = Client()

    def persist(self, saved):
        self.client.saved = True
        self.saved = deepcopy(saved)

    def prepare(self):
        return self.admission.prepare(self.client, HOST, 18081, 'a' * 32, self.persist)

    def test_gate_and_restore_preserve_unrelated_route(self):
        saved = self.prepare()
        self.assertEqual(OTHER, self.client.routes[0])
        gate = self.client.routes[1]
        self.assertEqual([{'host': [HOST]}], gate['match'])
        response = gate['handle'][0]['routes'][0]['handle'][0]
        self.assertEqual(503, response['status_code'])
        self.assertNotIn('upstreams', response)
        self.assertEqual(ORIGINAL, self.saved['original'])
        self.assertTrue(self.admission.restore(self.client, saved))
        self.assertEqual([OTHER, ORIGINAL], self.client.routes)
        self.assertFalse(self.admission.restore(self.client, saved))

    def test_changed_maintenance_route_is_not_overwritten(self):
        saved = self.prepare()
        self.client.routes[1]['handle'][0]['routes'][0]['handle'][0]['body'] = 'operator changed route'
        before = deepcopy(self.client.routes)
        with self.assertRaises(ValueError):
            self.admission.restore(self.client, saved)
        self.assertEqual(before, self.client.routes)

    def test_ambiguous_hostname_or_unexpected_backend_is_rejected_without_change(self):
        for routes in ([ORIGINAL, ORIGINAL], [{**ORIGINAL, 'match': [{'host': [HOST, 'extra.example.com']}]}],
                       [{**ORIGINAL, 'handle': [{'handler': 'reverse_proxy',
                                               'upstreams': [{'dial': '192.0.2.1:18081'}]}]}]):
            self.client.routes = deepcopy(routes)
            before = deepcopy(self.client.routes)
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                self.prepare()
            self.assertEqual(before, self.client.routes)

    def test_route_reordering_does_not_restore_over_another_service(self):
        saved = self.prepare()
        self.client.routes.reverse()
        self.admission.restore(self.client, saved)
        self.assertEqual([ORIGINAL, OTHER], self.client.routes)

    def test_interrupted_preparation_can_finish_only_the_saved_original_route(self):
        def interrupted(saved):
            self.persist(saved)
            raise RuntimeError('controller died before PATCH')
        with self.assertRaises(RuntimeError):
            self.admission.prepare(self.client, HOST, 18081, 'a' * 32, interrupted)
        self.admission.finish_preparation(self.client, self.saved)
        self.assertEqual(self.saved['gate'], self.client.routes[1])
        self.admission.finish_preparation(self.client, self.saved)
        self.client.routes[1] = {**ORIGINAL, 'terminal': False}
        with self.assertRaises(ValueError):
            self.admission.finish_preparation(self.client, self.saved)
