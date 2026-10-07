"""Private supervisor checkpoints and exclusive per-slot admission."""

import contextlib
import fcntl
import json
import os
import re
import secrets
import stat


PHASES = {'clean', 'preparing', 'registered', 'running', 'cleaning', 'quarantined'}
FIELDS = {'phase', 'generation', 'allocation', 'registration', 'primary_error', 'cleanup_errors'}


def clean_checkpoint():
    return {'phase': 'clean', 'generation': None, 'allocation': None,
            'registration': None, 'primary_error': None, 'cleanup_errors': []}


def validate_checkpoint(value):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or not isinstance(value['phase'], str) or value['phase'] not in PHASES):
        raise ValueError('Invalid supervisor checkpoint')
    generation = value['generation']
    if generation is not None and (not isinstance(generation, str)
                                  or re.fullmatch(r'[0-9a-f]{32}', generation) is None):
        raise ValueError('Invalid supervisor generation')
    allocation = value['allocation']
    if allocation is not None:
        fields = {'device', 'inode', 'uid', 'gid', 'mode', 'nlink', 'invocation'}
        if (not isinstance(allocation, dict) or set(allocation) != fields
                or any(type(allocation[key]) is not int or not 0 <= allocation[key] < 2**63
                       for key in fields - {'invocation'})
                or allocation['inode'] == 0 or allocation['uid'] != 0 or allocation['gid'] != 0
                or allocation['mode'] != stat.S_IFREG | 0o600 or allocation['nlink'] != 1
                or not isinstance(allocation['invocation'], str)
                or re.fullmatch(r'[0-9a-f]{32}', allocation['invocation']) is None):
            raise ValueError('Invalid owned runtime identity')
    registration = value['registration']
    if registration is not None:
        if (not isinstance(registration, dict) or set(registration) != {'id', 'name', 'repository'}
                or type(registration['id']) is not int or not 0 < registration['id'] < 2**63
                or not isinstance(registration['name'], str)
                or re.fullmatch(r'[a-z][a-z0-9-]{0,99}', registration['name']) is None
                or not isinstance(registration['repository'], str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',
                                registration['repository']) is None):
            raise ValueError('Invalid scoped registration identity')
    if value['phase'] in ('registered', 'running') and (
            generation is None or allocation is None or registration is None):
        raise ValueError('Admitted checkpoint lacks owned runtime or registration identity')
    if value['phase'] == 'clean' and (allocation is not None or registration is not None):
        raise ValueError('A clean slot cannot retain an active allocation')
    if value['phase'] in ('preparing', 'cleaning') and generation is None:
        raise ValueError('Incomplete supervisor generation')
    if (not isinstance(value['cleanup_errors'], list) or len(value['cleanup_errors']) > 32
            or any(not isinstance(error, str) or re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,63}', error) is None
                   for error in value['cleanup_errors'])
            or value['primary_error'] is not None and (
                not isinstance(value['primary_error'], str)
                or re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,63}', value['primary_error']) is None)):
        raise ValueError('Checkpoint errors must contain types without diagnostic text')
    return value


@contextlib.contextmanager
def private_directory(root, authority_uid):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != authority_uid or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('Supervisor state directory is not private and authority-owned')
        yield fd
    finally:
        os.close(fd)


def verify_file(fd, authority_uid):
    metadata = os.fstat(fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_uid != authority_uid or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise ValueError('Supervisor state file is not a private owned regular file')


def read_from_directory(directory_fd, authority_uid):
    try:
        fd = os.open('state.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
    except FileNotFoundError:
        return clean_checkpoint()
    with os.fdopen(fd, 'rb') as stream:
        verify_file(stream.fileno(), authority_uid)
        data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError('Supervisor checkpoint exceeds its fixed bound')
    return validate_checkpoint(json.loads(data))


def read_checkpoint(root, *, authority_uid=0):
    """Observational read: never creates a lock, state file or runtime."""
    with private_directory(root, authority_uid) as fd:
        return read_from_directory(fd, authority_uid)


class CheckpointStore:
    def __init__(self, root, *, authority_uid=0):
        self.root, self.authority_uid = root, authority_uid
        self.directory = None
        self.lock_fd = None

    def __enter__(self):
        self.directory = private_directory(self.root, self.authority_uid)
        self.directory_fd = self.directory.__enter__()
        try:
            try:
                self.lock_fd = os.open('admission.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                       0o600, dir_fd=self.directory_fd)
                os.fchmod(self.lock_fd, 0o600)
            except FileExistsError:
                self.lock_fd = os.open('admission.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                       dir_fd=self.directory_fd)
            verify_file(self.lock_fd, self.authority_uid)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.directory is not None:
            self.directory.__exit__(*args)
            self.directory = None

    def read(self):
        if self.lock_fd is None:
            raise ValueError('Supervisor checkpoint requires exclusive admission')
        return read_from_directory(self.directory_fd, self.authority_uid)

    def write(self, value):
        if self.lock_fd is None:
            raise ValueError('Supervisor checkpoint requires exclusive admission')
        validate_checkpoint(value)
        self.read()
        name = '.checkpoint-' + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.directory_fd)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            # The private directory descriptor keeps replacement and cleanup
            # attached to the validated directory rather than a mutable path.
            os.replace(name, 'state.json', src_dir_fd=self.directory_fd,
                       dst_dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
        finally:
            try:
                os.unlink(name, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass


def error_type(error):
    name = type(error).__name__
    return name if re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,63}', name) else 'RuntimeError'


def bound_checkpoint(config, state):
    registration = state['registration']
    if registration is not None and (
            (registration['repository'] != config['repository'] if 'repository' in config
             else registration['repository'] not in config['repositories'])
            or registration['name'] != config['name'] + '-' + str(state['generation'])):
        raise ValueError('Checkpoint belongs to another slot or generation')


def cleanup_generation(config, resources, enrollment, store, state):
    """Revoke admission and dispose known resources; retain both failure causes."""
    state = {**state, 'phase': 'cleaning', 'cleanup_errors': []}
    store.write(state)
    errors = []
    try:
        registration = enrollment.lookup(config['repository'], config['name'], state['generation'])
        if registration is not None:
            expected = state['registration']
            if expected is not None and registration.id != expected['id']:
                raise ValueError('Registration identity changed during cleanup')
            enrollment.retire(registration)
        state['registration'] = None
        store.write(state)
    except Exception as error:
        errors.append(error_type(error))
    # Missing Forgejo contact must not leave an untrusted process running. It
    # does block readmission until the outstanding registration is reconciled.
    try:
        resources.destroy(state['generation'], state['allocation'])
        resources.assert_empty()
        state['allocation'] = None
    except Exception as error:
        errors.append(error_type(error))
    if errors:
        state.update(phase='quarantined', cleanup_errors=errors)
    else:
        state = {**clean_checkpoint(), 'primary_error': state['primary_error']}
    store.write(state)
    return 1 if errors or state['primary_error'] else 0


def run_slot(config, resources, enrollment, store, *, recovery_only=False, generation=None):
    """Run at most one job with the caller holding the per-slot admission lock.

    The resource backend owns external timeouts and independent host ownership
    checks. Its destroy operation must also reconcile a interrupted prepare
    whose allocation identity has not yet reached this checkpoint.
    """
    if generation is not None and (not isinstance(generation, str) or re.fullmatch(r'[0-9a-f]{32}', generation) is None):
        raise ValueError('Reserved generation is invalid')
    state = store.read()
    try:
        bound_checkpoint(config, state)
    except ValueError:
        return 1
    if state['phase'] != 'clean':
        if state['generation'] is None:
            # No trusted generation exists to bind deletion. Recovery cannot
            # adopt an unexplained live runtime from its name alone.
            return 1
        if state['phase'] == 'quarantined' and not recovery_only:
            return 1
        cleanup_generation(config, resources, enrollment, store, state)
        state = store.read()
        if state['phase'] != 'clean':
            return 1
        if recovery_only:
            return 0
    elif recovery_only:
        try:
            resources.assert_empty()
        except Exception:
            return 1
        return 0
    try:
        resources.assert_empty()
    except Exception as error:
        store.write({**clean_checkpoint(), 'phase': 'quarantined', 'primary_error': error_type(error)})
        return 1
    generation = generation or secrets.token_hex(16)
    state = {**clean_checkpoint(), 'phase': 'preparing', 'generation': generation}
    store.write(state)
    try:
        state['allocation'] = resources.prepare(generation)
        store.write(state)
        registration = enrollment.enroll(config['repository'], config['name'], generation)
        state.update(phase='registered', registration={
            'id': registration.id, 'name': registration.name, 'repository': registration.repository})
        store.write(state)
        state['phase'] = 'running'
        store.write(state)
        resources.run(registration)
    except BaseException as error:
        state['primary_error'] = error_type(error)
    return cleanup_generation(config, resources, enrollment, store, state)
