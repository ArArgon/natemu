"""repmicro_report on synthetic serial output and reports: parsing, the judge and the kill-test's verdicts."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import json, shutil, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import repmicro_report as rr

ALL = rr.ROWS64 + rr.ROWS32


def serial(runs=(1, 2), skip=(), ns=lambda r: 100.0, sums=lambda run, r: 'ab' * 8):
    out = ['NATEMU-BENCH-START bench=repmicro']
    for run in runs:
        for r in ALL:
            if (run, r) in skip: continue
            bits = 32 if r in rr.ROWS32 else 64
            out.append(f'NATEMU-REPMICRO run={run} bits={bits} row={r} bytes=64 iters=1000 ns_med={ns(r):.2f} '
                       f'ns_min={ns(r) - 1:.2f} ns_max={ns(r) + 1:.2f} sum={sums(run, r)}')
    return '\n'.join(out) + '\n'


def rep(values):
    return {'rows': {r: values.get(r, 10.0) for r in ALL}}


class Report(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.d, True)

    def write(self, text, verified=True):
        (self.d / 'repmicro.serial').write_text(text)
        (self.d / 'bench.jsonl').write_text(json.dumps({'verified': verified}) + '\n')

    def test_a_good_run_reports_run_2s_medians(self):
        self.write(serial(ns=lambda r: 50.0 if r == 'movsb-4096' else 7.0))
        self.assertEqual(rr.report(self.d), 0)
        r = json.loads((self.d / 'repmicro.report.json').read_text())
        self.assertEqual(r['rows']['movsb-4096'], 50.0); self.assertEqual(r['rows']['movsd-4'], 7.0)
        self.assertEqual(r['problems'], [])

    def test_missing_rows_unverified_runs_and_differing_checksums_fail(self):
        self.write(serial(skip={(2, 'movsd-12')}))
        self.assertEqual(rr.report(self.d), 1)
        self.assertIn('run 2: rows missing: movsd-12', json.loads((self.d / 'repmicro.report.json').read_text())['problems'])
        self.write(serial(), verified=False)
        self.assertEqual(rr.report(self.d), 1)
        self.write(serial(sums=lambda run, r: ('cd' if run == 2 and r == 'movsq-8' else 'ab') * 8))
        self.assertEqual(rr.report(self.d), 1)
        self.assertTrue(any('movsq-8: checksums differ' in p
                            for p in json.loads((self.d / 'repmicro.report.json').read_text())['problems']))


class Compare(unittest.TestCase):
    BEFORE = [rep({'movsb-4096': 1000.0, 'movsq-4096': 160.0, 'movsb-1': 10.0}),
              rep({'movsb-4096': 1050.0, 'movsq-4096': 170.0, 'movsb-1': 11.0}),
              rep({'movsb-4096': 1100.0, 'movsq-4096': 165.0, 'movsb-1': 10.5})]

    def test_kept_when_the_hot_rows_are_fast_enough_and_nothing_regresses(self):
        after = [rep({'movsb-4096': 100.0, 'movsq-4096': 90.0, 'movsb-1': 11.9})] * 3     # 1-word cost within 11 + 1
        killed, rows = rr.compare(self.BEFORE, after)
        self.assertFalse(killed, rows)
        by = {r['row']: r for r in rows}
        self.assertAlmostEqual(by['movsb-4096']['speedup'], 10.5); self.assertEqual(by['movsb-1']['bound'], 12.0)

    def test_killed_by_a_slow_hot_row(self):
        after = [rep({'movsb-4096': 300.0, 'movsq-4096': 90.0, 'movsb-1': 10.0})] * 3      # 3.5x < 4x
        killed, rows = rr.compare(self.BEFORE, after)
        self.assertTrue(killed)
        self.assertTrue({r['row']: r for r in rows}['movsb-4096']['verdict'].startswith('killed'))
        killed, _ = rr.compare(self.BEFORE, [rep({'movsb-4096': 100.0, 'movsq-4096': 150.0, 'movsb-1': 10.0})] * 3)
        self.assertTrue(killed)                                                                # movsq 1.1x < 1.2x

    def test_killed_by_a_regressed_control_row(self):
        after = [rep({'movsb-4096': 100.0, 'movsq-4096': 90.0, 'movsb-1': 12.1})] * 3      # beyond 11 + (11 - 10)
        killed, rows = rr.compare(self.BEFORE, after)
        self.assertTrue(killed)
        self.assertTrue({r['row']: r for r in rows}['movsb-1']['verdict'].startswith('regressed'))

    def test_a_missing_row_kills(self):
        after = [{'rows': {r: 1.0 for r in ALL if r != 'movsd-4096'}}]
        killed, rows = rr.compare(self.BEFORE, after)
        self.assertTrue(killed); self.assertEqual({r['row']: r for r in rows}['movsd-4096']['verdict'], 'missing')


if __name__ == '__main__':
    unittest.main()
