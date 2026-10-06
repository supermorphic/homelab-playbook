import copy
import importlib
import unittest
from support import CONFIG

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        try: self.model = importlib.import_module('forgejo_metadata.model')
        except ModuleNotFoundError: self.fail('metadata configuration implementation is missing')

    def test_rejects_unsafe_enrollment(self):
        for field, value in [('instance', 'x'*100), ('source_id', True),
                             ('source_origin', 'http://forgejo.example.test'),
                             ('source_origin', 'https://user:secret@example.test'),
                             ('source_repo', '../project'), ('actor_id', 0)]:
            with self.subTest(field=field, value=value):
                doc=copy.deepcopy(CONFIG); doc['mappings'][0][field]=value
                with self.assertRaises(self.model.MirrorError): self.model.load_config(doc)
        doc=copy.deepcopy(CONFIG); doc['mappings']*=2
        with self.assertRaises(self.model.MirrorError): self.model.load_config(doc)

    def test_token_rotation_does_not_reset_initialization(self):
        before=self.model.enrollment_fingerprint(self.model.load_config(CONFIG))
        doc=copy.deepcopy(CONFIG); doc['mappings'][0]['source_credential']='rotated'
        self.assertEqual(before, self.model.enrollment_fingerprint(self.model.load_config(doc)))
        doc['mappings'][0]['destination_id']=10
        self.assertNotEqual(before, self.model.enrollment_fingerprint(self.model.load_config(doc)))
