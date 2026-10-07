import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from support import CONFIG
from forgejo_metadata.model import MirrorError, SourceKey

class StateTests(unittest.TestCase):
    def setUp(self):
        try: self.module=importlib.import_module('forgejo_metadata.state')
        except ModuleNotFoundError: self.fail('durable safety state is missing')
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.json'; self.store=self.module.StateStore(self.path,'fixture-fingerprint')
        self.key=SourceKey('fixture',7,'issue',23)

    def test_missing_corrupt_mismatched_and_symlinked_state_never_enables_writes(self):
        with self.assertRaises(MirrorError): self.store.require_initialized()
        self.path.write_text('{')
        with self.assertRaises(MirrorError): self.store.require_initialized()
        self.path.unlink(); self.store.bootstrap('initial','decision-1')
        with self.assertRaises(MirrorError): self.module.StateStore(self.path,'other').require_initialized()
        real=self.path.with_suffix('.real'); self.path.rename(real); self.path.symlink_to(real)
        with self.assertRaises(MirrorError): self.store.load()

    def test_unknown_outcome_blocks_retry_but_rejection_and_owned_object_clear(self):
        self.store.bootstrap('initial','decision-1'); self.store.begin_create(self.key)
        self.store.finish_create(self.key,'unknown',None)
        with self.assertRaises(MirrorError): self.store.begin_create(self.key)
        self.store.finish_create(self.key,'not_created',None); self.store.begin_create(self.key)
        self.store.finish_create(self.key,'created','/issues/90')
        self.assertEqual({},self.store.load()['pending'])

    def test_recovery_preserves_intents_and_initial_refuses_overwrite(self):
        self.store.bootstrap('initial','decision-1'); self.store.begin_create(self.key)
        with self.assertRaises(MirrorError): self.store.bootstrap('initial','decision-2')
        self.store.bootstrap('recover','decision-2')
        self.assertEqual(1,len(self.store.load()['pending']))
        self.path.write_text('{')
        self.store.bootstrap('recover','decision-resolved')
        self.assertTrue(list(self.path.parent.glob('state.json.recovery-*')))

    def test_failed_durable_intent_prevents_safe_send(self):
        self.store.bootstrap('initial','decision-1')
        with patch.object(self.module.os,'fsync',side_effect=OSError('disk unavailable')):
            with self.assertRaises(MirrorError): self.store.begin_create(self.key)
        self.assertEqual({},self.store.load()['pending'])

    def test_nonblocking_lock_and_release(self):
        path=self.path.parent/'operation.lock'
        with self.module.operation_lock(path):
            with self.assertRaises(MirrorError):
                with self.module.operation_lock(path): pass
        with self.module.operation_lock(path): pass
    def test_invalid_nested_intent_is_not_valid_initialization(self):
        from forgejo_metadata.state import atomic_json
        self.store.bootstrap('initial','test'); value=self.store.load(); value['pending']={'fixture:7:issue:99':{'key':{},'started':1}}
        atomic_json(self.store.path,value)
        with self.assertRaises(MirrorError): self.store.require_initialized()
    def test_reenrollment_and_damaged_initialization_preserve_valid_intents(self):
        self.store.bootstrap('initial','test'); self.store.begin_create(self.key)
        other=self.module.StateStore(self.path,'new-fingerprint'); other.bootstrap('recover','new-enrollment')
        self.assertEqual(1,len(other.load()['pending']))
        value=other.load(); value['initialization']={}; self.module.atomic_json(self.path,value)
        other.bootstrap('recover','damaged-initialization')
        self.assertEqual(1,len(other.load()['pending']))
