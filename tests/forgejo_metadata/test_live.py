import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


class LiveGuardTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[2]/'scripts/forgejo_metadata/live.py'
        spec=importlib.util.spec_from_file_location('metadata_live',path)
        self.module=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)

    def test_confirmation_and_target_are_required_before_running_commands(self):
        document={'host':'fixture','fingerprint':'a'*64,'mappings':[{'source':'fixture:7',
            'source_repo':'example/project','source_origin':'https://forgejo.example.test',
            'destination_repo':'example/recovery','destination_origin':'https://api.github.com',
            'destination_id':9,'visibility':'public','actor_id':11}]}
        self.assertEqual('fixture',self.module.validate_target(document,'test-live:'+'a'*64)['host'])
        for change,confirmation in [({},''),({'host':'--all'},'test-live:'+'a'*64),
                                    ({'mappings':[]},'test-live:'+'a'*64)]:
            with self.subTest(change=change):
                with self.assertRaises(ValueError): self.module.validate_target(dict(document,**change),confirmation)

    def test_fixture_ownership_requires_id_and_prefix(self):
        self.module.require_owned({'id':42,'body':'acceptance-fixture-abc: initial'},42,'acceptance-fixture-abc')
        for row in [{'id':43,'body':'acceptance-fixture-abc: initial'},
                    {'id':42,'body':'Unrelated history'}]:
            with self.subTest(row=row):
                with self.assertRaises(ValueError): self.module.require_owned(row,42,'acceptance-fixture-abc')

    def test_unknown_fixture_create_is_not_retried(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'receipt.json'
            state={'pending':'issue','objects':{},'prefix':'acceptance-fixture-abc'}
            path.write_text(json.dumps(state))
            with self.assertRaises(ValueError): self.module.load_receipt(path)

    def test_directory_persistence_failure_prevents_fixture_send(self):
        with tempfile.TemporaryDirectory() as root:
            target={'fingerprint':'a'*64,'mappings':[{'source_repo':'example/project'}]}
            experiment=self.module.Experiment(target,Path(root)/'receipt.json')
            experiment.preflight=lambda: None
            def forbidden(*arguments): self.fail('A fixture POST was sent before its intent was durable')
            experiment.tea=forbidden
            original=os.fsync
            def fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('directory persistence failed')
                return original(fd)
            with patch.object(self.module.os,'fsync',side_effect=fsync):
                with self.assertRaises(OSError): experiment.create('issue','issue','create')

    def test_comment_create_rechecks_parent_ownership_after_preflight(self):
        with tempfile.TemporaryDirectory() as root:
            target={'fingerprint':'a'*64,'mappings':[{'source_repo':'example/project'}]}
            experiment=self.module.Experiment(target,Path(root)/'receipt.json')
            experiment.state['objects']['issue']={'id':42,'number':5}
            parent={'id':42,'body':experiment.state['prefix']+': fixture'}
            def preflight(): parent['body']='Unrelated replacement'
            experiment.preflight=preflight
            def tea(*arguments):
                if arguments[:2]==('issue','get'): return parent
                self.fail('Comment POST was sent to an unowned parent')
            experiment.tea=tea
            with self.assertRaisesRegex(ValueError,'fixture_ownership_changed'):
                experiment.create('comment','issue','comment','create','example/project','5')

    def test_receipt_lock_excludes_a_second_writer_and_releases(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'receipt.json'
            with self.module.receipt_lock(path):
                with self.assertRaisesRegex(ValueError,'fixture_busy'):
                    with self.module.receipt_lock(path): self.fail('Second writer acquired receipt')
            with self.module.receipt_lock(path): pass


if __name__=='__main__': unittest.main()
