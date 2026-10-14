"""Conditional maintenance of one declared route over Caddy's protected socket."""
from copy import deepcopy
import http.client
import json
from pathlib import Path
import pwd
import re
import socket
import stat
from urllib.parse import quote


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=15)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class Client:
    def __init__(self):
        self.path = Path('/run/caddy/admin.sock')
        metadata = self.path.lstat()
        if (not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_uid != pwd.getpwnam('caddy').pw_uid or metadata.st_mode & 0o002):
            raise ValueError('Caddy administration requires its protected local socket')

    def request(self, method, path, body=None, etag=None):
        headers = {'Content-Type': 'application/json'}
        if etag is not None:
            headers['If-Match'] = etag
        connection = UnixConnection(self.path)
        try:
            connection.request(method, path, None if body is None else json.dumps(body).encode(), headers)
            response = connection.getresponse()
            content = response.read(16 * 1024 * 1024 + 1)
            if response.status != 200 or len(content) > 16 * 1024 * 1024:
                raise RuntimeError('Caddy route operation did not complete safely')
            return json.loads(content) if content else None, response.getheader('Etag')
        finally:
            connection.close()

    def get(self, path):
        content, etag = self.request('GET', path)
        if not etag:
            raise ValueError('Caddy did not provide conditional configuration identity')
        return content, etag

    def replace(self, path, body, etag):
        self.request('PATCH', path, body, etag)


def routes(client, hostname):
    servers, etag = client.get('/config/apps/http/servers')
    found = []
    def visit(value, path):
        if isinstance(value, dict):
            match = value.get('match', [])
            if any(isinstance(row, dict) and hostname in row.get('host', []) for row in match):
                if len(match) != 1 or match[0].get('host') != [hostname]:
                    raise ValueError('maintenance requires one exact hostname route')
                found.append((path, value))
                return
            for name, child in value.items():
                visit(child, path + '/' + quote(name, safe=''))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + '/' + str(index))
    visit(servers, '/config/apps/http/servers')
    if len(found) != 1:
        raise ValueError('Forgejo hostname does not identify one independent Caddy route')
    return found[0][0], deepcopy(found[0][1]), etag


def gate_for(original, port, identifier):
    if re.fullmatch(r'[0-9a-f]{32}', identifier) is None or type(port) is not int:
        raise ValueError('invalid maintenance target identity')
    gate = deepcopy(original)
    found = []
    def visit(value):
        if isinstance(value, dict):
            if value.get('handler') == 'reverse_proxy':
                if value.get('upstreams') != [{'dial': '127.0.0.1:' + str(port)}]:
                    raise ValueError('Forgejo route has an undeclared upstream')
                found.append(value)
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(gate)
    if len(found) != 1:
        raise ValueError('maintenance requires exactly one declared loopback proxy handler')
    found[0].clear()
    found[0].update(handler='static_response', status_code=503, body='Service maintenance\n')
    gate['@id'] = 'forgejo-upgrade-' + identifier
    return gate


def prepare(client, hostname, port, identifier, persist):
    path, original, etag = routes(client, hostname)
    saved = {'hostname': hostname, 'original': original,
             'gate': gate_for(original, port, identifier)}
    persist(saved)
    finish_preparation(client, saved)
    return saved


def finish_preparation(client, saved, *, before_replace=None):
    path, current, etag = routes(client, saved['hostname'])
    if current == saved['gate']:
        return False
    if current != saved['original']:
        raise ValueError('maintenance route changed; refusing to overwrite operator configuration')
    if before_replace is not None:
        before_replace()
    client.replace(path, saved['gate'], etag)
    require_gated(client, saved)
    return True


def require_gated(client, saved):
    _, current, _ = routes(client, saved['hostname'])
    if current != saved['gate']:
        raise ValueError('maintenance route changed; repeat attended preparation and queue evidence')


def restore(client, saved):
    path, current, etag = routes(client, saved['hostname'])
    if current == saved['original']:
        return False
    if current != saved['gate']:
        raise ValueError('maintenance route changed; refusing to overwrite operator configuration')
    client.replace(path, saved['original'], etag)
    _, restored, _ = routes(client, saved['hostname'])
    if restored != saved['original']:
        raise RuntimeError('restored route verification failed')
    return True
