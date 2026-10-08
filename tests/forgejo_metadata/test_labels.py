"""Adoption is reviewed, bounded, and separate from ordinary synchronization."""
import copy
import importlib
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import quote
from support import CONFIG, FakeSource, FakeDestination
from forgejo_metadata.model import MirrorError, SourceKey, load_config, enrollment_fingerprint
from forgejo_metadata.state import StateStore, atomic_json


class LabelAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.mapping=load_config(CONFIG)[0]
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store=StateStore(Path(temporary.name)/'state.json',enrollment_fingerprint([self.mapping]))
        self.store.bootstrap('initial','existing-enrollment')
        self.source=FakeSource(); self.destination=FakeDestination()
        self.label={'id':90,'name':'bug','color':'abcdef','description':'Defect'}
        self.destination.data.labels.append(self.label)
        self.destination.rows['/labels/bug']=self.label
        try: self.module=importlib.import_module('forgejo_metadata.labels')
        except ModuleNotFoundError: self.fail('label adoption implementation is missing')

    def clients(self,mapping): return self.source,self.destination
    def plan(self): return self.module.build_adoption_plan([self.mapping],self.store,self.clients)
    def adopt(self,plan,budget=30):
        return self.module.adopt_labels([self.mapping],self.store,self.clients,plan['digest'],'review-89',budget)

    def test_plan_is_read_only_and_lists_exact_pair_and_projection(self):
        before=self.store.path.read_bytes(); plan=self.plan()
        row=plan['mappings'][0]['labels'][0]
        self.assertEqual('example/project',plan['mappings'][0]['source_repo'])
        self.assertEqual('example/recovery',plan['mappings'][0]['destination_repo'])
        self.assertEqual('ready',row['status'])
        self.assertEqual({'instance':'fixture','repository_id':7,'kind':'label','object_id':42},row['key'])
        self.assertEqual(90,row['destination']['id'])
        self.assertEqual('bug',row['projection']['name'])
        self.assertEqual('forgejo-mirror:v=1;src=fixture;repo=7;kind=label;id=42 Defect',row['projection']['description'])
        self.assertEqual(before,self.store.path.read_bytes()); self.assertEqual([],self.destination.writes)

    def test_adoption_reuses_label_and_preserves_historical_assignments(self):
        self.destination.data.issues.append({'id':80,'number':80,'body':'History','labels':['bug'],'user':{'id':99}})
        before=copy.deepcopy(self.destination.data.issues)
        self.assertEqual(1,self.adopt(self.plan()))
        self.assertEqual(90,self.destination.data.labels[0]['id'])
        self.assertEqual(before,self.destination.data.issues)
        self.assertEqual([('PATCH','/labels/bug')],self.destination.writes)
        self.assertEqual('/labels/90',self.store.load()['ownership']['fixture:7:label:42'])
        self.assertEqual(0,self.adopt(self.plan()))
        from forgejo_metadata.reconcile import reconcile
        result=reconcile(self.mapping,self.source,self.destination,self.store,30)
        self.assertEqual([],result.errors); self.assertEqual(['bug'],self.destination.data.issues[1]['labels'])
        self.assertEqual(1,len(self.destination.data.labels))

    def test_changed_source_or_destination_invalidates_review_before_write(self):
        for row,field,value in [(self.label,'id',91),(self.label,'color','000000'),
                (self.label,'description','Changed'),(self.source.data.labels[0],'name','renamed'),
                (self.source.data.labels[0],'description','New description')]:
            with self.subTest(field=field):
                plan=self.plan(); old=row[field]; row[field]=value
                with self.assertRaisesRegex(MirrorError,'adoption_plan_changed'): self.adopt(plan)
                row[field]=old
                self.assertEqual([],self.destination.writes)

    def test_missing_ambiguous_different_case_and_conflicting_pairs_are_reported(self):
        self.destination.data.labels=[]
        self.assertEqual('missing',self.plan()['mappings'][0]['labels'][0]['status'])
        self.destination.data.labels=[self.label,dict(self.label,id=91)]
        self.assertEqual('ambiguous',self.plan()['mappings'][0]['labels'][0]['status'])
        with self.assertRaisesRegex(MirrorError,'adoption_conflict'): self.adopt(self.plan())
        self.destination.data.labels=[dict(self.label,name='Bug')]
        self.assertEqual('conflict',self.plan()['mappings'][0]['labels'][0]['status'])
        self.destination.data.labels=[self.label]
        self.label['description']='forgejo-mirror:v=1;src=other;repo=8;kind=label;id=42 Defect'
        self.assertEqual('conflict',self.plan()['mappings'][0]['labels'][0]['status'])
        with self.assertRaisesRegex(MirrorError,'adoption_conflict'): self.adopt(self.plan())
        self.assertEqual([],self.destination.writes)

    def test_missing_state_and_unresolved_create_do_not_allow_adoption(self):
        self.store.begin_create(SourceKey('fixture',7,'label',42))
        self.assertEqual('conflict',self.plan()['mappings'][0]['labels'][0]['status'])
        with self.assertRaisesRegex(MirrorError,'adoption_conflict'): self.adopt(self.plan())
        self.store.path.unlink(); plan=self.plan()
        with self.assertRaisesRegex(MirrorError,'state_uninitialized'): self.adopt(plan)
        self.assertEqual([],self.destination.writes)

    def test_journal_upgrade_preserves_existing_records_and_recovery(self):
        self.store.begin_create(SourceKey('fixture',7,'issue',99))
        document=self.store.load(); document['ownership']={'fixture:7:issue:23':'/issues/80'}
        atomic_json(self.store.path,document)
        self.destination.data.issues=[{'id':80,'number':80,'body':'History\n<!-- forgejo-mirror:v=1;src=fixture;repo=7;kind=issue;id=23 -->','user':{'id':11}}]
        self.assertEqual(1,self.adopt(self.plan()))
        after=self.store.load()
        self.assertEqual(document['initialization'],after['initialization'])
        self.assertEqual(document['pending'],after['pending'])
        self.assertEqual('/issues/80',after['ownership']['fixture:7:issue:23'])
        self.store.bootstrap('recover','restarted')
        self.assertEqual(after['ownership'],self.store.load()['ownership'])
        self.assertEqual(0,self.adopt(self.plan()))

    def test_rechecks_all_reviewed_pairs_immediately_before_each_write(self):
        other={'id':91,'name':'feature','color':'123abc','description':'Feature'}
        self.source.data.labels.append(dict(other,id=43))
        self.destination.data.labels.append(other); self.destination.rows['/labels/feature']=other
        update=self.destination.update
        def race(target,projection):
            update(target,projection)
            other['description']='Concurrent edit'
        self.destination.update=race
        with self.assertRaisesRegex(MirrorError,'adoption_plan_changed'): self.adopt(self.plan())
        self.assertEqual([('PATCH','/labels/bug')],self.destination.writes)
        self.assertEqual('Concurrent edit',other['description'])

    def test_budget_and_readback_mismatch_fail_without_creates(self):
        with self.assertRaisesRegex(MirrorError,'adoption_budget_exceeded'): self.adopt(self.plan(),0)
        self.assertEqual([],self.destination.writes)
        self.destination.drop='description'
        with self.assertRaisesRegex(MirrorError,'readback_mismatch'): self.adopt(self.plan())
        self.assertEqual([('PATCH','/labels/bug')],self.destination.writes)

    def test_surviving_retry_deadline_blocks_plan_and_adoption_before_api_reads(self):
        plan=self.plan(); self.store.defer(time.time()+3600,'destination')
        before=self.destination.preflights
        for action in (self.plan,lambda:self.adopt(plan)):
            with self.assertRaisesRegex(MirrorError,'rate_limited'): action()
            self.assertEqual(before,self.destination.preflights)
        self.assertEqual([],self.destination.writes)

    def test_numeric_label_evidence_survives_source_and_destination_renames(self):
        self.adopt(self.plan())
        self.source.data.labels[0]['name']='area/ui'
        self.label['name']='area/ui'
        self.destination.rows['/labels/'+quote('area/ui',safe='')]=self.destination.rows.pop('/labels/bug')
        self.assertEqual('owned',self.plan()['mappings'][0]['labels'][0]['status'])
        self.assertEqual(0,self.adopt(self.plan()))
        self.label['id']=91
        self.assertIn('ownership_conflict',self.plan()['mappings'][0]['errors'])
        with self.assertRaisesRegex(MirrorError,'adoption_conflict'): self.adopt(self.plan())
