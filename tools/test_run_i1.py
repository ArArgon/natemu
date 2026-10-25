"""Check the new worker boundary without starting Hypervisor.framework."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).with_name('run_i1.py').resolve()


class WorkerTests(unittest.TestCase):
    def run_job(self, script, **extra):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        job = {'argv': [sys.executable, '-c', script], 'cwd': str(root),
               'environment': {'CARGO_BUILD_JOBS': '2', 'RUSTUP_TOOLCHAIN': '1.97.0'}, **extra}
        path = root / 'job.json'
        path.write_text(json.dumps(job))
        env = dict(os.environ, RUSTFLAGS='bad-inherited-flag', CARGO_TARGET_DIR='/unwanted/path',
                   CARGO_BUILD_JOBS='500', RUSTUP_TOOLCHAIN='wrong', RUSTC_WRAPPER='unwanted')
        result = subprocess.run([sys.executable, '-B', str(RUNNER), '--worker', str(path)],
                                env=env, capture_output=True, text=True, timeout=5)
        return root, result

    def test_build_overrides_are_removed_or_pinned(self):
        root, result = self.run_job("import os,json; print(json.dumps({k:os.environ.get(k) for k in ['RUSTFLAGS','CARGO_TARGET_DIR','CARGO_BUILD_JOBS','RUSTUP_TOOLCHAIN','RUSTC_WRAPPER']}))")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((root / 'command.stdout').read_text()),
                         {'RUSTFLAGS': None, 'CARGO_TARGET_DIR': None, 'CARGO_BUILD_JOBS': '2',
                          'RUSTUP_TOOLCHAIN': '1.97.0', 'RUSTC_WRAPPER': None})

    def test_failure_preserves_output_and_does_not_pass(self):
        root, result = self.run_job("print('partial output'); raise SystemExit(7)")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((root / 'command.stdout').read_text(), 'partial output\n')
        self.assertNotIn('PASS', result.stdout)

    def test_expected_rejection_requires_matching_diagnostic(self):
        _, result = self.run_job("import sys; print('different failure', file=sys.stderr); raise SystemExit(1)",
                                 expected_exit=1, expected_error='I1 admits exactly one vCPU')
        self.assertNotEqual(result.returncode, 0)

    def test_oracle_failure_cannot_pass_worker(self):
        _, result = self.run_job("print('{}')", oracle=str(RUNNER.with_name('check_i1.py')))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()
