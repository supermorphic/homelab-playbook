import importlib
from pathlib import Path
import tempfile
import threading
import unittest


class ProcessProbeTests(unittest.TestCase):
    def module(self):
        return importlib.import_module('scripts.forgejo_runner.process_probe')

    def test_a_retained_child_blocks_reaping_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '123').mkdir()
            with self.assertRaisesRegex(RuntimeError, 'not reaped'):
                self.module().require_reaped([123], root, timeout=0)

    def test_child_exit_must_disappear_from_the_process_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / '123'
            child.mkdir()
            timer = threading.Timer(0.05, child.rmdir)
            timer.start()
            try:
                self.module().require_reaped([123], root, timeout=1)
            finally:
                timer.join()


if __name__ == '__main__':
    unittest.main()
