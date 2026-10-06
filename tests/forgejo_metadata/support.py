"""Independent synthetic metadata records; never production inputs."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'roles/forgejo_metadata/files'))

CONFIG = {'version': 1, 'mappings': [{
    'instance': 'fixture', 'source_origin': 'https://forgejo.example.test',
    'source_repo': 'example/project', 'source_id': 7,
    'destination_origin': 'https://api.github.com',
    'destination_repo': 'example/recovery', 'destination_id': 9,
    'visibility': 'public', 'actor_id': 11,
    'source_credential': 'source', 'destination_credential': 'destination',
}]}
ISSUE = {'id': 23, 'number': 5, 'title': 'Example issue', 'body': 'Original text',
         'state': 'closed', 'labels': [], 'milestone': None,
         'user': {'login': 'original'}, 'created_at': '2026-01-01T00:00:00Z',
         'html_url': 'https://forgejo.example.test/example/project/issues/5'}

import copy
from urllib.parse import quote
from forgejo_metadata.model import Inventory

class FakeSource:
    def __init__(self):
        self.data=Inventory([copy.deepcopy(ISSUE)], {23:[{'id':24,'body':'Original comment',
            'user':{'login':'commenter'},'created_at':'2026-01-02T00:00:00Z'}]},
            [{'id':42,'name':'bug','color':'abcdef','description':'Defect'}],
            [{'id':43,'title':'First milestone','description':'Delivery','state':'closed','due_on':None}])
        self.data.issues[0]['labels']=copy.deepcopy(self.data.labels)
        self.data.issues[0]['milestone']={'id':43}
    def inventory(self,mapping): return copy.deepcopy(self.data)

class FakeDestination:
    def __init__(self):
        self.data=Inventory(); self.rows={}; self.writes=[]; self.count=100
        self.drop=None; self.failure=None; self.lost=False; self.preflights=0
    def preflight(self,mapping): self.preflights+=1
    def inventory(self,mapping): return copy.deepcopy(self.data)
    def create(self,projection,parent=None):
        from forgejo_metadata.api import APIError
        if self.failure: raise APIError(self.failure,'not_created')
        self.count+=1; kind=projection.key.kind
        fields=copy.deepcopy(projection.fields)
        if self.drop: fields.pop(self.drop,None)
        if kind=='issue': fields['state']='open'
        row=dict(fields,id=self.count,number=self.count,user={'id':11})
        if kind=='label': locator='/labels/'+quote(row['name'],safe='')
        elif kind=='comment': locator='/issues/comments/'+str(self.count)
        else: locator='/'+('issues' if kind=='issue' else 'milestones')+'/'+str(self.count)
        self.rows[locator]=row
        if kind=='comment': self.data.comments.setdefault(parent.fields['id'],[]).append(row)
        else: getattr(self.data,{'issue':'issues','label':'labels','milestone':'milestones'}[kind]).append(row)
        self.writes.append(('POST',locator))
        if self.lost: self.lost=False; raise APIError('transport_failure')
        return locator
    def update(self,target,projection):
        fields=copy.deepcopy(projection.fields)
        if self.drop: fields.pop(self.drop,None)
        row=self.rows[target.locator]; row.update(fields)
        if projection.key.kind=='label':
            new='/labels/'+quote(row['name'],safe=''); self.rows[new]=row
            if new!=target.locator: del self.rows[target.locator]
        self.writes.append(('PATCH',target.locator))
    def read(self,locator): return copy.deepcopy(self.rows[locator])
