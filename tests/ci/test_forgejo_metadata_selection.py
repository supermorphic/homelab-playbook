"""Metadata API acceptance follows its scenario in local and hosted CI."""
from pathlib import Path
import sys
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/ci'))
import classify
import molecule_plan
import run_changed
class MetadataSelectionTests(unittest.TestCase):
    def result(self,path):
        result=classify.classify_paths([path]); result['molecule_plan']=molecule_plan.build_plan(result); return result
    def selectors(self,result): return {row['selector'] for row in result['molecule_plan']['matrix']['include']}
    def test_metadata_surfaces_select_only_metadata(self):
        for path in ('roles/forgejo_metadata/tasks/main.yml','playbooks/forgejo-metadata/verify.yml','scripts/forgejo_metadata/fixture.py','tests/forgejo_metadata/test_cli.py'):
            with self.subTest(path=path):
                result=self.result(path); self.assertEqual('molecule',result['depth']); self.assertEqual({'forgejo_metadata/default'},self.selectors(result))
                self.assertEqual(1,run_changed.commands_for(result).count(['mise','run','test:forgejo-metadata','--','fixture']))
    def test_docs_and_unrelated_roles_exclude_metadata(self):
        for path in ('docs/specs/012-forgejo-github-metadata-mirror.md','roles/forgejo/tasks/main.yml'):
            self.assertNotIn('forgejo_metadata/default',self.selectors(self.result(path)))
    def test_shared_unknown_paths_select_full_suite(self):
        for path in ('unknown/file','scripts/molecule.py','.mise.toml'):
            result=self.result(path); self.assertEqual('full',result['depth']); self.assertIn('forgejo_metadata/default',self.selectors(result))
    def test_hosted_and_full_local_include_acceptance(self):
        workflow=yaml.safe_load((ROOT/'.github/workflows/ci.yml').read_text())
        rows=[step for step in workflow['jobs']['molecule']['steps'] if 'test:forgejo-metadata' in step.get('run','')]
        self.assertEqual(1,len(rows)); self.assertEqual("matrix.selector == 'forgejo_metadata/default' && matrix.platform == 'debian13'",rows[0]['if'])
        text=(ROOT/'.mise.toml').read_text(); self.assertIn('mise run test:molecule -- forgejo_metadata/default',text); self.assertIn('mise run test:forgejo-metadata -- fixture',text)
