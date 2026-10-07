"""Bounded, repository-scoped ephemeral runner enrollment and reconciliation.

Runner management is absent from the configured teacli action commands. This
client uses only the supported repository runner REST endpoints.
"""

from dataclasses import dataclass, field
import json
import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class RegistrationUncertain(RuntimeError):
    """Enrollment or retirement lacks independent, unambiguous evidence."""


@dataclass(frozen=True)
class Registration:
    id: int
    name: str
    repository: str
    generation: str
    uuid: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)


class RegistrationClient:
    def __init__(self, url, repository, token, *, request=None):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')
                or not isinstance(repository, str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', repository) is None
                or not isinstance(token, str) or not token or '\r' in token or '\n' in token):
            raise ValueError('Invalid repository enrollment configuration')
        self.url, self.repository, self._token = url.rstrip('/'), repository, token
        self._request = request or self.request
        self.path = '/repos/' + repository

    def request(self, path, *, method='GET', payload=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                raise RegistrationUncertain('Runner API redirected outside its configured endpoint')

        request = urllib.request.Request(self.url + '/api/v1' + path, method=method,
            headers={'Authorization': 'token ' + self._token, 'Content-Type': 'application/json'},
            data=json.dumps(payload).encode() if payload is not None else None)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=15) as response:
                data = response.read(1048577)
                if len(data) > 1048576:
                    raise RegistrationUncertain('Runner API response exceeded its fixed bound')
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            raise RegistrationUncertain('Runner API returned HTTP ' + str(error.code)) from None
        except (OSError, ValueError):
            raise RegistrationUncertain('Runner API did not return usable evidence') from None

    def identity(self, repository, slot, generation):
        if (repository != self.repository or not isinstance(slot, str)
                or re.fullmatch(r'[a-z][a-z0-9-]{0,47}', slot) is None
                or not isinstance(generation, str) or re.fullmatch(r'[0-9a-f]{32}', generation) is None):
            raise ValueError('Invalid trusted runner identity')
        return slot + '-' + generation

    def rows(self):
        rows = []
        deadline = time.monotonic() + 15
        for page in range(1, 21):
            if time.monotonic() >= deadline:
                raise RegistrationUncertain('Runner listing exceeded its elapsed-time budget')
            result = self._request(self.path + f'/actions/runners?visible=false&page={page}&limit=50')
            if isinstance(result, dict):
                result = result.get('runners')
            if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
                raise RegistrationUncertain('Runner listing is invalid')
            rows.extend(result)
            if len(result) < 50:
                return rows
        raise RegistrationUncertain('Runner listing exceeded its fixed page bound')

    def pending_job(self, label, *, excluded=()):
        if not isinstance(label, str) or re.fullmatch(r'[a-z][a-z0-9-]{0,47}', label) is None:
            raise ValueError('Invalid trusted runner label')
        rows = self._request(self.path + '/actions/runners/jobs?labels=' + urllib.parse.quote(label, safe=''))
        if not isinstance(rows, list) or len(rows) > 1000:
            raise RegistrationUncertain('Pending-job lookup is invalid or exceeds its bound')
        if not rows:
            return None
        if any(not isinstance(row, dict) or not isinstance(row.get('handle'), str)
               or not 1 <= len(row['handle']) <= 4096
               or any(ord(character) < 32 or ord(character) == 127 for character in row['handle'])
               for row in rows):
            raise RegistrationUncertain('Pending-job lookup lacks a usable handle')
        return next((row['handle'] for row in rows
                     if hashlib.sha256(row['handle'].encode()).hexdigest() not in excluded), None)

    def lookup(self, repository, slot, generation):
        name = self.identity(repository, slot, generation)
        repo = self._request(self.path)
        if not isinstance(repo, dict) or type(repo.get('id')) is not int or repo['id'] <= 0:
            raise RegistrationUncertain('Repository identity is invalid')
        matches = [row for row in self.rows() if row.get('name') == name]
        if not matches:
            return None
        if len(matches) != 1:
            raise RegistrationUncertain('Runner identity is ambiguous')
        row = matches[0]
        if (row.get('description') != 'forgejo-runner:' + generation
                or row.get('repo_id') != repo['id'] or row.get('ephemeral') is not True
                or type(row.get('id')) is not int or row['id'] <= 0):
            raise RegistrationUncertain('Runner ownership does not match')
        return Registration(row['id'], name, repository, generation)

    def enroll(self, repository, slot, generation):
        name = self.identity(repository, slot, generation)
        if self.lookup(repository, slot, generation) is not None:
            raise RegistrationUncertain('Existing runner must be retired before admission')
        try:
            result = self._request(self.path + '/actions/runners', method='POST', payload={
                'name': name, 'description': 'forgejo-runner:' + generation, 'ephemeral': True})
            if (not isinstance(result, dict) or type(result.get('id')) is not int or result['id'] <= 0
                    or any(not isinstance(result.get(key), str) or not result[key]
                           for key in ('uuid', 'token'))):
                raise RegistrationUncertain('Enrollment response is invalid')
            observed = self.lookup(repository, slot, generation)
            if observed is None or observed.id != result['id']:
                raise RegistrationUncertain('Enrollment identity was not independently observed')
            return Registration(observed.id, name, repository, generation, result['uuid'], result['token'])
        except Exception:
            # A successful POST can lose its reply. Reconcile once, never retry
            # creation. Any inability to prove retirement blocks this generation.
            try:
                uncertain = self.lookup(repository, slot, generation)
                if uncertain is not None:
                    self.retire(uncertain)
            except Exception:
                raise RegistrationUncertain('Enrollment reconciliation requires operator attention') from None
            raise RegistrationUncertain('Enrollment did not complete; no creation retry was attempted') from None

    def retire(self, registration):
        if not isinstance(registration, Registration) or registration.repository != self.repository:
            raise ValueError('Retirement requires a trusted scoped identity')
        suffix = '-' + registration.generation
        if not registration.name.endswith(suffix):
            raise ValueError('Retirement generation does not match')
        slot = registration.name[:-len(suffix)]
        observed = self.lookup(registration.repository, slot, registration.generation)
        if observed is None:
            return
        if observed.id != registration.id:
            raise RegistrationUncertain('Retirement identity changed')
        self._request(self.path + '/actions/runners/' + str(registration.id), method='DELETE')
        if self.lookup(registration.repository, slot, registration.generation) is not None:
            raise RegistrationUncertain('Retirement was not independently observed')
