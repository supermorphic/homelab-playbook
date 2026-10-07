"""Shared worker declarations, durable queue assignments and aggregate admission."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from lifecycle import private_directory, verify_file, read_checkpoint


REPOSITORIES = {'supermorphic/career-ops', 'supermorphic/homelab-playbook', 'supermorphic/homelab-talos'}
LIMITS = {'memory_bytes': 2 * 1024**3, 'disk_bytes': 24 * 1024**3,
          'pids': 768, 'cpu_percent': 200, 'idle_seconds': 300, 'job_seconds': 10800}
FIELDS = {'name', 'repositories', 'enabled', 'label', 'state_root', 'controller', 'worker', 'limits'}


def read_json(root, name, maximum, *, authority_uid=0):
    with private_directory(root, authority_uid) as directory:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, 'rb') as stream:
            verify_file(stream.fileno(), authority_uid)
            raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError('Private pool metadata exceeds its bound')
    return json.loads(raw)


def read_registry(slot, *, authority_uid=0):
    root = Path(slot['state_root']).parent
    value = read_json(root, 'slots.json', 65536, authority_uid=authority_uid)
    if (not isinstance(value, dict) or set(value) != {'schema', 'slots'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not isinstance(value['slots'], list) or len(value['slots']) != 2):
        raise ValueError('Host registry must declare exactly two shared workers')
    slots = value['slots']
    for candidate in slots:
        if (not isinstance(candidate, dict) or set(candidate) != FIELDS
                or not isinstance(candidate['name'], str)
                or re.fullmatch(r'[a-z][a-z0-9-]{0,47}', candidate['name']) is None
                or candidate['state_root'] != str(root / candidate['name'])
                or type(candidate['enabled']) is not bool
                or not isinstance(candidate['label'], str)
                or re.fullmatch(r'[a-z][a-z0-9-]{0,47}', candidate['label']) is None
                or not isinstance(candidate['repositories'], list)
                or len(candidate['repositories']) != 3
                or any(not isinstance(repo, str) for repo in candidate['repositories'])
                or set(candidate['repositories']) != REPOSITORIES
                or not isinstance(candidate['limits'], dict) or set(candidate['limits']) != set(LIMITS)
                or any(type(number) is not int or not 0 < number <= LIMITS[key]
                       for key, number in candidate['limits'].items())
                or any(not isinstance(candidate[kind], dict) for kind in ('controller', 'worker'))):
            raise ValueError('Host worker declaration is outside its qualified boundary')
    declared = {key: value for key, value in slot.items() if key != 'repository'}
    if (sum(candidate == declared for candidate in slots) != 1
            or len({candidate['name'] for candidate in slots}) != 2
            or len({candidate['label'] for candidate in slots}) != 1):
        raise ValueError('Worker differs from its shared host registry')
    return slots


def token_name(repository):
    if repository not in REPOSITORIES:
        raise ValueError('Enrollment scope is outside the repository allowlist')
    return 'enrollment-' + hashlib.sha256(repository.encode()).hexdigest()


def read_assignment(slot, *, authority_uid=0):
    value = read_json(slot['state_root'], 'assignment.json', 4096, authority_uid=authority_uid)
    if value is not None and (
            not isinstance(value, dict) or set(value) != {'repository', 'job_digest', 'generation'}
            or value['repository'] not in slot['repositories']
            or not isinstance(value['job_digest'], str) or re.fullmatch(r'[0-9a-f]{64}', value['job_digest']) is None
            or not isinstance(value['generation'], str) or re.fullmatch(r'[0-9a-f]{32}', value['generation']) is None):
        raise ValueError('Worker assignment has invalid scope or generation')
    return value


def write_assignment(store, slot, repository, handle):
    if (repository not in slot['repositories'] or store.read()['phase'] != 'clean'
            or not isinstance(handle, str) or not 1 <= len(handle) <= 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in handle)):
        raise ValueError('Assignment requires an empty worker and an allowed queued job')
    read_assignment(slot, authority_uid=store.authority_uid)
    value = {'repository': repository, 'job_digest': hashlib.sha256(handle.encode()).hexdigest(),
             'generation': secrets.token_hex(16)}
    name = '.assignment-' + secrets.token_hex(16)
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=store.directory_fd)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, 'assignment.json', src_dir_fd=store.directory_fd, dst_dir_fd=store.directory_fd)
        os.fsync(store.directory_fd)
    finally:
        try:
            os.unlink(name, dir_fd=store.directory_fd)
        except FileNotFoundError:
            pass
    return value


def locked(slot, *, authority_uid=0):
    with private_directory(slot['state_root'], authority_uid) as directory:
        try:
            descriptor = os.open('admission.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            return False
        try:
            verify_file(descriptor, authority_uid)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(descriptor)


def peer_assignments(slot, slots, observer, *, authority_uid=0):
    result = []
    for candidate in slots:
        if candidate['name'] == slot['name']:
            continue
        state = read_checkpoint(candidate['state_root'], authority_uid=authority_uid)
        observation = observer(candidate)
        if (state['phase'] == 'quarantined'
                or state['phase'] == 'clean' and not observation['runtime_absent']):
            raise ValueError('Shared pool requires reconciliation before new admission')
        busy = state['phase'] != 'clean' or locked(candidate, authority_uid=authority_uid)
        if busy:
            assignment = read_assignment(candidate, authority_uid=authority_uid)
            if state['phase'] != 'clean' and (
                    assignment is None or assignment['generation'] != state['generation']):
                raise ValueError('Active peer lacks its exact durable assignment')
            if assignment is not None:
                result.append(assignment)
    return result


def poll_order(slot, previous):
    repositories = slot['repositories']
    if previous is None:
        return repositories
    start = (repositories.index(previous['repository']) + 1) % len(repositories)
    return repositories[start:] + repositories[:start]


def validate_capacity(slot, slots, observed, *, peer_anon_bytes, peer_disk_bytes=0):
    peers = [candidate for candidate in slots if candidate['name'] != slot['name'] and candidate['enabled']]
    allowance = sum(candidate['limits']['memory_bytes'] for candidate in peers)
    credit = min(max(peer_anon_bytes, 0), allowance)
    memory = slot['limits']['memory_bytes'] + allowance + 2 * 1024**3 - credit
    disk_allowance = sum(candidate['limits']['disk_bytes'] for candidate in peers)
    disk = sum(candidate['limits']['disk_bytes'] for candidate in slots) + 4 * 1024**3
    disk -= min(max(peer_disk_bytes, 0), disk_allowance)
    cpu = sum(candidate['limits']['cpu_percent'] for candidate in slots if candidate['enabled'])
    if (observed['memory_available'] < memory or observed['memory_total'] < memory + credit
            or observed['disk_available'] < disk or cpu > observed['cpu_count'] * 50):
        raise ValueError('Shared worker budgets and the host service reserve are unavailable')


def peer_anonymous_memory(slot, state):
    # Credit only independently bound, nonreclaimable anonymous memory. File
    # cache remains available to Linux and must not release a worker reservation.
    if state['allocation'] is None:
        return 0
    from verify import execute
    unit = 'forgejo-worker-' + slot['name'] + '-' + state['generation'] + '.service'
    raw = execute(['systemctl', 'show', '--property=InvocationID,ControlGroup', '--', unit])
    fields = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    group = fields.get('ControlGroup', '')
    if (fields.get('InvocationID') != state['allocation']['invocation'] or not group.startswith('/')
            or '..' in Path(group).parts or Path(group).name != unit):
        return 0
    try:
        rows = dict(line.split() for line in (Path('/sys/fs/cgroup') / group.lstrip('/') / 'memory.stat').read_text().splitlines())
        return max(int(rows.get('anon', '0')), 0)
    except (OSError, ValueError):
        return 0


def peer_allocated_disk(slot, state):
    allocation = state['allocation']
    if allocation is None:
        return 0
    try:
        metadata = (Path(slot['state_root']) / 'runtime/worker.ext4').lstat()
    except OSError:
        return 0
    actual = {'device': metadata.st_dev, 'inode': metadata.st_ino, 'uid': metadata.st_uid,
              'gid': metadata.st_gid, 'mode': metadata.st_mode, 'nlink': metadata.st_nlink}
    if not stat.S_ISREG(metadata.st_mode) or any(actual[key] != allocation[key] for key in actual):
        return 0
    return min(metadata.st_blocks * 512, slot['limits']['disk_bytes'])


def service_baseline(slot):
    # The private registry reserves the exact systemd namespaces of the two
    # disposable workers. Their normal completion must not look like an
    # unrelated host service disappearing, including during snapshot races.
    from scripts.forgejo_runner.host_resources import active_services
    slots = read_registry(slot)
    patterns = [re.compile(r'forgejo-(?:runner-' + re.escape(candidate['name'])
                           + r'|worker-' + re.escape(candidate['name']) + r'-[0-9a-f]{32})\.service')
                for candidate in slots]
    return {unit for unit in active_services() if not any(pattern.fullmatch(unit) for pattern in patterns)}


def require_capacity(slot):
    from scripts.forgejo_runner.host_resources import host_capacity
    slots = read_registry(slot)
    peers = [(candidate, read_checkpoint(candidate['state_root']))
             for candidate in slots if candidate['name'] != slot['name'] and candidate['enabled']]
    memory = sum(peer_anonymous_memory(candidate, state) for candidate, state in peers)
    disk = sum(peer_allocated_disk(candidate, state) for candidate, state in peers)
    validate_capacity(slot, slots, host_capacity(Path(slot['state_root'])),
                      peer_anon_bytes=memory, peer_disk_bytes=disk)
