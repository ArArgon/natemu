"""sampler_check_judge on synthetic runs of the validity program (two runs of its seven marked phases, the port's stop
and start in phase 5): a good run passes, and each check fails on a run broken its one way."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import shutil, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sample_report as sr
import sampler_check_judge as j
from test_sample_report import el0, hostrec, header, write_samples, USER64, KERNEL64, F

S = F // 10                    # a tenth of a second
LOOP, CALLEE, CROSS, CHASE = 0x40_1000, 0x40_2100, 0x40_3000, 0x40_4000


def run_records(base, broken=()):
    """One run from `base`: init's mark (the run's number), then phases 1-7 of 1 s each; the port's stop 0.5 s into phase 5
    and its start 0.5 s into phase 6; init's stop after phase 7. One sample every 0.1 s of each window."""
    t = lambda s: base + round(s * 10) * S
    host = [hostrec(sr.MARK, t(0), value1=1 if base < 150 * F else 2)]
    ring = []
    for k in range(1, 8):
        host.append(hostrec(sr.MARK, t(k), value1=k))
    for i in range(10):
        off = 'a' in broken and i < 2
        ring.append(el0(t(1 + i / 10) + S // 2, 0xffff_ffff_8100_0000 if off else LOOP, sr.BLOCK, KERNEL64 if off else USER64, frame=1))
        if i % 2: ring.append(el0(t(2 + i / 10) + S // 2, CALLEE, sr.BLOCK, USER64, frame=2))
        else:
            call = sr.CALL_YES if 'b' in broken and i % 4 == 0 else sr.CALL_NO
            ring.append(el0(t(2 + i / 10) + S // 2, CALLEE, sr.DISPATCH, USER64, frame=2, flags=call << sr.CALL_SHIFT))
        rip = LOOP if 'c' in broken and i % 2 else CROSS
        ring.append(el0(t(3 + i / 10) + S // 2, rip, sr.PROBE if i % 3 == 0 else sr.BLOCK, USER64, frame=3, at=8))
        ring.append(el0(t(4 + i / 10) + S // 2, CHASE, sr.BLOCK, USER64, frame=4, at=4 * (i % 3)))
        if i < 5: ring.append(el0(t(5 + i / 10) + S // 2, LOOP, sr.BLOCK, USER64, frame=1))
        if 'e3' not in broken:
            if i >= 5: ring.append(el0(t(6 + i / 10) + S // 2, LOOP, sr.BLOCK, USER64, frame=1))
            ring.append(el0(t(7 + i / 10) + S // 2, LOOP, sr.BLOCK, USER64, frame=1))
    stop, start = t(5.5), t(6.5)
    host += [hostrec(sr.TOGGLE, stop, value1=1, value2=0, value3=1, value4=0),
             hostrec(sr.TOGGLE, start, value1=2, value2=1, value3=1, value4=0),
             hostrec(sr.PASS, stop - 100, value1=stop + 200, value2=0, value3=1, value4=1),
             hostrec(sr.TOGGLE, t(8), value1=3, value2=0, value3=1, value4=0)]
    ring.append(el0(stop + 150, LOOP, sr.BLOCK, USER64, frame=1))            # rung by the pass in flight at the stop
    if 'stray' in broken: ring.append(el0(stop + 1000, LOOP, sr.BLOCK, USER64, frame=1))
    if 'marks' in broken: host = [h for h in host if not (h[2] >> 60 == sr.MARK and h[1] == 4)]
    return ring, host


def write(path, broken=(), broken2=()):
    r1, h1 = run_records(100 * F, broken)
    r2, h2 = run_records(200 * F, broken2)
    ring = sorted(r1 + r2)
    write_samples(path, [header(1), hostrec(sr.CLOCK, 99 * F), hostrec(sr.TOGGLE, 99 * F, value2=1, value4=sr.RUN),
                         hostrec(sr.VCPU, 0, value3=0, value4=len(ring)), *ring, *sorted(h1 + h2), hostrec(sr.CLOCK, 300 * F)])


class Judge(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.d, True)

    def judge(self, broken=(), broken2=()):
        write(self.d / 'sc.samples', broken, broken2)
        return j.judge(self.d / 'sc')

    def test_a_good_run_passes(self):
        r = self.judge()
        self.assertEqual(r['problems'], [])
        self.assertEqual(r['marks'], [1, 1, 2, 3, 4, 5, 6, 7, 2, 1, 2, 3, 4, 5, 6, 7])
        one = r['runs'][1]
        self.assertEqual((one['a']['share'], one['a']['user']), (100.0, True))
        self.assertEqual((one['b']['callee'], one['b']['slot_samples'], one['b']['callee_block_samples']), (hex(CALLEE), 5, 5))
        self.assertEqual((one['c']['probe'], one['c']['share']), (4, 100.0))
        self.assertEqual(one['d']['at'], {0: 4, 4: 3, 8: 3})
        self.assertEqual({k: one['e'][k] for k in ('e1_on_loop', 'e2', 'e2_in_flight', 'e2_stray', 'e3_on_loop')},
                         dict(e1_on_loop=5, e2=1, e2_in_flight=1, e2_stray=0, e3_on_loop=15))

    def test_a_loop_off_its_block_fails(self):
        self.assertEqual(self.judge(broken2=('a',))['problems'], ['run 2 a: 80.0% of 10 EL0 samples on one user block, want 95.0% or more'])

    def test_a_callee_slot_marked_a_return_fails(self):
        self.assertEqual(self.judge(('b',))['problems'], ["run 1 b: 3 of the callee's slot samples marked a return"])

    def test_probes_charged_elsewhere_fail(self):
        self.assertEqual(self.judge(('c',))['problems'], ["run 1 c: 50.0% of probe samples charged to the loop's block, want 95.0%"])

    def test_a_ring_while_stopped_fails(self):
        self.assertEqual(self.judge(('stray',))['problems'], ['run 1 e: 1 samples rung in the stopped window after the pass in flight'])

    def test_a_sampler_that_never_starts_again_fails(self):
        self.assertEqual(self.judge(('e3',))['problems'],
                         ['run 1 e: no EL0 sample on the loop after the start (the sampler did not start again)'])

    def test_marks_out_of_order_fail(self):
        self.assertIn('marks 1-7 in order 1 times, want 2', self.judge(('marks',))['problems'][0])


if __name__ == '__main__':
    unittest.main()
