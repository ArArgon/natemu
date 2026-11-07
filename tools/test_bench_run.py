"""Timing regressions for the real benchmark CLI loop, without a VM or wall-clock sleeps."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bench_run', Path(__file__).parent / 'bench/run.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

START = 'NATEMU-BENCH-START bench=locks jobs=1 cpus=1\n'
COLD_END = 'NATEMU-BENCH-END bench=locks jobs=1 rc=0 guest_s=0.1\nNATEMU-BENCH-VERIFY ok\n'
START2 = 'NATEMU-BENCH-START2 bench=locks jobs=1 cpus=1\n'
WARM_END = 'NATEMU-BENCH-END2 bench=locks jobs=1 rc=0 guest_s=0.1\nNATEMU-BENCH-VERIFY2 ok\n'


class TimingTests(unittest.TestCase):
    def run_boot(self, steps, query_delay=0, query_data=""):
        serial_path = []
        clock = [100.0]
        queries = []

        def foreign(_):
            if not queries:
                clock[0] += query_delay
                with serial_path[0].open('a') as f: f.write(query_data)
            queries.append(clock[0])
            return set()

        class Process:
            pid = 123
            returncode = None

            def __init__(self, cmd, **kw):
                self.serial = Path(cmd[cmd.index('--serial') + 1])
                serial_path.append(self.serial)
                self.steps = iter(steps)
                kw['stdout'].close(); kw['stderr'].close()

            def poll(self):
                if self.returncode is None:
                    rc, data, delay = next(self.steps)
                    clock[0] += delay
                    with self.serial.open('a') as f: f.write(data)
                    self.returncode = rc
                return self.returncode

        with tempfile.TemporaryDirectory() as tmp:
            argv = ['run.py', '--out', tmp, '--name', 'test', '--emu', 'natemu', '--vcpus', '1',
                    '--bench', 'locks', '--kernel', 'unused', '--initrd', 'unused', '--fexc', 'unused']
            with patch.object(bench.sys, 'argv', argv), patch.object(bench.subprocess, 'Popen', Process), \
                 patch.object(bench, 'foreign', foreign), patch.object(bench.time, 'monotonic', lambda: clock[0]), \
                 patch.object(bench.time, 'sleep', lambda t: clock.__setitem__(0, clock[0] + t)), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as exited: bench.main()
            self.assertEqual(exited.exception.code, 0)
            return json.loads((Path(tmp) / 'bench.jsonl').read_text()), queries

    def test_process_query_delay_is_not_used_as_marker_time(self):
        rec, queries = self.run_boot([(None, '', 0), (None, COLD_END + START2, 0), (0, WARM_END, 0.1)], query_delay=2, query_data=START)
        self.assertEqual(rec['wall_s'], 0.05)   # old pre-query timestamp inflated this to 2.05 seconds
        start = rec['timing']['markers']['START']
        self.assertGreaterEqual(start['read_start_s'], queries[0])
        self.assertGreaterEqual(start['upper_s'], start['read_start_s'])
        self.assertEqual(start['lower_s'], 100)
        self.assertGreater(rec['wall_bounds_s'][1], 2)   # process-query uncertainty remains visible

    def test_partial_marker_and_phase_wait_for_newline(self):
        rec, _ = self.run_boot([(None, START[:-1], 0),
                               (None, '\nNATEMU-BENCH-PHASE bench=locks jobs=1 name=work rc=0 guest_s=0.1', 0),
                               (0, '\n' + COLD_END + START2 + WARM_END, 0.3)])
        self.assertAlmostEqual(rec['timing']['markers']['START']['upper_s'], 100.1)
        self.assertAlmostEqual(rec['timing']['phases'][0]['upper_s'], 100.4)
        self.assertEqual(rec['phases'][0]['wall_s'], 0.3)
        self.assertEqual(rec['phases'][0]['name'], 'work')
        self.assertIsNotNone(rec['phases'][0]['wall_bounds_s'])

    def test_exit_drain_populates_both_runs(self):
        rec, queries = self.run_boot([(0, START + COLD_END + START2 + WARM_END, 0.2)])
        self.assertFalse(queries)
        self.assertTrue(rec['reached'] and rec['verified'])
        self.assertEqual((rec['wall_s'], rec['wall2_s']), (0.0, 0.0))
        self.assertEqual(set(rec['timing']['markers']), {'START', 'END', 'START2', 'END2'})
        self.assertLess(rec['wall_bounds_s'][0], 0)   # same-read markers have overlapping uncertainty intervals
        self.assertGreater(rec['wall_bounds_s'][1], 0)


if __name__ == '__main__':
    unittest.main()
