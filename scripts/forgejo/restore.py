"""Exact-generation recovery in an owned namespace containing only loopback."""
from __future__ import annotations
import argparse
import configparser
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit

from scripts.forgejo.runtime import ROOT, Run, defaults
from scripts.forgejo.shared import archive, backup, service

KEYS = ('database-password', 'secret-key', 'internal-token', 'lfs-jwt-secret', 'oauth2-jwt-secret')


@dataclass(frozen=True)
class Options:
    archive_id: str
    remote: str
    rclone_config: Path
    settings_dir: Path
    expected_state: Path
    destination: Path
    run_id: str
    confirm_restore_experiment: str
    attended: bool = False


def private_directory(path):
    service.check_parent_chain(path)
    item = path.lstat()
    if not path.is_dir() or item.st_uid != os.geteuid() or item.st_mode & 0o077:
        raise ValueError('restore input directory must be private and owned')


def preflight_archive(path, settings_dir, pins, destination):
    checked_destination(destination)
    private_directory(settings_dir)
    statistics = {}
    manifest = archive.validate_archive(path, True, statistics=statistics)
    if any(manifest[key] != pins[key] for key in ('image', 'postgres_image', 'rclone_image')):
        raise ValueError('archive runtime pins differ from this tested controller revision')
    generation = json.loads(service.safe_read(settings_dir / 'generation.json', private=True, owner=os.geteuid()))
    if generation != {'schema': 1, 'recovery_generation': manifest['recovery_generation']}:
        raise ValueError('protected settings do not match the selected recovery generation')
    found = set()
    with archive.checked_tar(path / 'files.tar.gz') as stream:
        for item in stream:
            name = item.name.removeprefix('./')
            if name.startswith('config/') and name.split('/')[-1] in KEYS:
                key = name.split('/')[-1]
                if name != 'config/' + key or key in found or not item.isfile():
                    raise ValueError('archive key path is ambiguous')
                expected = service.safe_read(settings_dir / key, private=True, owner=os.geteuid())
                if item.size > 65536 or stream.extractfile(item).read() != expected:
                    raise ValueError('archived durable keys differ from protected settings')
                found.add(key)
    if found != set(KEYS):
        raise ValueError('archive lacks required durable configuration keys')
    parent = destination.parent
    while not parent.exists():
        parent = parent.parent
    if (shutil.disk_usage(parent).free < statistics['filesystem_bytes'] + 256 * 1024 * 1024
            or os.statvfs(parent).f_favail < statistics['nodes']):
        raise ValueError('insufficient capacity for exact archive expansion')
    return manifest


def database_arguments(env, volume, image):
    return ['--network', 'none', '--userns=keep-id:uid=1000,gid=1000', '--user', '0:0', '--env-file', str(env),
            '--volume', f'{volume}:/var/lib/postgresql/data', image]


def application_arguments(database, data, config, auth, image):
    # Keep private bind mounts owned by the controller under the database's UID map.
    return ['--network', 'container:' + database, '--userns', 'container:' + database,
            '--user', '1000:1000', '--env', 'GITEA_APP_INI=/etc/gitea/app.ini',
            '--volume', f'{data}:/var/lib/gitea:U', '--volume', f'{config}:/etc/gitea:U',
            '--volume', f'{auth}:/test-auth:ro,U', image]


def checked_expected(path):
    document = json.loads(service.safe_read(path, private=True, owner=os.geteuid()))
    if document.get('schema') != 1:
        raise ValueError('unsupported independent expected-state schema')
    for key in ('username', 'repository'):
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', document.get(key, '')) is None:
            raise ValueError('unsafe expected repository identity')
    if not document.get('refs') or any(
            re.fullmatch(r'refs/(?:heads|tags|pull)/[A-Za-z0-9_./-]+', name) is None
            or '..' in name or re.fullmatch(r'[0-9a-f]{40,64}', value) is None
            for name, value in document['refs'].items()):
        raise ValueError('expected state must identify exact Git refs')
    for key in ('issue', 'pull_request', 'comment', 'attachment'):
        if not isinstance(document.get(key), dict) or type(document[key].get('id')) is not int:
            raise ValueError('expected application objects are absent')
    for key in ('attachment', 'lfs'):
        if re.fullmatch(r'[0-9a-f]{64}', document[key].get('sha256', '')) is None:
            raise ValueError('expected artifact hash is absent')
    if re.fullmatch(r'[A-Za-z0-9_.-]+', document['lfs'].get('path', '')) is None:
        raise ValueError('expected LFS pointer path is unsafe')
    checked_actions_data(document)
    credential = Path(document['credential_file'])
    service.safe_read(credential, private=True, owner=os.geteuid())
    document['credential_file'] = credential
    return document


def checked_actions_data(expected):
    if 'actions_data' not in expected:
        return {}
    values = expected['actions_data']
    if (not isinstance(values, dict) or set(values) != {'actions_log', 'actions_artifacts'}
            or any(not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None
                   for value in values.values())):
        raise ValueError('expected Actions fixture hashes are invalid')
    return values


def assert_actions_data(client, expected):
    for name, digest in checked_actions_data(expected).items():
        actual = client.command(['sha256sum', '/var/lib/gitea/data/' + name + '/recovery-fixture']).stdout.split()
        if not actual or actual[0] != digest:
            raise RuntimeError('restored Actions fixture content differs from the independent oracle')


def write_auth(experiment, directory, expected):
    directory.mkdir(mode=0o700)
    username = expected['username']
    credential = service.safe_read(expected['credential_file'], private=True, owner=os.geteuid()).decode()
    if any(character in credential for character in ('\n', '\r', '"', '\\')):
        raise ValueError('unsupported credential-file representation')
    experiment.private_file(directory, 'username', username)
    experiment.private_file(directory, 'password', credential)
    experiment.private_file(directory, 'curl.conf', f'user = "{username}:{credential}"\n')
    script = experiment.private_file(directory, 'askpass.sh',
        '#!/bin/sh\ncase "$1" in *Username*) cat /test-auth/username ;; *Password*) cat /test-auth/password ;; *) exit 1 ;; esac\n')
    script.chmod(0o700)


def lfs_config(content, headers):
    for name, value in headers.items():
        if not isinstance(value, str) or '\n' in value or '\r' in value:
            raise ValueError('unsupported LFS response header')
        if name.lower() == 'authorization':
            valid = re.fullmatch(r'(?:Bearer [A-Za-z0-9_.=-]+|Basic [A-Za-z0-9+/]+={0,2})', value) is not None
        elif name.lower() in ('accept', 'content-type'):
            valid = value in ('application/vnd.git-lfs', 'application/vnd.git-lfs+json', 'application/octet-stream')
        else:
            valid = False
        if not valid:
            raise ValueError('unsupported LFS response header: ' + re.sub(r'[^A-Za-z-]', '', name)[:64])
        content += f'header = "{name}: {value}"\n'
    return content


def assert_lfs_pointer(pointer, lfs):
    expected = f'version https://git-lfs.github.com/spec/v1\noid sha256:{lfs["sha256"]}\nsize {lfs["size"]}\n'
    if pointer != expected:
        raise RuntimeError('restored Git LFS pointer differs from the independent oracle')


class Client:
    def __init__(self, experiment, container, *, base='http://127.0.0.1:3000', auth='/test-auth/curl.conf'):
        self.experiment = experiment
        self.container = container
        self.base = base
        self.auth = auth
    def command(self, arguments, **kwargs):
        return self.experiment.command([self.experiment.podman, 'exec', '-i', self.container, *arguments], **kwargs)
    def request(self, path, *, method='GET', payload=None, api=True):
        if not path.startswith('/') or path.startswith('//') or '\n' in path:
            raise ValueError('application query must be a local absolute path')
        args = ['curl', '--fail', '--silent', '--show-error', '--max-time', '15',
                '--config', self.auth, '--request', method,
                self.base + ('/api/v1' if api else '') + path]
        body = None
        if payload is not None:
            media_type = 'application/vnd.git-lfs+json' if not api and '/info/lfs/' in path else 'application/json'
            args += ['--header', 'Content-Type: ' + media_type, '--header', 'Accept: ' + media_type, '--data-binary', '@-']
            body = json.dumps(payload)
        output = self.command(args, input_text=body).stdout
        return json.loads(output) if output else None
    def git(self, *arguments):
        return self.command(['env', 'GIT_ASKPASS=/test-auth/askpass.sh', 'GIT_TERMINAL_PROMPT=0',
                             'git', *arguments]).stdout
    def refs(self, url):
        output = self.git('ls-remote', '--refs', url)
        return {name: revision for revision, name in (line.split() for line in output.splitlines())}
    def hash_download(self, path, filename, *, auth=None):
        self.command(['curl', '--fail', '--silent', '--show-error', '--max-time', '15',
            '--config', auth or self.auth, '--output', filename, self.base + path])
        return self.command(['sha256sum', filename]).stdout.split()[0]
    def lfs_hash(self, owner, repository, oid, size):
        result = self.request(f'/{owner}/{repository}.git/info/lfs/objects/batch', method='POST', api=False,
            payload={'operation': 'download', 'transfers': ['basic'], 'objects': [{'oid': oid, 'size': size}]})
        action = result['objects'][0]['actions']['download']
        path = urlsplit(action['href']).path
        if not path.startswith(f'/{owner}/{repository}.git/info/lfs/') or '..' in path:
            raise ValueError('LFS response points outside the selected repository')
        headers = action.get('header', {})
        content = service.safe_read(Path(self.auth_directory) / 'curl.conf', private=True, owner=os.geteuid()).decode()
        content = lfs_config(content, headers)
        self.command(['/bin/sh', '-ec', 'umask 077; cat > /tmp/forgejo-lfs.conf'], input_text=content)
        return self.hash_download(path, '/tmp/forgejo-lfs-object', auth='/tmp/forgejo-lfs.conf')


def assert_expected(client, expected):
    user = client.request('/user')
    if user['login'] != expected['username']:
        raise RuntimeError('restored credential identifies a different user')
    prefix = f"/repos/{expected['username']}/{expected['repository']}"
    issue = client.request(prefix + '/issues/' + str(expected['issue']['id']))
    if issue['title'] != expected['issue']['title'] or issue['body'] != expected['issue']['body']:
        raise RuntimeError('restored issue differs from the independent oracle')
    pull = client.request(prefix + '/pulls/' + str(expected['pull_request']['id']))
    if pull['title'] != expected['pull_request']['title'] or pull['head']['sha'] != expected['pull_request']['head']:
        raise RuntimeError('restored pull request differs from the independent oracle')
    comments = client.request(prefix + '/issues/' + str(expected['issue']['id']) + '/comments')
    if not any(item['id'] == expected['comment']['id'] and item['body'] == expected['comment']['body'] for item in comments):
        raise RuntimeError('restored comment differs from the independent oracle')
    attachment = client.request(prefix + '/issues/' + str(expected['issue']['id']) + '/assets/' + str(expected['attachment']['id']))
    path = urlsplit(attachment['browser_download_url']).path
    if not re.fullmatch(r'/attachments/[a-f0-9-]+', path):
        raise ValueError('attachment URL points outside the selected application')
    if client.hash_download(path, '/tmp/forgejo-restored-attachment') != expected['attachment']['sha256']:
        raise RuntimeError('restored attachment content differs')
    url = client.base + '/' + expected['username'] + '/' + expected['repository'] + '.git'
    if client.refs(url) != expected['refs']:
        raise RuntimeError('restored Git refs differ from the independent oracle')
    client.git('clone', '--config', 'core.hooksPath=/dev/null', url, '/tmp/forgejo-restored-client')
    assert_lfs_pointer(client.git('-C', '/tmp/forgejo-restored-client', 'show',
                                'HEAD:' + expected['lfs']['path']), expected['lfs'])
    client.git('-C', '/tmp/forgejo-restored-client', 'config', 'user.name', 'Recovery fixture')
    client.git('-C', '/tmp/forgejo-restored-client', 'config', 'user.email', 'fixture@example.invalid')
    if client.lfs_hash(expected['username'], expected['repository'], expected['lfs']['sha256'], expected['lfs']['size']) != expected['lfs']['sha256']:
        raise RuntimeError('restored LFS object content differs')
    assert_actions_data(client, expected)
    client.git('-C', '/tmp/forgejo-restored-client', 'checkout', '-b', 'restored-write')
    client.command(['/bin/sh', '-ec', "printf 'restored fixture write\\n' > /tmp/forgejo-restored-client/recovery-write.txt"])
    client.git('-C', '/tmp/forgejo-restored-client', 'add', 'recovery-write.txt')
    client.git('-C', '/tmp/forgejo-restored-client', 'commit', '-m', 'Prove restored write')
    revision = client.git('-C', '/tmp/forgejo-restored-client', 'rev-parse', 'HEAD').strip()
    client.git('-C', '/tmp/forgejo-restored-client', 'push', 'origin', 'HEAD:refs/heads/restored-write')
    client.git('-C', '/tmp/forgejo-restored-client', 'fetch', 'origin')
    if client.refs(url).get('refs/heads/restored-write') != revision:
        raise RuntimeError('restored fetch/push failed independent ref comparison')


def extract(path, destination):
    destination.mkdir(mode=0o700)
    with archive.checked_tar(path / 'files.tar.gz') as stream:
        stream.extractall(destination, filter='data')
    for target in destination.rglob('*'):
        target.chmod((target.stat().st_mode & 0o700) | (0o700 if target.is_dir() else 0o400))
    config = destination / 'config/app.ini'
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    # Forgejo permits globals before sections; ConfigParser needs a section.
    parser.read_string('[forgejo_globals]\n' + config.read_text())
    values = {'database': {'HOST': '127.0.0.1:5432'},
        'server': {'ROOT_URL': 'http://127.0.0.1:3000/', 'DISABLE_SSH': 'true', 'START_SSH_SERVER': 'false'},
        'cron': {'ENABLED': 'false'}, 'cron.update_mirrors': {'ENABLED': 'false', 'RUN_AT_START': 'false'},
        'mailer': {'ENABLED': 'false'}}
    for section, options in values.items():
        if not parser.has_section(section):
            parser.add_section(section)
        for key, value in options.items():
            parser.set(section, key, value)
    import io
    result = io.StringIO()
    parser.write(result)
    config.write_text(result.getvalue().removeprefix('[forgejo_globals]\n'))
    config.chmod(0o600)


def restore_local(experiment, path, options, expected, *, ownership=None):
    pins = {key: defaults()['forgejo_' + key] for key in ('image', 'postgres_image', 'rclone_image')}
    manifest = preflight_archive(path, options.settings_dir, pins, options.destination)
    checked_destination(options.destination)
    options.destination.mkdir(mode=0o700)
    if ownership is not None:
        item = options.destination.lstat()
        ownership["destination"] = (item.st_dev, item.st_ino)
    marker = experiment.private_file(options.destination, 'ownership.json', json.dumps({'run_id': experiment.run_id}))
    extract(path, options.destination / 'extracted')
    auth = options.destination / 'auth'
    write_auth(experiment, auth, expected)
    environment = experiment.private_file(options.destination, 'postgres.env',
        'POSTGRES_USER=postgres\nPOSTGRES_DB=postgres\nPOSTGRES_PASSWORD=' + secrets.token_urlsafe(32) + '\n')
    volume = experiment.create('volume', 'restore-database', [])
    database = experiment.create('container', 'restore-postgres', database_arguments(environment, volume, pins['postgres_image']))
    experiment.wait([experiment.podman, 'exec', database, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'])
    password = service.safe_read(options.settings_dir / 'database-password', private=True, owner=os.geteuid()).decode()
    escaped = password.replace("'", "''")
    experiment.command([experiment.podman, 'exec', '-i', database, 'psql', '-X', '-U', 'postgres', '--set', 'ON_ERROR_STOP=1'],
        input_text=f"CREATE ROLE forgejo LOGIN PASSWORD '{escaped}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;\nCREATE DATABASE forgejo OWNER forgejo;\n")
    backup.stream_command([experiment.podman, 'exec', '-i', database, 'pg_restore', '-U', 'forgejo', '-d', 'forgejo',
        '--single-transaction', '--exit-on-error', '--no-owner', '--no-privileges'], source=path / 'database.dump', timeout=300)
    application = experiment.create('container', 'restore-app', application_arguments(database,
        options.destination / 'extracted/data', options.destination / 'extracted/config', auth, pins['image']))
    experiment.wait([experiment.podman, 'exec', application, 'curl', '--fail', '--silent', 'http://127.0.0.1:3000/api/healthz'])
    client = Client(experiment, application)
    client.auth_directory = auth
    interfaces = client.command(['cat', '/proc/net/dev']).stdout
    names = {line.split(':', 1)[0].strip() for line in interfaces.splitlines() if ':' in line}
    if names != {'lo'}:
        raise RuntimeError('restored namespace has an external network interface')
    denied = client.command(['curl', '--fail', '--silent', '--connect-timeout', '1', '--max-time', '2', 'http://203.0.113.1/'], check=False)
    if denied.returncode == 0:
        raise RuntimeError('restored namespace reached an external route')
    assert_expected(client, expected)
    return manifest


def checked_destination(destination):
    boundary = ROOT / '.tmp/forgejo'
    if not destination.is_absolute() or '..' in destination.parts:
        raise ValueError('restore destination must be an absolute path without parent traversal')
    service.check_parent_chain(destination.parent)
    canonical = destination.resolve()
    if canonical == boundary.resolve() or not canonical.is_relative_to(boundary.resolve()):
        raise ValueError('restore destination must be under this checkout .tmp/forgejo')
    if destination.exists() or destination.is_symlink():
        raise ValueError('restore destination is occupied')


def validate_options(options):
    archive.archive_time(options.archive_id)
    if re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9][A-Za-z0-9_./-]*", options.remote) is None or ".." in options.remote:
        raise ValueError("invalid remote prefix")
    if options.confirm_restore_experiment != options.run_id:
        raise ValueError('confirmation must match the new owned run identifier')
    Run(run_id=options.run_id)
    checked_destination(options.destination)
    private_directory(options.settings_dir)
    service.safe_read(options.rclone_config, private=True, owner=os.geteuid())
    checked_expected(options.expected_state)


def retrieve(experiment, options, target, *, network=None):
    target.mkdir(mode=0o700)
    pins = defaults()
    # Retrieval is finished and its egress-enabled client removed before startup.
    client = experiment.create('container', 'retrieve-client', [
        *(['--network', network] if network else []),
        '--userns=keep-id:uid=1000,gid=1000', '--user', '1000:1000',
        '--volume', f'{options.rclone_config}:/run/secrets/rclone.conf:ro',
        '--volume', f'{target}:/retrieved:rw', '--entrypoint', '/bin/sh', pins['forgejo_rclone_image'],
        '-ec', 'umask 077; exec rclone --config /run/secrets/rclone.conf "$@"', 'sh',
        'copy', options.remote.rstrip('/') + '/' + options.archive_id, '/retrieved',
        *[argument for name in (*archive.PAYLOADS, 'COMPLETE') for argument in ('--include', '/' + name)]])
    experiment.command([experiment.podman, 'wait', client], timeout=300)
    code = experiment.command([experiment.podman, 'inspect', '--format', '{{.State.ExitCode}}', client]).stdout.strip()
    if code != '0':
        raise RuntimeError('exact archive retrieval failed')
    for item in target.iterdir():
        service.regular(item, owner=os.geteuid())
        item.chmod(0o600)


def cleanup_destination(path, ownership, run_id):
    if 'destination' not in ownership:
        return
    service.check_parent_chain(path.parent)
    item = path.lstat()
    if (item.st_dev, item.st_ino) != ownership['destination']:
        raise ValueError('restore destination inode differs from this run')
    marker = json.loads(service.safe_read(path / 'ownership.json', private=True, owner=os.geteuid()))
    if marker != {'run_id': run_id}:
        raise ValueError('restore destination ownership differs')
    shutil.rmtree(path)


def run(options):
    experiment = Run(run_id=options.run_id)
    retrieval = Run(run_id=options.run_id)
    error = None
    cleanup = []
    ownership = {}
    try:
        validate_options(options)
        expected = checked_expected(options.expected_state)
        root = ROOT / '.tmp/forgejo'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='retrieve-', dir=root) as scratch:
            selected = Path(scratch) / options.archive_id
            retrieve(retrieval, options, selected)
            cleanup = retrieval.cleanup()
            if cleanup:
                raise RuntimeError('retrieval cleanup failed before restore startup')
            checked_destination(options.destination)
            restore_local(experiment, selected, options, expected, ownership=ownership)
    except (OSError, ValueError, RuntimeError, KeyError, tarfile.TarError):
        error = 'exact archive restore or independent application acceptance failed'
    finally:
        cleanup += experiment.cleanup()
        cleanup += retrieval.cleanup()
        if ownership and not cleanup:
            try:
                cleanup_destination(options.destination, ownership, experiment.run_id)
            except (OSError, ValueError):
                cleanup.append('owned restore destination cleanup failed')
        experiment.evidence(error, cleanup)
    if error or cleanup:
        print(json.dumps({'operation_error': error, 'cleanup_errors': cleanup}))
        return 1
    print('Exact Forgejo archive recovered; application oracle and owned cleanup passed')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Attended exact Forgejo archive restore experiment')
    parser.add_argument('--attended', required=True, action='store_true')
    for name in ('archive-id', 'remote', 'run-id', 'confirm-restore-experiment'):
        parser.add_argument('--' + name, required=True)
    for name in ('rclone-config', 'settings-dir', 'expected-state', 'destination'):
        parser.add_argument('--' + name, required=True, type=Path)
    values = vars(parser.parse_args(argv))
    return run(Options(**values))
