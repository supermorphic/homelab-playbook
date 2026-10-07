import importlib.util
from pathlib import Path
import unittest

source = Path(__file__).parents[2] / 'roles/forgejo_runner/files/controller_launch.py'
spec = importlib.util.spec_from_file_location('deployment_controller', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ControllerTests(unittest.TestCase):
    def request(self):
        return {'url': 'https://forge.example', 'label': 'homelab-podman-amd64',
                'job_image': 'localhost/forgejo-job:approved', 'worker_uid': 2201,
                'controller_uid': 2202, 'controller_gid': 2202, 'job_seconds': 10800,
                'uuid': 'synthetic-uuid', 'token': 'synthetic-private', 'handle': 'synthetic-handle'}

    def test_one_job_configuration_disables_caches_and_image_refresh(self):
        config = module.runner_configuration(self.request())
        self.assertEqual(1, config['runner']['capacity'])
        self.assertEqual({'enabled': False}, config['cache'])
        self.assertEqual(False, config['container']['force_pull'])
        self.assertEqual('host', config['container']['network'])
        self.assertEqual('unix:///run/user/2201/podman.sock', config['container']['docker_host'])
        self.assertEqual(['homelab-podman-amd64:docker://localhost/forgejo-job:approved'], config['runner']['labels'])
        self.assertEqual(['/work/job-workspace'], config['container']['valid_volumes'])

    def test_untrusted_label_cannot_supply_a_host_executor(self):
        for field, value in [('label', 'label:host'), ('job_image', 'image --privileged'),
                             ('worker_uid', True), ('controller_uid', 2201),
                             ('job_seconds', 0), ('handle', '\nsecret')]:
            request = self.request()
            request[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.runner_configuration(request)

    def test_jobs_use_the_paired_runtime_and_shared_bind_path_for_siblings(self):
        configuration = module.runner_configuration(self.request())
        self.assertEqual('unix:///var/run/docker.sock', configuration['runner']['envs']['CONTAINER_HOST'])
        self.assertEqual('/work/job-workspace', configuration['runner']['envs']['TMPDIR'])
        self.assertEqual('/work/job-workspace', configuration['runner']['envs']['FORGEJO_RUNNER_WORKSPACE'])
        self.assertEqual('/work/job-workspace', configuration['container']['workdir_parent'])

    def test_runner_arguments_never_contain_registration_credentials(self):
        request = self.request()
        args = module.runner_arguments(request, 9)
        self.assertNotIn(request['token'], ' '.join(args))
        self.assertNotIn(request['uuid'], ' '.join(args))
        self.assertIn('/proc/self/fd/9', args)
        self.assertEqual(['one-job', '--handle', 'synthetic-handle'], args[-3:])


if __name__ == '__main__':
    unittest.main()
