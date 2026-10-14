"""Durable upgrade exclusion and recovery retention; no service mutations."""
import json
import os
from pathlib import Path
import re

ADMIN_UID = 0
PHASES = frozenset(('prepared', 'quiesced', 'archive-sealed', 'migration-started',
                    'target-verified', 'committing', 'accepted', 'committed', 'aborting', 'aborted'))


def document(directory, name, owner):
    import service
    path = directory / name
    try:
        return json.loads(service.safe_read(path, owner=ADMIN_UID if owner is None else owner))
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError('upgrade metadata is a dangling link') from None
        return None


def read(directory: Path, owner=None):
    import archive
    import profiles
    saved = document(directory, 'upgrade-state.json', owner)
    if saved is not None and (not isinstance(saved, dict) or type(saved.get('schema')) is not int
            or saved['schema'] != 1 or saved.get('phase') not in PHASES
            or not isinstance(saved.get('id'), str)
            or re.fullmatch(r'[0-9a-f]{32}', saved['id']) is None):
        raise ValueError('invalid upgrade transaction')
    if saved is not None:
        controller = saved.get('controller')
        valid_hex = lambda value, size: isinstance(value, str) and re.fullmatch(r'[0-9a-f]{' + str(size) + '}', value) is not None
        if (saved.get('source') not in profiles.REVIEWED.values()
                or saved.get('target') not in profiles.REVIEWED.values()
                or not valid_hex(saved.get('source_generation'), 32)
                or not valid_hex(saved.get('controller_sha256'), 64)
                or not isinstance(saved.get('archive_id'), str)
                or archive.NAME.fullmatch(saved['archive_id']) is None
                or not isinstance(controller, dict) or type(controller.get('pid')) is not int
                or controller['pid'] < 1 or not isinstance(controller.get('boot'), str) or not controller['boot']
                or not isinstance(controller.get('ticks'), str) or not controller['ticks'].isdigit()):
            raise ValueError('upgrade transaction lacks a valid recovery identity')
    return saved


def require_idle(directory: Path, owner=None):
    saved = read(directory, owner)
    if saved is not None and saved['phase'] not in ('committed', 'aborted'):
        raise RuntimeError('pending upgrade requires explicit resume, abort or independent recovery')


def process_identity():
    return {'pid': os.getpid(), 'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'ticks': Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]}


def controller_alive(saved):
    try:
        controller = saved['controller']
        pid = controller['pid']
        if type(pid) is not int or pid < 1:
            return False
        if controller['boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
            return False
        root = Path('/proc') / str(pid)
        if root.stat().st_uid != ADMIN_UID:
            return False
        return controller['ticks'] == (root / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, KeyError, IndexError, TypeError, ValueError):
        return False


def require_start(directory: Path, image: str, owner=None):
    saved = read(directory, owner)
    if saved is None or saved['phase'] == 'aborted':
        return
    if saved['phase'] in ('accepted', 'committed') and saved.get('target', {}).get('image') == image:
        return
    if (saved['phase'] == 'committing' and saved.get('target', {}).get('image') == image
            and controller_alive(saved)):
        return
    if (saved['phase'] == 'aborting' and saved.get('source', {}).get('image') == image
            and controller_alive(saved)):
        return
    raise RuntimeError('upgrade transaction prohibits ordinary application startup')


def protected_archives(directory: Path, owner=None):
    import archive
    saved = document(directory, 'retained-archives.json', owner)
    if saved is None:
        return frozenset()
    if (not isinstance(saved, dict) or set(saved) != {'schema', 'archives'}
            or type(saved['schema']) is not int or saved['schema'] != 1
            or not isinstance(saved['archives'], list) or len(saved['archives']) > 256
            or any(not isinstance(name, str) or archive.NAME.fullmatch(name) is None
                   for name in saved['archives'])
            or len(set(saved['archives'])) != len(saved['archives'])):
        raise ValueError('invalid recovery archive retention references')
    return frozenset(saved['archives'])
