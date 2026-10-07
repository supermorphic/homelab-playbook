"""Attended, bounded metadata acceptance using owned source fixtures.

Requires separate operator authority for the exact repositories and host.
Runtime secrets are never accessed. Closed fixture history is retained.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from urllib.request import Request, urlopen
import uuid

ROOT=Path(__file__).resolve().parents[2]


def validate_target(document,confirmation):
    if not isinstance(document,dict): raise ValueError('invalid_target')
    fingerprint=document.get('fingerprint','')
    if (not re.fullmatch(r'[a-f0-9]{64}',fingerprint)
        or confirmation!='test-live:'+fingerprint
        or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_-]*',document.get('host',''))
        or len(document.get('mappings',[]))!=1): raise ValueError('invalid_target_confirmation')
    mapping=document['mappings'][0]
    for field in ('source_repo','destination_repo'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',mapping.get(field,'')):
            raise ValueError('invalid_repository')
    if (mapping.get('destination_origin')!='https://api.github.com'
        or mapping.get('visibility')!='public'
        or not re.fullmatch(r'[a-z0-9_-]{1,16}:[1-9][0-9]*',mapping.get('source',''))
        or not re.fullmatch(r'https://[a-zA-Z0-9.-]+',mapping.get('source_origin',''))
        or type(mapping.get('destination_id')) is not int or mapping['destination_id']<=0):
        raise ValueError('invalid_public_enrollment')
    return document


def require_owned(row,identity,prefix):
    text=row.get('body',row.get('description','')) or ''
    if row.get('id')!=identity or not text.startswith(prefix+':'):
        raise ValueError('fixture_ownership_changed')


def load_receipt(path):
    document=json.loads(path.read_text())
    if document.get('pending'): raise ValueError('fixture_create_unresolved')
    if (not isinstance(document.get('objects'),dict)
        or not re.fullmatch(r'acceptance-fixture-[a-f0-9]{12}',document.get('prefix',''))):
        raise ValueError('invalid_fixture_receipt')
    return document


@contextmanager
def receipt_lock(path):
    fd=os.open(str(path)+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_fixture_lock')
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('fixture_busy') from None
        yield
    finally: os.close(fd)


def save(path,document):
    fd,temporary=tempfile.mkstemp(prefix='.receipt-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(document,stream,sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path); temporary=None
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temporary: os.unlink(temporary)


def command(arguments):
    result=subprocess.run(arguments,cwd=ROOT,text=True,capture_output=True,timeout=120)
    if result.returncode: raise ValueError('acceptance_command_failed')
    return result.stdout


class Experiment:
    def __init__(self,target,path):
        self.target=target; self.mapping=target['mappings'][0]; self.path=path
        if path.exists(): self.state=load_receipt(path)
        else:
            self.state={'fingerprint':target['fingerprint'],'prefix':'acceptance-fixture-'+uuid.uuid4().hex[:12],
                        'objects':{},'pending':None}
        if self.state['fingerprint']!=target['fingerprint']: raise ValueError('fixture_enrollment_changed')

    def tea(self,*arguments):
        output=command(['mise','exec','--','teacli',*arguments,'--format','json'])
        return json.loads(output) if output.strip() else None

    def preflight(self):
        host=self.target['host']
        output=command(['mise','exec','--','uv','run','--frozen','--no-sync','ansible',host,
            '-i','inventory/production','-b','-m','ansible.builtin.command','-a',
            '/usr/sbin/runuser -u svc-forgejo-metadata -- /usr/bin/python3 -B -m forgejo_metadata '
            '--config /etc/forgejo-metadata/config.json plan chdir=/usr/local/libexec/forgejo-metadata'])
        installed=json.loads(output.split('>>',1)[1])
        if installed!={key:self.target[key] for key in ('fingerprint','mappings')}:
            raise ValueError('installed_enrollment_changed')
        source=self.tea('repo','get',self.mapping['source_repo'])
        if (source.get('id')!=int(self.mapping['source'].split(':')[1])
            or source.get('full_name')!=self.mapping['source_repo'] or source.get('private')
            or not source.get('html_url','').startswith(self.mapping['source_origin']+'/')):
            raise ValueError('source_enrollment_changed')
        request=Request('https://api.github.com/repos/'+self.mapping['destination_repo'],
                        headers={'User-Agent':'metadata-acceptance','Accept':'application/vnd.github+json'})
        with urlopen(request,timeout=30) as response: destination=json.load(response)
        if (destination.get('id')!=self.mapping['destination_id']
            or destination.get('full_name')!=self.mapping['destination_repo']
            or destination.get('visibility')!='public'): raise ValueError('destination_enrollment_changed')

    def owned(self,kind):
        row=self.state['objects'][kind]; repo=self.mapping['source_repo']
        number=row['number'] if kind=='issue' else row['id']
        arguments=('issue','comment','get',repo,str(number)) if kind=='comment' else (kind,'get',repo,str(number))
        observed=self.tea(*arguments); require_owned(observed,row['id'],self.state['prefix'])
        return row

    def create(self,kind,*arguments):
        if kind in self.state['objects']: self.owned(kind); return
        self.preflight()
        if kind=='comment': self.owned('issue')
        self.state['pending']=kind; save(self.path,self.state)
        row=self.tea(*arguments)
        require_owned(row,row['id'],self.state['prefix'])
        self.state['objects'][kind]={'id':row['id'],'number':row.get('number',row['id'])}
        self.state['pending']=None; save(self.path,self.state)

    def mutate(self,kind,*arguments):
        self.preflight(); self.owned(kind); self.tea(*arguments)

    def run(self,action):
        self.preflight(); repo=self.mapping['source_repo']; prefix=self.state['prefix']
        text=prefix+': bounded live acceptance for the metadata mirror. Forgejo remains authoritative.'
        if action=='create':
            self.create('label','label','create',repo,'--name',prefix,'--color','abcdef','--description',text)
            self.create('milestone','milestone','create',repo,'--title',prefix,'--description',text)
            self.create('issue','issue','create',repo,'--title',prefix,'--body',text,'--label',prefix)
            issue=str(self.state['objects']['issue']['number'])
            self.mutate('issue','issue','update',repo,issue,'--milestone',str(self.state['objects']['milestone']['id']))
            self.create('comment','issue','comment','create',repo,issue,'--body',text)
        elif action=='edit':
            issue=str(self.owned('issue')['number']); label=str(self.owned('label')['id'])
            milestone=str(self.owned('milestone')['id']); comment=str(self.owned('comment')['id'])
            edited=prefix+': edited fixture content.'
            self.mutate('label','label','update',repo,label,'--name',prefix+'-edited','--color','123abc','--description',edited)
            self.mutate('milestone','milestone','update',repo,milestone,'--title',prefix+'-edited',
                        '--description',edited,'--due-date','2026-12-31')
            self.mutate('comment','issue','comment','update',repo,comment,'--body',edited)
            self.mutate('issue','issue','update',repo,issue,'--title',prefix+'-edited','--body',edited,'--state','closed')
            self.mutate('milestone','milestone','close',repo,milestone)
        elif action in ('reopen','finish'):
            issue=str(self.owned('issue')['number']); milestone=str(self.owned('milestone')['id'])
            verb='reopen' if action=='reopen' else 'close'
            self.mutate('issue','issue',verb,repo,issue)
            self.mutate('milestone','milestone',verb,repo,milestone)
            if action=='finish':
                self.mutate('issue','issue','label','clear',repo,issue)
                self.mutate('issue','issue','update',repo,issue,'--milestone','0')
        elif action=='interrupt':
            host=self.target['host']
            def unit(arguments):
                return command(['mise','exec','--','uv','run','--frozen','--no-sync','ansible',host,
                    '-i','inventory/production','-b','-m','ansible.builtin.command','-a',arguments])
            output=unit('systemctl show forgejo-metadata-control.service --property=MainPID --property=ActiveState')
            pid=re.search(r'MainPID=([1-9][0-9]*)',output)
            if not pid or not re.search(r'ActiveState=(activating|active)\b',output):
                raise ValueError('control_not_running')
            unit('systemctl kill --signal=SIGTERM --kill-whom=main forgejo-metadata-control.service')
        self.state['last_action']=action; save(self.path,self.state)
        print(json.dumps(self.state,sort_keys=True))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['create','edit','reopen','finish','interrupt'])
    parser.add_argument('--enrollment',type=Path,required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    parser.add_argument('--confirm',required=True)
    args=parser.parse_args(argv)
    try:
        target=validate_target(json.loads(args.enrollment.read_text()),args.confirm)
        root=(ROOT/'.tmp').resolve(); receipt=args.receipt.resolve()
        if not receipt.is_relative_to(root) or args.receipt.is_symlink(): raise ValueError('private_receipt_required')
        with receipt_lock(receipt): Experiment(target,receipt).run(args.action)
        return 0
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error,ValueError) else 'acceptance_failed')
        return 1


if __name__=='__main__': raise SystemExit(main())
