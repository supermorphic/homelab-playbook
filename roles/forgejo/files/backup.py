"""Consistent capture and independently supervised availability recovery."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid

import archive
import service


def private_write(path, value):
    service.atomic_write(path, json.dumps(value, sort_keys=True).encode() + b'\n',
        mode=0o600, uid=os.geteuid(), gid=os.getegid())


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip() if sys.platform == 'linux' else 'fixture'


def intent_for(host):
    return {'schema': 1, 'generation': host.settings['recovery_generation'],
            'boot_id': boot_id(), 'pid': os.getpid(), 'phase': 'capturing'}


def checked_intent(host):
    path = host.state / 'restart-intent.json'
    intent = json.loads(service.safe_read(path, private=True, owner=os.geteuid()))
    if (intent.get('schema') != 1 or intent.get('generation') != host.settings['recovery_generation']
            or intent.get('phase') not in ('capturing', 'recovering', 'failed')
            or type(intent.get('pid')) is not int or intent['pid'] < 1):
        raise RuntimeError('backup recovery intent differs from the active generation')
    return intent


def capture_locked(host):
    host.preflight()
    intent = host.state / 'restart-intent.json'
    if intent.exists() or (host.state / 'recovery-failed').exists():
        raise RuntimeError('unfinished recovery blocks capture')
    if not host.active():
        return {'skipped': 'application-inactive'}
    created = datetime.now(timezone.utc).replace(microsecond=0)
    identifier = 'forgejo-' + created.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:16]
    staging = host.backups / ('.partial-' + identifier)
    staging.mkdir(mode=0o700)
    private_write(intent, intent_for(host))
    host.stop()
    host.stopped()
    host.dump(staging / 'database.dump')
    host.stopped()
    host.files(staging / 'files.tar.gz')
    host.stopped()
    host.readable_dump(staging / 'database.dump')
    manifest = {key: host.settings[key] for key in ('image', 'postgres_image', 'rclone_image', 'recovery_generation')}
    manifest.update(schema=1, archive_id=identifier,
        created_at=created.strftime('%Y-%m-%dT%H:%M:%SZ'), postgres_major=17, layout=archive.LAYOUT)
    archive.seal(staging, manifest)
    destination = host.backups / identifier
    if destination.exists():
        raise RuntimeError('new archive identifier unexpectedly exists')
    staging.rename(destination)
    descriptor = os.open(host.backups, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    saved = checked_intent(host)
    saved['archive_id'] = identifier
    private_write(intent, saved)
    return {'archive_id': identifier}


def recover_locked(host):
    intent = host.state / 'restart-intent.json'
    if not intent.exists():
        if (host.state / 'recovery-failed').exists():
            raise RuntimeError('recovery failure lacks valid restart intent')
        return
    saved = checked_intent(host)
    saved['phase'] = 'recovering'
    private_write(intent, saved)
    try:
        host.start()
        host.health()
    except Exception:
        saved['phase'] = 'failed'
        private_write(intent, saved)
        private_write(host.state / 'recovery-failed', {'schema': 1, 'recovery_failed': True})
        raise
    identifier = saved.get('archive_id')
    if identifier is not None:
        archive.archive_time(identifier)
        # Capture sealed and fsynced this generation before recording its ID.
        # Transfer independently verifies its payloads. Availability recovery
        # must not hash or decompress archive-sized data under its 300s budget.
    intent.unlink(missing_ok=True)
    (host.state / 'recovery-failed').unlink(missing_ok=True)
    if identifier is not None:
        private_write(host.state / 'backup-status.json', {'schema': 1, 'archive_id': identifier,
            'recovered_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
    return identifier


def recover(host):
    with service.operation_lock(host.lock, getattr(host, 'lock_timeout', 300)):
        identifier = recover_locked(host)
    if identifier is not None:
        host.trigger_transfer()


def perform_capture(host):
    failure = None
    result = None
    owned_intent = False
    try:
        with service.operation_lock(host.lock, getattr(host, 'lock_timeout', 300)):
            pending = (host.state / 'restart-intent.json').exists()
            try:
                result = capture_locked(host)
            finally:
                owned_intent = not pending and (host.state / 'restart-intent.json').exists()
    except Exception as error:
        failure = error
    if owned_intent:
        try:
            recover(host)
        except Exception as error:
            if failure is not None:
                raise RuntimeError('capture failed; recovery also failed') from failure
            raise RuntimeError('archive published; recovery failed') from error
    if failure is not None:
        raise failure
    return result


def stream_command(arguments, *, output=None, source=None, timeout=1800, environment=None):
    descriptor = None
    incoming = None
    try:
        if output is not None:
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        if source is not None:
            incoming = open(source, 'rb')
        with __import__('tempfile').TemporaryFile() as errors:
            process = subprocess.Popen(arguments, stdin=incoming or subprocess.DEVNULL,
                stdout=descriptor if descriptor is not None else subprocess.DEVNULL,
                stderr=errors, env=environment, start_new_session=True)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise RuntimeError('archive client exceeded its execution budget') from None
            if code:
                raise RuntimeError('archive client failed')
        if descriptor is not None:
            os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if incoming is not None:
            incoming.close()


class Host:
    def __init__(self):
        self.state = service.STATE
        self.backups = self.state / 'backups'
        self.lock = service.LOCK
        self.allocation = service.account_allocation()
        if os.geteuid() != self.allocation['uid']:
            raise ValueError('backup must run as its declared service account')
        self.settings = json.loads(service.safe_read(service.HELPERS / 'desired.json', owner=0))
        self.environment = {'PATH': '/usr/bin:/bin', 'HOME': '/var/lib/svc-forgejo',
            'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': f"/run/user/{self.allocation['uid']}"}
    def preflight(self):
        if (service.HELPERS / 'activation-pending.json').exists():
            raise RuntimeError('configuration activation must complete before capture')
        if service.account_allocation() != self.settings['allocation']:
            raise ValueError('foundation allocation changed')
        service.check_boundaries(self.allocation)
        service.check_database(self.state / 'database')
        service.manager(['is-active', 'forgejo-postgres.service'])
        for container, key in (('forgejo', 'image'), ('forgejo-postgres', 'postgres_image')):
            if not self.active() and container == 'forgejo':
                continue
            image = service.user_command(['/usr/bin/podman', 'image', 'inspect', '--format', '{{.Id}}', self.settings[key]], self.allocation).stdout.strip()
            running = service.user_command(['/usr/bin/podman', 'inspect', '--format', '{{.Image}}', container], self.allocation).stdout.strip()
            if image != running:
                raise ValueError('backup requires the exact declared runtime image')
        total = 0
        for root in (self.state / 'data', self.state / 'config', self.state / 'database'):
            for path in root.rglob('*'):
                item = path.lstat()
                if not (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode)):
                    raise ValueError('backup source contains unsupported filesystem state')
                if stat.S_ISREG(item.st_mode):
                    total += item.st_size
        if shutil.disk_usage(self.backups).free < max(256 * 1024 * 1024, total * 2):
            raise RuntimeError('insufficient space for a complete local archive')
    def active(self):
        return service.manager(['is-active', 'forgejo.service'], check=False).returncode == 0
    def stop(self):
        service.manager(['stop', 'forgejo.service'], timeout=120)
    def stopped(self):
        if self.active():
            raise RuntimeError('application unit has not stopped')
        running = service.user_command(['/usr/bin/podman', 'ps', '--filter', 'name=^forgejo$', '--format', '{{.ID}}'], self.allocation).stdout.strip()
        if running:
            raise RuntimeError('application container writers remain active')
    def dump(self, path):
        stream_command(['/usr/bin/podman', 'exec', 'forgejo-postgres', 'pg_dump', '-U', 'forgejo', '-d', 'forgejo', '-Fc', '--no-owner', '--no-acl'], output=path, environment=self.environment)
    def files(self, path):
        name = 'forgejo-archive-client'
        if service.user_command(['/usr/bin/podman', 'container', 'exists', name], self.allocation, check=False).returncode == 0:
            raise RuntimeError('archive client name is already occupied')
        try:
            stream_command(['/usr/bin/podman', 'run', '--rm', '--name', name,
                '--label', 'io.homelab.forgejo.client=archive', '--pull=never', '--network=none',
                '--userns=keep-id:uid=1000,gid=1000', '--user', '1000:1000', '--read-only',
                '--volume', f'{self.state}/data:/archive/data:ro',
                '--volume', f'{self.state}/config:/archive/config:ro', '--entrypoint', 'tar',
                self.settings['postgres_image'], '--hard-dereference', '-czf', '-', '-C', '/archive', 'data', 'config'],
                output=path, environment=self.environment)
        finally:
            exists = service.user_command(['/usr/bin/podman', 'container', 'exists', name], self.allocation, check=False)
            if exists.returncode == 0:
                label = service.user_command(['/usr/bin/podman', 'inspect', '--format', '{{index .Config.Labels "io.homelab.forgejo.client"}}', name], self.allocation).stdout.strip()
                if label != 'archive':
                    raise RuntimeError('archive cleanup ownership differs')
                service.user_command(['/usr/bin/podman', 'rm', '--force', name], self.allocation)
    def readable_dump(self, path):
        stream_command(['/usr/bin/podman', 'exec', '-i', 'forgejo-postgres', 'pg_restore', '--list'], source=path, timeout=120, environment=self.environment)
    def start(self):
        service.manager(['start', 'forgejo.service'], timeout=180)
    def health(self):
        service.wait_health(self.settings['backend_port'], min(60, self.settings['health_timeout_seconds']))
    def trigger_transfer(self):
        service.manager(['start', '--no-block', 'forgejo-transfer.service'])


def guard(host):
    if not (host.state / 'restart-intent.json').exists():
        if (host.state / 'recovery-failed').exists():
            raise RuntimeError('previous recovery failed; authorize recovery separately')
        return
    saved = checked_intent(host)
    if saved['phase'] == 'failed':
        raise RuntimeError('previous recovery failed; authorize recovery separately')
    if saved['phase'] == 'capturing' and saved['boot_id'] == boot_id():
        try:
            os.kill(saved['pid'], 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError('active capture still requires the application stopped')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Internal Forgejo backup lifecycle')
    parser.add_argument('action', choices=('capture', 'recover', 'guard'))
    options = parser.parse_args(argv)
    os.umask(0o077)
    try:
        host = Host()
        if options.action == 'capture':
            # ExecStopPost queues the independent 300-second recovery unit.
            with service.operation_lock(host.lock, 300):
                result = capture_locked(host)
        elif options.action == 'recover':
            recover(host)
            result = {'recovered': True}
        else:
            guard(host)
            result = {'startup_guard': True}
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError):
        print('Forgejo backup operation failed; inspect capture and recovery unit status', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
