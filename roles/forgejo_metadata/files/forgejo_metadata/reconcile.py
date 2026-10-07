"""Compare supported source projections with independently discovered shadows."""
from urllib.parse import quote
import time
from .api import APIError
from .identity import render_projection, parse_marker
from .model import MirrorError, SourceKey, DestinationObject, Projection, RunResult
from .state import key_string

def normalize(kind,row,desired):
    values={key:row.get(key) for key in desired}
    if kind=='issue':
        if 'labels' in values:
            values['labels']=sorted(x['name'] if isinstance(x,dict) else x for x in (row.get('labels') or []))
        if 'milestone' in values:
            milestone=row.get('milestone'); values['milestone']=milestone.get('number') if isinstance(milestone,dict) else milestone
    if kind=='label': values['color']=(row.get('color') or '').lstrip('#').lower()
    return values

def verify_projection(projection,observed):
    expected=dict(projection.fields)
    if 'labels' in expected: expected['labels']=sorted(expected['labels'])
    if normalize(projection.key.kind,observed,expected)!=expected: raise MirrorError('readback_mismatch')

def discover_owned(mapping,inventory):
    if not inventory.complete: raise MirrorError('incomplete_inventory')
    owned={}
    groups=[('label',inventory.labels,None),('milestone',inventory.milestones,None),('issue',inventory.issues,None)]
    groups += [('comment',rows,parent) for parent,rows in inventory.comments.items()]
    for kind,rows,parent in groups:
        for row in rows:
            if kind=='issue' and 'pull_request' in row: continue
            text=row.get('description' if kind in ('label','milestone') else 'body') or ''
            key=parse_marker(text,kind)
            actor=(row.get('user') or {}).get('id')
            if key is None:
                if kind in ('issue','comment') and actor==mapping.actor_id or kind in ('label','milestone') and (row.get('name','').startswith('fj-') or ' [fj-' in row.get('title','')):
                    raise MirrorError('ownership_conflict')
                continue
            if key.instance!=mapping.instance or key.repository_id!=mapping.source_id: continue
            if key in owned or kind in ('issue','comment') and actor!=mapping.actor_id: raise MirrorError('ownership_conflict')
            if kind=='label': locator='/labels/'+quote(row['name'],safe='')
            elif kind=='comment': locator='/issues/comments/'+str(row['id'])
            else: locator='/'+('issues' if kind=='issue' else 'milestones')+'/'+str(row['number'])
            fields=dict(row)
            if parent is not None: fields['_parent_id']=parent
            owned[key]=DestinationObject(key,locator,fields)
    return owned

def comment_parent(mapping,owned,parent_id,target):
    parent=owned.get(SourceKey(mapping.instance,mapping.source_id,'issue',parent_id))
    if parent is None or target and target.fields.get('_parent_id')!=parent.fields['id']:
        raise MirrorError('ownership_conflict')
    return parent

def reconcile(mapping,source,destination,store,write_budget):
    result=RunResult()
    try:
        store.require_initialized(); destination.preflight(mapping)
        source_data=source.inventory(mapping)
        if not source_data.complete: raise MirrorError('incomplete_inventory')
        owned=discover_owned(mapping,destination.inventory(mapping))
        # Rediscovery retires intents even when the source object has disappeared.
        for target in owned.values():
            if key_string(target.key) in store.load()['pending']:
                store.finish_create(target.key,'created',target.locator)
        objects=[]
        for kind,rows in [('label',source_data.labels),('milestone',source_data.milestones),('issue',source_data.issues)]:
            for row in sorted(rows,key=lambda r:r['id']):
                if kind=='issue' and row.get('pull_request') is not None: continue
                objects.append((kind,row,None))
                if kind=='issue': objects.extend(('comment',c,row['id']) for c in source_data.comments.get(row['id'],[]))
        for kind,row,parent_id in objects:
            try:
                projection=render_projection(mapping,kind,row)
                parent=None
                if kind=='issue':
                    label_keys=[SourceKey(mapping.instance,mapping.source_id,'label',label['id']) for label in row.get('labels',[])]
                    milestone_key=SourceKey(mapping.instance,mapping.source_id,'milestone',row['milestone']['id']) if row.get('milestone') else None
                    if any(key not in owned for key in label_keys) or milestone_key and milestone_key not in owned:
                        result.backlog+=1; continue
                    projection.fields['labels']=sorted(owned[key].fields['name'] for key in label_keys)
                    projection.fields['milestone']=owned[milestone_key].fields['number'] if milestone_key else None
                if kind=='comment':
                    parent=owned.get(SourceKey(mapping.instance,mapping.source_id,'issue',parent_id))
                    if parent is None: result.backlog+=1; continue
                target=owned.get(projection.key)
                if target and kind=='comment' and target.fields.get('_parent_id')!=parent.fields['id']: raise MirrorError('ownership_conflict')
                if target:
                    try: verify_projection(projection,target.fields); continue
                    except MirrorError: pass
                if result.writes>=write_budget: result.backlog+=1; continue
                destination.preflight(mapping)
                fresh=discover_owned(mapping,destination.inventory(mapping))
                observed=fresh.get(projection.key)
                if target and observed is None: raise MirrorError('ownership_conflict')
                target=observed
                if kind=='comment': parent=comment_parent(mapping,fresh,parent_id,target)
                if target is None:
                    destination.preflight(mapping)
                    store.begin_create(projection.key)
                    try:
                        result.writes+=1
                        locator=destination.create(projection,parent)
                    except APIError as error:
                        store.finish_create(projection.key,error.outcome,None)
                        raise
                    fresh=discover_owned(mapping,destination.inventory(mapping))
                    created=fresh.get(projection.key)
                    if created is None or created.locator!=locator: raise MirrorError('ownership_conflict')
                    if kind=='comment': parent=comment_parent(mapping,fresh,parent_id,created)
                    store.finish_create(projection.key,'created',locator)
                    target=created
                else: target=DestinationObject(target.key,target.locator,destination.read(target.locator))
                try: verify_projection(projection,target.fields)
                except MirrorError:
                    if result.writes>=write_budget: result.backlog+=1
                    else:
                        destination.preflight(mapping)
                        fresh=discover_owned(mapping,destination.inventory(mapping))
                        current=fresh.get(projection.key)
                        if current is None: raise MirrorError('ownership_conflict')
                        if kind=='comment': parent=comment_parent(mapping,fresh,parent_id,current)
                        destination.preflight(mapping)
                        result.writes+=1
                        destination.update(current,projection)
                        locator='/labels/'+quote(projection.fields['name'],safe='') if kind=='label' else current.locator
                        target=DestinationObject(projection.key,locator,destination.read(locator))
                        verify_projection(projection,target.fields)
                if kind=='comment': target.fields['_parent_id']=parent.fields['id']
                owned[projection.key]=target
            except (MirrorError,KeyError,TypeError,ValueError) as error:
                result.errors.append(str(error) if isinstance(error,MirrorError) else 'invalid_source_or_response')
                if isinstance(error,APIError) and error.retry_at: store.defer(error.retry_at,error.retry_key)
                if isinstance(error,MirrorError) and str(error) in ('run_timeout','ownership_conflict'): return result
        if any(intent['key']['instance']==mapping.instance and intent['key']['repository_id']==mapping.source_id for intent in store.load()['pending'].values()): result.errors.append('create_outcome_unresolved')
        if not result.errors and not result.backlog: result.converged_at=time.time()
    except MirrorError as error:
        result.errors.append(str(error))
        if isinstance(error,APIError) and error.retry_at: store.defer(error.retry_at,error.retry_key)
    return result
