import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from support import CONFIG
ROOT=Path(__file__).resolve().parents[2]
class SubmissionTests(unittest.TestCase):
    def module(self):
        path=ROOT/'roles/forgejo_metadata/files/submit.py'; self.assertTrue(path.exists(),'submission helper missing')
        spec=importlib.util.spec_from_file_location('submit',path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
    def test_cleanup_and_concurrent_submission(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def runner(*args,**kwargs):
                self.assertEqual(['systemctl','start','forgejo-metadata-control.service'],args[0])
                self.assertTrue((root/'control.json').is_file())
                self.assertEqual(1,module.submit({'operation':'manual-apply'},root,runner=lambda *a,**k:None))
                raise OSError('synthetic failure')
            self.assertEqual(1,module.submit({'operation':'manual-apply'},root,runner=runner))
            self.assertFalse((root/'control.json').exists())

    def test_plan_uses_fixed_read_only_service_and_returns_review_output(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=root/'config.json'
            config.write_text(json.dumps(dict(CONFIG,timeout_seconds=60)))
            before={p.name:p.read_bytes() for p in root.iterdir()}; calls=[]
            def runner(command,**kwargs):
                calls.append(command)
                return type('Result',(),{'returncode':0,'stdout':'{"digest":"review","mappings":[]}', 'stderr':''})()
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0,module.plan_labels(config,runner))
            self.assertEqual('review',json.loads(output.getvalue())['digest'])
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})
            self.assertIn('--property=User=svc-forgejo-metadata',calls[0])
            self.assertIn('--property=ProtectSystem=strict',calls[0])
            self.assertIn('--property=ReadOnlyPaths=/var/lib/forgejo-metadata',calls[0])
            self.assertIn('--property=LoadCredential=source:/etc/forgejo-metadata/credentials/source',calls[0])
            self.assertEqual(['--config','/etc/forgejo-metadata/config.json','labels-plan'],calls[0][-3:])
            self.assertFalse(any(arg.startswith('--property=ReadWritePaths') for arg in calls[0]))
