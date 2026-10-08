import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_policy import slot

sys.path.insert(0, str(Path(__file__).parents[2] / 'roles/forgejo_runner/files'))
import lifecycle
import control
from registration import RegistrationClient
from test_supervisor import Backend


class SharedPoolTests(unittest.TestCase):
    def setUp(self):
        self.pool = importlib.import_module('pool')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.slots = [slot(), slot(1)]
        for item in self.slots:
            item['enabled'] = True
            item['state_root'] = str(self.root / item['name'])
            Path(item['state_root']).mkdir(mode=0o700)
        registry = self.root / 'slots.json'
        registry.write_text(json.dumps({'schema': 1, 'slots': self.slots}))
        registry.chmod(0o600)

    def test_assignment_is_private_durable_and_contains_no_pending_handle(self):
        with lifecycle.CheckpointStore(self.slots[0]['state_root'], authority_uid=os.getuid()) as store:
            self.pool.write_assignment(store, self.slots[0], 'user:supermorphic', 'sensitive-job-handle')
        result = self.pool.read_assignment(self.slots[0], authority_uid=os.getuid())
        self.assertEqual('user:supermorphic', result['scope'])
        self.assertEqual(64, len(result['job_digest']))
        path = Path(self.slots[0]['state_root']) / 'assignment.json'
        self.assertNotIn('sensitive-job-handle', path.read_text())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        with lifecycle.CheckpointStore(self.slots[0]['state_root'], authority_uid=os.getuid()) as store:
            with self.assertRaises(ValueError):
                self.pool.write_assignment(store, self.slots[0], 'user:other', 'other-job')
        self.assertEqual(result, self.pool.read_assignment(self.slots[0], authority_uid=os.getuid()))

    def test_registry_rejects_a_third_worker_or_changed_declaration(self):
        self.assertEqual(self.slots, self.pool.read_registry(self.slots[0], authority_uid=os.getuid()))
        for changed in (self.slots + [slot(2)], [{**self.slots[0], 'label': 'other'}, self.slots[1]]):
            (self.root / 'slots.json').write_text(json.dumps({'schema': 1, 'slots': changed}))
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.pool.read_registry(self.slots[0], authority_uid=os.getuid())

    def test_full_worker_allowances_are_reserved_even_before_memory_is_consumed(self):
        observed = {'memory_total': 8*1024**3, 'memory_available': 6*1024**3,
                    'disk_available': 100*1024**3, 'cpu_count': 8}
        self.pool.validate_capacity(self.slots[0], self.slots, observed, peer_anon_bytes=0)
        too_busy = {**observed, 'memory_available': 5*1024**3}
        with self.assertRaises(ValueError):
            self.pool.validate_capacity(self.slots[0], self.slots, too_busy, peer_anon_bytes=0)
        self.pool.validate_capacity(self.slots[0], self.slots, too_busy, peer_anon_bytes=1024**3)
        with self.assertRaises(ValueError):
            self.pool.validate_capacity(self.slots[0], self.slots,
                                        {**observed, 'memory_available': 4*1024**3-1},
                                        peer_anon_bytes=2*1024**3)

    def test_memory_credit_cannot_exceed_the_other_workers_full_allowance(self):
        observed = {'memory_total': 8*1024**3, 'memory_available': 3*1024**3,
                    'disk_available': 100*1024**3, 'cpu_count': 8}
        with self.assertRaises(ValueError):
            self.pool.validate_capacity(self.slots[0], self.slots, observed, peer_anon_bytes=100*1024**3)

    def test_assignment_cannot_be_read_through_a_link_or_with_public_permissions(self):
        path = Path(self.slots[0]['state_root']) / 'assignment.json'
        path.write_text(json.dumps({'scope': 'user:supermorphic', 'job_digest': 'a'*64}))
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.pool.read_assignment(self.slots[0], authority_uid=os.getuid())
        path.chmod(0o600)
        other = path.with_name('other')
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(OSError):
            self.pool.read_assignment(self.slots[0], authority_uid=os.getuid())

    def test_two_workers_run_distinct_jobs_from_the_same_repository_concurrently(self):
        # Real locks, assignment files, checkpoints and registration client;
        # replace only the remote API and workload execution boundaries.
        first_running = threading.Event()
        both_running = threading.Barrier(2)
        executed = []
        registrations = {}
        api_lock = threading.Lock()
        def request(path, *, method='GET', payload=None):
            with api_lock:
                if path.endswith('/jobs?labels=homelab-podman-amd64'):
                    return [{'handle': 'job-one', 'owner_id': 1, 'repo_id': 902},
                            {'handle': 'job-two', 'owner_id': 1, 'repo_id': 902}]
                if '/actions/runners' not in path:
                    return {'id': 1, 'login': 'supermorphic'}
                if method == 'POST':
                    row = {**payload, 'id': len(registrations)+1, 'repo_id': 0, 'owner_id': 1,
                           'uuid': 'synthetic-uuid', 'token': 'synthetic-token'}
                    registrations[row['id']] = row
                    return row
                if method == 'DELETE':
                    registrations.pop(int(path.rsplit('/', 1)[1]))
                    return None
                return list(registrations.values())
        def client(url, scope, token):
            return RegistrationClient(url, scope, token, request=request)
        def store(path, **kwargs):
            return lifecycle.CheckpointStore(path, authority_uid=os.getuid())
        class Runtime(Backend):
            def __init__(self, selected, installation, assets, manifest, handle, checkpoint):
                super().__init__([])
                self.handle, self.selected = handle, selected
            def run(self, registration):
                executed.append((self.handle, registration.scope))
                first_running.set()
                both_running.wait(timeout=5)
        values = [{'slot': item, 'installation': 'synthetic', 'assets_root': 'synthetic', 'manifest': {}}
                  for item in self.slots]
        with patch.object(control, 'CheckpointStore', store), \
                patch.object(control, 'observe', return_value={'runtime_absent': True, 'verified': True}), \
                patch.object(control, 'private_token', return_value='synthetic-token'), \
                patch.object(control, 'RegistrationClient', side_effect=client), \
                patch.object(control, 'OwnedRuntime', Runtime), \
                patch.object(self.pool, 'require_capacity', create=True), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(control.perform, 'cycle', values[0])
            self.assertTrue(first_running.wait(timeout=5), 'First worker never entered its job')
            second = executor.submit(control.perform, 'cycle', values[1])
            self.assertEqual([0, 0], [first.result()[1], second.result()[1]])
        self.assertCountEqual([('job-one', 'user:supermorphic'),
                               ('job-two', 'user:supermorphic')], executed)
        self.assertEqual({}, registrations)
        for item in self.slots:
            self.assertEqual('clean', lifecycle.read_checkpoint(item['state_root'], authority_uid=os.getuid())['phase'])

    def test_nonclean_peer_requires_matching_assignment_before_new_admission(self):
        peer = self.slots[1]
        with lifecycle.CheckpointStore(peer['state_root'], authority_uid=os.getuid()) as store:
            assignment = self.pool.write_assignment(store, peer, 'user:supermorphic', 'reserved-job')
            store.write({**lifecycle.clean_checkpoint(), 'phase': 'preparing', 'generation': 'b'*32})
        with self.assertRaises(ValueError):
            self.pool.peer_assignments(self.slots[0], self.slots,
                lambda item: {'runtime_absent': True}, authority_uid=os.getuid())
        with lifecycle.CheckpointStore(peer['state_root'], authority_uid=os.getuid()) as store:
            store.write({**lifecycle.clean_checkpoint(), 'phase': 'preparing', 'generation': assignment['generation']})
            self.assertEqual([assignment], self.pool.peer_assignments(self.slots[0], self.slots,
                lambda item: {'runtime_absent': True}, authority_uid=os.getuid()))

    def test_memory_credit_excludes_reclaimable_cache_and_unbound_invocations(self):
        state = {**lifecycle.clean_checkpoint(), 'generation': 'a'*32,
                 'allocation': {'invocation': 'b'*32}}
        unit = 'forgejo-worker-worker-2-' + 'a'*32 + '.service'
        with patch('verify.execute', return_value='InvocationID=' + 'b'*32 + '\nControlGroup=/system.slice/' + unit), \
                patch.object(Path, 'read_text', return_value='anon 1048576\nfile 2147483648\n'):
            self.assertEqual(1048576, self.pool.peer_anonymous_memory(self.slots[1], state))
        with patch('verify.execute', return_value='InvocationID=' + 'c'*32 + '\nControlGroup=/system.slice/' + unit):
            self.assertEqual(0, self.pool.peer_anonymous_memory(self.slots[1], state))

    def test_assignment_excludes_a_job_before_the_preparing_checkpoint_is_written(self):
        peer = self.slots[1]
        with lifecycle.CheckpointStore(peer['state_root'], authority_uid=os.getuid()) as store:
            assignment = self.pool.write_assignment(store, peer, 'user:supermorphic', 'job-before-prepare')
            self.assertEqual('clean', store.read()['phase'])
            self.assertEqual([assignment], self.pool.peer_assignments(self.slots[0], self.slots,
                lambda item: {'runtime_absent': True}, authority_uid=os.getuid()))
        self.assertEqual([], self.pool.peer_assignments(self.slots[0], self.slots,
            lambda item: {'runtime_absent': True}, authority_uid=os.getuid()))

    def test_idle_cycle_checks_the_owner_queue_without_allocation_or_assignment(self):
        value = {'slot': self.slots[0]}
        polled = []
        def client(url, scope, token):
            def request(path, **kwargs):
                polled.append(path)
                return {'id': 1, 'login': 'supermorphic'} if path == '/user' else []
            return RegistrationClient(url, scope, token, request=request)
        with patch.object(control, 'CheckpointStore', lambda path, **kw:
                lifecycle.CheckpointStore(path, authority_uid=os.getuid())), \
                patch.object(control, 'observe', return_value={'runtime_absent': True, 'verified': True}), \
                patch.object(control, 'private_token', return_value='synthetic-token'), \
                patch.object(control, 'RegistrationClient', side_effect=client):
            self.assertEqual(0, control.perform('cycle', value)[1])
        self.assertEqual(['/user', '/user/actions/runners/jobs?labels=homelab-podman-amd64'], polled)
        self.assertFalse((Path(self.slots[0]['state_root']) / 'assignment.json').exists())
        self.assertFalse((Path(self.slots[0]['state_root']) / 'state.json').exists())

    def test_inspection_rejects_registration_bound_to_a_different_assignment(self):
        selected = self.slots[0]
        with lifecycle.CheckpointStore(selected['state_root'], authority_uid=os.getuid()) as store:
            assignment = self.pool.write_assignment(store, selected, 'user:supermorphic', 'pending-job')
            store.write({**lifecycle.clean_checkpoint(), 'phase': 'running', 'generation': assignment['generation'],
                'allocation': Backend([]).identity, 'registration': {'id': 1,
                'name': selected['name'] + '-' + assignment['generation'], 'scope': 'user:supermorphic'}})
        assignment_path = Path(selected['state_root']) / 'assignment.json'
        assignment_path.write_text(json.dumps({**assignment, 'generation': 'b'*32}))
        original = self.pool.read_assignment
        with patch.object(control, 'observe', return_value={'runtime_absent': False, 'verified': True}), \
                patch.object(control, 'read_checkpoint', side_effect=lambda path:
                    lifecycle.read_checkpoint(path, authority_uid=os.getuid())), \
                patch.object(self.pool, 'read_assignment', side_effect=lambda item:
                    original(item, authority_uid=os.getuid())):
            result, code = control.perform('inspect', {'slot': selected})
        self.assertEqual(1, code)
        self.assertFalse(result['registration_verified'])

    def test_allocation_rechecks_the_combined_pool_budget_before_allocating(self):
        from resources import OwnedRuntime
        import scripts.forgejo_runner.host_resources as host
        selected = self.slots[0]
        runtime = OwnedRuntime(selected, 'synthetic', 'synthetic', {}, None, None)
        registry_reader = self.pool.read_registry
        checkpoint_reader = self.pool.read_checkpoint
        with patch.object(self.pool, 'read_registry', side_effect=lambda item:
                    registry_reader(item, authority_uid=os.getuid())), \
                patch.object(self.pool, 'read_checkpoint', side_effect=lambda path:
                    checkpoint_reader(path, authority_uid=os.getuid())), \
                patch.object(host, 'host_capacity', return_value={
                    'memory_total': 8*1024**3, 'memory_available': 5*1024**3,
                    'disk_available': 100*1024**3, 'cpu_count': 8}):
            with self.assertRaises(ValueError):
                runtime.capacity()

    def test_allocated_peer_disk_is_not_reserved_twice_before_the_next_allocation(self):
        observed = {'memory_total': 8*1024**3, 'memory_available': 6*1024**3,
                    'disk_available': 40*1024**3, 'cpu_count': 8}
        self.pool.validate_capacity(self.slots[0], self.slots, observed,
            peer_anon_bytes=0, peer_disk_bytes=24*1024**3)
        with self.assertRaises(ValueError):
            self.pool.validate_capacity(self.slots[0], self.slots,
                {**observed, 'disk_available': 28*1024**3-1}, peer_anon_bytes=0, peer_disk_bytes=24*1024**3)

    def test_disk_credit_requires_checkpoint_identity_and_actual_allocated_blocks(self):
        peer = self.slots[1]
        image = Path(peer['state_root']) / 'runtime/worker.ext4'
        image.parent.mkdir(mode=0o700)
        image.write_bytes(b'x' * 65536)
        image.chmod(0o600)
        metadata = image.stat()
        expected = {'device': metadata.st_dev, 'inode': metadata.st_ino, 'uid': metadata.st_uid,
                    'gid': metadata.st_gid, 'mode': metadata.st_mode, 'nlink': metadata.st_nlink}
        self.assertEqual(metadata.st_blocks * 512, self.pool.peer_allocated_disk(peer, {'allocation': expected}))
        self.assertEqual(0, self.pool.peer_allocated_disk(peer, {'allocation': {**expected, 'inode': 1}}))
        image.unlink()
        with image.open('wb') as stream:
            stream.truncate(24*1024**3)
        image.chmod(0o600)
        metadata = image.stat()
        expected.update(device=metadata.st_dev, inode=metadata.st_ino)
        self.assertEqual(0, self.pool.peer_allocated_disk(peer, {'allocation': expected}))

    def test_peer_completion_does_not_remove_a_service_in_the_preservation_baseline(self):
        import scripts.forgejo_runner.host_resources as host
        peer_service = 'forgejo-worker-worker-2-' + 'a'*32 + '.service'
        unrelated_service = 'forgejo-worker-other-' + 'b'*32 + '.service'
        original = self.pool.read_registry
        before = {'forgejo.service', 'postgresql.service', unrelated_service, peer_service,
                  'forgejo-runner-worker-1.service', 'forgejo-runner-worker-2.service'}
        with patch.object(self.pool, 'read_registry', side_effect=lambda item:
                    original(item, authority_uid=os.getuid())), \
                patch.object(host, 'active_services', return_value=before):
            baseline = self.pool.service_baseline(self.slots[0])
        self.assertEqual({'forgejo.service', 'postgresql.service', unrelated_service}, baseline)
        after_peer_completion = {'forgejo.service', 'postgresql.service', unrelated_service}
        self.assertTrue(baseline <= after_peer_completion)
        self.assertFalse(baseline <= after_peer_completion - {'postgresql.service'})
