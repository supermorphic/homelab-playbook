"""Bounded HTTPS API access with read-only source and metadata-only destination."""
import json
import re
import ssl
import time
from urllib.parse import urlencode, urlsplit, quote, unquote
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from .model import MirrorError, Inventory

MAX_RESPONSE=8*1024*1024
MAX_PAGES=1000
class APIError(MirrorError):
    def __init__(self, code, outcome='unknown', retry_at=0, retry_key='*'):
        self.outcome=outcome; self.retry_at=retry_at; self.retry_key=retry_key
        super().__init__(code)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise APIError('redirect_refused')

class Client:
    def __init__(self,mapping,token,*,transport=None,sleep=time.sleep,clock=time.time):
        self.mapping=mapping; self._token=token; self.sleep=sleep; self.clock=clock
        self.next_write=0; self.retry_at=0
        self.retry_key=mapping.source_credential if self.source else mapping.destination_credential
        self.origin=mapping.source_origin if self.source else mapping.destination_origin
        self.prefix=('/api/v1' if self.source else '')+'/repos/'+(mapping.source_repo if self.source else mapping.destination_repo)
        context=ssl.create_default_context()
        if self.source and mapping.ca_file: context.load_verify_locations(cafile=mapping.ca_file)
        opener=build_opener(NoRedirect(),HTTPSHandler(context=context))
        def https(method,url,headers,payload):
            data=None if payload is None else json.dumps(payload).encode()
            req=Request(url,data=data,headers=headers,method=method)
            try:
                with opener.open(req,timeout=20) as response:
                    return response.status,response.read(MAX_RESPONSE+1),dict(response.headers)
            except HTTPError as error:
                return error.code,error.read(MAX_RESPONSE+1),dict(error.headers)
            except (URLError,OSError,TimeoutError): raise APIError('transport_failure') from None
        self.transport=transport or https

    def request(self,method,path,payload=None):
        method=method.upper()
        if (self.source and method!='GET') or method not in ('GET','POST','PATCH'):
            raise APIError('method_refused','not_created')
        if path=='@user' and not self.source and method=='GET': url=self.origin+'/user'
        else:
            parsed=urlsplit(path)
            if parsed.scheme:
                if parsed.scheme!='https' or parsed.netloc!=urlsplit(self.origin).netloc or parsed.username or parsed.fragment:
                    raise APIError('pagination_boundary','not_created')
                url=path; suffix=parsed.path.removeprefix(self.prefix)
                if not parsed.path.startswith(self.prefix+'/'): raise APIError('pagination_boundary','not_created')
            else:
                if not path.startswith('/') and path!='': raise APIError('endpoint_refused','not_created')
                suffix=parsed.path; url=self.origin+self.prefix+path
            if not re.fullmatch(r'(?:|/(?:issues(?:/[0-9]+(?:/comments)?)?|issues/comments(?:/[0-9]+)?|labels(?:/[^/]+)?|milestones(?:/[0-9]+)?))',suffix):
                raise APIError('endpoint_refused','not_created')
            label=re.fullmatch(r'/labels/([^/]+)',suffix)
            if label:
                try: name=unquote(label[1],errors='strict')
                except UnicodeDecodeError: raise APIError('endpoint_refused','not_created') from None
                if name in ('.','..') or '\x00' in name or quote(name,safe='')!=label[1]:
                    raise APIError('endpoint_refused','not_created')
            elif '..' in suffix or '%2f' in suffix.lower(): raise APIError('endpoint_refused','not_created')
            if method!='GET' and suffix=='': raise APIError('endpoint_refused','not_created')
        if self.clock()<self.retry_at: raise APIError('rate_limited','not_created',self.retry_at,self.retry_key)
        if method!='GET':
            self.sleep(max(0,self.next_write-self.clock())); self.next_write=self.clock()+1
        headers={'Authorization':('token ' if self.source else 'Bearer ')+self._token,
                 'Accept':'application/json' if self.source else 'application/vnd.github+json',
                 'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'}
        status,body,received=self.transport(method,url,headers,payload)
        received={k.lower():v for k,v in received.items()}
        if len(body)>MAX_RESPONSE: raise APIError('response_limit')
        try: value=json.loads(body)
        except (ValueError,UnicodeDecodeError): raise APIError('invalid_response') from None
        if status<200 or status>=300:
            delay=received.get('retry-after')
            retry=0
            try:
                if delay: retry=self.clock()+max(1,int(delay))
                elif received.get('x-ratelimit-remaining')=='0': retry=float(received['x-ratelimit-reset'])
                elif status in (403,429): retry=self.clock()+60
            except (ValueError,KeyError): retry=self.clock()+60
            self.retry_at=max(self.retry_at,retry)
            outcome='not_created' if status in (400,401,403,404,410,422,429) and isinstance(value,dict) else 'unknown'
            raise APIError(f'http_{status}',outcome,self.retry_at,self.retry_key)
        if not isinstance(value,(dict,list)): raise APIError('invalid_response')
        return value,received

    def pages(self,path):
        result=[]; visited=set(); seen=set(); page=1
        source_path=path
        while path is not None:
            canonical=path if path.startswith('https://') else self.origin+self.prefix+path
            if canonical in visited or len(visited)>=MAX_PAGES: raise APIError('pagination_cycle_or_limit')
            visited.add(canonical); rows,headers=self.request('GET',path)
            if not isinstance(rows,list): raise APIError('invalid_collection')
            for row in rows:
                if not isinstance(row,dict) or row.get('id') in seen: raise APIError('invalid_or_duplicate_page')
                seen.add(row.get('id')); result.append(row)
            link=headers.get('link','')
            match=re.search(r'<([^>]+)>;\s*rel="next"',link)
            if 'rel="next"' in link and not match: raise APIError('invalid_pagination')
            path=match[1] if match else None
            if self.source and path is None and len(rows)==50 and not link:
                page+=1; path=source_path+('&' if '?' in source_path else '?')+f'page={page}'
        return result

class SourceAPI(Client):
    source=True
    def inventory(self,mapping):
        repo,_=self.request('GET','')
        if not isinstance(repo,dict): raise APIError('invalid_response')
        if repo.get('id')!=mapping.source_id or repo.get('full_name')!=mapping.source_repo: raise APIError('source_identity')
        issues=[r for r in self.pages('/issues?type=issues&state=all&sort=oldest&limit=50') if r.get('pull_request') is None]
        comments={}
        for issue in issues:
            rows,_=self.request('GET',f"/issues/{issue['number']}/comments")
            if not isinstance(rows,list): raise APIError('invalid_collection')
            comments[issue['id']]=[r for r in rows if not r.get('pull_request_url')]
        return Inventory(issues,comments,self.pages('/labels?limit=50'),self.pages('/milestones?state=all&limit=50'))

class DestinationAPI(Client):
    source=False
    def preflight(self,mapping):
        repo,_=self.request('GET','')
        if not isinstance(repo,dict): raise APIError('invalid_response')
        if (repo.get('id')!=mapping.destination_id or repo.get('full_name','').lower()!=mapping.destination_repo.lower()
            or repo.get('visibility')!=mapping.visibility or not repo.get('permissions',{}).get('push')):
            raise APIError('destination_enrollment')
        actor,_=self.request('GET','@user')
        if not isinstance(actor,dict): raise APIError('invalid_response')
        if actor.get('id')!=mapping.actor_id: raise APIError('destination_actor')
    def inventory(self,mapping):
        issues=[r for r in self.pages('/issues?state=all&sort=created&direction=asc&per_page=100') if 'pull_request' not in r]
        comments={r['id']:self.pages(f"/issues/{r['number']}/comments?per_page=100") for r in issues}
        return Inventory(issues,comments,self.pages('/labels?per_page=100'),self.pages('/milestones?state=all&per_page=100'))
    def create(self,projection,parent=None):
        kind=projection.key.kind
        endpoint=f'/issues/{parent.fields["number"]}/comments' if kind=='comment' else '/'+{'issue':'issues','label':'labels','milestone':'milestones'}[kind]
        fields=dict(projection.fields)
        if kind=='issue': fields.pop('state',None)
        if kind=='milestone' and fields.get('due_on') is None: fields.pop('due_on',None)
        value,_=self.request('POST',endpoint,fields)
        if kind=='label': return '/labels/'+quote(value['name'],safe='')
        if kind=='comment': return '/issues/comments/'+str(value['id'])
        return endpoint+'/'+str(value['number'])
    def update(self,target,projection):
        fields=dict(projection.fields)
        if projection.key.kind=='label': fields['new_name']=fields.pop('name')
        self.request('PATCH',target.locator,fields)
    def read(self,locator): return self.request('GET',locator)[0]
