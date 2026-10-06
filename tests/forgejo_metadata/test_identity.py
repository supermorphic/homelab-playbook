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
        self.assertTrue(projection.fields['body'].endswith('<!-- forgejo-mirror:v1:fixture:7:issue:23 -->'))
        self.assertIn('original', projection.fields['body'])
        self.assertIn('2026-01-01T00:00:00Z', projection.fields['body'])
        self.assertEqual(('fixture', 7, 'issue', 23), tuple(vars(projection.key).values()))
        self.assertEqual(projection.key, self.identity.parse_marker(projection.fields['body'], 'issue'))

    def test_inner_marker_and_title_edits_do_not_change_identity(self):
        source = dict(ISSUE, title='Renamed', number=99,
                      body='<!-- forgejo-mirror:v1:other:8:issue:77 -->')
        projection = self.identity.render_projection(self.mapping, 'issue', source)
        self.assertEqual(23, self.identity.parse_marker(projection.fields['body'], 'issue').object_id)

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
