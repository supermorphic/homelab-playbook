"""Durable initialization and uncertainty journal, separate from object mappings."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from .model import MirrorError

def key_string(key):
    return f'{key.instance}:{key.repository_id}:{key.kind}:{key.object_id}'

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
    def load(self):
        try: value=read_private(self.path)
        except (OSError,ValueError): raise MirrorError('state_uninitialized_or_invalid') from None
        if (not isinstance(value,dict) or value.get('version')!=1 or value.get('fingerprint')!=self.fingerprint
            or not isinstance(value.get('pending'),dict) or not isinstance(value.get('initialization'),dict)
            or not value['initialization'].get('decision_ref')):
            raise MirrorError('state_uninitialized_or_invalid')
        for key,intent in value['pending'].items():
            if not isinstance(key,str) or not isinstance(intent,dict) or set(intent)!={'key','started'}:
                raise MirrorError('state_uninitialized_or_invalid')
        return value
    def require_initialized(self): self.load()
    def bootstrap(self,mode,decision_ref):
        if mode not in ('initial','recover') or not isinstance(decision_ref,str) or not decision_ref.strip():
            raise MirrorError('initialization_decision_required')
        if self.path.is_symlink(): raise MirrorError('unsafe_state_file')
        exists=self.path.exists()
        if exists and mode=='initial': raise MirrorError('already_initialized')
        pending={}; retry_at=0
        if exists:
            try:
                old=self.load(); pending=old['pending']; retry_at=old.get('retry_at',0)
            except MirrorError:
                # Preserve the bytes before explicitly attended recovery replaces them.
                archive=self.path.with_name(self.path.name+f'.recovery-{time.time_ns()}')
                fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as original:
                    data=original.read(1024*1024+1)
                if len(data)>1024*1024: raise MirrorError('state_recovery_limit')
                with open(archive,'xb') as output:
                    os.chmod(archive,0o600); output.write(data); output.flush(); os.fsync(output.fileno())
        atomic_json(self.path,{'version':1,'fingerprint':self.fingerprint,
            'initialization':{'mode':mode,'decision_ref':decision_ref,'at':time.time()},
            'pending':pending,'retry_at':retry_at})
    def begin_create(self,key):
        document=self.load(); identity=key_string(key)
        if identity in document['pending']: raise MirrorError('create_outcome_unresolved')
        document['pending'][identity]={'key':asdict(key),'started':time.time()}
        atomic_json(self.path,document)
    def finish_create(self,key,outcome,locator):
        if outcome=='unknown': return
        if outcome not in ('not_created','created') or outcome=='created' and not locator:
            raise MirrorError('invalid_create_resolution')
        document=self.load(); document['pending'].pop(key_string(key),None); atomic_json(self.path,document)
    def resolve_absent(self,key,decision_ref):
        if not isinstance(decision_ref,str) or not decision_ref.strip(): raise MirrorError('resolution_decision_required')
        document=self.load()
        if key_string(key) not in document['pending']: raise MirrorError('unknown_pending_create')
        document['pending'].pop(key_string(key))
        document['last_resolution']={'key':asdict(key),'decision_ref':decision_ref,'at':time.time()}
        atomic_json(self.path,document)
    def defer(self,deadline):
        document=self.load(); document['retry_at']=max(document.get('retry_at',0),deadline); atomic_json(self.path,document)
