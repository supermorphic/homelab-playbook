"""Exercise candidate safety and file publication with real private directories."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import socket
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "roles/forgejo/files/service.py"


def effective_units(helper='/usr/local/libexec/forgejo'):
    def command(argv):
        argv = argv.replace('/usr/local/libexec/forgejo', str(helper))
        return '{ path=' + argv.split()[0] + ' ; argv[]=' + argv + ' ; ignore_errors=no ; }'
    units = {
        'forgejo.service': {'TimeoutStartUSec': '1min 30s', 'TimeoutStopUSec': '1min',
            'Requires': 'forgejo-postgres.service', 'After': 'forgejo-postgres.service',
            'Restart': 'on-failure', 'ExecStartPre': command('/usr/bin/python3 /usr/local/libexec/forgejo/backup.py guard')},
        'forgejo-postgres.service': {'TimeoutStartUSec': '1min 30s', 'TimeoutStopUSec': '1min', 'Restart': 'on-failure'},
        'forgejo-backup.service': {'TimeoutStartUSec': '30min', 'TimeoutStopUSec': '30s',
            'Type': 'oneshot', 'RemainAfterExit': 'no', 'OnFailure': 'forgejo-recover.service',
            'ExecStart': command('/usr/bin/python3 /usr/local/libexec/forgejo/backup.py capture'),
            'ExecStopPost': command('/usr/bin/systemctl --user --no-block start forgejo-recover.service')},
        'forgejo-recover.service': {'TimeoutStartUSec': '5min', 'TimeoutStopUSec': '30s',
            'Type': 'oneshot', 'RemainAfterExit': 'no', 'Wants': 'forgejo-postgres.service', 'After': 'forgejo-postgres.service',
            'ExecStart': command('/usr/bin/python3 /usr/local/libexec/forgejo/backup.py recover')},
        'forgejo-transfer.service': {'TimeoutStartUSec': '2h', 'TimeoutStopUSec': '30s',
            'Type': 'oneshot', 'RemainAfterExit': 'no',
            'ExecStart': command('/usr/bin/python3 /usr/local/libexec/forgejo/transfer.py run'),
            'ExecStopPost': command('/usr/bin/python3 /usr/local/libexec/forgejo/transfer.py cleanup')},
        'forgejo-backup.timer': {'Persistent': 'yes', 'Unit': 'forgejo-backup.service',
            'TimersCalendar': '{ OnCalendar=*-*-* 03:30:00 ; next_elapse=fixture ; }'},
        'forgejo-transfer.timer': {'Persistent': 'yes', 'Unit': 'forgejo-transfer.service',
            'TimersCalendar': '{ OnCalendar=*-*-* 00/4:45:00 ; next_elapse=fixture ; }'},
    }
    for fields in units.values():
        fields['LoadState'] = 'loaded'
    return units


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), "serialized Forgejo reconciliation is missing")
        spec = importlib.util.spec_from_file_location("forgejo_service", MODULE)
        self.service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.service)
        root = ROOT / ".tmp/forgejo"
        root.mkdir(parents=True, exist_ok=True)
        self.scratch = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.candidate = self.root / "candidate"
        self.candidate.mkdir(mode=0o700)
        self.allocation = {"name": "svc-forgejo", "uid": os.getuid(), "gid": os.getgid(),
            "subuid_start": 200000, "subuid_count": 65536,
            "subgid_start": 200000, "subgid_count": 65536}
        self.manifest = {"schema": 1, "allocation": self.allocation,
            "hostname": "forgejo.infra.example.com", "backend_port": 18081,
            "health_timeout_seconds": 60,
            "rclone_remote": "nas:fixture-forgejo",
            "image": "codeberg.org/forgejo/forgejo:15.0.9-rootless@sha256:" + "a" * 64,
            "postgres_image": "docker.io/library/postgres:17.11@sha256:" + "b" * 64,
            "rclone_image": "docker.io/rclone/rclone:1.75.1@sha256:" + "c" * 64}
        for name in self.service.REQUIRED_FILES:
            path = self.candidate / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text("fixture-only\n")
            path.chmod(0o600)
        (self.candidate / "manifest.json").write_text(json.dumps(self.manifest))
        self.targets = {}
        for name in self.service.REQUIRED_FILES:
            if name != "manifest.json":
                self.targets[name] = self.root / "installed" / name

    def test_candidate_rejects_foreign_allocation_before_publication(self):
        expected = dict(self.allocation, uid=self.allocation["uid"] + 1)
        with self.assertRaises(ValueError):
            self.service.validate_candidate(self.candidate, expected)
        self.assertFalse((self.root / "installed").exists())

    def test_candidate_rejects_symlink(self):
        path = self.candidate / "config/secret-key"
        path.unlink()
        path.symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            self.service.validate_candidate(self.candidate, self.allocation)

    def test_candidate_rejects_group_writable_input(self):
        (self.candidate / "manifest.json").chmod(0o660)
        with self.assertRaises(ValueError):
            self.service.validate_candidate(self.candidate, self.allocation)

    def test_candidate_rejects_hardlinked_input(self):
        path = self.candidate / "config/secret-key"
        os.link(path, self.root / "outside-key")
        with self.assertRaises(ValueError):
            self.service.validate_candidate(self.candidate, self.allocation)

    def test_identical_publication_does_not_report_changes(self):
        self.service.validate_candidate(self.candidate, self.allocation)
        self.assertTrue(self.service.publish_files(self.candidate, self.targets,
                                                  self.allocation))
        self.assertFalse(self.service.publish_files(self.candidate, self.targets,
                                                   self.allocation))
        self.assertEqual("fixture-only\n",
                         self.targets["config/secret-key"].read_text())

    def test_private_askpass_program_is_executable_without_exposing_credentials(self):
        self.service.publish_files(self.candidate, self.targets, self.allocation)
        self.assertEqual(0o700, self.targets['config/mirror-credential-helper.sh'].stat().st_mode & 0o777)
        self.assertEqual(0o600, self.targets['config/mirror-credentials'].stat().st_mode & 0o777)

    def test_changed_stable_key_is_rejected_before_any_publication(self):
        self.service.publish_files(self.candidate, self.targets, self.allocation)
        (self.candidate / "config/secret-key").write_text("changed-fixture-only\n")
        (self.candidate / "config/app.ini").write_text("new-config\n")
        with self.assertRaises(ValueError):
            self.service.check_stable_inputs(self.candidate, self.targets)
        self.assertEqual("fixture-only\n", self.targets["config/app.ini"].read_text())

    def test_incompatible_database_marker_is_rejected(self):
        path = self.root / "database"
        path.mkdir()
        (path / "PG_VERSION").write_text("16\n")
        with self.assertRaises(ValueError):
            self.service.check_database(path)
        self.assertEqual("16\n", (path / "PG_VERSION").read_text())

    def test_operation_lock_times_out_without_replacing_locked_inode(self):
        lock = self.root / "operation.lock"
        lock.touch(mode=0o600)
        inode = lock.stat().st_ino
        with self.service.operation_lock(lock, 1):
            with self.assertRaises(RuntimeError):
                with self.service.operation_lock(lock, 0.05):
                    self.fail("second operation acquired held lock")
        self.assertEqual(inode, lock.stat().st_ino)

    def test_occupied_loopback_port_is_rejected(self):
        self.assertTrue(hasattr(self.service, "require_port_available"),
                        "backend binding precondition is missing")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaises(RuntimeError):
                self.service.require_port_available(listener.getsockname()[1])

    def test_verify_does_not_create_missing_deployment(self):
        self.assertTrue(hasattr(self.service, "verify"), "observational verifier is missing")
        self.service.HELPERS = self.root / "absent-helper"
        with self.assertRaises((OSError, ValueError)):
            self.service.verify()
        self.assertFalse(self.service.HELPERS.exists())

    def test_publication_rejects_linked_parent_before_writing(self):
        outside = self.root / "outside"
        outside.mkdir()
        installed = self.root / "installed"
        installed.mkdir()
        (installed / "config").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            self.service.publish_files(self.candidate, self.targets, self.allocation)
        self.assertEqual([], list(outside.iterdir()))

    def test_operation_lock_metadata_allows_only_exact_group_write(self):
        self.assertTrue(hasattr(self.service, "check_lock_metadata"),
                        "operation lock metadata check is missing")
        lock = self.root / "operation.lock"
        lock.touch()
        lock.chmod(0o660)
        self.service.check_lock_metadata(lock, os.getgid())
        lock.chmod(0o666)
        with self.assertRaises(ValueError):
            self.service.check_lock_metadata(lock, os.getgid())

    def runtime_world(self):
        self.service.STATE = self.root / "state"
        self.service.HELPERS = self.root / "helpers"
        self.service.LOCK = self.root / "operation.lock"
        self.service.STATE.mkdir()
        for child in ("data", "database", "config", "credentials", "backups"):
            (self.service.STATE / child).mkdir(mode=0o700)
        (self.service.HELPERS / "candidates").mkdir(parents=True, mode=0o700)
        moved = self.service.HELPERS / "candidates" / "fixture"
        self.candidate.rename(moved)
        self.candidate = moved
        self.service.LOCK.touch(mode=0o600)
        self.commands = []
        self.active = set()
        self.units = effective_units(self.service.HELPERS)
        self.targets = {name: self.service.STATE / name for name in self.targets}
        def manager(arguments, **kwargs):
            self.commands.append(("manager", *arguments))
            if arguments[0] == "is-active":
                return SimpleNamespace(returncode=0 if arguments[1] in self.active else 3)
            if arguments[0] in ("start", "restart"):
                self.active.add(arguments[1])
            if arguments[0] == 'show':
                return SimpleNamespace(returncode=0, stdout='\n'.join(
                    key + '=' + value for key, value in self.units[arguments[1]].items()))
            return SimpleNamespace(returncode=0)
        def user(arguments, allocation, **kwargs):
            self.commands.append(tuple(arguments))
            output = ""
            if "inspect" in arguments and "--format" in arguments:
                output = "sha256:" + "d" * 64
            elif "inspect" in arguments:
                ports = {} if arguments[-1] == "forgejo-postgres" else {
                    "3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]}
                output = json.dumps([{"NetworkSettings": {"Ports": ports}}])
            return SimpleNamespace(returncode=0, stdout=output)
        for name, value in (("account_allocation", lambda: self.allocation),
                ("check_boundaries", lambda allocation: None),
                ("targets_for", lambda allocation: self.targets),
                ("validate_generator", lambda *args: None), ("manager", manager),
                ("user_command", user), ("initialize_database", lambda *args: None),
                ("initialize_administrator", lambda *args: None),
                ("database_command", lambda allocation, sql, **kwargs: "f|f|f|f" if 'rolsuper' in sql else '[]'),
                ("wait_health", lambda *args: None),
                ("enable_unit_links", lambda *args: False)):
            patcher = patch.object(self.service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_repeat_reconciliation_does_not_reload_or_restart(self):
        self.runtime_world()
        self.assertTrue(self.service.reconcile(self.candidate)["changed"])
        self.commands.clear()
        self.assertFalse(self.service.reconcile(self.candidate)["changed"])
        self.assertFalse(any(command[0] == "manager" and command[1] in
            ("start", "restart", "daemon-reload") for command in self.commands))

    def test_changed_application_configuration_restarts_only_application(self):
        self.runtime_world()
        self.service.reconcile(self.candidate)
        self.commands.clear()
        (self.candidate / "config/app.ini").write_text("changed public configuration\n")
        self.assertTrue(self.service.reconcile(self.candidate)["changed"])
        self.assertIn(("manager", "restart", "forgejo.service"), self.commands)
        self.assertNotIn(("manager", "restart", "forgejo-postgres.service"), self.commands)

    def test_interrupted_publication_is_activated_on_retry(self):
        self.runtime_world()
        self.service.reconcile(self.candidate)
        for boundary in ('publication', 'reload', 'verification'):
            with self.subTest(boundary=boundary):
                self.commands.clear()
                (self.candidate / 'config/app.ini').write_text('new configuration ' + boundary)
                if boundary == 'publication':
                    original = self.service.publish_files
                    def interrupted(*args):
                        original(*args)
                        raise RuntimeError('interrupted publication')
                    guard = patch.object(self.service, 'publish_files', interrupted)
                elif boundary == 'reload':
                    original = self.service.manager
                    def interrupted(arguments, **kwargs):
                        if arguments == ['daemon-reload']:
                            raise RuntimeError('interrupted reload')
                        return original(arguments, **kwargs)
                    guard = patch.object(self.service, 'manager', interrupted)
                else:
                    guard = patch.object(self.service, 'verify', side_effect=RuntimeError('interrupted verification'))
                with guard, self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    self.service.reconcile(self.candidate)
                self.commands.clear()
                self.assertTrue(self.service.reconcile(self.candidate)['changed'])
                self.assertIn(('manager', 'daemon-reload'), self.commands)
                self.assertIn(('manager', 'restart', 'forgejo.service'), self.commands)
                self.assertNotIn(('manager', 'restart', 'forgejo-postgres.service'), self.commands)
                self.assertFalse((self.service.HELPERS / 'activation-pending.json').exists())

    def test_verify_has_no_mutating_commands_or_file_writes(self):
        self.runtime_world()
        self.service.reconcile(self.candidate)
        before = {path: (path.stat().st_mtime_ns, path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()}
        self.commands.clear()
        self.assertTrue(self.service.verify()["ready"])
        allowed = {"is-active", "show", "inspect"}
        for command in self.commands:
            self.assertIn(command[1] if command[0] == "manager" else command[2], allowed)
        after = {path: (path.stat().st_mtime_ns, path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_effective_unit_drift_is_rejected_without_repair(self):
        self.runtime_world()
        self.service.reconcile(self.candidate)
        cases = [('forgejo-backup.service', 'TimeoutStartUSec', 'infinity'),
            ('forgejo-backup.service', 'ExecStopPost', ''),
            ('forgejo.service', 'ExecStartPre', ''),
            ('forgejo.service', 'Requires', ''),
            ('forgejo-backup.timer', 'TimersCalendar', '{ OnCalendar=*-*-* 12:00:00 ; next_elapse=fixture ; }'),
            ('forgejo-transfer.timer', 'Persistent', 'no')]
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        for unit, key, value in cases:
            with self.subTest(unit=unit, property=key):
                original = self.units[unit][key]
                self.units[unit][key] = value
                self.commands.clear()
                try:
                    with self.assertRaisesRegex(ValueError, 'effective'):
                        self.service.verify()
                    self.assertEqual(value, self.units[unit][key])
                    self.assertFalse(any(command[0] == 'manager' and command[1] in
                        ('start', 'restart', 'daemon-reload') for command in self.commands))
                    self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()})
                finally:
                    self.units[unit][key] = original

    def test_capture_rejects_configuration_pending_activation(self):
        self.runtime_world()
        self.service.reconcile(self.candidate)
        (self.service.HELPERS / 'activation-pending.json').write_text('{}')
        import backup
        host = backup.Host.__new__(backup.Host)
        host.state = self.service.STATE
        host.backups = host.state / 'backups'
        host.settings = self.manifest
        host.allocation = self.allocation
        with patch.object(backup, 'service', self.service):
            with self.assertRaisesRegex(RuntimeError, 'activation'):
                host.preflight()

    def test_repeated_systemctl_command_properties_preserve_startup_guard(self):
        self.runtime_world()
        self.units['forgejo.service']['ExecStartPre'] += (
            '\nExecStartPre={ path=/bin/sh ; argv[]=/bin/sh -ec health-check ; ignore_errors=no ; }')
        try:
            self.service.verify_effective_units()
        except ValueError as error:
            self.fail('valid startup guard was lost from repeated properties: ' + str(error))

    def test_repeated_timer_properties_cannot_hide_an_extra_calendar(self):
        self.runtime_world()
        original = self.units['forgejo-backup.timer']['TimersCalendar']
        self.units['forgejo-backup.timer']['TimersCalendar'] = (
            '{ OnCalendar=*-*-* 12:00:00 ; next_elapse=fixture ; }\nTimersCalendar=' + original)
        with self.assertRaisesRegex(ValueError, 'effective'):
            self.service.verify_effective_units()


if __name__ == "__main__":
    unittest.main()
