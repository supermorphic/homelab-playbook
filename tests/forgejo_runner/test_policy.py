"""Declared runner slots must preserve repository and identity boundaries."""

import copy
import importlib.util
from pathlib import Path
import unittest
import subprocess
import tempfile
import hashlib


def slot(index=0, repository='supermorphic/homelab-playbook'):
    def account(name, offset):
        return {'name': name, 'uid': 2200 + offset, 'gid': 2200 + offset,
                'subuid_start': 1000000 + offset * 65536, 'subuid_count': 65536,
                'subgid_start': 1000000 + offset * 65536, 'subgid_count': 65536}
    name = ('playbook', 'talos', 'career')[index]
    return {'name': name, 'repository': repository, 'enabled': False,
            'label': 'homelab-podman-amd64',
            'state_root': '/var/lib/forgejo-runner/' + name,
            'controller': account('ci-' + name + '-ctl', index * 2 + 1),
            'worker': account('ci-' + name + '-exec', index * 2 + 2),
            'limits': {'memory_bytes': 2147483648, 'disk_bytes': 25769803776,
                       'pids': 768, 'cpu_percent': 200, 'idle_seconds': 60,
                       'job_seconds': 10800}}


class SlotPolicyTests(unittest.TestCase):
    def test_deployment_provenance_requires_the_exact_clean_committed_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, *args], text=True).strip()
            git('init', '-q')
            (root / 'source').write_text('reviewed source')
            git('add', 'source')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-qm', 'Fixture')
            commit = git('rev-parse', 'HEAD')
            manifest = {'source_tree': git('rev-parse', 'HEAD^{tree}')}
            self.assertEqual(manifest, self.policy.validate_provenance(manifest, commit, root=root))
            with self.assertRaises(ValueError):
                self.policy.validate_provenance({'source_tree': 'a'*40}, commit, root=root)
            with self.assertRaises(ValueError):
                self.policy.validate_provenance(manifest, 'b'*40, root=root)
            (root / 'source').write_text('uncommitted source')
            with self.assertRaises(ValueError):
                self.policy.validate_provenance(manifest, commit, root=root)
            git('add', 'source')
            with self.assertRaises(ValueError):
                self.policy.validate_provenance({'source_tree': git('write-tree')}, commit, root=root)

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / 'roles/forgejo_runner/filter_plugins/policy.py'
        spec = importlib.util.spec_from_file_location('runner_slot_policy', path)
        cls.policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.policy)

    def test_all_three_repositories_have_distinct_explicit_allocations(self):
        desired = [slot(0), slot(1, 'supermorphic/homelab-talos'),
                   slot(2, 'supermorphic/career-ops')]
        original = copy.deepcopy(desired)
        self.assertEqual(desired, self.policy.validate_slots(desired))
        self.assertEqual(original, desired)
        self.assertEqual([], self.policy.validate_slots([]))

    def test_deployment_manifest_cannot_substitute_unreviewed_runtime_sources(self):
        candidate = {'probe_images': {'amd64': 'quay.io/podman/stable@sha256:' + 'a'*64},
                     'runner_images': {'amd64': 'data.forgejo.org/forgejo/runner@sha256:' + 'b'*64},
                     'mise_images': {'amd64': 'ghcr.io/jdx/mise@sha256:' + 'c'*64}}
        manifest = {'schema': 1, 'architecture': 'amd64', 'source_tree': 'd'*40,
                    'job_image_id': 'e'*64, 'sources': {'probe_image': candidate['probe_images']['amd64'],
                    'runner_image': candidate['runner_images']['amd64'], 'mise_image': candidate['mise_images']['amd64']},
                    'registry_images': ['docker.io/library/debian:13'],
                    'artifacts': {name: {'size': 100, 'sha256': 'f'*64}
                                  for name in ('job.oci', 'forgejo-runner', 'netavark')}}
        self.assertEqual(manifest, self.policy.validate_manifest(manifest, candidate))
        for field in ('runner_image', 'probe_image', 'mise_image'):
            changed = copy.deepcopy(manifest)
            changed['sources'][field] = 'unreviewed.invalid/image:latest'
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.policy.validate_manifest(changed, candidate)
        for value in (['docker.io/library/debian:13;unsafe'], ['docker.io/library/debian:13']*65):
            changed = copy.deepcopy(manifest)
            changed['registry_images'] = value
            with self.assertRaises(ValueError):
                self.policy.validate_manifest(changed, candidate)

    def test_repository_is_an_exact_owner_and_name_without_a_url(self):
        for repository in ('', 'career-ops', 'https://example.invalid/o/r',
                           'o/r/extra', '../r', 'o/r?token=x', 'o/*'):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                self.policy.validate_slots([slot(repository=repository)])

    def test_duplicate_repository_cannot_share_another_slot(self):
        with self.assertRaises(ValueError):
            self.policy.validate_slots([slot(), slot(1)])

    def test_state_root_is_owned_by_the_named_slot(self):
        for root in ('/', '/var/lib/forgejo', '/var/lib/forgejo-runner/../playbook',
                     '/var/lib/forgejo-runner/other', '/var/lib/forgejo-runner/playbook/'):
            declaration = slot()
            declaration['state_root'] = root
            with self.subTest(root=root), self.assertRaises(ValueError):
                self.policy.validate_slots([declaration])

    def test_overlap_is_rejected_across_all_controllers_and_workers(self):
        for field in ('name', 'uid', 'gid', 'subuid_start', 'subgid_start'):
            desired = [slot(), slot(1, 'supermorphic/homelab-talos')]
            desired[1]['worker'][field] = desired[0]['controller'][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.policy.validate_slots(desired)

    def test_subordinate_ranges_cannot_include_a_primary_identity(self):
        declaration = slot()
        declaration['worker']['subuid_start'] = declaration['controller']['uid']
        with self.assertRaises(ValueError):
            self.policy.validate_slots([declaration])

    def test_runner_identities_match_the_qualified_runtime_uid_floor(self):
        for kind in ('controller', 'worker'):
            declaration = slot()
            declaration[kind]['uid'] = 999
            declaration[kind]['gid'] = 999
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.policy.validate_slots([declaration])

    def test_both_subordinate_mapping_counts_match_the_runtime_contract(self):
        declaration = slot()
        declaration['worker']['subgid_count'] *= 2
        with self.assertRaises(ValueError):
            self.policy.validate_slots([declaration])

    def test_every_limit_is_positive_bounded_and_typed(self):
        for key in slot()['limits']:
            for value in (0, -1, True, '100', 2**64):
                declaration = slot()
                declaration['limits'][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.policy.validate_slots([declaration])

    def test_limits_cannot_exceed_the_qualified_runtime_profile(self):
        for key, value in {'memory_bytes': 2147483649, 'disk_bytes': 25769803777,
                           'pids': 769, 'cpu_percent': 201, 'idle_seconds': 301,
                           'job_seconds': 10801}.items():
            declaration = slot()
            declaration['limits'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.policy.validate_slots([declaration])

    def test_ansible_yaml_numeric_values_remain_supported(self):
        from ansible.parsing.dataloader import DataLoader
        import yaml
        declaration = DataLoader().load(yaml.safe_dump([slot()]))
        self.assertEqual(declaration, self.policy.validate_slots(declaration))

    def test_an_existing_host_account_cannot_be_reused_as_a_disposable_worker(self):
        declaration = slot()
        account = declaration['worker']
        passwd = f"{account['name']}:x:{account['uid']}:{account['gid']}::/var/lib/example:/usr/sbin/nologin\n"
        groups = f"{account['name']}:x:{account['gid']}:\n"
        subuid = f"{account['name']}:{account['subuid_start']}:{account['subuid_count']}\n"
        subgid = f"{account['name']}:{account['subgid_start']}:{account['subgid_count']}\n"
        with self.assertRaises(ValueError):
            self.policy.validate_unused_slots([declaration], passwd, groups, subuid, subgid)
        self.assertEqual([declaration], self.policy.validate_unused_slots([declaration], '', '', '', ''))

    def test_artifacts_are_measured_before_host_provisioning(self):
        scratch = Path(self.policy.__file__).resolve().parents[3] / '.tmp'
        scratch.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            manifest = {'artifacts': {}}
            for name in ('job.oci', 'forgejo-runner', 'netavark'):
                payload = ('synthetic-' + name).encode()
                (root / name).write_bytes(payload)
                manifest['artifacts'][name] = {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
            self.assertEqual(manifest, self.policy.validate_public_assets(manifest, str(root)))
            (root / 'job.oci').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                self.policy.validate_public_assets(manifest, str(root))
        with self.assertRaises(ValueError):
            self.policy.validate_public_assets(manifest, '/unmanaged/other-task')

    def test_enablement_is_an_explicit_boolean(self):
        for value in (1, None, 'true'):
            declaration = slot()
            declaration['enabled'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.policy.validate_slots([declaration])

    def test_slot_and_label_cannot_be_command_or_path_arguments(self):
        for key in ('name', 'label'):
            for value in ('../other', '-unsafe', 'x;y', '', 'x\ny'):
                declaration = slot()
                declaration[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.policy.validate_slots([declaration])

    def test_unexpected_keys_and_missing_limits_are_rejected(self):
        for key in ('token', 'capacity', 'privileged'):
            declaration = slot()
            declaration[key] = 'unapproved'
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.policy.validate_slots([declaration])
        declaration = slot()
        del declaration['limits']['pids']
        with self.assertRaises(ValueError):
            self.policy.validate_slots([declaration])


if __name__ == '__main__':
    unittest.main()
