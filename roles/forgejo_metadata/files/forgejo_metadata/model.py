"""Validated public configuration and shared metadata types."""
from dataclasses import dataclass, field, asdict
import hashlib
import json
import re
from urllib.parse import urlsplit

class MirrorError(RuntimeError):
    """Sanitized actionable error class; never include remote payloads."""

@dataclass(frozen=True)
class SourceKey:
    instance: str
    repository_id: int
    kind: str
    object_id: int

@dataclass(frozen=True)
class MappingConfig:
    instance: str
    source_origin: str
    source_repo: str
    source_id: int
    destination_origin: str
    destination_repo: str
    destination_id: int
    visibility: str
    actor_id: int
    source_credential: str
    destination_credential: str
    ca_file: str = ''

@dataclass
class Projection:
    key: SourceKey
    fields: dict

@dataclass
class DestinationObject:
    key: SourceKey
    locator: str
    fields: dict

@dataclass
class Inventory:
    issues: list = field(default_factory=list)
    comments: dict = field(default_factory=dict)
    labels: list = field(default_factory=list)
    milestones: list = field(default_factory=list)
    complete: bool = True

@dataclass
class RunResult:
    writes: int = 0
    backlog: int = 0
    errors: list = field(default_factory=list)
    converged_at: float | None = None

def positive(value):
    return type(value) is int and 0 < value < 2**63

def load_config(document: dict) -> list[MappingConfig]:
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('mappings'), list):
        raise MirrorError('invalid_configuration')
    mappings=[]; destinations=set(); sources=set()
    for row in document['mappings']:
        try: mapping=MappingConfig(**row)
        except (TypeError, ValueError): raise MirrorError('invalid_mapping') from None
        if not re.fullmatch(r'[a-z0-9_-]{1,16}', mapping.instance): raise MirrorError('invalid_instance')
        for origin in (mapping.source_origin, mapping.destination_origin):
            parsed=urlsplit(origin)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise MirrorError('invalid_origin')
        for repo in (mapping.source_repo, mapping.destination_repo):
            if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*', repo):
                raise MirrorError('invalid_repository')
        if not all(positive(v) for v in (mapping.source_id, mapping.destination_id, mapping.actor_id)):
            raise MirrorError('invalid_identity')
        if mapping.visibility not in ('public', 'private'): raise MirrorError('invalid_visibility')
        for name in (mapping.source_credential, mapping.destination_credential):
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name): raise MirrorError('invalid_credential_reference')
        destination=(mapping.destination_origin, mapping.destination_id)
        source=(mapping.instance, mapping.source_id)
        if destination in destinations or source in sources: raise MirrorError('duplicate_mapping')
        destinations.add(destination); sources.add(source); mappings.append(mapping)
    return mappings

def enrollment_fingerprint(mappings: list[MappingConfig]) -> str:
    fields=('instance','source_origin','source_repo','source_id','destination_origin',
            'destination_repo','destination_id','visibility','actor_id')
    rows=[{k:asdict(m)[k] for k in fields} for m in mappings]
    return hashlib.sha256(json.dumps(sorted(rows,key=lambda r:(r['instance'],r['source_id'])),sort_keys=True).encode()).hexdigest()
