import contextlib
import copy
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from support import CONFIG, FakeSource, FakeDestination
from forgejo_metadata.model import enrollment_fingerprint, load_config

class CLITests(unittest.TestCase):
    def setUp(self):
        try: self.module=importlib.import_module('forgejo_metadata.cli')
        except ModuleNotFoundError: self.fail('metadata runtime CLI is missing')
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name); self.config=self.root/'config.json'
        self.config.write_text(json.dumps(dict(CONFIG,state_root=str(self.root),write_budget=30,timeout_seconds=60)))
        self.output=io.StringIO(); self.source=FakeSource(); self.destination=FakeDestination()
    def run_cli(self,*args):
        with contextlib.redirect_stdout(self.output),contextlib.redirect_stderr(self.output),patch.object(self.module,'clients',return_value=(self.source,self.destination)):
            return self.module.main(['--config',str(self.config),*args])
    def request(self,operation,**extra):
        fingerprint=enrollment_fingerprint(load_config(CONFIG))
        value=dict(operation=operation,fingerprint=fingerprint,decision_ref='offline-decision',
            confirmation=f'{operation}:{fingerprint}',previous_writers_stopped=True,outcomes_resolved=True,**extra)
        path=self.root/'request.json'; path.write_text(json.dumps(value)); path.chmod(0o640)
        return path
    def test_missing_state_and_check_do_not_mutate_or_initialize(self):
        self.assertEqual(1,self.run_cli('apply')); self.assertEqual([],self.destination.writes)
        self.assertFalse((self.root/'state.json').exists())
        before=list(self.root.iterdir()); self.assertEqual(1,self.run_cli('check'))
        self.assertEqual(before,list(self.root.iterdir()))
    def test_attended_bootstrap_apply_and_unchanged_repeat(self):
        request=self.request('bootstrap-initial')
        with patch.object(self.module,'trusted_request',return_value=json.loads(request.read_text())):
            self.assertEqual(0,self.run_cli('control','--request',str(request)))
        self.assertEqual(0,self.run_cli('apply')); count=len(self.destination.writes)
        self.assertEqual(0,self.run_cli('apply')); self.assertEqual(count,len(self.destination.writes))
        status=json.loads((self.root/'status.json').read_text()); self.assertTrue(status['mappings'][0]['last_converged'])
    def test_wrong_confirmation_and_untrusted_request_fail_before_state_write(self):
        request=self.request('bootstrap-initial'); value=json.loads(request.read_text()); value['confirmation']='wrong'
        with patch.object(self.module,'trusted_request',return_value=value): self.assertEqual(1,self.run_cli('control','--request',str(request)))
        self.assertFalse((self.root/'state.json').exists())
        request.chmod(0o666)
        self.assertEqual(1,self.run_cli('control','--request',str(request)))
    def test_partial_failure_preserves_last_convergence_and_sanitizes_output(self):
        request=self.request('bootstrap-initial')
        with patch.object(self.module,'trusted_request',return_value=json.loads(request.read_text())): self.run_cli('control','--request',str(request))
        self.run_cli('apply'); previous=json.loads((self.root/'status.json').read_text())['mappings'][0]['last_converged']
        self.source.data.issues[0]['milestone']=None; self.destination.drop='milestone'
        self.assertEqual(1,self.run_cli('apply'))
        status=json.loads((self.root/'status.json').read_text())
        self.assertEqual(previous,status['mappings'][0]['last_converged'])
        self.assertNotIn('Original text',self.output.getvalue()); self.assertNotIn('synthetic-token',self.output.getvalue())
    def test_check_success_is_observational_and_arguments_are_bounded(self):
        request=self.request('bootstrap-initial')
        with patch.object(self.module,'trusted_request',return_value=json.loads(request.read_text())): self.run_cli('control','--request',str(request))
        self.run_cli('apply'); before={p.name:p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(0,self.run_cli('check'))
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
        with self.assertRaises(SystemExit) as error: self.run_cli('unknown')
        self.assertEqual(2,error.exception.code)
    def test_credentials_have_no_ambient_fallback(self):
        with patch.dict('os.environ',{'GITHUB_TOKEN':'synthetic-token'},clear=True):
            with self.assertRaisesRegex(Exception,'credentials_unavailable'): self.module.credential('destination')
    def test_recovery_preserves_unmatched_pending_intent(self):
        from forgejo_metadata.model import SourceKey
        from forgejo_metadata.state import StateStore
        request=self.request('bootstrap-initial'); fingerprint=enrollment_fingerprint(load_config(CONFIG))
        with patch.object(self.module,'trusted_request',return_value=json.loads(request.read_text())): self.run_cli('control','--request',str(request))
        store=StateStore(self.root/'state.json',fingerprint); store.begin_create(SourceKey('fixture',7,'issue',99))
        request=self.request('bootstrap-recover')
        with patch.object(self.module,'trusted_request',return_value=json.loads(request.read_text())): self.assertEqual(0,self.run_cli('control','--request',str(request)))
        self.assertEqual(1,len(store.load()['pending']))
    def test_plan_reports_bound_enrollment_without_state_or_network(self):
        before=list(self.root.iterdir())
        self.assertEqual(0,self.run_cli('plan'))
        value=json.loads(self.output.getvalue()); self.assertEqual('example/recovery',value['mappings'][0]['destination_repo'])
        self.assertEqual(enrollment_fingerprint(load_config(CONFIG)),value['fingerprint'])
        self.assertEqual(before,list(self.root.iterdir())); self.assertEqual([],self.destination.writes)
    def test_rate_limit_survives_restart_and_other_credentials_progress(self):
        from forgejo_metadata.api import APIError
        from forgejo_metadata.state import StateStore
        document=json.loads(self.config.read_text()); other=copy.deepcopy(document['mappings'][0])
        other.update(instance='other',source_id=8,destination_id=10,source_credential='other_source',destination_credential='other_destination')
        document['mappings'].append(other); self.config.write_text(json.dumps(document))
        store=StateStore(self.root/'state.json',enrollment_fingerprint(load_config(document))); store.bootstrap('initial','fixture-only')
        independent=FakeDestination(); calls=[]
        def denied(mapping):
            calls.append(mapping.instance); raise APIError('http_429','not_created',self.module.time.time()+60,'destination')
        self.destination.preflight=denied
        def clients(mapping): return self.source,self.destination if mapping.instance=='fixture' else independent
        with contextlib.redirect_stdout(self.output),patch.object(self.module,'clients',side_effect=clients):
            self.assertEqual(1,self.module.main(['--config',str(self.config),'apply']))
            self.assertTrue(independent.writes)
            self.assertEqual(1,self.module.main(['--config',str(self.config),'apply']))
        self.assertEqual(['fixture'],calls)
    def test_reenrollment_cannot_reuse_old_convergence(self):
        from forgejo_metadata.state import StateStore
        request=self.request('bootstrap-initial')
        with patch.object(self.module,'trusted_request',return_value=json.loads(request.read_text())): self.run_cli('control','--request',str(request))
        self.assertEqual(0,self.run_cli('apply'))
        document=json.loads(self.config.read_text()); document['mappings'][0]['destination_id']=10
        self.config.write_text(json.dumps(document))
        StateStore(self.root/'state.json',enrollment_fingerprint(load_config(document))).bootstrap('recover','new-enrollment')
        self.assertEqual(1,self.run_cli('check'))
        self.destination=FakeDestination(); self.destination.failure='http_422'
        self.assertEqual(1,self.run_cli('apply'))
        status=json.loads((self.root/'status.json').read_text())
        self.assertIsNone(status['mappings'][0]['last_converged'])
