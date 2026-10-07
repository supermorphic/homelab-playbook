"""Native workload authority is finite and selected before host allocation."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_host_network import network_target


class NativeWorkloadTests(unittest.TestCase):
    def target(self):
        target=network_target()
        target['limits'].update(memory_bytes=5*1024**3//2,disk_bytes=24*1024**3,
                                pids=768,cpu_percent=200,job_seconds=10800)
        return target

    def test_workload_budget_preserves_memory_disk_and_cpu_reserves(self):
        from scripts.forgejo_runner import host_worker
        target=self.target()
        capacity={'memory_available':7*1024**3,'memory_total':8*1024**3,
                  'disk_available':32*1024**3,'cpu_count':4}
        host_worker.validate_workload_budget(target,capacity)
        for field,value in (('memory_available',6*1024**3),('disk_available',27*1024**3),('cpu_count',2)):
            with self.subTest(field=field),self.assertRaises(ValueError):
                host_worker.validate_workload_budget(target,{**capacity,field:value})
        for field,value in (('job_seconds',10801),('memory_bytes',4*1024**3),('pids',1025),('cpu_percent',201),('disk_bytes',33*1024**3)):
            with self.subTest(field=field),self.assertRaises(ValueError):
                host_worker.validate_workload_budget({**target,'limits':{**target['limits'],field:value}})
        with self.assertRaises(ValueError): host_worker.validate_job_budget(target)

    def test_workload_policy_adds_only_selected_registry_sources(self):
        from scripts.forgejo_runner import host_worker
        assets=[f'image-{index}.oci' for index in range(5)]
        files=host_worker.worker_files(self.target(),'trusted launcher',oci_assets=assets,
                                      registry_images=['docker.io/library/debian:13'])
        policy=json.loads(files['etc/containers/policy.json'])
        self.assertEqual([{'type':'reject'}],policy['default'])
        self.assertEqual({'docker.io/library/debian:13'},set(policy['transports']['docker']))
        self.assertEqual({'/work/input/'+asset for asset in assets},set(policy['transports']['oci-archive']))
        for refs in ([''],['docker.io/library/debian:13; false'],['registry.example.invalid/image']):
            with self.subTest(refs=refs),self.assertRaises(ValueError):
                host_worker.worker_files(self.target(),'trusted launcher',registry_images=refs)

    def test_workload_overlay_is_native_and_confined_to_owned_storage(self):
        import tomllib
        from scripts.forgejo_runner import host_worker,worker_probe
        target=self.target(); uid=target['worker']['uid']
        files=host_worker.worker_files(target,'trusted launcher',storage_driver='overlay')
        store=tomllib.loads(files['etc/containers/storage.conf'])['storage']
        import configparser
        manager=configparser.ConfigParser(interpolation=None); manager.optionxform=str
        manager.read_string(files['etc/systemd/user.conf'])
        self.assertEqual('CONTAINERS_STORAGE_CONF=/etc/containers/storage.conf',
                         manager['Manager']['DefaultEnvironment'])
        self.assertEqual('overlay',store['driver'])
        self.assertEqual('',store['options']['overlay']['mount_program'])
        config=json.loads(files['worker.json'])
        observed={'graphDriverName':'overlay','graphRoot':'/work/graph',
                  'runRoot':f'/run/user/{uid}/storage','graphOptions':{'overlay.mountopt':'nodev'}}
        worker_probe.validate_storage_driver(observed,config)
        for key,value in (('graphDriverName','vfs'),('graphRoot','/host/state'),
                          ('runRoot','/host/run'),('graphOptions',{'overlay.mount_program':'/usr/bin/fuse-overlayfs'})):
            with self.subTest(key=key),self.assertRaises(ValueError):
                worker_probe.validate_storage_driver({**observed,key:value},config)
        with self.assertRaises(ValueError):
            host_worker.worker_files(target,'trusted launcher',storage_driver='arbitrary')

    def test_native_build_arguments_use_the_verified_local_oci_identity(self):
        import subprocess
        from scripts.forgejo_runner.native_job import NativeRun
        from scripts.forgejo_runner.fixture import RunnerRun
        pin='ghcr.io/jdx/mise@sha256:'+'a'*64
        local='localhost/native-image-4:fixture'
        run=NativeRun({pin:local},'/private-api.sock')
        with patch.object(RunnerRun,'command',return_value=subprocess.CompletedProcess([],0,'','')) as command:
            run.command(['/usr/bin/podman','build','--build-arg','MISE_IMAGE='+pin,'/work/source'])
        argv=command.call_args.args[0]
        self.assertIn('MISE_IMAGE='+local,argv)
        self.assertIn('--url=unix:/private-api.sock',argv)

    def test_synthetic_siblings_live_until_the_finite_outer_watchdog(self):
        from scripts.forgejo_runner import worker_probe
        self.assertEqual(10830,worker_probe.sibling_lifetime({'job_seconds':10800}))
        for value in (0,True,10801):
            with self.subTest(value=value),self.assertRaises(ValueError):
                worker_probe.sibling_lifetime({'job_seconds':value})

    def test_job_image_git_seeds_exact_archived_tree_without_credentials_in_argv(self):
        import os, subprocess, sys, tempfile
        from types import SimpleNamespace
        from scripts.forgejo_runner import native_job
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); original=root/'original'; restored=root/'restored'
            original.mkdir(); restored.mkdir()
            def git(path,*args):
                return subprocess.check_output(['git','-C',str(path),*args],text=True,stderr=subprocess.PIPE).strip()
            git(original,'init','-q'); (original/'candidate').write_text('exact source')
            git(original,'add','--all'); tree=git(original,'write-tree')
            archive=root/'archive.tar'
            subprocess.run(['git','-C',str(original),'archive','--output',str(archive),tree],check=True)
            import tarfile
            with tarfile.open(archive) as a: a.extractall(restored,filter='data')
            remote=root/'remote'/'fixture'/'repository.git'; remote.mkdir(parents=True)
            git(remote,'init','--bare','-q')
            password='synthetic-private-secret'
            app=SimpleNamespace(url=str(root/'remote'),user='fixture',password=password)
            captured={}
            class LocalImage:
                def foreground(self,suffix,argv,**kwargs):
                    captured['argv']=argv
                    self_result=subprocess.run([sys.executable,*argv[-3:]],
                        input=kwargs['input_text'],cwd=restored,env={**os.environ,
                        'PYTHONPATH':str(Path.cwd())},capture_output=True,text=True,check=True)
                    return self_result
            native_job.seed_native_source(LocalImage(),'localhost/job:fixture',restored,tree,app,{'name':'repository'})
            self.assertEqual(tree,git(remote,'rev-parse','main^{tree}'))
            self.assertNotIn(password,' '.join(captured['argv']))
            self.assertIn('--userns=keep-id:uid=0,gid=0',captured['argv'])
            self.assertEqual(tree,git(restored,'write-tree'))

    def test_native_workload_inspection_requires_all_twenty_six_checks(self):
        from scripts.forgejo_runner import host_fixture
        fields=('rootless_api','sibling_containers','mapped_bind','private_loopback','published_loopback',
            'user_manager','bounded_storage','outer_limits','process_boundary','allowed_ipv4','allowed_ipv6',
            'denied_ipv4','denied_ipv6','policy_immutable','alternate_network_modes','kernel_policy',
            'controlled_peer','dns','forgejo_https','public_http','public_https','gateway_boundary',
            'one_job','job_runtime','job_public_access','job_workload')
        outcome={'acceptance':'workload-only','exit_code':0,'primary_error':None,'cleanup_errors':[],
                 'observations':{field:True for field in fields}}
        selected={'selector':'forgejo/default'}
        configuration={'source_tree':'a'*40,'architecture':'amd64','images':[],
                       'candidate':{},'workload':selected,'mise_image':'public-pin'}
        def transport(target,payload,**kwargs):
            compile(payload,'<native-workload-payload>','exec')
            return outcome if payload.splitlines()[-1].startswith('print(json.dumps(run_image_probe(') else {'architecture':'amd64'}
        with patch('scripts.forgejo_runner.native_assets.prepare',return_value=({},configuration)), \
             patch.object(host_fixture,'ssh_observation',side_effect=transport):
            result=host_fixture.inspect_native_jobs(self.target(),Path('.tmp/images.json'),
                                                    public_network=True,workload=selected)
            self.assertEqual(outcome['observations'],result['observations'])
            for field in fields:
                bad={**outcome,'observations':{**outcome['observations'],field:False}}
                def failure(target,payload,**kwargs):
                    return bad if payload.splitlines()[-1].startswith('print(json.dumps(run_image_probe(') else {'architecture':'amd64'}
                with self.subTest(field=field), patch.object(host_fixture,'ssh_observation',side_effect=failure),self.assertRaises(ValueError):
                    host_fixture.inspect_native_jobs(self.target(),Path('.tmp/images.json'),public_network=True,workload=selected)
