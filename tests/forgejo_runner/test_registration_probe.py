"""Fixture API failures must not expose credentials or response bodies."""

import importlib.util
from pathlib import Path
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
import tempfile
import configparser
import json
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit


@contextmanager
def runner_api(rows, *, retain_deleted=False):
    state = {'rows': rows.copy(), 'deleted': [], 'created': 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            page = int(query.get('page', ['1'])[0])
            limit = int(query.get('limit', ['50'])[0])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(state['rows'][(page - 1) * limit:page * limit]).encode())

        def do_DELETE(self):
            identity = int(self.path.rsplit('/', 1)[-1])
            state['deleted'].append(identity)
            if not retain_deleted:
                state['rows'] = [row for row in state['rows'] if row['id'] != identity]
            self.send_response(204)
            self.end_headers()

        def do_POST(self):
            state['created'] += 1
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/forgejo_runner/registration_probe.py"


class RegistrationProbeTests(unittest.TestCase):
    def reconciliation(self):
        from scripts.forgejo_runner import registration_probe
        self.assertTrue(hasattr(registration_probe, 'retire_uncertain_registration'),
                        'Lost-response reconciliation is not implemented')
        return registration_probe.retire_uncertain_registration

    def test_lost_response_retires_only_the_exact_owned_registration_without_retry(self):
        reconcile = self.reconciliation()
        unrelated = {'id': 18, 'name': 'unrelated', 'description': 'other',
                     'repo_id': 7, 'ephemeral': True}
        owned = {'id': 19, 'name': 'fixture-controller', 'description': 'fixture-run',
                 'repo_id': 7, 'ephemeral': True}
        with runner_api([unrelated, owned]) as (url, state):
            self.assertEqual(19, reconcile(url, '/repos/fixture/a/actions/runners',
                                          'synthetic-token', repo_id=7,
                                          name='fixture-controller', marker='fixture-run'))
            self.assertEqual([unrelated], state['rows'])
            self.assertEqual([19], state['deleted'])
            self.assertEqual(0, state['created'])

    def test_uncertain_registration_lookup_rejects_ambiguous_or_changed_ownership(self):
        reconcile = self.reconciliation()
        owned = {'id': 19, 'name': 'fixture-controller', 'description': 'fixture-run',
                 'repo_id': 7, 'ephemeral': True}
        cases = [[], [owned, dict(owned, id=20)], [dict(owned, repo_id=8)],
                 [dict(owned, description='other')], [dict(owned, ephemeral=False)],
                 [dict(owned, id='19')]]
        for rows in cases:
            with self.subTest(rows=rows), runner_api(rows) as (url, state):
                with self.assertRaises(RuntimeError):
                    reconcile(url, '/repos/fixture/a/actions/runners', 'synthetic-token',
                              repo_id=7, name='fixture-controller', marker='fixture-run')
                self.assertEqual([], state['deleted'])
                self.assertEqual(0, state['created'])

    def test_uncertain_registration_is_found_after_unrelated_first_page(self):
        reconcile = self.reconciliation()
        unrelated = [{'id': i + 1, 'name': f'unrelated-{i}', 'description': 'other',
                      'repo_id': 7, 'ephemeral': True} for i in range(50)]
        owned = {'id': 80, 'name': 'fixture-controller', 'description': 'fixture-run',
                 'repo_id': 7, 'ephemeral': True}
        with runner_api(unrelated + [owned]) as (url, state):
            self.assertEqual(80, reconcile(url, '/repos/fixture/a/actions/runners',
                                          'synthetic-token', repo_id=7,
                                          name='fixture-controller', marker='fixture-run'))
            self.assertEqual(unrelated, state['rows'])

    def test_retirement_requires_observed_absence_after_delete(self):
        reconcile = self.reconciliation()
        owned = {'id': 19, 'name': 'fixture-controller', 'description': 'fixture-run',
                 'repo_id': 7, 'ephemeral': True}
        with runner_api([owned], retain_deleted=True) as (url, state):
            with self.assertRaises(RuntimeError):
                reconcile(url, '/repos/fixture/a/actions/runners', 'synthetic-token',
                          repo_id=7, name='fixture-controller', marker='fixture-run')
            self.assertEqual([owned], state['rows'])
            self.assertEqual(0, state['created'])

    def test_actions_fixture_preserves_the_ini_global_section(self):
        spec = importlib.util.spec_from_file_location("registration_probe", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from scripts.forgejo_runner.fixture import RunnerRun
        with tempfile.TemporaryDirectory() as directory:
            application = module.ActionsApplication.__new__(module.ActionsApplication)
            application.config = Path(directory)
            application.db_password = "synthetic-database-value"
            application.run = RunnerRun(run_id="fixture123")
            application.access_log = False
            try:
                rendered = application.configuration()
            except configparser.Error:
                self.fail("Actions fixture cannot parse the managed INI global section")
            parsed = configparser.ConfigParser(interpolation=None, allow_unnamed_section=True)
            parsed.read_string(rendered)
            self.assertTrue(parsed.getboolean("actions", "ENABLED"))
            self.assertEqual("Forgejo", parsed[configparser.UNNAMED_SECTION]["APP_NAME"])

    def test_server_error_does_not_expose_the_response_or_token(self):
        self.assertTrue(MODULE.is_file(), "scoped registration probe is missing")
        spec = importlib.util.spec_from_file_location("registration_probe", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"synthetic-private-response")
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(RuntimeError) as caught:
                module.token_request(f"http://127.0.0.1:{server.server_port}",
                                     "/repos/fixture/a/actions/runners", "fixture-private-token",
                                     method="POST", payload={"name": "fixture", "ephemeral": True})
            self.assertIn("403", str(caught.exception))
            self.assertNotIn("fixture-private-token", str(caught.exception))
            self.assertNotIn("synthetic-private-response", str(caught.exception))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
