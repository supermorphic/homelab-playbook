"""Copy verified Forgejo generations and apply creation-time retention."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

import archive
import backup
import service


def remote_complete(remote, identifier, expected=None):
    entries = remote.listing(identifier)
    if 'COMPLETE' not in entries:
        if not set(entries).issubset(archive.PAYLOADS) or any(item['directory'] or item['link'] for item in entries.values()):
            raise ValueError('unexpected incomplete remote archive layout')
        return False
    if set(entries) != {*archive.PAYLOADS, 'COMPLETE'}:
        raise ValueError('remote completed archive has unexpected contents')
    if any(entries[name]['size'] > 65536 for name in ('COMPLETE', 'manifest.json', 'SHA256SUMS')):
        raise ValueError('remote control metadata exceeds its bound')
    marker = json.loads(remote.read(identifier, 'COMPLETE'))
    if (marker.get('schema') != 1 or marker.get('archive_id') != identifier
            or set(marker.get('files', {})) != set(archive.PAYLOADS)):
        raise ValueError('remote completion metadata is malformed')
    for name in archive.PAYLOADS:
        item = marker['files'][name]
        if (set(item) != {'size', 'sha256'} or type(item['size']) is not int or item['size'] < 1
                or re.fullmatch(r'[0-9a-f]{64}', item.get('sha256', '')) is None
                or entries[name]['directory'] or entries[name]['link']
                or entries[name]['size'] != item['size']):
            raise ValueError('remote payload metadata differs from completion')
    if expected is not None and marker != expected:
        raise ValueError('remote completion differs from the selected local generation')
    controls = {}
    for name in ('manifest.json', 'SHA256SUMS'):
        content = remote.read(identifier, name)
        if hashlib.sha256(content).hexdigest() != marker['files'][name]['sha256']:
            raise ValueError('remote control-file checksum differs from completion')
        controls[name] = content
    manifest = archive.validate_manifest(json.loads(controls['manifest.json']))
    if manifest['archive_id'] != identifier:
        raise ValueError('remote archive identifier differs from its manifest')
    sums = ''.join(marker['files'][name]['sha256'] + '  ' + name + '\n'
                   for name in archive.PAYLOADS[:3])
    if controls['SHA256SUMS'].decode() != sums:
        raise ValueError('remote checksum sidecar differs from completion')
    return True


def remove_local(path):
    archive.validate_archive(path, False)
    # Delete only known regular payloads, never recursively follow a tree.
    for name in archive.PAYLOADS:
        (path / name).unlink()
    if (path / 'COMPLETE').exists():
        (path / 'COMPLETE').unlink()
    path.rmdir()


def transfer(backups, remote, *, now=None):
    now = now or datetime.now(timezone.utc)
    errors = []
    verified = 0
    transferred = 0
    for path in sorted(backups.iterdir()):
        identifier = path.name
        if identifier.startswith('.partial-'):
            continue
        try:
            created = archive.archive_time(identifier)
            if created > now:
                raise ValueError('future archive creation time')
            archive.validate_archive(path, False)
            expected = archive.completion(path)
            complete = remote_complete(remote, identifier, expected)
            age = (now - created).total_seconds() / 86400
            if not complete:
                if age >= 90:
                    raise ValueError('expired untransferred archive requires operator resolution')
                remote.copy(path, identifier)
                remote.check(path, identifier)
                # Publish only after checking every payload, including both controls.
                remote.publish(identifier, (json.dumps(expected, sort_keys=True) + '\n').encode())
                if not remote_complete(remote, identifier, expected):
                    raise RuntimeError('completion publication could not be verified')
                transferred += 1
            verified += 1
            if age >= 7:
                remove_local(path)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            errors.append({'archive_id': identifier if archive.NAME.fullmatch(identifier) else 'unrecognized-entry',
                           'phase': 'transfer-or-local-retention'})
    try:
        entries = remote.listing()
    except (OSError, ValueError, RuntimeError):
        errors.append({'phase': 'remote-listing'})
        entries = {}
    for identifier, item in entries.items():
        try:
            created = archive.archive_time(identifier)
            if not item['directory'] or item['link'] or created > now:
                raise ValueError('unexpected remote generation')
            if (now - created).total_seconds() < 90 * 86400:
                continue
            if not remote_complete(remote, identifier):
                raise ValueError('expired remote generation is incomplete')
            if (backups / identifier).exists():
                raise ValueError('local generation has not yet been safely retained')
            # Keep the marker until all payload deletions complete; partial deletion
            # fails closed on the next run rather than treating leftovers as complete.
            for name in archive.PAYLOADS:
                remote.remove_file(identifier, name)
            remote.remove_file(identifier, 'COMPLETE')
            remote.remove_directory(identifier)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            errors.append({'archive_id': identifier if archive.NAME.fullmatch(identifier) else 'unrecognized-entry',
                           'phase': 'remote-retention'})
    return {'schema': 1, 'verified': verified, 'transferred': transferred, 'errors': errors,
            'observed_at': now.strftime('%Y-%m-%dT%H:%M:%SZ')}


class Remote:
    """Pinned rclone clients; private configuration never enters argv or output."""
    def __init__(self, settings, *, backups, config, prefix, command=None):
        self.settings = settings
        self.backups = backups
        self.config = config
        self.prefix = prefix.rstrip('/')
        if re.fullmatch(r'[A-Za-z0-9_-]+:[A-Za-z0-9][A-Za-z0-9_./-]*', self.prefix) is None or '..' in self.prefix:
            raise ValueError('invalid remote prefix')
        self.command_adapter = command
        self.name = 'forgejo-transfer-client'
        self.label = 'io.homelab.forgejo.client'
        self.marker_mount = None
    def cleanup(self):
        present = service.execute(['/usr/bin/podman', 'container', 'exists', self.name], check=False)
        if present.returncode == 1:
            return
        if present.returncode:
            raise RuntimeError('transfer client existence check failed')
        actual = service.execute(['/usr/bin/podman', 'inspect', '--format', '{{index .Config.Labels "io.homelab.forgejo.client"}}', self.name]).stdout.strip()
        if actual != 'transfer':
            raise RuntimeError('transfer client ownership differs')
        service.execute(['/usr/bin/podman', 'rm', '--force', self.name])
    def command(self, arguments, *, check=True):
        if self.command_adapter:
            return self.command_adapter(arguments, check=check, marker_mount=self.marker_mount)
        present = service.execute(['/usr/bin/podman', 'container', 'exists', self.name], check=False)
        if present.returncode != 1:
            raise RuntimeError('transfer client name is already occupied')
        argv = ['/usr/bin/podman', 'run', '--rm', '--name', self.name,
            '--label', self.label + '=transfer', '--pull=never',
            '--userns=keep-id:uid=1000,gid=1000', '--user', '1000:1000', '--read-only',
            '--volume', f'{self.backups}:/backups:ro',
            '--volume', f'{self.config}:/run/secrets/rclone.conf:ro',
        ]
        if self.marker_mount is not None:
            argv += ['--volume', f'{self.marker_mount}:/transfer-metadata/COMPLETE:ro']
        argv += [self.settings['rclone_image'], '--config', '/run/secrets/rclone.conf', *arguments]
        try:
            return service.execute(argv, check=check, timeout=600)
        finally:
            self.cleanup()
    def destination(self, identifier):
        archive.archive_time(identifier)
        return self.prefix + '/' + identifier
    def listing(self, identifier=None):
        destination = self.destination(identifier) if identifier else self.prefix
        result = self.command(['lsjson', '--links', destination], check=False)
        if result.returncode == 3:
            return {}
        if result.returncode:
            raise RuntimeError('remote listing failed')
        values = json.loads(result.stdout)
        entries = {}
        for value in values:
            name = value['Path']
            if '/' in name or name in entries:
                raise ValueError('unexpected remote path')
            entries[name] = {'size': value['Size'], 'directory': value['IsDir'],
                             'link': name.endswith('.rclonelink') or value.get('IsLink', False)}
        return entries
    def read(self, identifier, name):
        if name not in ('COMPLETE', 'manifest.json', 'SHA256SUMS'):
            raise ValueError('metadata query cannot download payloads')
        return self.command(['cat', self.destination(identifier) + '/' + name]).stdout.encode()
    def copy(self, source, identifier):
        filters = [argument for name in archive.PAYLOADS for argument in ('--include', '/' + name)]
        self.command(['copy', '/backups/' + identifier, self.destination(identifier), *filters])
    def check(self, source, identifier):
        filters = [argument for name in archive.PAYLOADS for argument in ('--include', '/' + name)]
        self.command(['check', '--download', '--one-way', '/backups/' + identifier,
                      self.destination(identifier), *filters])
    def publish(self, identifier, value):
        # Small public marker staged outside the archive, mounted independently.
        private = self.backups.parent / '.transfer-complete'
        service.atomic_write(private, value, mode=0o600, uid=os.geteuid(), gid=os.getegid())
        # Mount this one marker file independently of other service state.
        try:
            self.marker_mount = private
            self.command(['copyto', '/transfer-metadata/COMPLETE', self.destination(identifier) + '/COMPLETE'])
        finally:
            self.marker_mount = None
            private.unlink(missing_ok=True)
    def remove_file(self, identifier, name):
        if name not in (*archive.PAYLOADS, 'COMPLETE'):
            raise ValueError('retention cannot delete an unknown file')
        self.command(['deletefile', self.destination(identifier) + '/' + name])
    def remove_directory(self, identifier):
        self.command(['rmdir', self.destination(identifier)])


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Internal Forgejo archive transfer')
    parser.add_argument('action', choices=('run', 'cleanup'))
    options = parser.parse_args(argv)
    os.umask(0o077)
    try:
        allocation = service.account_allocation()
        if os.geteuid() != allocation['uid']:
            raise ValueError('transfer must run as the declared account')
        settings = json.loads(service.safe_read(service.HELPERS / 'desired.json', owner=0))
        remote = Remote(settings, backups=service.STATE / 'backups',
            config=service.STATE / 'credentials/rclone.conf', prefix=settings['rclone_remote'])
        if options.action == 'cleanup':
            remote.cleanup()
            return 0
        service.check_boundaries(allocation)
        lock = service.STATE / 'transfer.lock'
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        service.regular(lock, private=True, owner=allocation['uid'])
        with service.operation_lock(lock, 1):
            result = transfer(service.STATE / 'backups', remote)
            backup.private_write(service.STATE / 'transfer-status.json', result)
        print(json.dumps({'verified': result['verified'], 'transferred': result['transferred'],
                          'errors': len(result['errors'])}))
        return 1 if result['errors'] else 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        print('Forgejo archive transfer failed; inspect independent transfer status', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
