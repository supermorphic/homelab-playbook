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

    def test_permissions_and_pinned_identity_are_checked(self):
        repo={'id':9,'visibility':'public','full_name':'example/recovery','permissions':{'push':True}}
        self.client([(200,repo,{}),(200,{'id':11},{})]).preflight(self.mapping)
        for changes in ({'id':10},{'visibility':'private'},{'permissions':{'push':False}}):
            with self.assertRaises(MirrorError):
                self.client([(200,dict(repo,**changes),{})]).preflight(self.mapping)

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

    def test_malformed_final_page_does_not_return_partial_inventory(self):
        client=self.client([(200,[{'id':1}],{'Link':'<https://api.github.com/repos/example/recovery/issues?page=2>; rel="next"'}),(500,{}, {})])
        with self.assertRaises(MirrorError): client.pages('/issues')
