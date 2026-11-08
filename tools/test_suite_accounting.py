"""A worker exception or failed result write must not become a passing suite."""
import contextlib
import io
import os
import pathlib
import tempfile
import types
import unittest
from unittest.mock import patch

import run_suite as suite


class Accounting(unittest.TestCase):
    def run_main(self, broken):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fexc = root / 'fexc'
            fexc.touch()
            out = root / 'out'
            step = suite.Step('synthetic', ['never-executed'])
            owners = []
            initialize = suite.Suite.__init__

            def init(owner, args):
                initialize(owner, args)
                owners.append(owner)

            def run(owner, selected):
                if broken:
                    raise RuntimeError('synthetic worker failure')
                return True, 0, 0.0, owner.O / 'logs/synthetic.log', ''

            with patch.object(suite, 'RAW', root), patch.object(suite, 'steps', return_value=[step]), \
                 patch.object(suite.Suite, 'run', run), \
                 patch.object(suite.Suite, '__init__', init), \
                 patch.object(suite.subprocess, 'run', return_value=types.SimpleNamespace(stdout='1')), \
                 patch('sys.argv', ['run_suite', '--fexc', str(fexc), '--out', str(out)]), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    suite.main()
            for owner in owners:
                owner.results.close()
                os.close(owner.vmlock)
            return result.exception.code, (out / 'results.txt').read_text()

    def test_worker_exception_fails_suite(self):
        code, rows = self.run_main(True)
        self.assertEqual(code, 1)
        self.assertIn('0/1 passed; FAILED: synthetic', rows)

    def test_completed_worker_passes(self):
        code, rows = self.run_main(False)
        self.assertEqual(code, 0)
        self.assertIn('1/1 passed', rows)

    def test_failed_write_is_not_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(suite, 'RAW', pathlib.Path(directory)):
                owner = suite.Suite(types.SimpleNamespace(out=pathlib.Path(directory)))
            owner.results.close()
            os.close(owner.vmlock)
            with patch.object(owner, 'results') as results:
                results.write.side_effect = OSError('synthetic disk full')
                with self.assertRaises(OSError):
                    owner.record(suite.Step('synthetic', []), (True, 0, 0, owner.O / 'logs/synthetic.log', ''))
            self.assertEqual(owner.finished, set())

    def test_print_failure_preserves_failed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(suite, 'RAW', pathlib.Path(directory)):
                owner = suite.Suite(types.SimpleNamespace(out=pathlib.Path(directory)))
            try:
                with patch('builtins.print', side_effect=BrokenPipeError('synthetic output failure')):
                    with self.assertRaises(BrokenPipeError):
                        owner.record(suite.Step('synthetic', []), (False, 1, 0, owner.O / 'logs/synthetic.log', ''))
                self.assertEqual(owner.finished, {'synthetic'})
                self.assertEqual(owner.failed, ['synthetic'])
            finally:
                owner.results.close()
                os.close(owner.vmlock)
