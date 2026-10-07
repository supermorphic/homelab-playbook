import configparser
import importlib.util
from pathlib import Path
import unittest

from jinja2 import Template
import yaml

ROOT = Path(__file__).parents[2]
WORKTREE = ROOT
spec = importlib.util.spec_from_file_location('forgejo_input_oracle', WORKTREE / 'tests/forgejo/test_role_inputs.py')
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


class ActionsConfigurationTests(unittest.TestCase):
    def settings(self):
        values = oracle.InputTests().settings()
        defaults = yaml.safe_load((ROOT / 'roles/forgejo/defaults/main.yml').read_text())
        values.update({key: value for key, value in defaults.items() if key.startswith('forgejo_actions_')})
        return values

    def render(self, values):
        config = configparser.ConfigParser(interpolation=None)
        config.read_string('[DEFAULT]\n' + Template((ROOT / 'roles/forgejo/templates/app.ini.j2').read_text()).render(**values))
        return config

    def test_actions_remain_disabled_until_the_explicit_opt_in(self):
        values = self.settings()
        self.assertFalse(self.render(values).getboolean('actions', 'ENABLED'))
        values['forgejo_actions_enabled'] = True
        self.assertTrue(self.render(values).getboolean('actions', 'ENABLED'))

    def test_actions_data_stays_inside_the_existing_backup_boundary(self):
        config = self.render(self.settings())
        root = Path(config['server']['APP_DATA_PATH'])
        for section in ('storage.actions_log', 'storage.artifacts'):
            self.assertEqual('local', config[section]['STORAGE_TYPE'])
            self.assertTrue(Path(config[section]['PATH']).is_relative_to(root))
        self.assertEqual(7, config.getint('actions', 'LOG_RETENTION_DAYS'))
        self.assertEqual(7, config.getint('actions', 'ARTIFACT_RETENTION_DAYS'))

    def test_invalid_or_unbounded_actions_inputs_are_rejected_by_real_ansible(self):
        oracle_case = oracle.InputTests()
        inputs = ROOT / 'roles/forgejo/tasks/inputs.yml'
        self.assertEqual(0, oracle_case.evaluate(self.settings(), inputs))
        for key, value in (('forgejo_actions_enabled', 'true'), ('forgejo_actions_log_retention_days', 0),
                           ('forgejo_actions_artifact_retention_days', -1),
                           ('forgejo_actions_log_retention_days', True)):
            settings = self.settings()
            settings[key] = value
            with self.subTest(key=key, value=value):
                self.assertNotEqual(0, oracle_case.evaluate(settings, inputs))
