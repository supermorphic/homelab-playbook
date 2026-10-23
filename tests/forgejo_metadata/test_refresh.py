"""Changed-object destination refresh against a synthetic GitHub collection model."""
import json
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from support import CONFIG, ISSUE
from forgejo_metadata.api import DestinationAPI
from forgejo_metadata.identity import render_projection
from forgejo_metadata.model import load_config, MirrorError
from forgejo_metadata.reconcile import discover_owned

PARENT='https://api.github.com/repos/example/recovery/issues/'
OLD='2026-01-01T00:00:00Z'

class GitHub:
    """Paginated issue and comment collections that honor since like GitHub."""
    def __init__(self,size=0):
        self.issues=[{'id':i,'number':i,'updated_at':OLD} for i in range(1,size+1)]
        self.comments=[{'id':100000+i,'issue_url':PARENT+str(i),'updated_at':OLD} for i in range(1,size+1)]
        self.date='Fri, 23 Oct 2026 03:00:00 GMT'; self.now='2026-10-23T03:00:00Z'
        self.urls=[]; self.fail=False
    def transport(self,method,url,headers,payload):
        self.urls.append(url)
        if self.fail: return 500,b'{}',{}
        parsed=urlsplit(url); query=parse_qs(parsed.query)
        rows={'issues':self.issues,'comments':self.comments}.get(parsed.path.rsplit('/',1)[-1],[])
        since=query.get('since',[''])[0]
        rows=[row for row in rows if row['updated_at']>=since]
        page=int(query.get('page',['1'])[0]); headers={'Date':self.date}
        if page*100<len(rows):
            following=urlencode(dict(query,page=[str(page+1)]),doseq=True)
            headers['Link']=f'<https://api.github.com{parsed.path}?{following}>; rel="next"'
        return 200,json.dumps(rows[(page-1)*100:page*100]).encode(),headers
    def issue(self,**fields):
        row=dict({'id':len(self.issues)+1,'number':len(self.issues)+1},**fields,updated_at=self.now)
        self.issues=[r for r in self.issues if r['id']!=row['id']]+[row]; return row
    def comment(self,parent,**fields):
        row=dict({'id':200000+len(self.comments)},**fields,issue_url=PARENT+str(parent),updated_at=self.now)
        self.comments=[r for r in self.comments if r['id']!=row['id']]+[row]; return row

class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.mapping=load_config(CONFIG)[0]
    def client(self,github):
        return DestinationAPI(self.mapping,'synthetic-token',transport=github.transport,sleep=lambda _:None)

    def test_refresh_requests_do_not_depend_on_destination_size(self):
        costs={}
        for size in (50,1500):
            with self.subTest(size=size):
                github=GitHub(size); client=self.client(github)
                client.inventory(self.mapping); costs[size]=[]
                for _ in range(3):
                    parent=github.issue(); github.comment(parent['number'])
                    before=len(github.urls); inventory=client.refresh(self.mapping)
                    costs[size].append(len(github.urls)-before)
                    self.assertEqual(len(github.issues),len(inventory.issues))
                    self.assertEqual(len(github.comments),sum(len(rows) for rows in inventory.comments.values()))
        self.assertEqual([4,4,4],costs[50]); self.assertEqual(costs[50],costs[1500])

    def test_refresh_overlaps_previous_server_time_and_merges_by_id(self):
        github=GitHub(2); client=self.client(github); client.inventory(self.mapping)
        github.issue(id=1,number=1,title='Edited by another writer')
        github.comment(1,id=100001,body='Edited comment')
        github.date='Fri, 23 Oct 2026 03:10:00 GMT'
        before=len(github.urls); inventory=client.refresh(self.mapping)
        issues,comments=github.urls[before:before+2]
        for url in (issues,comments):
            self.assertEqual(['2026-10-23T02:55:00Z'],parse_qs(urlsplit(url).query)['since'])
        self.assertTrue(all('since' not in url for url in github.urls[before+2:]))
        self.assertEqual([1,2],[row['id'] for row in inventory.issues])
        self.assertEqual('Edited by another writer',inventory.issues[0]['title'])
        self.assertEqual(['Edited comment'],[row.get('body') for row in inventory.comments[1]])
        before=len(github.urls); client.refresh(self.mapping)
        self.assertEqual(['2026-10-23T03:05:00Z'],parse_qs(urlsplit(github.urls[before]).query)['since'])

    def test_comment_with_unknown_parent_forces_complete_read(self):
        github=GitHub(1); client=self.client(github); client.inventory(self.mapping)
        github.issues.append({'id':2,'number':2,'updated_at':OLD})
        github.comment(2,id=300)
        before=len(github.urls); inventory=client.refresh(self.mapping)
        self.assertTrue(all('since' not in url for url in github.urls[before+2:]))
        self.assertEqual([300],[row['id'] for row in inventory.comments[2]])
        github.comment(99,id=301)
        with self.assertRaisesRegex(MirrorError,'incomplete_inventory'): client.refresh(self.mapping)

    def test_missing_server_time_or_failed_refresh_never_narrows_later_reads(self):
        github=GitHub(1); github.date='not a date'; client=self.client(github)
        client.inventory(self.mapping); before=len(github.urls); client.refresh(self.mapping)
        self.assertTrue(all('since' not in url for url in github.urls[before:]))
        github.date='Fri, 23 Oct 2026 03:00:00 GMT'; client.inventory(self.mapping)
        github.issue(id=2,number=2); github.fail=True
        with self.assertRaises(MirrorError): client.refresh(self.mapping)
        github.fail=False; github.date='Fri, 23 Oct 2026 04:00:00 GMT'
        before=len(github.urls); inventory=client.refresh(self.mapping)
        self.assertEqual(['2026-10-23T02:55:00Z'],parse_qs(urlsplit(github.urls[before]).query)['since'])
        self.assertEqual([1,2],[row['id'] for row in inventory.issues])

    def test_owned_copies_created_or_changed_by_another_writer_between_reads_stop_mapping(self):
        body=render_projection(self.mapping,'issue',ISSUE).fields['body']
        for change in ('duplicate','marker_removed'):
            with self.subTest(change=change):
                github=GitHub(); github.issue(body=body,user={'id':11}); client=self.client(github)
                self.assertEqual(1,len(discover_owned(self.mapping,client.inventory(self.mapping))))
                if change=='duplicate': github.issue(body=body,user={'id':11})
                else: github.issue(id=1,number=1,body=body.split('\n<!-- forgejo-mirror:')[0],user={'id':11})
                with self.assertRaisesRegex(MirrorError,'ownership_conflict'):
                    discover_owned(self.mapping,client.refresh(self.mapping))
