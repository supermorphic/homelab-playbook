"""Real TLS and process restart acceptance against owned local APIs."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from support import CONFIG
from forgejo_metadata.model import load_config,enrollment_fingerprint
from forgejo_metadata.state import StateStore
from forgejo_metadata.api import SourceAPI
ROOT=Path(__file__).resolve().parents[2]
class FixtureTests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/forgejo_metadata/fixture.py'; self.assertTrue(path.exists(),'HTTPS fixture missing')
        spec=importlib.util.spec_from_file_location('fixture',path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.fixture=module.Fixture(); self.fixture.__enter__(); self.addCleanup(self.fixture.__exit__,None,None,None)
        self.root=self.fixture.root; self.package=ROOT/'roles/forgejo_metadata/files'
        mapping=dict(CONFIG['mappings'][0],source_origin=self.fixture.source_origin,destination_origin=self.fixture.destination_origin,ca_file=str(self.root/'ca.pem'))
        self.document={'version':1,'mappings':[mapping],'state_root':str(self.root/'runtime'),'write_budget':20,'timeout_seconds':30}
        (self.root/'runtime').mkdir(mode=0o700); (self.root/'credentials').mkdir(mode=0o700)
        for name in ('source','destination'):
            path=self.root/'credentials'/name; path.write_text('synthetic-fixture-token'); path.chmod(0o600)
        self.config=self.root/'config.json'; self.save()
        self.store=StateStore(self.root/'runtime/state.json',enrollment_fingerprint(load_config(self.document))); self.store.bootstrap('initial','fixture-only')
        self.environment={'PATH':os.environ['PATH'],'PYTHONPATH':str(self.package),'CREDENTIALS_DIRECTORY':str(self.root/'credentials'),'SSL_CERT_FILE':str(self.root/'ca.pem')}
    def save(self): self.config.write_text(json.dumps(self.document))
    def command(self): return [sys.executable,'-B','-m','forgejo_metadata','--config',str(self.config),'apply']
    def apply(self):
        result=subprocess.run(self.command(),env=self.environment,cwd=self.package,capture_output=True,text=True,timeout=45)
        self.assertNotIn('synthetic-fixture-token',result.stdout+result.stderr)
        self.assertNotIn('Original text',result.stdout+result.stderr)
        return result.returncode
    def test_tls_backfill_repeat_and_retained_history(self):
        self.assertEqual(0,self.apply()); count=len(self.fixture.writes())
        self.assertEqual(0,self.apply()); self.assertEqual(count,len(self.fixture.writes()))
        data=self.fixture.snapshot(); self.assertEqual('closed',data['issues'][0]['state'])
        self.assertEqual(['fj-bug-42'],data['issues'][0]['labels']); self.assertEqual(102,data['issues'][0]['milestone'])
        self.assertEqual(1,len(data['comments'])); self.assertTrue(all(row[0]=='GET' for row in self.fixture.source_ledger))
        self.fixture.source_data['issues']=[]; self.assertEqual(0,self.apply()); self.assertEqual(1,len(self.fixture.snapshot()['issues']))

    def test_tls_accepted_issue_waits_for_collection_visibility(self):
        self.fixture.mode={'hide_new_issues_scans':1}
        self.assertEqual(0,self.apply())
        data=self.fixture.snapshot()
        self.assertEqual(1,len(data['issues']))
        self.assertEqual('closed',data['issues'][0]['state'])
        self.assertEqual(1,len(data['comments']))
        self.assertEqual(1,sum(method=='POST' and path.endswith('/issues') for method,path in self.fixture.writes()))

    def test_tls_milestone_due_date_preserves_utc_calendar_day(self):
        self.fixture.source_data['milestones'][0]['due_on']='2026-01-01T17:00:00-07:00'
        self.assertEqual(0,self.apply())
        self.assertEqual('2026-01-02T00:00:00Z',self.fixture.snapshot()['milestones'][0]['due_on'])
    def test_killed_after_accepted_post_rediscovered_without_duplicate(self):
        self.fixture.mode={'hang_after_post':True}
        process=subprocess.Popen(self.command(),env=self.environment,cwd=self.package,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+10
            while not self.fixture.writes() and time.monotonic()<deadline: time.sleep(.05)
            self.assertTrue(self.fixture.writes()); process.kill(); process.wait(timeout=5)
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=5)
        self.assertEqual(1,len(self.store.load()['pending']))
        self.fixture.mode={}; self.assertEqual(0,self.apply()); self.assertEqual(1,len(self.fixture.snapshot()['labels']))
        self.assertEqual({},self.store.load()['pending'])
    def test_rejection_lost_state_untrusted_tls_redirect_and_budget(self):
        self.fixture.mode={'reject':True}; self.assertEqual(1,self.apply()); self.assertEqual({},self.store.load()['pending'])
        before=len(self.fixture.writes()); self.fixture.mode={}; self.document['write_budget']=1; self.save(); self.assertEqual(1,self.apply())
        self.assertEqual(before+1,len(self.fixture.writes())); self.document['write_budget']=20; self.save(); self.assertEqual(0,self.apply())
        before=len(self.fixture.writes()); self.store.path.unlink(); self.assertEqual(1,self.apply()); self.assertEqual(before,len(self.fixture.writes()))
        self.store.bootstrap('recover','fixture-only'); self.document['mappings'][0]['ca_file']=''; self.environment.pop('SSL_CERT_FILE'); self.save()
        self.assertEqual(1,self.apply()); self.assertEqual(before,len(self.fixture.writes()))
        self.document['mappings'][0]['ca_file']=str(self.root/'ca.pem'); self.environment['SSL_CERT_FILE']=str(self.root/'ca.pem'); self.save(); self.fixture.mode={'redirect':True}
        self.assertEqual(1,self.apply()); self.assertEqual(before,len(self.fixture.writes()))
    def test_label_slash_and_dots_can_be_updated_and_renamed_over_tls(self):
        self.fixture.source_data['labels'][0]['name']='area/ui'; self.fixture.source_data['issues'][0]['labels'][0]['name']='area/ui'
        self.assertEqual(0,self.apply()); self.assertEqual('fj-area/ui-42',self.fixture.snapshot()['labels'][0]['name'])
        self.fixture.source_data['labels'][0]['color']='000000'; self.assertEqual(0,self.apply())
        self.assertEqual('000000',self.fixture.snapshot()['labels'][0]['color'])
        self.fixture.source_data['labels'][0]['name']='release..next'; self.fixture.source_data['issues'][0]['labels'][0]['name']='release..next'
        self.assertEqual(0,self.apply()); self.assertEqual('fj-release..next-42',self.fixture.snapshot()['labels'][0]['name'])
        self.assertEqual(1,len(self.fixture.snapshot()['labels'])); self.assertEqual(0,self.apply())
    def test_complete_source_pagination_over_tls_with_full_final_page(self):
        self.fixture.source_data['labels']=[{'id':number,'name':f'label-{number}','color':'abcdef'} for number in range(1,101)]
        mapping=load_config(self.document)[0]
        inventory=SourceAPI(mapping,'synthetic-fixture-token').inventory(mapping)
        self.assertEqual(list(range(1,101)),[row['id'] for row in inventory.labels])
        self.assertEqual(2,sum(path.endswith('/labels') for _,path in self.fixture.source_ledger))
