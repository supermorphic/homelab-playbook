"""Guarded archive-backed application transition; host effects use an explicit adapter."""
from datetime import datetime, timezone
import json
import re
import uuid

import profiles
import service
import upgrade_state

REQUIRED_CHECKS = frozenset(('migration', 'continuity', 'source_restore', 'isolation', 'cleanup', 'actions'))


def pins(settings):
    return {key: settings[key] for key in profiles.KEYS}


def validate_evidence(evidence, source, target, controller_sha256):
    if (not isinstance(evidence, dict) or type(evidence.get('schema')) is not int
            or evidence['schema'] != 1 or evidence.get('completed') is not True
            or evidence.get('source') != pins(source) or evidence.get('target') != pins(target)
            or evidence.get('controller_sha256') != controller_sha256
            or re.fullmatch(r'[0-9a-f]{64}', controller_sha256) is None
            or not isinstance(evidence.get('checks'), dict)
            or any(evidence['checks'].get(name) is not True for name in REQUIRED_CHECKS)
            or evidence.get('cleanup_errors') != []):
        raise ValueError('upgrade requires complete rehearsal evidence for the exact source, target and controller')
    if pins(source) not in profiles.REVIEWED.values() or pins(target) not in profiles.REVIEWED.values():
        raise ValueError('upgrade transition has no reviewed exact runtime profile')
    if any(source[key] != target[key] for key in ('postgres_image', 'rclone_image')):
        raise ValueError('application upgrade cannot replace database or transfer images')


def write(host, saved, phase=None):
    if phase is not None:
        saved['phase'] = phase
    service.atomic_write(host.helpers / 'upgrade-state.json',
        (json.dumps(saved, sort_keys=True) + '\n').encode(),
        mode=0o640, uid=host.admin_uid, gid=host.group)


def run_pending(host, saved):
    try:
        if saved['phase'] == 'prepared':
            host.preserve(saved)
            host.block_admission(saved)
            host.pin(saved)
            host.quiesce(saved)
            write(host, saved, 'quiesced')
        if saved['phase'] == 'quiesced':
            captured = host.capture(saved)
            if (captured.get('archive_id') != saved['archive_id']
                    or captured.get('source') != saved['source']
                    or captured.get('source_generation') != saved['source_generation']
                    or re.fullmatch(r'[0-9a-f]{64}', captured.get('complete_sha256', '')) is None):
                raise ValueError('capture differs from the selected source recovery generation')
            saved['capture'] = captured
            write(host, saved, 'archive-sealed')
        if saved['phase'] in ('archive-sealed', 'migration-started', 'target-verified'):
            host.check_archive(saved)
            write(host, saved, 'migration-started')
            host.migrate(saved)
            host.verify_isolated(saved)
            write(host, saved, 'target-verified')
            host.stop_isolated(saved)
            host.publish(saved)
            # This is the durable decision to reopen normal target admission.
            # Later activation failure cannot turn it into lossless source rollback.
            write(host, saved, 'accepted')
        if saved['phase'] == 'accepted':
            host.activate(saved)
            host.verify_current(saved)
            write(host, saved, 'committed')
        return {'changed': True, 'phase': saved['phase'], 'archive_id': saved['archive_id']}
    except BaseException:
        if saved['phase'] in ('migration-started', 'target-verified'):
            host.stop_isolated(saved)
        raise


def perform(host, evidence, *, prepare_only=False):
    with host.locks():
        saved = upgrade_state.read(host.helpers, owner=host.admin_uid)
        if saved is not None and saved['phase'] == 'committed' and saved.get('target') == pins(host.target):
            host.verify_current(saved)
            return {'changed': False, 'phase': 'committed', 'archive_id': saved['archive_id']}
        upgrade_state.require_idle(host.helpers, owner=host.admin_uid)
        validate_evidence(evidence, host.source, host.target, host.controller_sha256)
        host.preflight()
        identifier = uuid.uuid4().hex
        created = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        saved = {'schema': 1, 'id': identifier, 'phase': 'prepared',
                 'source': pins(host.source), 'target': pins(host.target),
                 'source_generation': host.source['recovery_generation'],
                 'controller_sha256': host.controller_sha256, 'controller': host.identity(),
                 'archive_id': 'forgejo-' + created + '-' + identifier[:16]}
        write(host, saved)
        if prepare_only:
            host.preserve(saved)
            host.block_admission(saved)
            host.pin(saved)
            write(host, saved)
            return {'changed': True, 'phase': 'prepared', 'transaction_id': saved['id'],
                    'archive_id': saved['archive_id']}
        return run_pending(host, saved)


def resume(host, evidence):
    with host.locks():
        saved = upgrade_state.read(host.helpers, owner=host.admin_uid)
        if saved is None or saved['phase'] in ('committed', 'aborted', 'aborting'):
            raise RuntimeError('no resumable upgrade transaction')
        if saved['target'] != pins(host.target) or saved['controller_sha256'] != host.controller_sha256:
            raise ValueError('resume candidate differs from the pending transaction')
        validate_evidence(evidence, saved['source'], saved['target'], host.controller_sha256)
        host.preflight(saved)
        saved['controller'] = host.identity()
        write(host, saved)
        return run_pending(host, saved)


def abort(host):
    with host.locks():
        saved = upgrade_state.read(host.helpers, owner=host.admin_uid)
        if saved is None or saved['phase'] not in ('prepared', 'quiesced', 'archive-sealed', 'aborting'):
            raise RuntimeError('source abort is unavailable after migration; recover into separate storage')
        host.preflight(saved)
        saved['controller'] = host.identity()
        write(host, saved, 'aborting')
        host.abort_source(saved)
        write(host, saved, 'aborted')
        return {'changed': True, 'phase': 'aborted', 'archive_id': saved['archive_id']}
