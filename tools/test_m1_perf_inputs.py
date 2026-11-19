"""run_m1_perf's input options reach the runner command lines `plan` prints (no VM); without them the natemu-raw paths
stay the defaults."""
import os
import pathlib
import subprocess
import sys
import unittest

TOOLS = pathlib.Path(__file__).resolve().parent


def plan(*args, raw='/rawroot'):
    env = dict(os.environ, NATEMU_RAW_ROOT=raw)
    r = subprocess.run([sys.executable, TOOLS / 'run_m1_perf.py', 'plan', *args], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    runs = {l.split()[1]: l for l in lines if l[:2].strip().isdigit()}
    prep = next(l for l in lines if l.startswith('Matrix inputs prepared once'))
    return runs, prep


class M1PerfInputs(unittest.TestCase):
    def test_overrides_reach_the_runner_command_lines(self):
        runs, prep = plan('--baseline-build', '/b/base', '--final-build', '/b/final', '--fexc', '/b/fexc',
                          '--fio-prep', '/i/fio-prep', '--block-inputs', '/i/block', '--io5-inputs', '/i/io5',
                          '--inputs', '/o/matrix', '--dir', '/o', '--lock', '/o/vm.lock')
        self.assertIn('--fio-prep /i/fio-prep --block-inputs /i/block --out /o/matrix', prep)
        self.assertNotIn('/rawroot', ' '.join(runs.values()) + prep)
        base, final = runs['A-base-raw-0'], runs['A-final-raw-0']
        self.assertIn('--executable /b/base/release/natemu --runtime /b/base/aarch64-unknown-none/release/natemu-runtime', base)
        self.assertIn('--executable /b/final/release/natemu --runtime /b/final/aarch64-unknown-none/release/natemu-runtime', final)
        for line in (base, final): self.assertIn('--fexc /b/fexc --inputs /o/matrix', line)
        io5 = runs['D-io5-overlay-0']
        self.assertIn('--inputs /i/io5', io5)
        self.assertIn('--natemu /b/final/release/natemu', io5)
        self.assertIn('--fexc /b/fexc', io5)

    def test_the_old_option_names_still_work(self):
        runs, _ = plan('--baseline-dir', '/b/base', '--final-dir', '/b/final')
        self.assertIn('--executable /b/base/release/natemu', runs['A-base-raw-0'])
        self.assertIn('--executable /b/final/release/natemu', runs['A-final-raw-0'])

    def test_defaults_are_the_raw_root_paths(self):
        runs, prep = plan()
        self.assertIn('--fio-prep /rawroot/io-benchmark-prep-20261108-01 --block-inputs /rawroot/io3-block-20261108-01/inputs-01 '
                      '--out /rawroot/m1-perf/inputs', prep)
        self.assertIn('--executable /rawroot/m1-perf/baseline/release/natemu', runs['A-base-raw-0'])
        self.assertIn('--executable /rawroot/m1-perf/final/release/natemu', runs['A-final-raw-0'])
        self.assertIn('--fexc /rawroot/batch1-fexc-20261116-03/obj/fexc', runs['A-final-raw-0'])
        self.assertIn('--inputs /rawroot/io5-inputs-20261113-01', runs['D-io5-raw-0'])

    def test_vm_lock_skipped_when_the_caller_holds_it(self):
        """Under natemu-test (or run_suite) the VM lock is already held: taking it again would wait forever."""
        sys.path.insert(0, str(TOOLS))
        import run_m1_perf
        lock = pathlib.Path(self.id().replace('.', '_') + '.lock')
        try:
            os.environ['NATEMU_VM_LOCK_HELD'] = '1'
            with run_m1_perf.vm_lock(lock): pass
            self.assertFalse(lock.exists())
        finally:
            os.environ.pop('NATEMU_VM_LOCK_HELD', None); lock.unlink(missing_ok=True)


if __name__ == '__main__': unittest.main()



class InterruptedPreparation(unittest.TestCase):
    def test_an_unfinished_matrix_preparation_is_started_again(self):
        """Review of Task 4: run_io_matrix.py prepare refuses an existing --out; the resume folder is fixed, so a
        preparation interrupted before inputs.json was written would block the case for good."""
        import tempfile, types
        from unittest import mock
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import run_m1_perf
        with tempfile.TemporaryDirectory() as t:
            inp = pathlib.Path(t) / 'matrix-inputs'; inp.mkdir(); (inp / 'partial').write_text('x')
            args = types.SimpleNamespace(inputs=inp, fio_prep='F', block_inputs='B')
            seen = {}
            def fake(cmd, check):
                seen['existed'] = inp.exists(); seen['cmd'] = cmd
            with mock.patch.object(run_m1_perf.subprocess, 'run', fake):
                run_m1_perf.ensure_inputs(args)
            self.assertFalse(seen['existed'])



class Limit(unittest.TestCase):
    def test_limit_runs_one_and_says_more_are_left(self):
        import tempfile, types
        from unittest import mock
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import run_m1_perf
        plan = [dict(id=f'A-{i}', group='A', setup='final-raw', rep=i, kind='matrix') for i in range(3)]
        runs = []
        def one(args, spec, dest): runs.append(spec['id']); return dict(id=spec['id'], ok=True, problems=[], seconds=1,
                                                                        others_before=[], others_after=[])
        with tempfile.TemporaryDirectory() as t, mock.patch.object(run_m1_perf, 'one_run', one), \
                mock.patch.object(run_m1_perf, 'ensure_inputs', lambda a: None), \
                mock.patch.object(run_m1_perf, 'env_record', lambda a, f: None), \
                mock.patch.object(run_m1_perf, 'analyze', lambda d: 0):
            args = types.SimpleNamespace(retry_failed=True, limit=1)
            self.assertEqual(run_m1_perf.execute(args, t, plan), run_m1_perf.INCOMPLETE)
            self.assertEqual(run_m1_perf.execute(args, t, plan), run_m1_perf.INCOMPLETE)
            self.assertEqual(run_m1_perf.execute(args, t, plan), 0)          # the last one: the verdict
            self.assertEqual(runs, ['A-0', 'A-1', 'A-2'])
            self.assertEqual(run_m1_perf.execute(args, t, plan), 0)          # nothing left: the verdict again
