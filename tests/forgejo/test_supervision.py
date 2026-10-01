"""OS supervision evidence must be present alongside stock application evidence."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SupervisionTests(unittest.TestCase):
    def test_registered_scenario_installs_and_runs_the_recovery_probe(self):
        scenario = ROOT / 'roles/forgejo/molecule/default'
        self.assertTrue((scenario / 'recovery_probe.py').is_file(), 'supervised recovery probe missing')
        self.assertIn('/usr/local/libexec/forgejo-recovery-probe.py', (scenario / 'verify.yml').read_text())
        self.assertIn('src: recovery_probe.py', (scenario / 'prepare.yml').read_text())
