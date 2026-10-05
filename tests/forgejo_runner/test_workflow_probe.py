"""Aggregate validation rejects incomplete or unrelated workflow evidence."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class WorkflowEvidenceTests(unittest.TestCase):
    def checker(self):
        try:
            module = importlib.import_module('scripts.forgejo_runner.workflow_probe')
        except ModuleNotFoundError:
            self.fail('Workflow evidence reconciliation is not implemented')
        return module.check_evidence

    def inputs(self, directory):
        for child in ('a', 'b'):
            (directory / (child + '.json')).write_text(json.dumps({
                'candidate': 'a' * 40, 'child': child, 'result': 'success',
            }))
        return {
            'plan': {'result': 'success'}, 'matrix': {'result': 'success'},
            'required': {'result': 'success'}, 'conditional': {'result': 'skipped'},
        }

    def test_complete_exact_candidate_evidence_passes(self):
        check = self.checker()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            needs = self.inputs(directory)
            check(directory, 'a' * 40, needs)

    def test_missing_matrix_child_blocks_the_gate(self):
        check = self.checker()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            needs = self.inputs(directory)
            (directory / 'b.json').unlink()
            with self.assertRaises(RuntimeError):
                check(directory, 'a' * 40, needs)

    def test_wrong_candidate_evidence_blocks_the_gate(self):
        check = self.checker()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            needs = self.inputs(directory)
            (directory / 'b.json').write_text(json.dumps({
                'candidate': 'b' * 40, 'child': 'b', 'result': 'success',
            }))
            with self.assertRaises(RuntimeError):
                check(directory, 'a' * 40, needs)

    def test_required_failure_cancellation_and_skip_block_the_gate(self):
        check = self.checker()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            needs = self.inputs(directory)
            for result in ('failure', 'cancelled', 'skipped', 'waiting', None):
                with self.subTest(result=result), self.assertRaises(RuntimeError):
                    check(directory, 'a' * 40, dict(needs, required={'result': result}))

    def test_duplicate_unexpected_and_malformed_evidence_block_the_gate(self):
        check = self.checker()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            needs = self.inputs(directory)
            evidence = directory / 'b.json'
            for raw in ('not-json', '[]', '{}', json.dumps({
                'candidate': 'a' * 40, 'child': 'a', 'result': 'success',
            }), json.dumps({'candidate': 'a' * 40, 'child': 'c', 'result': 'success'})):
                evidence.write_text(raw)
                with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                    check(directory, 'a' * 40, needs)
            self.inputs(directory)
            (directory / 'extra.json').write_text('{}')
            with self.assertRaises(RuntimeError):
                check(directory, 'a' * 40, needs)

    def test_missing_dependency_evidence_blocks_the_gate(self):
        check = self.checker()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            needs = self.inputs(directory)
            del needs['matrix']
            with self.assertRaises(RuntimeError):
                check(directory, 'a' * 40, needs)

    def test_job_gate_script_executes_the_same_evidence_contract(self):
        self.checker()
        from scripts.forgejo_runner import workflow_probe
        self.assertTrue(hasattr(workflow_probe, 'gate_script'), 'Executable workflow gate is missing')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            needs = self.inputs(directory)
            environment = dict(os.environ, CANDIDATE_SHA='a' * 40,
                               NEEDS_JSON=json.dumps(needs), EVIDENCE_DIRECTORY=str(directory))
            passed = subprocess.run(['/bin/sh', '-ec', workflow_probe.gate_script()],
                                    env=environment, capture_output=True, timeout=10)
            self.assertEqual(0, passed.returncode, passed.stderr)
            (directory / 'b.json').unlink()
            failed = subprocess.run(['/bin/sh', '-ec', workflow_probe.gate_script()],
                                    env=environment, capture_output=True, timeout=10)
            self.assertNotEqual(0, failed.returncode)

    def test_observed_run_must_match_the_candidate_and_required_jobs(self):
        self.checker()
        from scripts.forgejo_runner import workflow_probe
        self.assertTrue(hasattr(workflow_probe, 'require_result'), 'Workflow result reconciliation is missing')
        commit = 'a' * 40
        tasks = [{'name': name, 'status': 'success', 'head_sha': commit}
                 for name in ('plan', 'matrix-a (a)', 'matrix-b (b)', 'required', 'gate')]
        observed = {'commit_sha': commit, 'status': 'success'}
        try:
            workflow_probe.require_result(observed, tasks, commit, 'complete')
        except RuntimeError as error:
            self.fail(str(error))
        for run, jobs in [(dict(observed, commit_sha='b' * 40), tasks),
                          (observed, tasks[:-1]),
                          (observed, [dict(row, status='skipped') if row['name'] == 'gate' else row
                                      for row in tasks]), (observed, tasks + [tasks[0]])]:
            with self.subTest(run=run, jobs=jobs), self.assertRaises(RuntimeError):
                workflow_probe.require_result(run, jobs, commit, 'complete')

    def test_negative_case_requires_gate_failure_after_successful_matrix_jobs(self):
        self.checker()
        from scripts.forgejo_runner import workflow_probe
        self.assertTrue(hasattr(workflow_probe, 'require_result'), 'Workflow result reconciliation is missing')
        commit = 'a' * 40
        tasks = [{'name': name, 'status': 'success', 'head_sha': commit}
                 for name in ('plan', 'matrix-a (a)', 'matrix-b (b)', 'required')]
        tasks.append({'name': 'gate', 'status': 'failure', 'head_sha': commit})
        observed = {'commit_sha': commit, 'status': 'failure'}
        try:
            workflow_probe.require_result(observed, tasks, commit, 'wrong-candidate')
        except RuntimeError as error:
            self.fail(str(error))
        failed = [dict(row, status='failure') if row['name'] == 'matrix-b (b)' else row for row in tasks]
        with self.assertRaises(RuntimeError):
            workflow_probe.require_result(observed, failed, commit, 'wrong-candidate')

    def test_cancelled_required_job_can_never_satisfy_a_gate(self):
        self.checker()
        from scripts.forgejo_runner import workflow_probe
        self.assertTrue(hasattr(workflow_probe, 'require_result'), 'Workflow result reconciliation is missing')
        commit = 'a' * 40
        observed = {'commit_sha': commit, 'status': 'cancelled'}
        tasks = [{'name': 'plan', 'status': 'success', 'head_sha': commit},
                 {'name': 'required', 'status': 'cancelled', 'head_sha': commit}]
        workflow_probe.require_result(observed, tasks, commit, 'cancelled')
        with self.assertRaises(RuntimeError):
            workflow_probe.require_result(observed, tasks + [
                {'name': 'gate', 'status': 'success', 'head_sha': commit}], commit, 'cancelled')

    def test_early_run_failure_is_not_a_finished_aggregate_gate(self):
        self.checker()
        from scripts.forgejo_runner import workflow_probe
        self.assertTrue(hasattr(workflow_probe, 'finished'), 'Workflow gate completion observation is missing')
        commit = 'a' * 40
        observed = {'commit_sha': commit, 'status': 'failure'}
        jobs = [{'name': 'matrix-b (b)', 'head_sha': commit, 'status': 'failure'}]
        self.assertFalse(workflow_probe.finished(observed, jobs, commit, 'wrong-candidate'))
        self.assertTrue(workflow_probe.finished(observed, jobs + [
            {'name': 'gate', 'head_sha': commit, 'status': 'failure'}], commit, 'wrong-candidate'))


if __name__ == '__main__':
    unittest.main()
