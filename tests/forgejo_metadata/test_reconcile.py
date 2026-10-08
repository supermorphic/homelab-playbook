import copy
import importlib
from pathlib import Path
import tempfile
import unittest
from support import CONFIG, FakeSource, FakeDestination
from forgejo_metadata.model import load_config, MirrorError
from forgejo_metadata.state import StateStore

class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        try: self.module=importlib.import_module('forgejo_metadata.reconcile')
        except ModuleNotFoundError: self.fail('metadata reconciliation is missing')
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.store=StateStore(Path(temp.name)/'state.json','fixture'); self.store.bootstrap('initial','test')
        self.mapping=load_config(CONFIG)[0]; self.source=FakeSource(); self.destination=FakeDestination()
    def run_mirror(self,budget=30):
        return self.module.reconcile(self.mapping,self.source,self.destination,self.store,budget)
    def test_historical_backfill_closure_assignments_and_repeat_noop(self):
        result=self.run_mirror(); self.assertEqual([],result.errors)
        issue=self.destination.data.issues[0]
        self.assertEqual('closed',issue['state']); self.assertEqual(['bug'],issue['labels'])
        self.assertEqual(102,issue['milestone']); self.assertEqual(1,len(self.destination.data.comments[103]))
        self.assertIn('Original text',issue['body']); self.assertIn('Original comment',self.destination.data.comments[103][0]['body'])
        self.destination.writes=[]; self.assertEqual(0,self.run_mirror().writes)
        self.assertEqual([],self.destination.writes)

    def test_unmarked_same_name_label_stops_without_adoption_or_creation(self):
        self.destination.data.labels.append({'id':90,'name':'bug','color':'abcdef','description':'Defect'})
        self.assertIn('label_name_collision', self.run_mirror().errors)
        self.assertEqual([], self.destination.writes)
        self.assertEqual({}, self.store.load()['pending'])

    def test_label_rename_preserves_numeric_identity_and_restart_evidence(self):
        self.assertEqual([], self.run_mirror().errors)
        identity=self.destination.data.labels[0]['id']
        self.source.data.labels[0]['name']='area/ui'
        self.assertEqual([], self.run_mirror().errors)
        self.store=StateStore(self.store.path,'fixture')
        self.assertEqual(identity,self.destination.data.labels[0]['id'])
        self.assertEqual(['area/ui'],self.destination.data.issues[0]['labels'])
        self.assertEqual('/labels/'+str(identity), self.store.load()['ownership']['fixture:7:label:42'])
        self.destination.writes=[]
        self.assertEqual(0,self.run_mirror().writes)
        self.assertEqual([],self.destination.writes)

    def test_known_label_missing_marker_or_replaced_id_stops_mapping(self):
        self.assertEqual([],self.run_mirror().errors)
        row=self.destination.data.labels[0]; original=row['description']
        row['description']='Defect'; self.destination.writes=[]
        self.assertIn('ownership_conflict',self.run_mirror().errors)
        self.assertEqual([],self.destination.writes)
        row['description']=original; row['id']=999
        self.assertIn('ownership_conflict',self.run_mirror().errors)
        self.assertEqual([],self.destination.writes)

    def test_rename_collision_preserves_both_labels(self):
        self.assertEqual([],self.run_mirror().errors)
        self.destination.data.labels.append({'id':90,'name':'occupied','color':'abcdef','description':'Human label'})
        before=copy.deepcopy(self.destination.data)
        self.source.data.labels[0]['name']='occupied'; self.destination.writes=[]
        self.assertIn('label_name_collision',self.run_mirror().errors)
        self.assertEqual(before,self.destination.data)
        self.assertEqual([],self.destination.writes)

    def test_label_update_readback_rejects_replacement_numeric_identity(self):
        self.assertEqual([],self.run_mirror().errors)
        self.source.data.issues=[]; self.source.data.milestones=[]
        self.source.data.labels[0]['name']='renamed'
        update=self.destination.update
        def replaced(target,projection):
            update(target,projection)
            if projection.key.kind=='label': self.destination.data.labels[0]['id']=999
        self.destination.update=replaced
        self.assertIn('ownership_conflict',self.run_mirror().errors)
    def test_edits_and_cleared_assignments_preserve_missing_objects(self):
        self.run_mirror(); original_count=self.destination.count
        issue=self.source.data.issues[0]; issue.update(title='Changed',state='open',labels=[],milestone=None)
        self.source.data.comments[23][0]['body']='Edited comment'
        self.source.data.labels[0]['name']='renamed'
        result=self.run_mirror(); self.assertEqual([],result.errors)
        observed=self.destination.data.issues[0]
        self.assertEqual(('Changed','open',[],None),(observed['title'],observed['state'],observed['labels'],observed['milestone']))
        self.assertIn('Edited comment',self.destination.data.comments[103][0]['body'])
        self.assertEqual(original_count,self.destination.count)
        self.source.data.issues=[]; self.source.data.comments={}
        self.assertEqual([],self.run_mirror().errors); self.assertEqual(1,len(self.destination.data.issues))
    def test_ignored_fields_fail_convergence_without_duplicate_creation(self):
        self.destination.drop='milestone'; result=self.run_mirror()
        self.assertIn('readback_mismatch',result.errors); self.assertIsNone(result.converged_at)
        self.assertEqual(1,len(self.destination.data.issues)); self.run_mirror()
        self.assertEqual(1,len(self.destination.data.issues)); self.assertEqual({},self.store.load()['pending'])
    def test_lost_response_recovers_by_marker_and_definite_rejection_retries(self):
        self.destination.lost=True; result=self.run_mirror(); self.assertTrue(result.errors)
        self.assertTrue(self.store.load()['pending']); self.run_mirror()
        self.assertEqual(1,len(self.destination.data.labels)); self.assertEqual({},self.store.load()['pending'])
        self.source.data.issues.append(dict(self.source.data.issues[0],id=25,number=6))
        self.destination.failure='http_422'; self.run_mirror(); self.assertEqual({},self.store.load()['pending'])
        self.destination.failure=None; self.run_mirror(); self.assertEqual(2,len(self.destination.data.issues))
    def test_normalized_accepted_label_resolves_intent_without_duplicate(self):
        from forgejo_metadata.api import APIError
        create=self.destination.create; first=[True]
        def normalized(projection,parent=None):
            locator=create(projection,parent)
            if projection.key.kind=='label':
                row=self.destination.rows[locator]
                row['description']=' '.join(row['description'].split())
                if first[0]:
                    first[0]=False
                    raise APIError('transport_failure')
            return locator
        self.destination.create=normalized
        self.assertTrue(self.run_mirror().errors)
        self.assertTrue(self.store.load()['pending'])
        self.assertEqual([],self.run_mirror().errors)
        self.assertEqual(1,len(self.destination.data.labels))
        self.assertEqual({},self.store.load()['pending'])

    def test_accepted_issue_waits_for_complete_inventory_visibility(self):
        from unittest.mock import patch
        create=self.destination.create; scan=self.destination.inventory; remaining=[0]
        def delayed_create(projection,parent=None):
            locator=create(projection,parent)
            if projection.key.kind=='issue': remaining[0]=3
            return locator
        def delayed_scan(mapping):
            inventory=scan(mapping)
            if remaining[0]:
                remaining[0]-=1; inventory.issues=[]
            return inventory
        self.destination.create=delayed_create; self.destination.inventory=delayed_scan
        with patch.object(self.module.time,'sleep'):
            result=self.run_mirror()
        self.assertEqual([],result.errors)
        self.assertEqual(1,len(self.destination.data.issues))
        self.assertEqual('closed',self.destination.data.issues[0]['state'])
        self.assertEqual({},self.store.load()['pending'])

    def test_accepted_issue_invisible_beyond_bound_preserves_intent(self):
        from unittest.mock import patch
        scan=self.destination.inventory; scans=[0]
        def invisible(mapping):
            inventory=scan(mapping)
            if self.destination.data.issues:
                scans[0]+=1; inventory.issues=[]
            return inventory
        self.destination.inventory=invisible
        with patch.object(self.module.time,'sleep'):
            result=self.run_mirror()
        self.assertEqual(4,scans[0])
        self.assertIn('ownership_conflict',result.errors)
        self.assertEqual(1,len(self.destination.data.issues))
        self.assertTrue(self.store.load()['pending'])
    def test_budget_makes_progress_and_unmarked_human_history_survives(self):
        self.destination.data.issues.append({'id':90,'number':90,'body':'Human history','user':{'id':99}})
        for _ in range(8): self.run_mirror(1)
        self.assertEqual(2,len(self.destination.data.issues)); self.assertEqual('Human history',self.destination.data.issues[0]['body'])
        self.assertEqual(0,self.run_mirror(1).writes)
    def test_duplicate_markers_actor_changes_and_missing_marker_stop_mapping(self):
        self.run_mirror(); row=self.destination.data.issues[0]
        self.destination.data.issues.append(copy.deepcopy(row))
        self.assertIn('ownership_conflict',self.run_mirror().errors)
        self.destination.data.issues.pop(); row['user']['id']=77
        self.assertIn('ownership_conflict',self.run_mirror().errors)
        row['user']['id']=11; row['body']='Removed marker'
        self.assertIn('ownership_conflict',self.run_mirror().errors)
    def test_unmarked_owner_issues_and_comments_survive_backfill_and_repeat(self):
        issue={'id':90,'number':90,'body':'Owner history','user':{'id':11}}
        comment={'id':91,'body':'Owner comment','user':{'id':11}}
        self.destination.data.issues.append(copy.deepcopy(issue))
        self.destination.data.comments[90]=[copy.deepcopy(comment)]
        self.assertEqual([],self.run_mirror().errors)
        self.assertEqual(issue,self.destination.data.issues[0])
        self.assertEqual([comment],self.destination.data.comments[90])
        self.destination.writes=[]
        self.assertEqual(0,self.run_mirror().writes)
        self.assertEqual([],self.destination.writes)
    def test_fresh_discovery_rejects_recognizable_shadow_with_removed_marker(self):
        self.run_mirror()
        row=self.destination.data.issues[0]
        row['body']=row['body'].split('\n<!-- forgejo-mirror:')[0]
        self.store.path.unlink(); self.store.bootstrap('initial','fresh')
        self.destination.writes=[]
        self.assertIn('ownership_conflict',self.run_mirror().errors)
        self.assertEqual([],self.destination.writes)
    def test_known_shadow_cannot_be_replaced_after_body_erasure_or_deletion(self):
        for kind in ('issue','comment'):
            with self.subTest(kind=kind):
                self.run_mirror()
                row=(self.destination.data.issues[0] if kind=='issue'
                     else self.destination.data.comments[103][0])
                original=row['body']; row['body']='Human-looking replacement'
                self.destination.writes=[]
                self.assertIn('ownership_conflict',self.run_mirror().errors)
                self.assertEqual([],self.destination.writes)
                row['body']=original
        self.destination.data.issues=[]; self.destination.writes=[]
        self.assertIn('ownership_conflict',self.run_mirror().errors)
        self.assertEqual([],self.destination.writes)
    def test_pr_with_marker_like_body_is_excluded(self):
        self.source.data.issues.append({'id':99,'number':99,'pull_request':{}})
        self.destination.data.issues.append({'id':98,'pull_request':{},'user':{'id':11},'body':'junk'})
        self.assertEqual([],self.run_mirror().errors)
        self.assertEqual(2,len(self.destination.data.issues))
    def test_rejected_requests_consume_the_write_budget(self):
        from forgejo_metadata.api import APIError
        calls=[]
        def reject(projection,parent=None):
            calls.append(projection.key); raise APIError('http_422','not_created')
        self.destination.create=reject
        result=self.module.reconcile(self.mapping,self.source,self.destination,self.store,1)
        self.assertEqual(1,len(calls)); self.assertEqual(1,result.writes)
    def test_created_marker_with_wrong_actor_retains_uncertain_intent(self):
        original=self.destination.create
        def create(projection,parent=None):
            locator=original(projection,parent)
            if projection.key.kind=='issue': self.destination.rows[locator]['user']={'id':999}
            return locator
        self.destination.create=create
        result=self.run_mirror()
        self.assertIn('ownership_conflict',result.errors)
        self.assertTrue(self.store.load()['pending'])
    def test_runtime_deadline_stops_remaining_objects(self):
        calls=[]
        def timeout(projection,parent=None):
            calls.append(projection.key); raise MirrorError('run_timeout')
        self.destination.create=timeout
        result=self.run_mirror()
        self.assertEqual(1,len(calls)); self.assertIn('run_timeout',result.errors)
    def test_eligibility_changed_during_create_discovery_prevents_post(self):
        eligible=[True]; original=self.destination.inventory; count=[0]
        def scan(mapping):
            count[0]+=1
            if count[0]==2: eligible[0]=False
            return original(mapping)
        def preflight(mapping):
            if not eligible[0]: raise MirrorError('destination_enrollment')
        self.destination.inventory=scan; self.destination.preflight=preflight
        result=self.run_mirror()
        self.assertIn('destination_enrollment',result.errors); self.assertEqual([],self.destination.writes)
        self.assertEqual({},self.store.load()['pending'])
    def test_eligibility_changed_during_update_discovery_prevents_patch(self):
        self.run_mirror(); self.destination.writes=[]; self.source.data.labels[0]['color']='000000'
        eligible=[True]; original=self.destination.inventory; count=[0]
        def scan(mapping):
            count[0]+=1
            if count[0]==3: eligible[0]=False
            return original(mapping)
        def preflight(mapping):
            if not eligible[0]: raise MirrorError('destination_enrollment')
        self.destination.inventory=scan; self.destination.preflight=preflight
        result=self.run_mirror()
        self.assertIn('destination_enrollment',result.errors); self.assertEqual([],self.destination.writes)

    def test_changed_parent_identity_during_discovery_prevents_comment_post(self):
        self.run_mirror(); self.destination.writes=[]
        self.destination.data.comments[103]=[]
        del self.destination.rows['/issues/comments/104']
        original=self.destination.inventory; scans=[0]
        def scan(mapping):
            scans[0]+=1
            if scans[0]==2:
                issue=self.destination.data.issues[0]
                issue['body']=issue['body'].replace('kind=issue;id=23', 'kind=issue;id=99')
            return original(mapping)
        self.destination.inventory=scan
        result=self.run_mirror()
        self.assertIn('ownership_conflict',result.errors)
        self.assertEqual([],self.destination.writes)
        self.assertEqual({},self.store.load()['pending'])
        self.assertIsNone(result.converged_at)

    def test_replaced_comment_is_checked_against_fresh_parent_before_patch(self):
        for replace_at in (2,3):
            with self.subTest(replace_at=replace_at):
                self.source=FakeSource(); self.destination=FakeDestination()
                self.run_mirror(); self.destination.writes=[]
                self.source.data.comments[23][0]['body']='Edited source comment'
                original=self.destination.inventory; scans=[0]
                def scan(mapping):
                    scans[0]+=1
                    if scans[0]==replace_at:
                        comment=self.destination.data.comments[103].pop()
                        del self.destination.rows['/issues/comments/104']
                        replacement=dict(comment,id=999)
                        self.destination.data.issues.append({'id':900,'number':900,'body':'Human issue','user':{'id':77}})
                        self.destination.data.comments[900]=[replacement]
                        self.destination.rows['/issues/comments/999']=replacement
                    return original(mapping)
                self.destination.inventory=scan
                result=self.run_mirror()
                self.assertIn('ownership_conflict',result.errors)
                self.assertEqual([],self.destination.writes)
                self.assertIsNone(result.converged_at)

    def test_valid_comment_replacement_is_read_back_at_its_current_locator(self):
        self.run_mirror(); self.destination.writes=[]
        self.source.data.comments[23][0]['body']='Edited source comment'
        original=self.destination.inventory; scans=[0]
        def scan(mapping):
            scans[0]+=1
            if scans[0]==3:
                comment=self.destination.data.comments[103].pop()
                del self.destination.rows['/issues/comments/104']
                replacement=dict(comment,id=999)
                self.destination.data.comments[103]=[replacement]
                self.destination.rows['/issues/comments/999']=replacement
            return original(mapping)
        self.destination.inventory=scan
        result=self.run_mirror()
        self.assertEqual([],result.errors)
        self.assertEqual([('PATCH','/issues/comments/999')],self.destination.writes)
        self.assertIn('Edited source comment',self.destination.rows['/issues/comments/999']['body'])
        self.assertIsNotNone(result.converged_at)
