#!/usr/bin/env python3
"""Owned, loopback-only HTTPS doubles. All identities and tokens are synthetic."""
import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote,urlsplit,parse_qs,urlencode

SOURCE={
    'issues':[{'id':23,'number':5,'title':'Example issue','body':'Original text','state':'closed',
        'user':{'login':'original'},'created_at':'2026-01-01T00:00:00Z','html_url':'https://forgejo.example.test/example/project/issues/5',
        'labels':[{'id':42,'name':'bug'}],'milestone':{'id':43}}],
    'comments':[{'id':24,'body':'Original comment','user':{'login':'commenter'},'created_at':'2026-01-01T01:00:00Z'}],
    'labels':[{'id':42,'name':'bug','color':'abcdef','description':'Defect'}],
    'milestones':[{'id':43,'number':1,'title':'First milestone','description':'Delivery','state':'closed','due_on':None}],
}


class Fixture:
    def __init__(self,root=None,source_port=0,destination_port=0):
        self.temporary=None; self.root=Path(root) if root else None
        self.ports=[source_port,destination_port]; self.servers=[]; self.threads=[]
        self.source_data=copy.deepcopy(SOURCE); self.data={'issues':[],'comments':[],'labels':[],'milestones':[]}
        self.source_ledger=[]; self.ledger=[]; self.mode={}; self.lock=threading.Lock()
    def __enter__(self):
        if self.root is None:
            self.temporary=tempfile.TemporaryDirectory(prefix='forgejo-metadata-fixture-'); self.root=Path(self.temporary.name)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if not (self.root/'ca.pem').exists():
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=localhost',
                '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1','-keyout',str(self.root/'key.pem'),'-out',str(self.root/'ca.pem')],
                check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
            (self.root/'key.pem').chmod(0o600); (self.root/'ca.pem').chmod(0o644)
        fixture=self
        for source,port in zip((True,False),self.ports):
            class Handler(BaseHTTPRequestHandler):
                is_source=source
                def log_message(self,*args): pass
                def do_GET(self): self.handle_api('GET')
                def do_POST(self): self.handle_api('POST')
                def do_PATCH(self): self.handle_api('PATCH')
                def collection(self,rows):
                    parsed=urlsplit(self.path); query=parse_qs(parsed.query)
                    size=int(query.get('limit',query.get('per_page',['100']))[0]); page=int(query.get('page',['1'])[0])
                    size=max(1,min(size,100)); page=max(1,page); links=[]
                    for other,relation in ((page+1,'next'),(page-1,'prev')):
                        if relation=='next' and page*size>=len(rows) or relation=='prev' and page==1: continue
                        updated=dict(query,page=[str(other)])
                        url=f'https://127.0.0.1:{self.server.server_address[1]}{parsed.path}?'+urlencode(updated,doseq=True)
                        links.append(f'<{url}>; rel="{relation}"')
                    headers={'Link':', '.join(links)} if links else None
                    self.reply(200,copy.deepcopy(rows[(page-1)*size:page*size]),headers)
                def reply(self,status,value,headers=None):
                    payload=json.dumps(value).encode(); self.send_response(status)
                    self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(payload)))
                    for key,value in (headers or {}).items(): self.send_header(key,value)
                    self.end_headers()
                    try: self.wfile.write(payload)
                    except (OSError,ssl.SSLError): pass
                def handle_api(self,method):
                    mode=fixture.mode
                    if (fixture.root/'mode.json').exists(): mode=json.loads((fixture.root/'mode.json').read_text())
                    expected=('token ' if self.is_source else 'Bearer ')+'synthetic-fixture-token'
                    if self.headers.get('Authorization')!=expected: self.reply(401,{}); return
                    path=unquote(urlsplit(self.path).path)
                    ledger=fixture.source_ledger if self.is_source else fixture.ledger
                    with fixture.lock: ledger.append((method,path))
                    if mode.get('delay'): time.sleep(mode['delay'])
                    if mode.get('redirect'): self.reply(302,{}, {'Location':'https://foreign.example.test/issues'}); return
                    if mode.get('rate_limit'): self.reply(429,{}, {'Retry-After':'60'}); return
                    prefix='/api/v1/repos/example/project' if self.is_source else '/repos/example/recovery'
                    if path=='/user' and not self.is_source: self.reply(200,{'id':11}); return
                    if not path.startswith(prefix): self.reply(404,{}); return
                    suffix=path.removeprefix(prefix)
                    if suffix=='':
                        self.reply(200,{'id':7 if self.is_source else 9,'full_name':'example/project' if self.is_source else 'example/recovery','visibility':'public','permissions':{'push':True}}); return
                    if self.is_source:
                        if method!='GET': self.reply(405,{}); return
                        kind='comments' if suffix.endswith('/comments') else suffix.strip('/')
                        rows=fixture.source_data.get(kind,[])
                        if kind=='comments': self.reply(200,copy.deepcopy(rows))
                        else: self.collection(rows)
                        return
                    parts=suffix.strip('/').split('/')
                    kind='comments' if parts[:2]==['issues','comments'] or len(parts)==3 and parts[-1]=='comments' else parts[0]
                    if kind not in fixture.data: self.reply(404,{}); return
                    with fixture.lock:
                        rows=fixture.data[kind]
                        if method=='GET':
                            if len(parts)==1: self.collection(rows); return
                            elif kind=='comments' and len(parts)==3 and parts[-1]=='comments': self.collection([row for row in rows if row['_parent']==int(parts[1])]); return
                            else:
                                ident=suffix.removeprefix('/labels/') if kind=='labels' else parts[-1]; value=next((copy.deepcopy(row) for row in rows if str(row.get('name') if kind=='labels' else row['id'] if kind=='comments' else row['number'])==ident),None)
                            self.reply(200 if value is not None else 404,value or ([] if isinstance(value,list) else {})); return
                        if mode.get('reject'): self.reply(422,{'message':'synthetic rejection'}); return
                        length=int(self.headers.get('Content-Length','0'))
                        if length>65536: self.reply(413,{}); return
                        fields=json.loads(self.rfile.read(length))
                        if kind=='labels' and isinstance(fields.get('description'),str):
                            # GitHub normalizes label descriptions to a single line.
                            fields['description']=' '.join(fields['description'].split())
                        if mode.get('drop'): fields.pop(mode['drop'],None)
                        if method=='POST':
                            number=101+sum(len(value) for value in fixture.data.values())
                            row=dict(fields,id=number,number=number,user={'id':11})
                            if kind=='issues': row['state']='open'
                            if kind=='comments': row['_parent']=int(parts[1])
                            rows.append(row)
                        else:
                            ident=suffix.removeprefix('/labels/') if kind=='labels' else parts[-1]; row=next((row for row in rows if str(row.get('name') if kind=='labels' else row['id'] if kind=='comments' else row['number'])==ident),None)
                            if row is None: self.reply(404,{}); return
                            if 'new_name' in fields: fields['name']=fields.pop('new_name')
                            row.update(fields)
                        (fixture.root/'destination.json').write_text(json.dumps(fixture.data))
                        value=copy.deepcopy(row)
                    if method=='POST' and mode.get('hang_after_post'):
                        # The committed record is observable while this request remains uncertain.
                        for _ in range(200):
                            if not fixture.mode.get('hang_after_post'): break
                            time.sleep(.05)
                        try: self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError: pass
                        self.connection.close(); return
                    if method=='POST' and mode.get('disconnect'):
                        self.connection.close(); return
                    self.reply(201 if method=='POST' else 200,value)
            server=ThreadingHTTPServer(('127.0.0.1',port),Handler); server.daemon_threads=True
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(self.root/'ca.pem',self.root/'key.pem')
            server.socket=context.wrap_socket(server.socket,server_side=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            self.servers.append(server); self.threads.append(thread)
        self.source_origin=f'https://127.0.0.1:{self.servers[0].server_port}'
        self.destination_origin=f'https://127.0.0.1:{self.servers[1].server_port}'
        return self
    def snapshot(self):
        with self.lock: return copy.deepcopy(self.data)
    def writes(self):
        with self.lock: return [row for row in self.ledger if row[0]!='GET']
    def __exit__(self,*args):
        self.mode={}
        for server in self.servers: server.shutdown(); server.server_close()
        for thread in self.threads: thread.join(timeout=2)
        if self.temporary: self.temporary.cleanup()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['serve'])
    parser.add_argument('--root',required=True); parser.add_argument('--source-port',type=int,default=18443); parser.add_argument('--destination-port',type=int,default=18444)
    args=parser.parse_args()
    with Fixture(args.root,args.source_port,args.destination_port):
        while True: time.sleep(1)

if __name__=='__main__': main()
