"""sampler_check_judge on synthetic runs of the validity program (two runs of its seven marked phases, the port's stop
and start in phase 5): a good run passes, and each check fails on a run broken its one way."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import shutil, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sample_report as sr
import sampler_check_judge as j
from test_sample_report import el0, hostrec, header, write_samples, write_objects, obj_record, code_object, USER64, KERNEL64, F

S = F // 10                    # a tenth of a second
LOOP, CALLEE, RETURN, CROSS, CHASE = 0x40_1000, 0x40_2100, 0x40_2014, 0x40_3000, 0x40_4000


def run_records(base, broken=()):
    """One run from `base`: init's mark (the run's number), then phases 1-7 of 1 s each; the port's stop 0.5 s into phase 5
    and its start 0.5 s into phase 6; init's stop after phase 7. One sample every 0.1 s of each window."""
    t = lambda s: base + round(s * 10) * S
    host = [hostrec(sr.MARK, t(0), value1=(1 if base < 150 * F else 2) | (0x80 if 'old-init' not in broken else 0))]
    ring = []
    for k in range(1, 8):
        host.append(hostrec(sr.MARK, t(k), value1=k))
    for i in range(10):
        off = 'a' in broken and i < 2
        at = 64 if 'far' in broken else 4 * (i % 4)        # the loop's object has 16 bytes of code
        ring.append(el0(t(1 + i / 10) + S // 2, 0xffff_ffff_8100_0000 if off else LOOP, sr.BLOCK, KERNEL64 if off else USER64,
                        frame=1, at=at))
        # Phase b: the callee's block, the indirect call to it (not a call before the target), its return (a call before).
        if i % 2: ring.append(el0(t(2 + i / 10) + S // 2, CALLEE, sr.BLOCK, USER64, frame=2))
        elif i % 4 == 0 or 'noret' in broken:
            call = sr.CALL_YES if 'b' in broken and i == 0 else sr.CALL_NO
            target = CALLEE if 'other' not in broken or i != 0 else CALLEE + 0x40
            ring.append(el0(t(2 + i / 10) + S // 2, target, sr.DISPATCH, USER64, frame=2, flags=call << sr.CALL_SHIFT))
        else:
            ring.append(el0(t(2 + i / 10) + S // 2, RETURN, sr.DISPATCH, USER64, frame=2, flags=sr.CALL_YES << sr.CALL_SHIFT))
        rip = LOOP if 'c' in broken and i % 2 else CROSS
        place = sr.PROBE if i % 3 == 0 and 'noprobe' not in broken else sr.BLOCK
        if 'c2' in broken: rip, place = (LOOP, sr.PROBE) if i < 7 else (CROSS, sr.BLOCK)   # probes outnumber the loop's words
        ring.append(el0(t(3 + i / 10) + S // 2, rip, place, USER64, frame=3, at=8))
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
    if 'pass' in broken: host.append(hostrec(sr.PASS, stop + 2000, value1=stop + 2100, value2=1, value3=1, value4=1))
    if 'halted' in broken: host.append(hostrec(sr.HALTED, stop + 3000, value1=1, value3=0))
    if 'marks' in broken: host = [h for h in host if not (h[2] >> 60 == sr.MARK and h[1] == 4)]
    return ring, host


def write(path, broken=(), broken2=()):
    path.with_suffix('.objects').unlink(missing_ok=True)
    if 'noobj' not in broken:
        write_objects(path.with_suffix('.objects'), [obj_record(LOOP, 0x1000, USER64, code_object(LOOP, bytes(range(16))), b'\x90' * 12)])
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
        self.assertEqual(r['marks'], [1, 2, 3, 4, 5, 6, 7] * 2)                  # init's 0x81 and 0x82 left out
        one = r['runs'][1]
        self.assertEqual((one['a']['share'], one['a']['user'], one['a']['located'], one['a']['words']), (100.0, True, 10, 10))
        self.assertEqual((one['e']['e2_passes_rung'], one['e']['e2_halted']), (0, 0))
        self.assertEqual((one['b']['callee'], one['b']['slot_samples'], one['b']['returns'], one['b']['callee_block_samples']),
                         (hex(CALLEE), 3, 2, 5))
        self.assertEqual(one['b']['dispatch_calls'], {'not_call': 3, 'return': 2})
        self.assertEqual((one['c']['probe'], one['c']['share']), (4, 100.0))
        self.assertEqual(one['d']['at'], {0: 4, 4: 3, 8: 3})
        self.assertEqual({k: one['e'][k] for k in ('e1_on_loop', 'e2', 'e2_in_flight', 'e2_stray', 'e3_on_loop')},
                         dict(e1_on_loop=5, e2=1, e2_in_flight=1, e2_stray=0, e3_on_loop=15))

    def test_a_loop_off_its_block_fails(self):
        self.assertEqual(self.judge(broken2=('a',))['problems'], ['run 2 a: 80.0% of 10 EL0 samples on one user block, want 95.0% or more'])

    def test_a_callee_slot_marked_a_return_fails(self):
        self.assertEqual(self.judge(('b',))['problems'], ["run 1 b: 1 of the callee's slot samples marked a return"])

    def test_an_indirect_call_to_two_targets_fails(self):
        self.assertEqual(self.judge(('other',))['problems'], ["run 1 b: 66.67% of the indirect calls' samples to the callee, want 90.0%"])

    def test_no_return_sample_fails(self):
        self.assertEqual(self.judge(('noret',))['problems'], ['run 1 b: no dispatcher sample of the callee\'s return (call test "call before")'])

    def test_an_older_init_s_run_marks_are_skipped(self):
        r = self.judge(('old-init',), ('old-init',))
        self.assertEqual((r['problems'], r['marks']), ([], [1, 1, 2, 3, 4, 5, 6, 7, 2, 1, 2, 3, 4, 5, 6, 7]))

    def test_probes_charged_elsewhere_fail(self):
        self.assertEqual(self.judge(('c',))['problems'], ["c: 75.0% of probe samples charged to the loop's block, want 95.0%"])

    def test_a_ring_while_stopped_fails(self):
        self.assertEqual(self.judge(('stray',))['problems'], ['run 1 e: 1 samples rung in the stopped window after the pass in flight'])

    def test_a_sampler_that_never_starts_again_fails(self):
        self.assertEqual(self.judge(('e3',))['problems'],
                         ['run 1 e: no EL0 sample on the loop after the start (the sampler did not start again)'])

    def test_the_loops_samples_must_locate_to_its_words(self):
        self.assertEqual(self.judge(('far',))['problems'], ["run 1 a: 0 of the loop's 10 samples locate to a word of its object, want 95.0%"])
        self.assertEqual(self.judge(('noobj',))['problems'], ['run 1 a: no objects file', 'run 2 a: no objects file'])

    def test_probes_are_judged_over_both_runs(self):
        r = self.judge(('noprobe',))
        self.assertEqual((r['problems'], r['runs'][1]['c']['probe'], r['c']['probe']), ([], 0, 4))
        self.assertEqual(self.judge(('noprobe',), ('noprobe',))['problems'], ['c: no probe sample in either run'])

    def test_the_loops_block_is_its_own_words_not_its_probes(self):
        self.assertEqual(self.judge(('c2',))['problems'], ["c: 36.36% of probe samples charged to the loop's block, want 95.0%"])

    def test_a_pass_that_rang_or_a_halted_sample_while_stopped_fails(self):
        self.assertEqual(self.judge(('pass',))['problems'], ['run 1 e: 1 ringing passes rang bells in the stopped window'])
        self.assertEqual(self.judge(('halted',))['problems'], ['run 1 e: 1 halted samples in the stopped window'])

    def test_marks_out_of_order_fail(self):
        self.assertIn('marks 1-7 in order 1 times, want 2', self.judge(('marks',))['problems'][0])


if __name__ == '__main__':
    unittest.main()
