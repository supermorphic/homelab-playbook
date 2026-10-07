import importlib.util
from pathlib import Path
import tempfile
import unittest
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
