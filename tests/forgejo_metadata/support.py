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
