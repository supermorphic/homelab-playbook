"""Fixed host adapter for attended upgrades; isolated migration precedes admission."""
import argparse
from contextlib import contextmanager
import configparser
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
sys.dont_write_bytecode = True
import time

import admission
import install
import profiles
import service
import upgrade
import upgrade_state

LABEL = 'io.homelab.forgejo.upgrade'
CAPTURE_LOCK = Path('/run/forgejo/upgrade-capture.lock')
CONTINUITY_TABLES = ('repository', 'issue', 'pull_request', 'user', 'access_token', 'action_run', 'action_run_job')


def container_name(identifier, kind):
    if re.fullmatch(r'[0-9a-f]{32}', identifier) is None or kind not in ('app', 'postgres'):
        raise ValueError('invalid isolated upgrade resource identity')
    return 'forgejo-upgrade-' + identifier + '-' + kind


def database_arguments(settings, state, identifier):
    return ['/usr/bin/podman', 'run', '--detach', '--name', container_name(identifier, 'postgres'),
            '--label', LABEL + '=' + identifier, '--restart', 'no', '--network', 'none',
            '--userns', 'keep-id:uid=999,gid=999', '--user', '999:999',
            '--env-file', str(state / 'credentials/postgres.env'),
            '--volume', str(state / 'credentials/postgres-admin-password') + ':/run/secrets/postgres-admin-password:ro,z',
            '--volume', str(state / 'database') + ':/var/lib/postgresql/data:rw,z',
            '--security-opt', 'no-new-privileges', settings['postgres_image']]


def application_arguments(settings, state, config, identifier):
    return ['/usr/bin/podman', 'run', '--detach', '--name', container_name(identifier, 'app'),
            '--label', LABEL + '=' + identifier, '--restart', 'no',
            '--network', 'container:' + container_name(identifier, 'postgres'),
            '--userns', 'keep-id:uid=1000,gid=1000', '--user', '1000:1000',
            '--env', 'GITEA_APP_INI=/etc/gitea/app.ini',
            '--volume', str(state / 'data') + ':/var/lib/gitea:rw,z',
            '--volume', str(config) + ':/etc/gitea:rw,z',
            '--security-opt', 'no-new-privileges', settings['image']]


def maintenance_config(text):
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.optionxform = str
    parsed.read_string('[forgejo_globals]\n' + text)
    parsed['database']['HOST'] = '127.0.0.1:5432'
    if 'cron' not in parsed:
        parsed.add_section('cron')
    parsed['cron']['ENABLED'] = 'false'
    result = io.StringIO()
    parsed.write(result)
    return result.getvalue().removeprefix('[forgejo_globals]\n')


def validate_prerequisites(document, saved, now=None, *, gate_since):
    now = now or datetime.now(timezone.utc)
    try:
        observed = datetime.strptime(document['observed_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        gated = datetime.fromisoformat(gate_since)
        age = (now - observed).total_seconds()
        if (type(document.get('schema')) is not int or document['schema'] != 1
                or document.get('transaction_id') != saved['id']
                or document.get('source_generation') != saved['source_generation']
                or not 0 <= age <= 300
                or gated.tzinfo is None or observed < gated
                or any(document.get(name) is not True for name in
                       ('runners_drained', 'queues_flushed', 'mirrors_quiesced'))):
            raise ValueError('invalid prerequisites')
    except (KeyError, TypeError, ValueError):
        raise ValueError('provide fresh transaction-bound attended runner, mirror and queue prerequisites') from None


def helper_digest(directory):
    hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in install.FILES}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


class Host:
    admin_uid = 0

    def __init__(self, candidate, prerequisites, *, source_profile, runners_drained=False):
        if os.geteuid() != 0:
            raise ValueError('upgrade requires managed host administration')
        self.helpers = service.HELPERS
        self.allocation = service.account_allocation()
        self.group = self.allocation['gid']
        self.candidate = candidate
        if not candidate.is_absolute() or candidate.parent != self.helpers / 'candidates':
            raise ValueError('upgrade candidate is outside private administrator staging')
        self.source = json.loads(service.safe_read(self.helpers / 'desired.json', owner=0))
        self.settings = service.validate_candidate(candidate, self.allocation)
        self.target = upgrade.pins(self.settings)
        self.source_profile = profiles.REVIEWED[source_profile]
        self.prerequisites = prerequisites
        self.runners_drained = runners_drained
        self.controller_sha256 = helper_digest(Path(__file__).resolve().parent)
        self.proxy = admission.Client()

    @contextmanager
    def locks(self):
        with service.operation_lock(service.LOCK, 300):
            yield

    def identity(self):
        return upgrade_state.process_identity()

    def command(self, arguments, **kwargs):
        return service.user_command(arguments, self.allocation, **kwargs)

    def private_dir(self, path, *, service_owned=False):
        base = service.STATE if service_owned else self.helpers
        if not path.is_relative_to(base):
            raise ValueError('upgrade directory is outside its fixed storage boundary')
        owner = self.allocation['uid'] if service_owned else 0
        current = base
        for component in path.relative_to(base).parts:
            current = current / component
            service.check_parent_chain(current)
            os.close(service.open_directory(current, create=True))
            metadata = current.lstat()
            if metadata.st_uid not in (0, owner) or metadata.st_mode & 0o077:
                raise ValueError('unsafe private upgrade directory')
            os.chown(current, owner, self.group)
            current.chmod(0o700)

    def write_file(self, path, content, *, service_owned=False):
        self.private_dir(path.parent, service_owned=service_owned)
        owner = self.allocation['uid'] if service_owned else 0
        service.atomic_write(path, content, mode=0o600, uid=owner, gid=self.group)

    def recovery(self, saved):
        return self.helpers / 'recovery' / saved['id']

    def maintenance(self, saved):
        return service.STATE / 'upgrade' / saved['id'] / 'config'

    def preflight(self, saved=None):
        if service.account_allocation() != self.allocation:
            raise ValueError('foundation allocation changed')
        service.check_boundaries(self.allocation)
        service.check_database(service.STATE / 'database')
        current = json.loads(service.safe_read(self.helpers / 'desired.json', owner=0))
        allowed = (self.source_profile,) if saved is None else (saved['source'], saved['target'])
        if upgrade.pins(current) not in allowed or current['allocation'] != self.allocation:
            raise ValueError('installed source differs from the reviewed transaction')
        for name in ('restart-intent.json', 'recovery-failed'):
            if (service.STATE / name).exists():
                raise RuntimeError('unfinished backup recovery blocks upgrade')
        if (self.helpers / 'activation-pending.json').exists():
            raise RuntimeError('unfinished provisioning activation blocks upgrade')
        with service.operation_lock(CAPTURE_LOCK, 1):
            pass
        service.check_stable_inputs(self.candidate, service.targets_for(self.allocation))
        # Configuration changes need a separate compatible provision first.
        for name, target in service.targets_for(self.allocation).items():
            expected = service.safe_read(target)
            if name == 'quadlets/forgejo.container':
                expected = expected.replace(current['image'].encode(), self.target['image'].encode())
            if service.safe_read(self.candidate / name) != expected:
                raise ValueError('upgrade candidate changes configuration outside the application image')
        if saved is None:
            if self.runners_drained is not True:
                raise ValueError('attended runner drain is required before preparation')
            service.verify()
            self.assert_no_running_jobs()
            with service.transfer_operation_lock(self.allocation):
                timers = [unit for unit in ('forgejo-backup.timer', 'forgejo-transfer.timer')
                          if service.manager(['is-active', unit], check=False).returncode == 0]
                try:
                    for unit in timers:
                        service.manager(['stop', unit])
                    for unit in ('forgejo-backup.service', 'forgejo-recover.service', 'forgejo-transfer.service'):
                        active = service.manager(['show', '--property=ActiveState', '--value', unit], check=False)
                        if active.returncode == 0 and active.stdout.strip() not in ('', 'inactive', 'failed'):
                            raise RuntimeError('source helper operation must finish before guard installation')
                    install.publish_bundle(Path(__file__).resolve().parent, self.helpers,
                        {'image': current['image'], 'postgres_image': current['postgres_image']},
                        current, group=self.group)
                finally:
                    for unit in timers:
                        service.manager(['start', unit])
        elif saved['phase'] in ('prepared', 'quiesced', 'archive-sealed'):
            if current['recovery_generation'] != saved['source_generation']:
                raise ValueError('source recovery generation changed')
        if saved is not None and saved['phase'] in (
                'quiesced', 'archive-sealed', 'migration-started', 'target-verified', 'accepted'):
            if not (self.recovery(saved) / 'COMPLETE').exists():
                raise ValueError('source recovery package has no completed preservation record')
            self.preserve(saved)
        for key in profiles.KEYS:
            if self.command(['/usr/bin/podman', 'image', 'exists', self.target[key]], check=False).returncode:
                self.command(['/usr/bin/podman', 'pull', self.target[key]], timeout=600)
        service.validate_generator(self.candidate, self.allocation)

    def assert_no_running_jobs(self):
        # StatusRunning=6 is the reviewed Forgejo actions Status contract.
        count = service.database_command(self.allocation,
            'SELECT count(*) FROM action_task WHERE status=6;', user='forgejo', database='forgejo')
        if count != '0':
            raise RuntimeError('running Actions tasks must drain before source shutdown')

    def preserve(self, saved):
        root = self.recovery(saved)
        self.private_dir(root)
        document = root / 'source.json'
        seal = root / 'COMPLETE'
        if seal.exists():
            source = json.loads(service.safe_read(document, private=True, owner=0))
            if upgrade.pins(source) != saved['source'] or source['recovery_generation'] != saved['source_generation']:
                raise ValueError('preserved source controls differ from the transaction')
            recorded = json.loads(service.safe_read(seal, private=True, owner=0))
            try:
                current = self.recovery_hashes(root)
            except OSError:
                raise ValueError('preserved source recovery inputs are incomplete') from None
            if recorded != current:
                raise ValueError('preserved source recovery inputs changed')
            return
        for name, source in service.targets_for(self.allocation).items():
            self.write_file(root / 'controls' / name, service.safe_read(source))
        selected = self.helpers / 'helper-generation.json'
        selector = json.loads(service.safe_read(selected, owner=0))
        bundle = self.helpers / 'bundles' / selector['generation']
        for name in install.FILES:
            self.write_file(root / 'helpers' / name, service.safe_read(bundle / name, owner=0))
        self.write_file(root / 'helper-generation.json', service.safe_read(selected, owner=0))
        self.write_file(document, service.safe_read(self.helpers / 'desired.json', owner=0))
        # Old secrets remain private on the host; no controller output contains their values.
        for name in ('database-password', 'secret-key', 'internal-token', 'lfs-jwt-secret', 'oauth2-jwt-secret'):
            self.write_file(root / 'settings' / name, service.safe_read(service.STATE / 'config' / name, private=True))
        self.write_file(root / 'settings/generation.json', json.dumps(
            {'schema': 1, 'recovery_generation': saved['source_generation']}).encode())
        self.write_file(seal, json.dumps(self.recovery_hashes(root), sort_keys=True).encode())

    def recovery_hashes(self, root):
        names = ['source.json', 'helper-generation.json', 'settings/generation.json']
        names += ['controls/' + name for name in service.targets_for(self.allocation)]
        names += ['helpers/' + name for name in install.FILES]
        names += ['settings/' + name for name in
                  ('database-password', 'secret-key', 'internal-token', 'lfs-jwt-secret', 'oauth2-jwt-secret')]
        return {name: hashlib.sha256(service.safe_read(root / name, private=True, owner=0)).hexdigest()
                for name in names}

    def block_admission(self, saved):
        path = self.recovery(saved) / 'admission.json'
        if path.exists():
            record = json.loads(service.safe_read(path, private=True, owner=0))
            def invalidate_epoch():
                record.pop('gated_at', None)
                self.write_file(path, json.dumps(record, sort_keys=True).encode())
            changed = admission.finish_preparation(self.proxy, record, before_replace=invalidate_epoch)
        else:
            def persist(record):
                self.write_file(path, json.dumps(record, sort_keys=True).encode())
            record = admission.prepare(self.proxy, self.source['hostname'], self.source['backend_port'], saved['id'], persist)
            changed = True
        if changed or 'gated_at' not in record:
            record['gated_at'] = datetime.now(timezone.utc).isoformat()
            self.write_file(path, json.dumps(record, sort_keys=True).encode())

    def pin(self, saved):
        with service.transfer_operation_lock(self.allocation):
            names = upgrade_state.protected_archives(self.helpers) | {saved['archive_id']}
            if len(names) > 256:
                raise ValueError('explicitly release obsolete protected recovery references before another upgrade')
            service.atomic_write(self.helpers / 'retained-archives.json', json.dumps(
                {'schema': 1, 'archives': sorted(names)}).encode(), mode=0o640, uid=0, gid=self.group)

    def quiesce(self, saved):
        gate = json.loads(service.safe_read(self.recovery(saved) / 'admission.json', private=True, owner=0))
        admission.require_gated(self.proxy, gate)
        validate_prerequisites(self.prerequisites, saved, gate_since=gate.get('gated_at'))
        self.assert_no_running_jobs()
        if 'backup_timer_active' not in saved:
            saved['backup_timer_active'] = service.manager(['is-active', 'forgejo-backup.timer'], check=False).returncode == 0
        upgrade.write(self, saved)
        if saved['backup_timer_active']:
            service.manager(['stop', 'forgejo-backup.timer'])
        service.manager(['stop', 'forgejo.service'], timeout=120)
        if service.manager(['is-active', 'forgejo.service'], check=False).returncode == 0:
            raise RuntimeError('source application has not stopped')
        admission.require_gated(self.proxy, gate)

    def capture(self, saved):
        existing = service.STATE / 'backups' / saved['archive_id']
        action = 'inspect-upgrade' if existing.exists() else 'capture-upgrade'
        result = self.command(['/usr/bin/python3', str(self.helpers / 'backup.py'),
            action, '--transaction-id', saved['id']], timeout=1900)
        captured = json.loads(result.stdout)
        captured['continuity'] = self.continuity('forgejo-postgres')
        return captured

    def check_archive(self, saved):
        result = self.command(['/usr/bin/python3', str(self.helpers / 'backup.py'),
            'inspect-upgrade', '--transaction-id', saved['id']], timeout=1900)
        current = json.loads(result.stdout)
        if any(current.get(key) != saved['capture'][key] for key in
               ('archive_id', 'source', 'source_generation', 'complete_sha256')):
            raise ValueError('protected source archive changed')

    def continuity(self, database):
        counts = {}
        for name in CONTINUITY_TABLES:
            result = self.command(['/usr/bin/podman', 'exec', database, 'psql', '-X',
                '-U', 'forgejo', '-d', 'forgejo', '--set', 'ON_ERROR_STOP=1', '-Atc',
                'SELECT count(*) FROM "' + name + '";'])
            counts[name] = int(result.stdout.strip())
        return counts

    def remove_owned(self, saved, kind):
        name = container_name(saved['id'], kind)
        if self.command(['/usr/bin/podman', 'container', 'exists', name], check=False).returncode == 1:
            return
        info = json.loads(self.command(['/usr/bin/podman', 'inspect', name]).stdout)[0]
        if info.get('Config', {}).get('Labels', {}).get(LABEL) != saved['id']:
            raise ValueError('isolated upgrade resource is not owned by this transaction')
        self.command(['/usr/bin/podman', 'rm', '--force', name], timeout=120)

    def migrate(self, saved):
        with service.operation_lock(CAPTURE_LOCK, 1):
            self.stop_isolated(saved)
            service.manager(['stop', 'forgejo-postgres.service'], timeout=120)
            config = self.maintenance(saved)
            self.private_dir(config, service_owned=True)
            for path in (service.STATE / 'config').iterdir():
                service.regular(path, private=True, owner=self.allocation['uid'])
                content = service.safe_read(path, private=True)
                if path.name == 'app.ini':
                    content = maintenance_config(content.decode()).encode()
                self.write_file(config / path.name, content, service_owned=True)
                if path.name == 'mirror-credential-helper.sh':
                    (config / path.name).chmod(0o700)
            self.command(database_arguments(saved['source'], service.STATE, saved['id']))
            database = container_name(saved['id'], 'postgres')
            self.wait(['/usr/bin/podman', 'exec', database, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'])
            self.command(application_arguments(saved['target'], service.STATE, config, saved['id']))
            self.wait(['/usr/bin/podman', 'exec', container_name(saved['id'], 'app'),
                       'curl', '--fail', '--silent', 'http://127.0.0.1:3000/api/healthz'])

    def wait(self, arguments):
        deadline = time.monotonic() + self.settings['health_timeout_seconds']
        while self.command(arguments, check=False, timeout=10).returncode:
            if time.monotonic() >= deadline:
                raise RuntimeError('isolated upgrade readiness deadline exceeded')
            time.sleep(1)

    def verify_isolated(self, saved):
        database = container_name(saved['id'], 'postgres')
        application = container_name(saved['id'], 'app')
        interfaces = self.command(['/usr/bin/podman', 'exec', application, 'cat', '/proc/net/dev']).stdout
        if {line.split(':', 1)[0].strip() for line in interfaces.splitlines() if ':' in line} != {'lo'}:
            raise RuntimeError('migration namespace has an external interface')
        if self.command(['/usr/bin/podman', 'exec', application, 'curl', '--silent', '--fail',
                         '--max-time', '2', 'http://203.0.113.1/'], check=False).returncode == 0:
            raise RuntimeError('migration namespace reached an external route')
        if self.continuity(database) != saved['capture']['continuity']:
            raise RuntimeError('migration changed independently captured application record counts')
        info = json.loads(self.command(['/usr/bin/podman', 'inspect', application]).stdout)[0]
        if any((info.get('NetworkSettings', {}).get('Ports') or {}).values()):
            raise RuntimeError('migration published a host listener')
        expected = self.command(['/usr/bin/podman', 'image', 'inspect', '--format', '{{.Id}}', saved['target']['image']]).stdout.strip()
        if info['Image'] != expected:
            raise RuntimeError('migration did not run the reviewed immutable target')

    def stop_isolated(self, saved):
        self.remove_owned(saved, 'app')
        self.remove_owned(saved, 'postgres')

    def publish(self, saved):
        settings = service.validate_candidate(self.candidate, self.allocation)
        targets = service.targets_for(self.allocation)
        service.publish_files(self.candidate, targets, self.allocation)
        settings['recovery_generation'] = saved['id']
        settings['checksums'] = {name: hashlib.sha256(service.safe_read(path)).hexdigest() for name, path in targets.items()}
        service.atomic_write(self.helpers / 'desired.json', (json.dumps(settings, sort_keys=True) + '\n').encode(),
                             mode=0o640, uid=0, gid=self.group)
        service.validate_generator(self.candidate, self.allocation)
        service.manager(['daemon-reload'])

    def activate(self, saved):
        service.manager(['start', 'forgejo-postgres.service'])
        self.wait(['/usr/bin/podman', 'exec', 'forgejo-postgres', 'pg_isready', '-U', 'postgres'])
        service.manager(['start', 'forgejo.service'])
        service.wait_health(self.settings['backend_port'], self.settings['health_timeout_seconds'])
        if saved.get('backup_timer_active'):
            service.manager(['start', 'forgejo-backup.timer'])
        admission.restore(self.proxy, json.loads(service.safe_read(
            self.recovery(saved) / 'admission.json', private=True, owner=0)))

    def verify_current(self, saved):
        result = service.verify(completing_activation=saved is not None and saved['phase'] == 'accepted')
        if not result['ready']:
            raise RuntimeError('target native verification failed')

    def abort_source(self, saved):
        service.manager(['start', 'forgejo-postgres.service'])
        self.wait(['/usr/bin/podman', 'exec', 'forgejo-postgres', 'pg_isready', '-U', 'postgres'])
        service.manager(['start', 'forgejo.service'])
        service.wait_health(self.source['backend_port'], self.source['health_timeout_seconds'])
        if saved.get('backup_timer_active'):
            service.manager(['start', 'forgejo-backup.timer'])
        path = self.recovery(saved) / 'admission.json'
        if path.exists():
            admission.restore(self.proxy, json.loads(service.safe_read(path, private=True, owner=0)))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Attended, guarded Forgejo application upgrade')
    parser.add_argument('action', choices=('prepare', 'upgrade', 'resume', 'abort'))
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--oracle-sha256', required=True)
    parser.add_argument('--prerequisites', type=Path)
    parser.add_argument('--source-profile', choices=tuple(profiles.REVIEWED), required=True)
    parser.add_argument('--runners-drained', action='store_true')
    options = parser.parse_args(argv)
    try:
        evidence = json.loads(service.safe_read(options.evidence, private=True, owner=0))
        if (re.fullmatch(r'[0-9a-f]{64}', options.oracle_sha256) is None
                or evidence.get('oracle_sha256') != options.oracle_sha256):
            raise ValueError('rehearsal oracle differs from the current controller checkout')
        prerequisites = {} if options.prerequisites is None else json.loads(
            service.safe_read(options.prerequisites, private=True, owner=0))
        host = Host(options.candidate, prerequisites, source_profile=options.source_profile,
                    runners_drained=options.runners_drained)
        if options.action == 'prepare':
            result = upgrade.perform(host, evidence, prepare_only=True)
        elif options.action == 'abort':
            result = upgrade.abort(host)
        elif options.action == 'resume':
            result = upgrade.resume(host, evidence)
        else:
            saved = upgrade_state.read(host.helpers)
            result = (upgrade.resume(host, evidence) if saved is not None and saved['phase'] == 'prepared'
                      else upgrade.perform(host, evidence))
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        print('Forgejo upgrade failed; retain the transaction and use explicit abort/resume or independent recovery', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
