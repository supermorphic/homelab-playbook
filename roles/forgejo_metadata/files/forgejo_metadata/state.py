"""Durable initialization and uncertainty journal, separate from object mappings."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
import math
import os
import re
from pathlib import Path
import stat
import tempfile
import time
from .model import MirrorError, SourceKey, positive

def key_string(key):
    return f'{key.instance}:{key.repository_id}:{key.kind}:{key.object_id}'

def valid_ownership(identity,locator):
    if not isinstance(identity,str) or not isinstance(locator,str): return False
    match=re.fullmatch(r'[a-z0-9_-]{1,16}:([1-9][0-9]*):(issue|comment|label):([1-9][0-9]*)',identity)
    if not match: return False
    prefix={'comment':'/issues/comments/','issue':'/issues/','label':'/labels/'}[match[2]]
    return re.fullmatch(re.escape(prefix)+r'[1-9][0-9]*',locator) is not None

def ownership_locator(target):
    if target.key.kind!='label': return target.locator
    if not positive(target.fields.get('id')): raise MirrorError('invalid_destination_label')
    return '/labels/'+str(target.fields['id'])

def valid_intent(key,intent):
    if not isinstance(key,str) or not isinstance(intent,dict) or set(intent)!={'key','started'}: return False
    try: identity=SourceKey(**intent['key'])
    except (TypeError,KeyError): return False
    return (identity.kind in ('issue','comment','label','milestone') and isinstance(identity.instance,str)
        and positive(identity.repository_id) and positive(identity.object_id) and key==key_string(identity)
        and type(intent['started']) in (int,float) and math.isfinite(intent['started']) and intent['started']>0)

def read_private(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'r') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid!=os.geteuid() or info.st_size>1024*1024:
            raise MirrorError('unsafe_state_file')
        return json.load(stream)

def atomic_json(path,value):
    path=Path(path)
    if path.is_symlink(): raise MirrorError('unsafe_state_file')
    temporary=None
    try:
        fd,temporary=tempfile.mkstemp(prefix='.state-',dir=path.parent)
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600)
            json.dump(value,stream,sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path); temporary=None
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    except (OSError,ValueError): raise MirrorError('state_persistence_failed') from None
    finally:
        if temporary: os.unlink(temporary)

@contextmanager
def operation_lock(path):
    try:
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid():
            os.close(fd); raise MirrorError('unsafe_lock')
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd); raise MirrorError('operation_busy') from None
    except OSError: raise MirrorError('lock_failed') from None
    try: yield
    finally: os.close(fd)

class StateStore:
    def __init__(self,path:Path,fingerprint:str): self.path=Path(path); self.fingerprint=fingerprint
    def load(self,allow_other_fingerprint=False):
        try: value=read_private(self.path)
        except (OSError,ValueError): raise MirrorError('state_uninitialized_or_invalid') from None
        if (not isinstance(value,dict) or type(value.get('version')) is not int or value.get('version')!=1
            or not isinstance(value.get('fingerprint'),str)
            or not allow_other_fingerprint and value.get('fingerprint')!=self.fingerprint
            or not isinstance(value.get('pending'),dict) or not isinstance(value.get('initialization'),dict)
            or not isinstance(value['initialization'].get('decision_ref'),str)
            or not value['initialization']['decision_ref'].strip()
            or value['initialization'].get('mode') not in ('initial','recover')
            or type(value['initialization'].get('at')) not in (int,float)
            or not math.isfinite(value['initialization']['at']) or value['initialization']['at']<=0):
            raise MirrorError('state_uninitialized_or_invalid')
        if any(not valid_intent(key,intent) for key,intent in value['pending'].items()):
            raise MirrorError('state_uninitialized_or_invalid')
        ownership=value.get('ownership',{})
        if not isinstance(ownership,dict) or any(not valid_ownership(k,v) for k,v in ownership.items()):
            raise MirrorError('state_uninitialized_or_invalid')
        retries=value.get('retry_deadlines',{})
        if (not isinstance(retries,dict) or any(not isinstance(k,str) or type(v) not in (int,float)
                or not math.isfinite(v) or v<0 for k,v in retries.items())):
            raise MirrorError('state_uninitialized_or_invalid')
        return value
    def require_initialized(self): self.load()
    def ownership_evidence(self):
        # Recovery must use surviving ownership before it replaces initialization.
        try: document=read_private(self.path)
        except (FileNotFoundError,ValueError): return {}
        ownership=document.get('ownership',{}) if isinstance(document,dict) else {}
        if not isinstance(ownership,dict): return {}
        return {key:locator for key,locator in ownership.items() if valid_ownership(key,locator)}
    def bootstrap(self,mode,decision_ref):
        if mode not in ('initial','recover') or not isinstance(decision_ref,str) or not decision_ref.strip():
            raise MirrorError('initialization_decision_required')
        if self.path.is_symlink(): raise MirrorError('unsafe_state_file')
        exists=self.path.exists()
        if exists and mode=='initial': raise MirrorError('already_initialized')
        pending={}; retries={}; ownership={}
        if exists:
            try:
                old=self.load(allow_other_fingerprint=True); pending=old['pending']; retries=old.get('retry_deadlines',{}); ownership=old.get('ownership',{})
            except MirrorError:
                # Preserve the bytes before explicitly attended recovery replaces them.
                archive=self.path.with_name(self.path.name+f'.recovery-{time.time_ns()}')
                fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as original:
                    data=original.read(1024*1024+1)
                if len(data)>1024*1024: raise MirrorError('state_recovery_limit')
                # Corrupt initialization must not discard otherwise valid uncertainty evidence.
                try:
                    surviving=json.loads(data)
                    if isinstance(surviving,dict) and isinstance(surviving.get('pending'),dict):
                        pending={key:intent for key,intent in surviving['pending'].items() if valid_intent(key,intent)}
                    if isinstance(surviving,dict) and isinstance(surviving.get('ownership'),dict):
                        ownership={key:locator for key,locator in surviving['ownership'].items() if valid_ownership(key,locator)}
                except (ValueError,UnicodeDecodeError): pass
                with open(archive,'xb') as output:
                    os.chmod(archive,0o600); output.write(data); output.flush(); os.fsync(output.fileno())
        atomic_json(self.path,{'version':1,'fingerprint':self.fingerprint,
            'initialization':{'mode':mode,'decision_ref':decision_ref,'at':time.time()},
            'pending':pending,'retry_deadlines':retries,'ownership':ownership})
    def remember_owned(self,owned):
        document=self.load(); ownership=document.setdefault('ownership',{})
        updates={key_string(key):ownership_locator(target) for key,target in owned.items() if key.kind in ('issue','comment','label')}
        if any(ownership.get(key)!=locator for key,locator in updates.items()):
            ownership.update(updates); atomic_json(self.path,document)
    def begin_create(self,key):
        document=self.load(); identity=key_string(key)
        if identity in document['pending']: raise MirrorError('create_outcome_unresolved')
        document['pending'][identity]={'key':asdict(key),'started':time.time()}
        atomic_json(self.path,document)
    def record_adoption(self,digest,decision_ref,writes):
        document=self.load()
        document['last_label_adoption']={'digest':digest,'decision_ref':decision_ref,'writes':writes,'at':time.time()}
        atomic_json(self.path,document)
    def finish_create(self,key,outcome,locator):
        if outcome=='unknown': return
        if outcome not in ('not_created','created') or outcome=='created' and not locator:
            raise MirrorError('invalid_create_resolution')
        document=self.load(); document['pending'].pop(key_string(key),None)
        if outcome=='created' and key.kind in ('issue','comment'):
            if not valid_ownership(key_string(key),locator): raise MirrorError('invalid_create_resolution')
            document.setdefault('ownership',{})[key_string(key)]=locator
        atomic_json(self.path,document)
    def resolve_absent(self,key,decision_ref):
        if not isinstance(decision_ref,str) or not decision_ref.strip(): raise MirrorError('resolution_decision_required')
        document=self.load()
        if key_string(key) not in document['pending']: raise MirrorError('unknown_pending_create')
        document['pending'].pop(key_string(key))
        document['last_resolution']={'key':asdict(key),'decision_ref':decision_ref,'at':time.time()}
        atomic_json(self.path,document)
    def defer(self,deadline,key='*'):
        document=self.load(); retries=document.setdefault('retry_deadlines',{})
        retries[key]=max(retries.get(key,0),deadline); atomic_json(self.path,document)
    def deferred(self,mapping):
        retries=self.load().get('retry_deadlines',{})
        return max(retries.get(key,0) for key in ('*',mapping.source_credential,mapping.destination_credential))>time.time()
