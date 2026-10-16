"""Exercise the Caddy admin protocol over an actual disposable Unix socket."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import json
import socketserver
import tempfile
import threading
import unittest

import test_archive as fixtures


class CaddyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *arguments):
        pass

    def accepted_host(self):
        self.server.requests.append((self.command, dict(self.headers)))
        if self.headers.get('Host') != '':
            self.send_response(403)
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            return False
        return True

    def do_GET(self):
        if not self.accepted_host():
            return
        body = json.dumps(self.server.document).encode()
        etag = self.server.etag()
        style = self.server.style
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        if style == 'header':
            self.send_header('ETag', etag)
        elif style == 'conflicting':
            self.send_header('ETag', '"different-header-identity"')
        if style in ('header', 'missing'):
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_header('Transfer-Encoding', 'chunked')
        if style != 'undeclared':
            self.send_header('Trailer', 'ETag')
        self.end_headers()
        trailers = b'ETag: ' + etag.encode() + b'\r\n'
        if style == 'duplicate':
            trailers += b'ETag: ' + etag.encode() + b'\r\n'
        elif style == 'oversized':
            trailers = b'ETag: ' + b'x' * 65537 + b'\r\n'
        self.wfile.write(f'{len(body):x}\r\n'.encode() + body + b'\r\n0\r\n' + trailers + b'\r\n')

    def do_PATCH(self):
        if not self.accepted_host():
            return
        body = self.rfile.read(int(self.headers['Content-Length']))
        if self.headers.get('If-Match') != self.server.etag():
            self.send_response(412)
        else:
            self.server.document = json.loads(body)
            self.server.revision += 1
            self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()


class CaddyServer(socketserver.UnixStreamServer):
    def etag(self):
        return f'"/config/apps/http/servers version-{self.revision}"'


class AdmissionTransportTests(unittest.TestCase):
    def setUp(self):
        self.module = fixtures.load('admission')
        self.scratch = tempfile.TemporaryDirectory(prefix='caddy-api-')
        self.path = Path(self.scratch.name) / 'admin.sock'
        self.server = CaddyServer(str(self.path), CaddyHandler)
        self.server.document = {'private': {'routes': [{'handler': 'static_response', 'body': 'fixture'}]}}
        self.server.revision = 1
        self.server.style = 'header'
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.addCleanup(self.close)
        # The transport fixture owns its socket; production ownership validation
        # remains in Client.__init__ and is not replaced in production.
        self.client = self.module.Client.__new__(self.module.Client)
        self.client.path = self.path

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.scratch.cleanup()
        self.assertFalse(self.thread.is_alive())

    def test_config_read_uses_empty_unix_host_and_header_identity(self):
        expected = deepcopy(self.server.document)
        document, etag = self.client.get('/config/apps/http/servers')
        self.assertEqual(expected, document)
        self.assertEqual(self.server.etag(), etag)
        self.assertEqual('', self.server.requests[0][1]['Host'])

    def test_caddy_26_declared_trailer_identity_is_retained(self):
        self.server.style = 'trailer'
        document, etag = self.client.get('/config/apps/http/servers')
        self.assertEqual(self.server.document, document)
        self.assertEqual(self.server.etag(), etag)

    def test_trailer_identity_guards_a_conditional_update(self):
        self.server.style = 'trailer'
        document, etag = self.client.get('/config/apps/http/servers')
        document['private']['routes'][0]['body'] = 'maintenance'
        self.client.replace('/config/apps/http/servers', document, etag)
        self.assertEqual(document, self.server.document)
        method, headers = self.server.requests[-1]
        self.assertEqual('PATCH', method)
        self.assertEqual('', headers['Host'])
        self.assertEqual(etag, headers['If-Match'])

    def test_concurrent_change_rejects_update_without_overwriting_it(self):
        self.server.style = 'trailer'
        document, etag = self.client.get('/config/apps/http/servers')
        self.server.revision += 1
        self.server.document = {'independent': 'operator change'}
        before = deepcopy(self.server.document)
        with self.assertRaises(RuntimeError):
            self.client.replace('/config/apps/http/servers', document, etag)
        self.assertEqual(before, self.server.document)

    def test_missing_identity_is_rejected_before_any_update(self):
        self.server.style = 'missing'
        with self.assertRaises(ValueError):
            self.client.get('/config/apps/http/servers')
        self.assertEqual([('GET', '')], [(m, h.get('Host')) for m, h in self.server.requests])

    def test_invalid_trailers_are_rejected_before_any_update(self):
        for style in ('undeclared', 'duplicate', 'conflicting', 'oversized'):
            with self.subTest(style=style):
                self.server.style = style
                self.server.requests.clear()
                before = deepcopy(self.server.document)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.client.get('/config/apps/http/servers')
                self.assertEqual(before, self.server.document)
                self.assertEqual([('GET', '')], [(m, h.get('Host')) for m, h in self.server.requests])

    def test_replace_without_identity_sends_no_request(self):
        for etag in (None, ''):
            with self.subTest(etag=etag), self.assertRaises(ValueError):
                self.client.replace('/config/apps/http/servers', {}, etag)
        self.assertEqual([], self.server.requests)
