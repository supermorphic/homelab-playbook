"""Deterministic source attribution and durable destination markers."""
import re
from .model import MirrorError, SourceKey, Projection, positive
PATTERN=r'forgejo-mirror:v1:([a-z0-9_-]{1,16}):([0-9]+):(issue|comment|label|milestone):([0-9]+)'

def marker(key):
    return f'forgejo-mirror:v1:{key.instance}:{key.repository_id}:{key.kind}:{key.object_id}'

def parse_marker(text: str, kind: str) -> SourceKey | None:
    if not isinstance(text,str): return None
    pattern = r'^'+PATTERN+r'(?:\n[^\n]*)?$' if kind=='label' else r'<!-- '+PATTERN+r' -->$'
    match=re.search(pattern,text)
    if not match or match[3]!=kind: return None
    key=SourceKey(match[1],int(match[2]),kind,int(match[4]))
    if not positive(key.repository_id) or not positive(key.object_id): return None
    return key

def render_projection(mapping, kind: str, source: dict) -> Projection:
    if kind not in ('issue','comment','label','milestone') or not positive(source.get('id')):
        raise MirrorError('invalid_source_object')
    key=SourceKey(mapping.instance,mapping.source_id,kind,source['id'])
    if kind=='label':
        suffix=f"-{source['id']}"
        name='fj-'+source['name'][:50-len(suffix)-3]+suffix
        identity=marker(key)
        if len(identity)>100: raise MirrorError('projection_limit')
        text=source.get('description') or ''
        description=identity+ ('\n'+text[:99-len(identity)] if text and len(identity)<99 else '')
        fields={'name':name,'color':source['color'].lstrip('#').lower(),'description':description}
    else:
        body=source.get('description' if kind=='milestone' else 'body') or ''
        author=source.get('original_author') or (source.get('user') or {}).get('login','unknown')
        url=source.get('html_url') or f'{mapping.source_origin}/{mapping.source_repo}'
        number=source.get('number','')
        footer=f'\n\n---\nForgejo {mapping.source_repo} #{number}; author: {author}; created: {source.get("created_at","unknown")}.\nSource: {url}'
        if kind=='issue' and source.get('labels'):
            footer+='\nOriginal labels: '+', '.join(label['name'] for label in source['labels'])
        body+=footer+'\n<!-- '+marker(key)+' -->'
        if len(body.encode('utf-8'))>65536: raise MirrorError('projection_limit')
        if kind=='milestone':
            title=source['title']+f" [fj-{source['id']}]"
            if len(title)>255: raise MirrorError('projection_limit')
            fields={'title':title,'description':body,'state':source['state'],'due_on':source.get('due_on')}
        elif kind=='issue':
            if len(source['title'])>256: raise MirrorError('projection_limit')
            fields={'title':source['title'],'body':body,'state':source['state']}
        else: fields={'body':body}
    return Projection(key,fields)
