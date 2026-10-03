"""Fixture API failures must not expose credentials or response bodies."""

import importlib.util
from pathlib import Path
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
import tempfile
import configparser

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/forgejo_runner/registration_probe.py"


class RegistrationProbeTests(unittest.TestCase):
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
