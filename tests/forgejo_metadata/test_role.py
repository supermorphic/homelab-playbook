"""Execute real role assertions with synthetic, secret-free Ansible inputs."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml
from support import CONFIG
ROOT=Path(__file__).resolve().parents[2]
class RoleTests(unittest.TestCase):
    def settings(self):
        path=ROOT/'roles/forgejo_metadata/defaults/main.yml'
        self.assertTrue(path.exists(),'role defaults missing')
        values=yaml.safe_load(path.read_text())
        values.update(forgejo_metadata_uid=2105,forgejo_metadata_gid=2105,
            forgejo_metadata_mappings=copy.deepcopy(CONFIG['mappings']),forgejo_metadata_tokens={'source':'synthetic-only-not-live','destination':'synthetic-only-not-live'})
        return values
    def evaluate(self,values,task='inputs.yml'):
        root=ROOT/'.tmp/012-execution'; root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            directory=Path(directory); config=directory/'ansible.cfg'
            config.write_text('[defaults]\nvars_plugins_enabled=host_group_vars\nretry_files_enabled=false\n')
            (directory/'vars.json').write_text(json.dumps(values))
            (directory/'test.yml').write_text(yaml.safe_dump([{'name':'Synthetic role assertions','hosts':'localhost','gather_facts':False,'tasks':[{'name':'Check inputs','ansible.builtin.import_tasks':str(ROOT/'roles/forgejo_metadata/tasks'/task)}]}]))
            env={k:v for k,v in os.environ.items() if not k.startswith(('SOPS_AGE_','ANSIBLE_SOPS_','ANSIBLE_VAULT_'))}
            env.update(ANSIBLE_CONFIG=str(config),ANSIBLE_VARS_ENABLED='host_group_vars',SOPS_ANSIBLE_AWX_DISABLE_VARS_PLUGIN_TEMPORARILY='true')
            result=subprocess.run(['ansible-playbook','-i','localhost,','-c','local',str(directory/'test.yml'),'-e','@'+str(directory/'vars.json')],env=env,capture_output=True,text=True,timeout=30)
            self.assertNotIn('synthetic-only-not-live',result.stdout+result.stderr)
            (root/'role-assertions.log').write_text(result.stdout+result.stderr)
            return result.returncode
    def test_valid_enrollment(self): self.assertEqual(0,self.evaluate(self.settings()))
    def test_missing_identity_unsafe_path_and_missing_token(self):
        for key,value in [('forgejo_metadata_uid',None),('forgejo_metadata_state_root','/tmp/state'),('forgejo_metadata_tokens',{})]:
            values=self.settings(); values[key]=value
            with self.subTest(key=key): self.assertNotEqual(0,self.evaluate(values))
    def test_duplicate_enrollment(self):
        values=self.settings(); values['forgejo_metadata_mappings']*=2
        self.assertNotEqual(0,self.evaluate(values))
    def test_existing_identity_and_conflicting_allocations(self):
        values=self.settings(); name=values['forgejo_metadata_account']
        values['ansible_facts']={'getent_passwd':{name:['x','2105','2105','','/nonexistent','/usr/sbin/nologin']},'getent_group':{name:['x','2105','']}}
        self.assertEqual(0,self.evaluate(values,'identity-check.yml'))
        for conflict in ('mismatch','uid','gid','members'):
            changed=copy.deepcopy(values)
            if conflict=='mismatch': changed['ansible_facts']['getent_passwd'][name][1]='2200'
            elif conflict=='uid': changed['ansible_facts']['getent_passwd']['other']=['x','2105','3000','','/nonexistent','/usr/sbin/nologin']
            elif conflict=='gid': changed['ansible_facts']['getent_group']['other']=['x','2105','']
            else: changed['ansible_facts']['getent_group'][name][2]='other'
            self.assertNotEqual(0,self.evaluate(changed,'identity-check.yml'))
