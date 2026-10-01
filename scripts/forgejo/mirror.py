"""Native 02:00 cron, HTTPS Git and sanitized status in owned fixtures."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

from scripts.forgejo.compatibility import Application, temp_root
from scripts.forgejo.runtime import ROOT
from scripts.forgejo.shared import mirror_status, service

evaluate_status = mirror_status.evaluate_status
summarize = mirror_status.summarize


def fixture_timezone(now, *, seconds=20):
    """A fixed-offset TZif v1 file; epoch time and shipped cron stay unchanged."""
    utc = now.astimezone(timezone.utc)
    offset = 7200 - (utc.hour * 3600 + utc.minute * 60 + utc.second) - seconds
    abbreviation = b'Fixture\0'
    return (b'TZif\0' + b'\0' * 15 + struct.pack('>6I', 0, 0, 0, 0, 1, len(abbreviation))
            + struct.pack('>iBB', offset, 0, 0) + abbreviation)


def write(path, content):
    service.atomic_write(path, content if isinstance(content, bytes) else content.encode(),
                         mode=0o600, uid=path.parent.stat().st_uid, gid=path.parent.stat().st_gid)


def cron_instant(value, tzif):
    # Go's RFC3339 JSON rounds timezone offsets to minutes. This disposable
    # zone includes seconds; the independent TZif oracle preserves its instant.
    return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(
        tzinfo=ZoneInfo.from_file(io.BytesIO(tzif)))


def rows(application):
    return json.loads(application.run.command([application.run.podman, 'exec',
        application.db, 'psql', '-X', '-U', 'forgejo', '-d', 'forgejo',
        '--tuples-only', '--no-align', '--set', 'ON_ERROR_STOP=1',
        '--command', mirror_status.QUERY]).stdout)


def cron(application):
    return next(task for task in application.request('/admin/cron') if task['name'] == 'update_mirrors')


def failure_category(message):
    lowered = message.lower()
    for category, words in (
        ('TLS trust', ('certificate', 'x509', 'ssl')),
        ('authentication', ('authentication', 'username', '401', 'credentials')),
        ('destination access', ('not found', '403', '404')),
        ('network policy', ('allowed', 'blocked', 'permitted', 'local network')),
        ('LFS protocol', ('lfs',)),
    ):
        if any(word in lowered for word in words):
            return category
    return 'unclassified native failure'


def scan(run, application, zone_file):
    """Exercise the shipped native schedule at its next real local 02:00."""
    before = rows(application)
    run.command([run.podman, 'stop', application.app])
    write(zone_file, fixture_timezone(datetime.now(timezone.utc)))
    run.command([run.podman, 'start', application.app])
    application.wait()
    task = cron(application)
    if task['schedule'] != '0 0 2 * * *' or task['exec_times'] != 0 or rows(application) != before:
        raise RuntimeError('startup changed mirror state or the shipped schedule')
    next_run = cron_instant(task['next'], zone_file.read_bytes())
    delay = (next_run - datetime.now(timezone.utc)).total_seconds()
    if not 0 < delay < 35 or (next_run.hour, next_run.minute, next_run.second) != (2, 0, 0):
        raise RuntimeError('native cron did not use the selected local timezone boundary')
    deadline = time.monotonic() + delay + 30
    while time.monotonic() < deadline:
        if cron(application)['exec_times'] > 0:
            # Cron enqueues work; wait for the native queue to finish separately.
            time.sleep(2)
            return
        time.sleep(0.5)
    raise RuntimeError('native 02:00 mirror scan deadline exceeded')


def run_fixture(run):
    from scripts.forgejo.fixture import source_auth
    phase = 'setup'
    try:
        with tempfile.TemporaryDirectory(prefix='mirror-', dir=temp_root()) as temporary:
            root = Path(temporary)
            auth = root / 'source-auth'
            auth.mkdir(mode=0o700)
            zones = root / 'zones'
            zones.mkdir(mode=0o700)
            zone_file = zones / 'MirrorFixture'
            # Begin with UTC so setup cannot schedule an accelerated early scan.
            write(zone_file, fixture_timezone(datetime(2000, 1, 1, 1, 59, 40, tzinfo=timezone.utc)))
            source = Application(run, root, suffix='mirror-source', auth_dir=auth,
                                 extra_environment=('TZ=MirrorFixture',
                                                    'SSL_CERT_FILE=/etc/gitea/mirror-ca.pem'),
                                 additional_volumes=(f'{zones}:/usr/share/zoneinfo:ro',))
            source.initialize_admin()
            target = Application(run, root, suffix='mirror-target')
            target.initialize_admin()
            target.request('/user/repos', method='POST', payload={'name': 'nightly', 'private': True})
            client, _ = source_auth(run, source, root)
            source.request('/user/repos', method='POST',
                payload={'name': 'nightly', 'private': True, 'auto_init': True, 'default_branch': 'main'})
            url = client.base + '/' + source.user + '/nightly.git'
            client.git('clone', url, '/tmp/forgejo-mirror-client')
            client.git('-C', '/tmp/forgejo-mirror-client', 'config', 'user.name', 'Mirror fixture')
            client.git('-C', '/tmp/forgejo-mirror-client', 'config', 'user.email', 'fixture@example.invalid')
            client.git('-C', '/tmp/forgejo-mirror-client', 'checkout', '-b', 'mirror-feature')
            client.command(['/bin/sh', '-ec', "printf 'independent mirror feature\\n' > /tmp/forgejo-mirror-client/feature.txt"])
            client.git('-C', '/tmp/forgejo-mirror-client', 'add', 'feature.txt')
            client.git('-C', '/tmp/forgejo-mirror-client', 'commit', '-m', 'Seed fixed mirror feature')
            client.git('-C', '/tmp/forgejo-mirror-client', 'tag', 'mirror-tag')
            client.git('-C', '/tmp/forgejo-mirror-client', 'push', 'origin', 'mirror-feature', 'refs/tags/mirror-tag')
            expected = client.refs(url)
            if set(expected) != {'refs/heads/main', 'refs/heads/mirror-feature', 'refs/tags/mirror-tag'}:
                raise RuntimeError('fixed mirror seed refs are incomplete')
            phase = 'HTTPS destination'
            certificate = target.config / 'mirror-cert.pem'
            key = target.config / 'mirror-key.pem'
            run.command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(key), '-out', str(certificate), '-days', '1', '-subj', '/CN=mirror-target',
                '-addext', 'subjectAltName=DNS:mirror-target,IP:127.0.0.1'])
            certificate.chmod(0o600)
            key.chmod(0o600)
            run.command([run.podman, 'stop', target.app])
            ini = target.ini.replace('PROTOCOL = http', 'PROTOCOL = https').replace(
                'HOST = forgejo-postgres:5432', 'HOST = ' + target.db + ':5432')
            ini = ini.replace('HTTP_PORT = 3000', 'HTTP_PORT = 3000\nCERT_FILE = /etc/gitea/mirror-cert.pem\nKEY_FILE = /etc/gitea/mirror-key.pem')
            write(target.config / 'app.ini', ini)
            run.command([run.podman, 'network', 'connect', '--alias', 'mirror-target', source.network, target.app])
            run.command([run.podman, 'start', target.app])
            run.wait([run.podman, 'exec', target.app, 'curl', '--fail', '--silent',
                '--cacert', '/etc/gitea/mirror-cert.pem', 'https://127.0.0.1:3000/api/healthz'])
            write(source.config / 'mirror-ca.pem', certificate.read_bytes())
            write(source.config / 'app.ini', source.ini + '\n[git.config]\nhttp.sslCAInfo = /etc/gitea/mirror-ca.pem\n'
                '\n[migrations]\nALLOWED_DOMAINS = mirror-target\nALLOW_LOCALNETWORKS = true\n')
            run.command([run.podman, 'restart', source.app])
            source.wait()
            address = 'https://mirror-target:3000/' + target.user + '/nightly.git'
            credentials = 'https://' + quote(target.user, safe='') + ':' + quote(target.password, safe='') + '@' + address[8:] + '\n'
            write(source.config / 'mirror-credentials', credentials)
            phase = 'HTTPS Git credential lookup'
            git_options = ['-c', 'http.sslCAInfo=/etc/gitea/mirror-ca.pem',
                '-c', 'core.askPass=/etc/gitea/mirror-credential-helper.sh',
                '-c', 'credential.useHttpPath=true']
            def destination_refs():
                output = client.command(['git', *git_options, 'ls-remote', '--refs', address]).stdout
                return {name: revision for revision, name in (line.split() for line in output.splitlines())}
            probe = client.command(['git', *git_options, 'ls-remote', '--refs', address], check=False)
            if probe.returncode:
                raise RuntimeError(failure_category(probe.stderr))
            phase = 'native enrollment'
            prefix = '/repos/' + source.user + '/nightly'
            source.request(prefix + '/push_mirrors', method='POST', payload={
                'remote_address': address, 'interval': '8h', 'sync_on_commit': False})
            if rows(source)[0]['last_attempt'] != 0:
                raise RuntimeError('enrollment unexpectedly synchronized')
            phase = 'native manual initial sync'
            try:
                source.request(prefix + '/push_mirrors-sync', method='POST')
            except RuntimeError:
                message = source.sql('\\connect forgejo\nSELECT last_error FROM push_mirror;')
                raise RuntimeError(failure_category(message)) from None
            if destination_refs() != expected or summarize(rows(source), datetime.now(timezone.utc))[0]['status'] != 'healthy':
                raise RuntimeError('native initial HTTPS mirror does not match fixed refs')
            phase = 'no sync on push; native force-update and deletion'
            initial = rows(source)
            client.git('-C', '/tmp/forgejo-mirror-client', 'push', '--force', 'origin',
                expected['refs/heads/main'] + ':refs/heads/mirror-feature', ':refs/tags/mirror-tag')
            changed = client.refs(url)
            time.sleep(2)
            if rows(source) != initial or destination_refs() != expected:
                raise RuntimeError('push synchronized despite disabled event mirroring')
            phase = 'eight-hour eligibility and no startup sync'
            scan(run, source, zone_file)
            if rows(source) != initial or destination_refs() != expected:
                raise RuntimeError('ineligible mirror synchronized at the nightly scan')
            source.sql('\\connect forgejo\nUPDATE push_mirror SET last_update=extract(epoch from now())::bigint-9*3600;')
            phase = 'eligible native 02:00 synchronization'
            scan(run, source, zone_file)
            if destination_refs() != changed or summarize(rows(source), datetime.now(timezone.utc))[0]['status'] != 'healthy':
                raise RuntimeError('eligible nightly mirror did not force-update/delete fixed refs')
            # Native completion is second-rounded; eight hours leaves eligibility
            # before the next local night even when completion follows 02:00.
            next_run = cron_instant(cron(source)['next'], zone_file.read_bytes())
            if rows(source)[0]['last_attempt'] + 28800 >= next_run.timestamp():
                raise RuntimeError('completion rounding would skip the next night')
            phase = 'visible native authentication failure'
            wrong = credentials.replace(quote(target.password, safe=''), 'incorrect-fixture-password')
            write(source.config / 'mirror-credentials', wrong)
            credential_hash = hashlib.sha256((source.config / 'mirror-credentials').read_bytes()).hexdigest()
            source.sql('\\connect forgejo\nUPDATE push_mirror SET last_update=extract(epoch from now())::bigint-9*3600;')
            scan(run, source, zone_file)
            if summarize(rows(source), datetime.now(timezone.utc))[0]['status'] != 'failed':
                raise RuntimeError('native authentication failure was not visible')
            if hashlib.sha256((source.config / 'mirror-credentials').read_bytes()).hexdigest() != credential_hash:
                raise RuntimeError('failed Git authentication changed declared credential inputs')
            write(source.config / 'mirror-credentials', credentials)
            if destination_refs() != changed:
                raise RuntimeError('failed authentication changed destination refs')
            phase = 'retry at next eligible native scan'
            source.sql('\\connect forgejo\nUPDATE push_mirror SET last_update=extract(epoch from now())::bigint-9*3600;')
            scan(run, source, zone_file)
            if summarize(rows(source), datetime.now(timezone.utc))[0]['status'] != 'healthy' or destination_refs() != changed:
                raise RuntimeError('native retry did not clear failed evidence')
    except (OSError, ValueError, RuntimeError, KeyError, StopIteration) as error:
        raise RuntimeError('native mirror ' + phase + ': ' + str(error)) from None
