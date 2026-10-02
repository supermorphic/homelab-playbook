"""Prove forwarded-header behavior using the stock application's access logger."""

import ipaddress
from http.cookies import SimpleCookie
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode


def verify_headers(run, application):
    headers = {
        'X-Forwarded-For': '198.51.100.77',
        'X-Real-IP': '203.0.113.88',
        'Forwarded': 'for=198.51.100.77;proto=http;host=untrusted.example.invalid',
        'X-Forwarded-Host': 'untrusted.example.invalid',
        'X-Forwarded-Proto': 'http',
        'X-WEBAUTH-USER': application.user,
    }
    repository = application.request(f'/repos/{application.user}/compatibility', extra_headers=headers)
    if repository['clone_url'] != f'https://forgejo.infra.example.com/{application.user}/compatibility.git':
        raise RuntimeError('forwarded headers changed the canonical HTTPS clone URL')
    expected = {}
    # Loopback is an upstream-default trusted source: this catches a missing
    # REVERSE_PROXY_LIMIT even when the rootless forwarder is outside that range.
    for origin in ('published', 'loopback'):
        for kind in ('baseline', 'forwarded', 'unauthenticated'):
            tag = f'{run.run_id}-{origin}-{kind}'
            path = '/api/v1/user' if kind == 'unauthenticated' else '/user/login'
            supplied = headers if kind != 'baseline' else {}
            if origin == 'published':
                request = urllib.request.Request(application.url + path + '?' + tag, headers=supplied)
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        status = response.status
                except urllib.error.HTTPError as error:
                    status = error.code
            else:
                arguments = [argument for key, value in supplied.items()
                             for argument in ('--header', f'{key}: {value}')]
                status = int(run.command([run.podman, 'exec', application.app, 'curl',
                    '--silent', '--show-error', '--max-time', '5', '--output', '/dev/null',
                    '--write-out', '%{http_code}', *arguments,
                    'http://127.0.0.1:3000' + path + '?' + tag]).stdout)
            if status not in ((401, 403) if kind == 'unauthenticated' else (200,)):
                raise RuntimeError(f'{origin}/{kind}: unexpected HTTP status {status}')
            expected[tag] = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        logs = run.command([run.podman, 'logs', application.app])
        for tag, peer in re.findall(r'forgejo-proxy-probe=(\S+) peer=(\S+)', logs.stdout + logs.stderr):
            if tag in expected:
                expected[tag] = str(ipaddress.ip_address(peer))
        if all(expected.values()):
            break
        time.sleep(0.1)
    if not all(expected.values()):
        raise RuntimeError('application client-identity evidence is incomplete')
    for origin in ('published', 'loopback'):
        peers = {expected[f'{run.run_id}-{origin}-{kind}']
                 for kind in ('baseline', 'forwarded', 'unauthenticated')}
        if len(peers) != 1 or peers & {'198.51.100.77', '203.0.113.88'}:
            raise RuntimeError('forwarded headers changed the application client identity')
    verify_web_login(application)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_web_login(application):
    """Exercise the backend requests Caddy sends for a browser's HTTPS session."""
    cookies = SimpleCookie()
    opener = urllib.request.build_opener(NoRedirect())
    public = 'https://forgejo.infra.example.com'

    def request(path, *, form=None, origin=public):
        headers = {'Host': 'forgejo.infra.example.com', 'Origin': origin,
                   'Referer': public + '/user/login', 'X-Forwarded-Proto': 'https',
                   'Cookie': '; '.join(f'{key}={value.coded_value}' for key, value in cookies.items())}
        data = urlencode(form).encode() if form is not None else None
        try:
            response = opener.open(urllib.request.Request(application.url + path, data=data,
                                                         headers=headers), timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            for value in response.headers.get_all('Set-Cookie', []):
                cookies.load(value)
            return response.status, response.headers.get('Location', '')

    status, _ = request('/user/login')
    if status != 200:
        raise RuntimeError('browser login page is unavailable')
    status, location = request('/user/login', form={'user_name': application.user,
                                                  'password': application.password})
    if status not in (302, 303) or location not in ('/', public + '/'):
        raise RuntimeError('browser password login did not complete')
    if not any(cookie['secure'] and cookie['httponly'] for cookie in cookies.values()):
        raise RuntimeError('browser login did not set a secure HTTP-only session cookie')
    status, _ = request('/user/settings')
    if status != 200:
        raise RuntimeError('browser session did not authenticate a protected page')
    status, _ = request('/user/settings', form={'full_name': 'must not be applied'},
                        origin='https://untrusted.example.invalid')
    if status != 403:
        raise RuntimeError('browser cross-origin write was not rejected')
