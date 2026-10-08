"""Readiness must accept the paired controller, not generic group access."""

import errno
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.forgejo_runner import host_worker

# Linux access ACL: owner rwx, controller2201 x, group none, mask x, other none.
# Independent fixture, not production output.
CONTROLLER_ACL = bytes.fromhex(
    '02000000 01000700ffffffff 0200010099080000 '
    '04000000ffffffff 10000100ffffffff 20000000ffffffff')


class RuntimeAclTests(unittest.TestCase):
    def setUp(self):
        self.target = {'worker': {'uid': 2202, 'gid': 2202},
                       'limits': {'disk_bytes': 268435456}}
        self.metadata = SimpleNamespace(st_dev=12, st_ino=34, st_uid=2202,
                                        st_gid=2202, st_mode=0o40710)
        self.filesystem = SimpleNamespace(f_blocks=65536, f_frsize=4096)

    def validate(self, acl=CONTROLLER_ACL, *, controller_uid=2201, **changes):
        runtime = SimpleNamespace(**{**vars(self.metadata), **changes})
        try:
            host_worker.validate_runtime_mount(self.target, self.metadata, runtime,
                12, self.filesystem, controller_uid=controller_uid, access_acl=acl)
        except TypeError as error:
            self.fail('Readiness lacks the paired-controller ACL contract: ' + str(error))

    def test_required_controller_access_preserves_mount_identity(self):
        self.validate()
        for changes in ({'st_uid': 0}, {'st_gid': 0}, {'st_dev': 13},
                        {'st_ino': 35}, {'st_mode': 0o40700}, {'st_mode': 0o40711}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate(**changes)

    def test_unintended_access_and_malformed_acls_are_rejected(self):
        cases = [None, b'', CONTROLLER_ACL[:-1], CONTROLLER_ACL + b'\0',
                 CONTROLLER_ACL.replace(bytes.fromhex('99080000'), bytes.fromhex('9b080000')),
                 CONTROLLER_ACL.replace(bytes.fromhex('02000100'), bytes.fromhex('02000300')),
                 CONTROLLER_ACL.replace(bytes.fromhex('02000100'), bytes.fromhex('02000500')),
                 CONTROLLER_ACL.replace(bytes.fromhex('04000000'), bytes.fromhex('04000100')),
                 CONTROLLER_ACL.replace(bytes.fromhex('10000100'), bytes.fromhex('10000700')),
                 CONTROLLER_ACL.replace(bytes.fromhex('20000000'), bytes.fromhex('20000100')),
                 CONTROLLER_ACL + bytes.fromhex('020001009b080000'),
                 CONTROLLER_ACL + bytes.fromhex('080001009b080000'),
                 b'\x03' + CONTROLLER_ACL[1:]]
        for acl in cases:
            with self.subTest(acl=acl), self.assertRaises(ValueError):
                self.validate(acl)
        for uid in (True, 0, -1, 2202, 2**32):
            with self.subTest(controller_uid=uid), self.assertRaises(ValueError):
                self.validate(controller_uid=uid)

    def test_native_fixture_still_requires_private_runtime(self):
        with self.assertRaises(ValueError):
            host_worker.validate_runtime_mount(self.target, self.metadata, self.metadata,
                                              12, self.filesystem)
        private = SimpleNamespace(**{**vars(self.metadata), 'st_mode': 0o40700})
        host_worker.validate_runtime_mount(self.target, private, private, 12, self.filesystem)

    @unittest.skipUnless(sys.platform == 'linux', 'Requires Linux POSIX ACL metadata')
    def test_kernel_acl_is_accepted_only_for_the_paired_controller(self):
        import ctypes
        import ctypes.util
        library = ctypes.util.find_library('acl')
        self.assertIsNotNone(library, 'Linux fixture requires libacl')
        acl = ctypes.CDLL(library, use_errno=True)
        acl.acl_from_text.argtypes = [ctypes.c_char_p]
        acl.acl_from_text.restype = ctypes.c_void_p
        acl.acl_set_fd.argtypes = [ctypes.c_int, ctypes.c_void_p]
        acl.acl_set_fd.restype = ctypes.c_int
        acl.acl_free.argtypes = [ctypes.c_void_p]
        controller = 2203 if os.getuid() == 2201 else 2201
        with tempfile.TemporaryDirectory() as directory:
            runtime = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fchmod(runtime, 0o700)
                filesystem = os.fstatvfs(runtime)
                target = {'worker': {'uid': os.getuid(), 'gid': os.getgid()},
                          'limits': {'disk_bytes': filesystem.f_blocks * filesystem.f_frsize}}
                for identity, permission, accepted in ((controller, '--x', True),
                                                       (controller + 1, '--x', False),
                                                       (controller, 'rwx', False)):
                    value = acl.acl_from_text(f'u::rwx,u:{identity}:{permission},g::---,m::{permission},o::---'.encode())
                    self.assertTrue(value)
                    try:
                        self.assertEqual(0, acl.acl_set_fd(runtime, value), ctypes.get_errno())
                    finally:
                        acl.acl_free(value)
                    metadata = os.fstat(runtime)
                    args = (target, metadata, metadata, metadata.st_dev, filesystem)
                    kwargs = {'controller_uid': controller,
                              'access_acl': os.getxattr(runtime, 'system.posix_acl_access')}
                    if accepted:
                        host_worker.validate_runtime_mount(*args, **kwargs)
                    else:
                        with self.assertRaises(ValueError):
                            host_worker.validate_runtime_mount(*args, **kwargs)
            finally:
                os.close(runtime)

    def test_deployed_readiness_validates_controller_access_before_handoff(self):
        from test_deployed_resources import module
        config = {**self.target, 'name': 'worker', 'label': 'synthetic',
                  'state_root': '/var/lib/forgejo-runner/worker',
                  'worker': {**self.target['worker'], 'name': 'worker', 'subuid_count': 65536},
                  'controller': {'uid': 2201, 'gid': 2201},
                  'limits': {**self.target['limits'], 'memory_bytes': 2147483648,
                             'pids': 768, 'cpu_percent': 200, 'job_seconds': 60}}
        state = {'allocation': None}
        store = SimpleNamespace(read=lambda: state.copy(), write=state.update)
        runtime = module.OwnedRuntime(config, '/synthetic', '/synthetic',
                                     {'job_image_id': 'a'*64}, 'synthetic-handle', store)
        observed = {'InvocationID': 'b'*32, 'ActiveState': 'active'}
        runtime.unit = SimpleNamespace(name='synthetic.service', invocation='b'*32,
                                       claim=lambda: None, observe=lambda: observed)
        runtime.generation = 'c'*32
        runtime.helper_binds = []
        runtime.network = {}
        events = []
        def readiness(target, unit, *, controller_uid=None):
            host_worker.validate_runtime_mount(target, self.metadata, self.metadata,
                12, self.filesystem, controller_uid=controller_uid, access_acl=CONTROLLER_ACL)
            events.append('verified')
            return {'observations': {'ready': True}, 'diagnostic': ''}
        with patch.object(module, 'command'), patch.object(module, 'start_gateway'), \
                patch.object(runtime, 'persist'), patch.object(runtime, 'allocation', return_value={}), \
                patch.object(module, 'read_worker_result', side_effect=readiness), \
                patch.object(runtime, 'run_controller', side_effect=lambda *_: events.append('controller')):
            runtime.run(SimpleNamespace(uuid='synthetic', token='synthetic'))
        self.assertEqual(['verified', 'controller'], events)

    def test_deployed_storage_reads_acl_from_pinned_directory_and_fails_closed(self):
        # macOS cannot create this Linux bind mount/ACL. Descriptor traversal
        # stays real; only kernel metadata/xattr observations are substituted.
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory)
            (image / 'work/run').mkdir(parents=True)
            (image / 'run/user/2202').mkdir(parents=True)
            root = os.open(image, os.O_RDONLY | os.O_DIRECTORY)
            work = os.open(image / 'work', os.O_RDONLY | os.O_DIRECTORY)
            real_stat = os.fstat
            try:
                def acl(descriptor, attribute):
                    self.assertIsInstance(descriptor, int)
                    self.assertEqual('system.posix_acl_access', attribute)
                    real_stat(descriptor)
                    return CONTROLLER_ACL
                for result in (acl, OSError(errno.ENODATA, 'No ACL')):
                    with patch.object(host_worker.os, 'getxattr', create=True, side_effect=result), \
                            patch.object(host_worker.os, 'fstat', return_value=self.metadata), \
                            patch.object(host_worker.os, 'fstatvfs', return_value=self.filesystem):
                        if isinstance(result, Exception):
                            with self.assertRaises(OSError):
                                host_worker.validate_runtime_storage(self.target, root, work, controller_uid=2201)
                        else:
                            try:
                                host_worker.validate_runtime_storage(self.target, root, work, controller_uid=2201)
                            except TypeError as error:
                                self.fail('Readiness does not inspect its ACL: ' + str(error))
            finally:
                os.close(work)
                os.close(root)


if __name__ == '__main__':
    unittest.main()
