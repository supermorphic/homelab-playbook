"""Real stock-container Git/application backup, SMB transfer and exact restore."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

from scripts.forgejo.compatibility import Application, temp_root
from scripts.forgejo.runtime import Run, defaults
from scripts.forgejo.shared import archive, backup, service, transfer
from scripts.forgejo import restore

SAMBA_IMAGE = ('ghcr.io/servercontainers/samba:smbd-only-a3.24.1-s4.23.8-r0@'
               'sha256:0b8ce469f097a1afdd4f286c4313dae52ce18aad6a7e3b25ee3d93fd09200866')


def seed_content():
    return {'attachment': b'independent Forgejo attachment fixture\n',
            'lfs': b'independent Forgejo LFS fixture\n'}


def assert_refs(expected, actual):
    if expected != actual:
        raise RuntimeError('Git refs differ from independently recorded revisions')


def assert_outbound(before, after, hooks, mirrors):
    if before != after or hooks != 1 or mirrors != 1:
        raise RuntimeError('restored outbound targets or isolation differ from the oracle')


class Sentinel:
    """Reachable source-side listener; restored components cannot join its network."""
    def __init__(self, experiment, source, client, root):
        self.directory = root / 'sentinel'
        self.directory.mkdir(mode=0o700)
        self.count = experiment.private_file(self.directory, 'requests', '0')
        program = experiment.private_file(root, 'sentinel.pl', r'''
use strict; use warnings; use IO::Socket::INET;
my $server = IO::Socket::INET->new(LocalAddr=>'0.0.0.0',LocalPort=>8000,Listen=>10,ReuseAddr=>1) or die;
while (my $socket = $server->accept()) {
    my $line = <$socket> // ''; while (my $header = <$socket>) {last if $header =~ /^\r?\n$/;}
    if ($line !~ /^GET \/probe /) {
        open(my $in,'<','/evidence/requests') or die; my $count=<$in>; close($in);
        open(my $out,'>','/evidence/requests') or die; print $out $count+1; close($out);
    }
    print $socket "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"; close($socket);
}
''')
        self.container = experiment.create('container', 'sentinel', [
            '--network', source.network, '--network-alias', 'sentinel',
            '--userns=keep-id:uid=1000,gid=1000', '--user', '1000:1000',
            '--volume', f'{program}:/sentinel.pl:ro', '--volume', f'{self.directory}:/evidence',
            '--entrypoint', 'perl', source.pins['forgejo_postgres_image'], '/sentinel.pl'])
        experiment.wait([experiment.podman, 'exec', source.app, 'curl', '--fail', '--silent',
                         '--max-time', '2', 'http://sentinel:8000/probe'])
        self.address = experiment.command([experiment.podman, 'inspect', '--format',
            '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', self.container]).stdout.strip()
        client.request(f'/repos/{source.user}/recovery/hooks', method='POST', payload={
            'type': 'gitea', 'active': True, 'events': ['push'],
            'config': {'url': 'http://sentinel:8000/hook', 'content_type': 'json'}})
        # Seed stored mirror metadata without initiating a mirror transaction.
        # Task 8 independently proves the native push-mirror API and HTTPS sync.
        result = experiment.command([experiment.podman, 'exec', '-i', source.db,
            'psql', '-X', '-U', 'forgejo', '-d', 'forgejo', '--set', 'ON_ERROR_STOP=1'], input_text=
            "INSERT INTO push_mirror(repo_id,remote_name,remote_address,interval,created_unix,last_update,sync_on_commit) "
            "SELECT id,'sentinel','https://sentinel:8000/mirror.git',28800000000000,"
            "extract(epoch from now())::bigint,extract(epoch from now())::bigint,false "
            "FROM repository WHERE name='recovery';\n", check=False)
        if result.returncode:
            # This statement and its data contain synthetic public metadata only.
            raise RuntimeError('synthetic mirror seed: ' + result.stderr.splitlines()[0][:240])
    def verify(self, recovered, expected):
        client = restore.Client(recovered, recovered.name('restore-app'))
        hooks = client.request(f'/repos/{expected["username"]}/recovery/hooks')
        mirrors = recovered.command([recovered.podman, 'exec', recovered.name('restore-postgres'),
            'psql', '-X', '-U', 'forgejo', '-d', 'forgejo', '-Atc',
            "SELECT count(*) FROM push_mirror WHERE remote_address='https://sentinel:8000/mirror.git'"]).stdout.strip()
        for endpoint in ('http://' + self.address + ':8000/hook', 'https://' + self.address + ':8000/mirror.git'):
            result = client.command(['curl', '--silent', '--connect-timeout', '1', '--max-time', '2', endpoint], check=False)
            if result.returncode == 0:
                raise RuntimeError('restored application reached the source-side sentinel')
        time.sleep(2)
        assert_outbound(0, int(self.count.read_text()), len(hooks), int(mirrors))


class Capture:
    """Actual stopped stock application using the production capture engine."""
    def __init__(self, experiment, application, root):
        self.experiment = experiment
        self.application = application
        self.state = root / 'state'
        self.state.mkdir(mode=0o700)
        self.backups = self.state / 'backups'
        self.backups.mkdir(mode=0o700)
        self.lock = self.state / 'operation.lock'
        self.lock.touch(mode=0o600)
        self.settings = {key: application.pins['forgejo_' + key]
                         for key in ('image', 'postgres_image', 'rclone_image')}
        self.settings['recovery_generation'] = secrets.token_hex(16)
        self.transfer_requests = 0
    def preflight(self):
        self.experiment.command([self.experiment.podman, 'exec', self.application.db, 'pg_isready', '-U', 'postgres'])
    def active(self):
        return self.experiment.command([self.experiment.podman, 'inspect', '--format', '{{.State.Running}}', self.application.app]).stdout.strip() == 'true'
    def stop(self):
        self.experiment.command([self.experiment.podman, 'stop', '--time', '30', self.application.app])
    def stopped(self):
        if self.active():
            raise RuntimeError('source application still has active writers')
    def dump(self, path):
        self.stopped()
        backup.stream_command([self.experiment.podman, 'exec', self.application.db, 'pg_dump', '-U', 'forgejo', '-d', 'forgejo', '-Fc', '--no-owner', '--no-acl'], output=path)
    def files(self, path):
        self.stopped()
        self.experiment.stream('capture-files', ['--network', 'none', '--read-only',
            '--userns=keep-id:uid=1000,gid=1000', '--user', '1000:1000',
            '--volume', f'{self.application.data_volume}:/archive/data:ro',
            '--volume', f'{self.application.config}:/archive/config:ro',
            '--entrypoint', 'tar', self.settings['postgres_image'],
            '--hard-dereference', '-czf', '-', '-C', '/archive', 'data', 'config'], output=path)
    def readable_dump(self, path):
        backup.stream_command([self.experiment.podman, 'exec', '-i', self.application.db, 'pg_restore', '--list'], source=path, timeout=120)
    def start(self):
        self.experiment.command([self.experiment.podman, 'start', self.application.app])
    def health(self):
        self.application.wait()
    def trigger_transfer(self):
        self.transfer_requests += 1


class Nas:
    def __init__(self, experiment, application, capture, root):
        self.experiment = experiment
        self.application = application
        self.capture = capture
        self.root = root
        self.counter = 0
        self.commands = []
        self.storage = root / 'nas'
        self.storage.mkdir(mode=0o700)
        password = secrets.token_urlsafe(24)
        obscured = experiment.foreground('obscure', ['--interactive', '--network', 'none',
            application.pins['forgejo_rclone_image'], 'obscure', '-'], input_text=password + '\n').stdout.strip()
        self.config = experiment.private_file(root, 'rclone.conf',
            '[nas]\ntype = smb\nhost = nas\nuser = fixture\npass = ' + obscured + '\n')
        self.environment = experiment.private_file(root, 'samba.env',
            'ACCOUNT_fixture=' + password + '\nUID_fixture=1000\nAVAHI_DISABLE=1\n'
            'WSDD2_DISABLE=1\nNETBIOS_DISABLE=1\n'
            'SAMBA_VOLUME_CONFIG_forgejo=[forgejo]; path=/shares/forgejo; valid users = fixture; '
            'guest ok = no; read only = no; browsable = yes; create mask = 0600; directory mask = 0700\n')
        self.remote = transfer.Remote(capture.settings, backups=capture.backups, config=self.config,
            prefix='nas:forgejo', command=self.command)
    def command(self, arguments, *, check=True, marker_mount=None):
        self.counter += 1
        self.commands.append(tuple(arguments))
        mounts = ['--volume', f'{self.capture.backups}:/backups:ro',
                  '--volume', f'{self.config}:/run/secrets/rclone.conf:ro']
        if marker_mount:
            mounts += ['--volume', f'{marker_mount}:/transfer-metadata/COMPLETE:ro']
        return self.experiment.foreground('nas-client-' + str(self.counter), [
            '--network', self.application.network, '--userns=keep-id:uid=1000,gid=1000',
            '--user', '1000:1000', *mounts, self.application.pins['forgejo_rclone_image'],
            '--config', '/run/secrets/rclone.conf', '--contimeout', '2s', '--timeout', '5s',
            '--retries', '1', '--low-level-retries', '1', *arguments], check=check, timeout=60)
    def start(self):
        self.server = self.experiment.create('container', 'nas-server', [
            '--userns=keep-id:uid=1000,gid=1000', '--user', '0:0',
            '--network', self.application.network, '--network-alias', 'nas',
            '--env-file', str(self.environment), '--volume', f'{self.storage}:/shares/forgejo:rw', SAMBA_IMAGE])
        deadline = time.monotonic() + 60
        while self.command(['lsd', 'nas:forgejo'], check=False).returncode:
            if time.monotonic() >= deadline:
                raise RuntimeError('synthetic SMB readiness deadline exceeded')
            time.sleep(1)


def source_auth(experiment, application, directory):
    token = application.request('/users/' + application.user + '/tokens', method='POST',
                                payload={'name': 'recovery-fixture', 'scopes': ['all']})['sha1']
    credential = experiment.private_file(directory, 'original-token', token)
    auth = directory / 'source-auth'
    auth.mkdir(mode=0o700, exist_ok=True)
    for name, value in (('username', application.user), ('password', token),
                        ('curl.conf', f'user = "{application.user}:{token}"\n'),
                        ('askpass.sh', '#!/bin/sh\ncase "$1" in *Username*) cat /test-auth/username ;; *Password*) cat /test-auth/password ;; *) exit 1 ;; esac\n')):
        target = auth / name
        service.atomic_write(target, value.encode(), mode=0o700 if name.endswith('.sh') else 0o600,
                             uid=os.geteuid(), gid=os.getegid())
    client = restore.Client(experiment, application.app)
    client.auth_directory = auth
    return client, credential


def seed(application, client, credential):
    content = seed_content()
    repository = application.request('/user/repos', method='POST',
        payload={'name': 'recovery', 'auto_init': True, 'private': True, 'default_branch': 'main'})
    username = application.user
    prefix = f'/repos/{username}/recovery'
    issue = client.request(prefix + '/issues', method='POST',
        payload={'title': 'Independent recovery issue', 'body': 'Independent issue body'})
    comment = client.request(prefix + '/issues/' + str(issue['number']) + '/comments', method='POST',
                             payload={'body': 'Independent comment body'})
    client.command(['/bin/sh', '-ec', 'umask 077; cat > /tmp/forgejo-source-attachment'],
                   input_text=content['attachment'].decode())
    result = client.command(['curl', '--fail', '--silent', '--show-error', '--config', client.auth,
        '--form', 'attachment=@/tmp/forgejo-source-attachment;filename=fixture.txt;type=text/plain',
        client.base + '/api/v1' + prefix + '/issues/' + str(issue['number']) + '/assets'])
    attachment = json.loads(result.stdout)
    oid = hashlib.sha256(content['lfs']).hexdigest()
    import base64
    authorization = 'Basic ' + base64.b64encode((username + ':' + application.password).encode()).decode()
    request = urllib.request.Request(application.url + f'/{username}/recovery.git/info/lfs/objects/batch',
        data=json.dumps({'operation': 'upload', 'transfers': ['basic'],
                         'objects': [{'oid': oid, 'size': len(content['lfs'])}]}).encode(),
        headers={'Authorization': authorization, 'Content-Type': 'application/vnd.git-lfs+json',
                 'Accept': 'application/vnd.git-lfs+json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            batch = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'LFS batch returned HTTP {error.code}') from None
    action = batch['objects'][0]['actions']['upload']
    path = urlsplit(action['href']).path
    headers = action.get('header', {})
    request = urllib.request.Request(application.url + path, data=content['lfs'],
        headers={**headers, 'Content-Type': 'application/octet-stream'}, method='PUT')
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except urllib.error.HTTPError as error:
        # The synthetic server response is inspected privately, never credentials.
        body = error.read().decode()
        message = json.loads(body).get('message', 'no message') if body.startswith('{') else 'non-JSON response'
        raise RuntimeError(f'LFS upload returned HTTP {error.code}: {message}') from None
    url = client.base + f'/{username}/recovery.git'
    client.git('clone', url, '/tmp/forgejo-source-client')
    for field, value in (('user.name', 'Recovery fixture'), ('user.email', 'fixture@example.invalid')):
        client.git('-C', '/tmp/forgejo-source-client', 'config', field, value)
    pointer = f'version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(content["lfs"])}\n'
    client.command(['/bin/sh', '-ec', 'cat > /tmp/forgejo-source-client/fixture-lfs.bin'], input_text=pointer)
    client.command(['/bin/sh', '-ec', "printf 'fixture-lfs.bin filter=lfs diff=lfs merge=lfs -text\\n' > /tmp/forgejo-source-client/.gitattributes"])
    client.git('-C', '/tmp/forgejo-source-client', 'add', '.gitattributes', 'fixture-lfs.bin')
    client.git('-C', '/tmp/forgejo-source-client', 'commit', '-m', 'Seed independent LFS pointer')
    client.git('-C', '/tmp/forgejo-source-client', 'push', 'origin', 'main')
    client.git('-C', '/tmp/forgejo-source-client', 'checkout', '-b', 'fixture-feature')
    client.command(['/bin/sh', '-ec', "printf 'independent feature\\n' > /tmp/forgejo-source-client/feature.txt"])
    client.git('-C', '/tmp/forgejo-source-client', 'add', 'feature.txt')
    client.git('-C', '/tmp/forgejo-source-client', 'commit', '-m', 'Seed independent feature')
    head = client.git('-C', '/tmp/forgejo-source-client', 'rev-parse', 'HEAD').strip()
    client.git('-C', '/tmp/forgejo-source-client', 'tag', 'fixture-tag')
    client.git('-C', '/tmp/forgejo-source-client', 'push', 'origin', 'fixture-feature', 'refs/tags/fixture-tag')
    pull = client.request(prefix + '/pulls', method='POST',
        payload={'base': 'main', 'head': 'fixture-feature', 'title': 'Independent recovery PR', 'body': 'Independent PR body'})
    refs = client.refs(url)
    if set(refs) != {'refs/heads/main', 'refs/heads/fixture-feature', 'refs/tags/fixture-tag', 'refs/pull/2/head'}:
        # Forgejo can advertise its PR refs; record them exactly rather than omit.
        if not {'refs/heads/main', 'refs/heads/fixture-feature', 'refs/tags/fixture-tag'}.issubset(refs):
            raise RuntimeError('source seed lacks independent refs')
    return {'schema': 1, 'username': username, 'repository': repository['name'], 'credential_file': str(credential),
        'refs': refs, 'issue': {'id': issue['number'], 'title': 'Independent recovery issue', 'body': 'Independent issue body'},
        'comment': {'id': comment['id'], 'body': 'Independent comment body'},
        'pull_request': {'id': pull['number'], 'title': 'Independent recovery PR', 'head': head},
        'attachment': {'id': attachment['id'], 'sha256': hashlib.sha256(content['attachment']).hexdigest()},
        'lfs': {'sha256': oid, 'size': len(content['lfs']), 'path': 'fixture-lfs.bin'}}


def run():
    experiment = Run()
    retrieved = Run()
    recovered = Run()
    phase = 'source application'
    error = None
    cleanup = []
    try:
        with tempfile.TemporaryDirectory(prefix='fixture-', dir=temp_root()) as temporary:
            root = Path(temporary)
            auth = root / 'source-auth'
            auth.mkdir(mode=0o700)
            source = Application(experiment, root, auth_dir=auth)
            source.initialize_admin()
            phase = 'source user token and independent seed'
            client, credential = source_auth(experiment, source, root)
            expected = seed(source, client, credential)
            phase = 'synthetic outbound targets'
            sentinel = Sentinel(experiment, source, client, root)
            expected_file = experiment.private_file(root, 'expected-state.json', json.dumps(expected))
            capture = Capture(experiment, source, root)
            phase = 'consistent dump and file capture'
            result = backup.perform_capture(capture)
            if capture.transfer_requests != 1:
                raise RuntimeError('capture did not trigger independent transfer after recovery')
            selected = capture.backups / result['archive_id']
            settings = root / 'settings'
            settings.mkdir(mode=0o700)
            for key in restore.KEYS:
                experiment.private_file(settings, key, service.safe_read(source.config / key, private=True, owner=os.geteuid()).decode())
            experiment.private_file(settings, 'generation.json', json.dumps({'schema': 1, 'recovery_generation': capture.settings['recovery_generation']}))
            phase = 'NAS outage and three-generation backlog'
            for days in (1, 2):
                created = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=days)
                identifier = 'forgejo-' + created.strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(8)
                path = capture.backups / identifier
                shutil.copytree(selected, path)
                manifest = json.loads((path / 'manifest.json').read_text())
                manifest.update(archive_id=identifier, created_at=created.strftime('%Y-%m-%dT%H:%M:%SZ'))
                (path / 'manifest.json').unlink()
                (path / 'SHA256SUMS').unlink()
                archive.seal(path, manifest)
            nas = Nas(experiment, source, capture, root)
            if not transfer.transfer(capture.backups, nas.remote)['errors']:
                raise RuntimeError('NAS outage unexpectedly passed')
            nas.start()
            status = transfer.transfer(capture.backups, nas.remote)
            if status['errors'] or status['transferred'] != 3:
                raise RuntimeError('NAS backlog recovery did not verify all generations')
            phase = 'unchanged transfer efficiency'
            nas.commands.clear()
            status = transfer.transfer(capture.backups, nas.remote)
            if status['errors'] or any(command[0] in ('copy', 'copyto', 'check') for command in nas.commands):
                raise RuntimeError('unchanged generation transferred historical payload')
            options = restore.Options(archive_id=result['archive_id'], remote='nas:forgejo',
                rclone_config=nas.config, settings_dir=settings, expected_state=expected_file,
                destination=root / 'restored', run_id=recovered.run_id,
                confirm_restore_experiment=recovered.run_id)
            phase = 'exact NAS retrieval'
            downloaded = root / result['archive_id']
            restore.retrieve(retrieved, options, downloaded, network=source.network)
            retrieval_cleanup = retrieved.cleanup()
            if retrieval_cleanup:
                raise RuntimeError('retrieval client cleanup failed before restored startup')
            phase = 'isolated application and original-credential oracle'
            restore.restore_local(recovered, downloaded, options, restore.checked_expected(expected_file))
            phase = 'restored webhook and mirror isolation'
            sentinel.verify(recovered, expected)
            phase = 'negative original-credential oracle'
            restored_client = restore.Client(recovered, recovered.name('restore-app'))
            restored_client.auth_directory = options.destination / 'auth'
            restored_client.command(['/bin/sh', '-ec', 'umask 077; cat > /tmp/forgejo-wrong.conf'],
                                    input_text='user = "fixture-admin:incorrect-fixture-credential"\n')
            restored_client.auth = '/tmp/forgejo-wrong.conf'
            try:
                restored_client.request('/user')
            except RuntimeError:
                pass
            else:
                raise RuntimeError('incorrect restored credential unexpectedly authenticated')
            assert_refs(expected['refs'], client.refs(client.base + f'/{source.user}/recovery.git'))
    except Exception as failure:
        error = phase + ': ' + (str(failure) if isinstance(failure, (RuntimeError, ValueError)) else type(failure).__name__)
    finally:
        cleanup += recovered.cleanup()
        cleanup += retrieved.cleanup()
        cleanup += experiment.cleanup()
        experiment.evidence(error, cleanup)
    if error or cleanup:
        print(json.dumps({'operation_error': error, 'cleanup_errors': cleanup}))
        return 1
    print('Forgejo dump/files, SMB backlog, exact isolated application restore and owned cleanup passed')
    return 0
