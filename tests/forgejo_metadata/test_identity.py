import copy
import importlib
import unittest
from support import CONFIG, ISSUE

class IdentityTests(unittest.TestCase):
    def setUp(self):
        try:
            self.model = importlib.import_module('forgejo_metadata.model')
            self.identity = importlib.import_module('forgejo_metadata.identity')
        except ModuleNotFoundError:
            self.fail('metadata identity implementation is missing')
        self.mapping = self.model.load_config(CONFIG)[0]

    def test_terminal_marker_and_attribution_preserve_source_identity(self):
        projection = self.identity.render_projection(self.mapping, 'issue', ISSUE)
        self.assertTrue(projection.fields['body'].endswith('<!-- forgejo-mirror:v=1;src=fixture;repo=7;kind=issue;id=23 -->'))
        self.assertIn('original', projection.fields['body'])
        self.assertIn('2026-01-01T00:00:00Z', projection.fields['body'])
        self.assertEqual(('fixture', 7, 'issue', 23), tuple(vars(projection.key).values()))
        self.assertEqual(projection.key, self.identity.parse_marker(projection.fields['body'], 'issue'))

    def test_inner_marker_and_title_edits_do_not_change_identity(self):
        source = dict(ISSUE, title='Renamed', number=99,
                      body='<!-- forgejo-mirror:v=1;src=other;repo=8;kind=issue;id=77 -->')
        projection = self.identity.render_projection(self.mapping, 'issue', source)
        self.assertEqual(23, self.identity.parse_marker(projection.fields['body'], 'issue').object_id)

    def test_named_marker_contract_for_each_object_kind(self):
        for kind in ('issue', 'comment', 'label', 'milestone'):
            with self.subTest(kind=kind):
                payload=f'forgejo-mirror:v=1;src=fixture;repo=7;kind={kind};id=23'
                text=payload+'\nDescription' if kind=='label' else 'Body\n<!-- '+payload+' -->'
                expected=self.model.SourceKey('fixture',7,kind,23)
                self.assertEqual(payload,self.identity.marker(expected))
                self.assertEqual(expected,self.identity.parse_marker(text,kind))

    def test_marker_requires_supported_version_kind_and_terminal_position(self):
        marker='<!-- forgejo-mirror:v=1;src=fixture;repo=7;kind=issue;id=23 -->'
        for text in (marker.replace('v=1;', 'v=2;'),
                     marker.replace('kind=issue;', 'kind=comment;'),
                     marker.replace('repo=7;', 'repo=0;'),
                     marker+'\nUnowned trailing text'):
            with self.subTest(text=text):
                self.assertIsNone(self.identity.parse_marker(text,'issue'))

    def test_largest_label_identity_fits_without_truncation(self):
        config=copy.deepcopy(CONFIG)
        config['mappings'][0].update(instance='x'*16,source_id=2**63-1)
        mapping=self.model.load_config(config)[0]
        label={'id':2**63-1,'name':'bug','color':'abcdef','description':'Long description'}
        projection=self.identity.render_projection(mapping,'label',label)
        payload=f'forgejo-mirror:v=1;src={"x"*16};repo=9223372036854775807;kind=label;id=9223372036854775807'
        self.assertEqual(payload,projection.fields['description'].partition(' ')[0])
        self.assertLessEqual(len(projection.fields['description']),100)
        self.assertEqual(projection.key,self.identity.parse_marker(projection.fields['description'],'label'))

    def test_label_marker_fits_and_full_name_is_retained_in_issue(self):
        label = {'id': 42, 'name': '長' * 80, 'color': '#ABCDEF', 'description': 'x' * 200}
        p = self.identity.render_projection(self.mapping, 'label', label)
        self.assertLessEqual(len(p.fields['name']), 50)
        self.assertTrue(p.fields['name'].endswith('-42'))
        self.assertLessEqual(len(p.fields['description']), 100)
        self.assertEqual('abcdef', p.fields['color'])
        self.assertEqual(42, self.identity.parse_marker(p.fields['description'], 'label').object_id)
        issue = self.identity.render_projection(self.mapping, 'issue', dict(ISSUE, labels=[label]))
        self.assertIn(label['name'], issue.fields['body'])

    def test_original_migration_author_and_oversized_history(self):
        p = self.identity.render_projection(self.mapping, 'issue', dict(ISSUE, original_author='migrated'))
        self.assertIn('migrated', p.fields['body'])
        with self.assertRaisesRegex(self.model.MirrorError, 'projection_limit'):
            self.identity.render_projection(self.mapping, 'issue', dict(ISSUE, body='x' * 300000))
    def test_multiline_label_description_remains_discoverable(self):
        projection=self.identity.render_projection(self.mapping,'label',{'id':42,'name':'bug','color':'abcdef','description':'first\nsecond'})
        self.assertEqual(projection.key,self.identity.parse_marker(projection.fields['description'],'label'))
    def test_label_marker_survives_github_single_line_normalization(self):
        text='forgejo-mirror:v=1;src=fixture;repo=7;kind=label;id=42 Something is broken'
        expected=self.model.SourceKey('fixture',7,'label',42)
        self.assertEqual(expected,self.identity.parse_marker(text,'label'))
        projection=self.identity.render_projection(self.mapping,'label',
            {'id':42,'name':'bug','color':'abcdef','description':'  Something\nis\tbroken  '})
        self.assertEqual(text,projection.fields['description'])
    def test_label_description_truncation_does_not_leave_trailing_space(self):
        payload='forgejo-mirror:v=1;src=fixture;repo=7;kind=label;id=42'
        capacity=99-len(payload)
        projection=self.identity.render_projection(self.mapping,'label',
            {'id':42,'name':'bug','color':'abcdef','description':'a'*(capacity-1)+' b'})
        self.assertEqual(payload+' '+'a'*(capacity-1),projection.fields['description'])
