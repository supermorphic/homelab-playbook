import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

files = Path(__file__).parents[2] / 'roles/forgejo_runner/files'
spec = importlib.util.spec_from_file_location('runner_maintenance', files / 'maintenance.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MaintenanceTests(unittest.TestCase):
    def configuration(self):
        return {'slot': {'name': 'playbook', 'limits': {'job_seconds': 10800}}}

    def test_drain_stops_polling_and_waits_without_cancelling_the_job(self):
        commands = []
        states = iter(('active', 'inactive'))
        def command(argv):
            commands.append(argv)
            return next(states) if argv[1] == 'show' else ''
        control = Mock(return_value=({'verified': True, 'runtime_absent': True}, 0))
        result = module.drain(self.configuration(), command=command, control=control, sleep=lambda duration: None)
        self.assertEqual({'drained': True}, result)
        self.assertEqual(['systemctl', 'stop', 'forgejo-runner-playbook.timer'], commands[0])
        self.assertNotIn(['systemctl', 'stop', 'forgejo-runner-playbook.service'], commands)
        control.assert_called_once_with('inspect', self.configuration())

    def test_drain_refuses_remaining_runtime_despite_inactive_supervisor(self):
        command = Mock(return_value='inactive')
        control = Mock(return_value=({'runtime_absent': False}, 0))
        with self.assertRaises(ValueError):
            module.drain(self.configuration(), command=command, control=control)

    def test_drain_timeout_keeps_admission_stopped_and_does_not_force_cancel(self):
        commands = []
        def command(argv):
            commands.append(argv)
            return 'active'
        clock = iter((0, 12000))
        with self.assertRaises(TimeoutError):
            module.drain(self.configuration(), command=command, clock=lambda: next(clock))
        self.assertEqual(1, len(commands))
        self.assertEqual('stop', commands[0][1])

    def test_explicit_recovery_stops_supervision_before_owned_disposal(self):
        commands = []
        def command(argv):
            commands.append(argv)
            return ''
        control = Mock(return_value=({'phase': 'quarantined'}, 1))
        result, code = module.recover(self.configuration(), command=command, control=control)
        self.assertEqual(1, code)
        self.assertEqual({'phase': 'quarantined'}, result)
        self.assertEqual([['systemctl', 'stop', 'forgejo-runner-playbook.timer'],
                          ['systemctl', 'stop', 'forgejo-runner-playbook.service']], commands)
        control.assert_called_once_with('recover', self.configuration())
