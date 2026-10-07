"""Installed, trusted per-slot supervisor and observational verification entry."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import secrets

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lifecycle import CheckpointStore, read_checkpoint, run_slot, bound_checkpoint
from registration import RegistrationClient
from resources import OwnedRuntime
from verify import observe, execute


def private_token(path, *, authority_uid=0):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'r') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != authority_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError('Enrollment input is not private and authority-owned')
        raw = stream.read(4097)
    value = raw.removesuffix('\n')
    if not value or len(raw) > 4096 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('Enrollment input is absent or malformed')
    return value


def load_configuration(path):
    from scripts.forgejo_runner.host_probe import secure_path
    path = Path(path)
    if not secure_path(path.parent):
        raise ValueError('Configuration parent is not authority-owned')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError('Installed configuration is not private and authority-owned')
        raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('Installed configuration exceeded its fixed bound')
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {'schema', 'slot', 'manifest', 'assets_root', 'installation'}
            or type(value['schema']) is not int or value['schema'] != 1):
        raise ValueError('Installed configuration has an unsupported schema')
    slot = value['slot']
    if (not isinstance(slot, dict) or not isinstance(slot.get('name'), str)
            or re.fullmatch(r'[a-z][a-z0-9-]{0,47}', slot['name']) is None
            or slot.get('state_root') != '/var/lib/forgejo-runner/' + slot['name']
            or path != Path(slot['state_root']) / 'config.json' or type(slot.get('enabled')) is not bool
            or not isinstance(slot.get('limits'), dict)
            or any(type(number) is not int or not 0 < number < 2**63 for number in slot['limits'].values())
            or re.fullmatch(r'/usr/local/libexec/forgejo-runner/[0-9a-f]{40}', value['installation']) is None
            or re.fullmatch(r'/var/lib/forgejo-runner/assets/[0-9a-f]{64}', value['assets_root']) is None):
        raise ValueError('Installed slot is outside its canonical boundaries')
    return value


@contextlib.contextmanager
def admission_lease(slot, *, recovery_only=False):
    """NUC4 admits one qualified worker across its three repository slots."""
    root = Path(slot['state_root']).parent
    lease = CheckpointStore(root)
    try:
        lease.__enter__()
    except BlockingIOError:
        yield False
        return
    try:
        descriptor = os.open(root / 'slots.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != 0
                    or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError('Host slot registry is not private and authority-owned')
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError('Host slot registry exceeds its fixed bound')
        registry = json.loads(raw)
        if (not isinstance(registry, dict) or set(registry) != {'schema', 'slots'}
                or type(registry['schema']) is not int or registry['schema'] != 1
                or not isinstance(registry['slots'], list) or not 1 <= len(registry['slots']) <= 3):
            raise ValueError('Host slot registry is invalid')
        slots = registry['slots']
        if sum(candidate == slot for candidate in slots) != 1:
            raise ValueError('Slot differs from its host admission registry')
        for candidate in slots:
            if (not isinstance(candidate, dict) or not isinstance(candidate.get('name'), str)
                    or re.fullmatch(r'[a-z][a-z0-9-]{0,47}', candidate['name']) is None
                    or candidate.get('state_root') != '/var/lib/forgejo-runner/' + candidate['name']):
                raise ValueError('Host admission registry contains an unsafe slot')
        allowed = True
        if not recovery_only:
            for candidate in slots:
                if candidate == slot:
                    continue
                snapshot = observe(candidate)
                if snapshot['phase'] != 'clean' or not snapshot['runtime_absent']:
                    allowed = False
                    break
        yield allowed
    finally:
        lease.__exit__(None, None, None)


def restore_authority(value, raw, repository):
    slot = value['slot']
    token = raw.removesuffix('\n')
    if (repository != slot['repository'] or not token or len(raw.encode()) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in token)):
        raise ValueError('Replacement authority must match the installed repository and private input contract')
    with CheckpointStore(slot['state_root']) as store:
        state = store.read()
        bound_checkpoint(slot, state)
        timer = execute(['systemctl', 'show', '--property=ActiveState', '--value',
                         'forgejo-runner-' + slot['name'] + '.timer']).strip()
        if timer != 'inactive' or not observe(slot)['runtime_absent']:
            raise ValueError('Stop admission and dispose the owned runtime before restoring authority')
        try:
            current = os.stat('enrollment-token', dir_fd=store.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or current.st_uid != os.geteuid() or stat.S_IMODE(current.st_mode) != 0o600):
                raise ValueError('Existing enrollment input has ambiguous ownership')
        temporary = '.enrollment-' + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=store.directory_fd)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(token + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, 'enrollment-token', src_dir_fd=store.directory_fd, dst_dir_fd=store.directory_fd)
            os.fsync(store.directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=store.directory_fd)
            except FileNotFoundError:
                pass
    return {'authority_restored': True, 'admission_stopped': True}


class UnavailableEnrollment:
    def lookup(self, *arguments):
        raise RuntimeError('Enrollment authority is unavailable; registration reconciliation remains pending')


def perform(mode, value):
    slot = value['slot']
    root = Path(slot['state_root'])
    if mode in ('verify', 'inspect'):
        result = observe(slot)
        if mode == 'verify':
            raw = execute(['systemctl', 'show', '--property=LoadState,ActiveState,UnitFileState',
                           'forgejo-runner-' + slot['name'] + '.timer'])
            timer = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
            result['polling_verified'] = (
                timer.get('LoadState') == 'loaded'
                and timer.get('ActiveState') == ('active' if slot['enabled'] else 'inactive')
                and timer.get('UnitFileState') == ('enabled' if slot['enabled'] else 'disabled'))
            result['verified'] = result['verified'] and result['polling_verified']
        state = read_checkpoint(root)
        registration = state['registration']
        if registration is not None:
            client = RegistrationClient('https://forgejo.infra.supermorphic.com', slot['repository'],
                                        private_token(root / 'enrollment-token'))
            found = client.lookup(slot['repository'], slot['name'], state['generation'])
            result['registration_verified'] = found is not None and found.id == registration['id']
            result['verified'] = result['verified'] and result['registration_verified']
        return result, 0 if result['verified'] else 1
    with CheckpointStore(root) as store:
        state = store.read()
        if state['phase'] == 'clean' and (mode == 'recover' or not slot['enabled']):
            result = observe(slot)
            return result, 0 if result['runtime_absent'] else 1
        if mode == 'cycle' and state['phase'] == 'quarantined':
            return {'phase': 'quarantined'}, 1
        recovery_only = mode == 'recover' or state['phase'] != 'clean'
        with admission_lease(slot, recovery_only=recovery_only) as admitted:
            if not admitted:
                return {'admission_busy': True}, 0
            try:
                client = RegistrationClient('https://forgejo.infra.supermorphic.com', slot['repository'],
                                            private_token(root / 'enrollment-token'))
            except (OSError, ValueError):
                if not recovery_only:
                    raise
                # Trusted disposal does not depend on a working secret store.
                # Unknown remote retirement keeps the slot quarantined.
                client = UnavailableEnrollment()
            handle = None
            if not recovery_only:
                handle = client.pending_job(slot['label'])
                if handle is None:
                    # Observe idle absence too; missing metadata alone is not
                    # proof that a prior job's processes and files are gone.
                    result = observe(slot)
                    return result, 0 if result['runtime_absent'] else 1
            runtime = OwnedRuntime(slot, value['installation'], value['assets_root'], value['manifest'], handle, store)
            code = run_slot(slot, runtime, client, store, recovery_only=recovery_only)
            return {'phase': store.read()['phase'], 'exit_code': code}, code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('cycle', 'verify', 'inspect', 'recover', 'restore-authority'))
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--repository', help='Exact installed scope required when restoring private authority from stdin')
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('This installed helper requires administrator authority')
    try:
        value = load_configuration(arguments.config)
        if arguments.mode == 'restore-authority':
            result, code = restore_authority(value, sys.stdin.read(4097), arguments.repository), 0
        else:
            result, code = perform(arguments.mode, value)
    except Exception as error:
        print(json.dumps({'exit_code': 1, 'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
