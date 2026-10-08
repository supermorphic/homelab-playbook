import importlib
import json
import unittest
from support import CONFIG
from forgejo_metadata.model import load_config, MirrorError

class APITests(unittest.TestCase):
    def setUp(self):
        try: self.api=importlib.import_module('forgejo_metadata.api')
        except ModuleNotFoundError: self.fail('metadata API boundary is missing')
        self.mapping=load_config(CONFIG)[0]; self.calls=[]

    def client(self, responses, source=False):
        def transport(method,url,headers,payload):
            self.calls.append((method,url,headers,payload))
            status,value,extra=responses.pop(0)
            return status,json.dumps(value).encode(),extra
        cls=self.api.SourceAPI if source else self.api.DestinationAPI
        return cls(self.mapping, 'synthetic-token', transport=transport, sleep=lambda _:None)

    def test_method_boundary_rejects_source_writes_and_destination_git(self):
        for client,method,path in [(self.client([],True),'POST','/issues'),
                                   (self.client([]),'POST','/git/refs')]:
            with self.assertRaises(MirrorError): client.request(method,path)
        self.assertEqual([],self.calls)

    def test_foreign_pagination_and_cycle_fail_closed(self):
        for next_url in ('https://evil.example/repos/example/recovery/issues',
                         'https://api.github.com/repos/example/recovery/issues'):
            client=self.client([(200,[],{'Link':f'<{next_url}>; rel="next"'})])
            with self.assertRaises(MirrorError): client.pages('/issues')
            self.assertEqual(1,len(self.calls)); self.calls=[]

    def test_github_pagination_uses_pinned_numeric_repository_paths(self):
        for endpoint in ('issues','issues/comments','labels','milestones'):
            with self.subTest(endpoint=endpoint):
                self.calls=[]
                next_url=f'https://api.github.com/repositories/9/{endpoint}?page=2'
                client=self.client([(200,[{'id':1}],{'Link':f'<{next_url}>; rel="next"'}),
                                    (200,[{'id':2}],{})])
                self.assertEqual([{'id':1},{'id':2}],client.pages('/'+endpoint))
                self.assertEqual(['GET','GET'],[call[0] for call in self.calls])
                self.assertEqual(next_url,self.calls[1][1])

    def test_numeric_pagination_rejects_other_repository_and_origin(self):
        for next_url in ('https://api.github.com/repositories/10/issues?page=2',
                         'https://api.github.com/repositories/90/issues?page=2',
                         'https://foreign.example/repositories/9/issues?page=2',
                         'https://synthetic-user@api.github.com/repositories/9/issues?page=2',
                         'http://api.github.com/repositories/9/issues?page=2',
                         'https://api.github.com/repositories/9/git/refs?page=2',
                         'https://api.github.com/repositories/9/issues?page=2#fragment'):
            with self.subTest(next_url=next_url):
                self.calls=[]
                client=self.client([(200,[{'id':1}],{'Link':f'<{next_url}>; rel="next"'})])
                with self.assertRaises(MirrorError): client.pages('/issues')
                self.assertEqual(1,len(self.calls))

    def test_numeric_repository_paths_allow_only_destination_reads(self):
        for source,method,path in (
            (False,'PATCH','https://api.github.com/repositories/9/issues/1'),
            (False,'POST','https://api.github.com/repositories/9/issues'),
            (True,'GET','https://forgejo.example.test/api/v1/repositories/7/issues')):
            with self.subTest(source=source,method=method):
                self.calls=[]
                with self.assertRaises(MirrorError): self.client([],source).request(method,path)
                self.assertEqual([],self.calls)

    def test_permissions_and_pinned_identity_are_checked(self):
        repo={'id':9,'visibility':'public','full_name':'example/recovery','permissions':{'push':True}}
        self.client([(200,repo,{}),(200,{'id':11},{})]).preflight(self.mapping)
        for changes in ({'id':10},{'visibility':'private'},{'permissions':{'push':False}}):
            with self.assertRaises(MirrorError):
                self.client([(200,dict(repo,**changes),{})]).preflight(self.mapping)

    def test_milestone_create_without_deadline_respects_github_date_schema(self):
        from forgejo_metadata.identity import render_projection
        def transport(method,url,headers,payload):
            # GitHub's create schema permits an optional date-time string, not null.
            if 'due_on' in payload and not isinstance(payload['due_on'],str):
                return 422,json.dumps({'message':'invalid due_on'}).encode(),{}
            return 201,json.dumps(dict(payload,id=101,number=1)).encode(),{}
        client=self.api.DestinationAPI(self.mapping,'synthetic-token',transport=transport,sleep=lambda _:None)
        for due_on in (None,'2026-12-31T00:00:00Z'):
            projection=render_projection(self.mapping,'milestone',{'id':43,'title':'Example','description':'','state':'open','due_on':due_on})
            self.assertEqual('/milestones/1',client.create(projection))

    def test_rejection_and_uncertain_creation_have_distinct_outcomes(self):
        for status,expected in [(422,'not_created'),(503,'unknown'),(429,'not_created')]:
            with self.assertRaises(self.api.APIError) as raised:
                self.client([(status,{'message':'never-print-this'}, {'Retry-After':'20'})]).request('POST','/issues',{'title':'x'})
            self.assertEqual(expected,raised.exception.outcome)
            self.assertNotIn('never-print-this',str(raised.exception))

    def test_source_inventory_excludes_prs_and_reads_issue_comments(self):
        client=self.client([(200,{'id':7,'full_name':'example/project'},{}),
            (200,[{'id':1,'number':1,'pull_request':None},{'id':2,'number':2,'pull_request':{}}],{}),
            (200,[{'id':4,'body':'comment'}],{}),(200,[],{}),(200,[],{})],True)
        inventory=client.inventory(self.mapping)
        self.assertEqual([1],[r['id'] for r in inventory.issues])
        self.assertEqual([4],[r['id'] for r in inventory.comments[1]])
        self.assertTrue(any('type=issues' in c[1] for c in self.calls))
        self.assertFalse(any('/issues/2/comments' in c[1] for c in self.calls))

    def test_repository_comments_keep_issue_parents_and_exclude_pull_requests(self):
        parent='https://api.github.com/repos/example/recovery/issues/'
        next_page='https://api.github.com/repos/example/recovery/issues/comments?per_page=100&page=2'
        client=self.client([(200,[{'id':101,'number':5},{'id':102,'number':6,'pull_request':{}}],{}),
            (200,[{'id':201,'issue_url':parent+'5'}],{'Link':f'<{next_page}>; rel="next"'}),
            (200,[{'id':202,'issue_url':parent+'6'},{'id':203,'issue_url':parent.upper()+'5'}],{}),
            (200,[],{}),(200,[],{})])
        inventory=client.inventory(self.mapping)
        self.assertEqual([101],[row['id'] for row in inventory.issues])
        self.assertEqual([201,203],[row['id'] for row in inventory.comments[101]])
        self.assertFalse(any('/issues/5/comments' in call[1] or '/issues/6/comments' in call[1] for call in self.calls))

    def test_repository_comments_with_unknown_or_foreign_parents_fail_closed(self):
        for parent in (None,'https://api.github.com/repos/example/recovery/issues/99',
                       'https://foreign.example/repos/example/recovery/issues/5'):
            with self.subTest(parent=parent):
                client=self.client([(200,[{'id':101,'number':5}],{}),
                    (200,[{'id':201,'issue_url':parent}],{}),(200,[],{}),(200,[],{})])
                with self.assertRaises(MirrorError): client.inventory(self.mapping)

    def test_malformed_final_page_does_not_return_partial_inventory(self):
        client=self.client([(200,[{'id':1}],{'Link':'<https://api.github.com/repos/example/recovery/issues?page=2>; rel="next"'}),(500,{}, {})])
        with self.assertRaises(MirrorError): client.pages('/issues')
    def test_private_source_ca_does_not_replace_destination_system_trust(self):
        from dataclasses import replace
        from unittest.mock import patch,MagicMock
        mapping=replace(self.mapping,ca_file='/etc/fixture-source-ca.pem')
        source_context=MagicMock(); destination_context=MagicMock()
        with patch.object(self.api.ssl,'create_default_context',side_effect=[source_context,destination_context]) as contexts:
            self.api.SourceAPI(mapping,'synthetic-token',transport=lambda *args:None)
            self.api.DestinationAPI(mapping,'synthetic-token',transport=lambda *args:None)
        self.assertEqual([(),()], [call.args for call in contexts.call_args_list])
        self.assertEqual([{},{}], [call.kwargs for call in contexts.call_args_list])
        source_context.load_verify_locations.assert_called_once_with(cafile='/etc/fixture-source-ca.pem')
        destination_context.load_verify_locations.assert_not_called()
    def test_label_components_with_slash_and_dots_use_only_label_endpoints(self):
        from forgejo_metadata.identity import render_projection
        from forgejo_metadata.model import DestinationObject
        from urllib.parse import quote
        for name in ('area/ui','release..next','../issues/comments/1','unicode/🍃'):
            self.calls=[]
            projection=render_projection(self.mapping,'label',{'id':42,'name':name,'color':'abcdef','description':'Defect'})
            locator='/labels/'+quote(projection.fields['name'],safe='')
            target=DestinationObject(projection.key,locator,dict(projection.fields))
            client=self.client([(200,dict(projection.fields),{})])
            client.update(target,projection)
            self.assertEqual('PATCH',self.calls[0][0])
            self.assertTrue(self.calls[0][1].startswith('https://api.github.com/repos/example/recovery/labels/'))
            self.assertEqual(1,len(self.calls))
    def test_source_exact_full_last_page_stops_at_complete_links(self):
        next_url='https://forgejo.example.test/api/v1/repos/example/project/labels?limit=50&page=2'
        previous='https://forgejo.example.test/api/v1/repos/example/project/labels?limit=50&page=1'
        client=self.client([(200,[{'id':i} for i in range(1,51)],{'Link':f'<{next_url}>; rel="next"'}),
            (200,[{'id':i} for i in range(51,101)],{'Link':f'<{previous}>; rel="prev"'})],True)
        rows=client.pages('/labels?limit=50')
        self.assertEqual(list(range(1,101)),[row['id'] for row in rows]); self.assertEqual(2,len(self.calls))
