import collections
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / 'bench' / 'smp')]
import sharedpt as S
import smp


class SharedPageTableProgramTests(unittest.TestCase):
    def test_every_program_builds_into_a_qemu_image(self):
        with tempfile.TemporaryDirectory() as d:
            stub = smp.build_stub(pathlib.Path(d))
            for prog in S.generate():
                smp.build_image(prog, stub)     # asserts: bodies and regions inside the image, none overlapping
                self.assertEqual(prog['ncpu'], len(prog['cpus']))

    def test_lcg_regions_hide_only_the_holes(self):
        regions = S.lcg_regions(0x1000, 0x100, 7, [(0x20, 0x10)])
        self.assertEqual([(r['addr'], r.get('capture', True)) for r in regions], [('0x1000', True), ('0x1020', False), ('0x1030', True)])
        self.assertEqual(b''.join(bytes.fromhex(r['data']) for r in regions), smp.lcg_bytes(0x100, 7))


class RemapJudgeTests(unittest.TestCase):
    """remap_cpu holds one CPU's logged observations against the model's cache history."""
    def setUp(self):
        prog = S.remap(2)
        self.init = S.Regions.initial(prog)
        self.leaf = S.P.sys_leaf(0, 0, S.V)
        self.frames = {S.F1: self.init.q(S.F1 + 0x80), S.F2: self.init.q(S.F2 + 0x80)}
        self.w1, self.w2 = self.frames[S.F1], self.frames[S.F2]

    def run_ops(self, ops):
        pte = lambda r: (S.F1, S.F2)[r % 2] | S.LEAF
        newer = lambda r: {'pte': pte(r), 'frame': pte(r) & ~0xfff, 'f1': S.F1, 'f2': S.F2}
        tables = S.model_tables(self.init, [self.leaf])
        return S.remap_cpu(len(ops) - 1, ops, self.frames, tables, self.leaf, S.V + 0x80, S.V + 0x200, newer=newer)

    def ops(self, pre, post_word=None, mode=0):
        first = {'mode': 2, 'pre': [], 'post': (self.w1, 1)}
        return [first, {'mode': mode, 'pre': pre, 'post': (post_word or self.w2, 2)}]

    def test_stale_then_new_frame_is_a_permitted_history(self):
        hist = self.run_ops(self.ops([(self.w1, 1), (self.w1, 1)]))
        self.assertEqual(hist['pre-INVLPG read: old frame (stale translation)'], 2)
        hist = self.run_ops(self.ops([(self.w1, 1), (self.w2, 2)]))                  # a fresh walk after a stale hit
        self.assertEqual(hist['pre-INVLPG read: new frame'], 1)

    def test_old_after_new_before_invlpg_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_ops(self.ops([(self.w2, 2), (self.w1, 1)]))

    def test_old_frame_after_invlpg_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_ops(self.ops([(self.w1, 1)], post_word=self.w1))
        with self.assertRaises(ValueError):          # a store after INVLPG that went to the old frame
            self.run_ops([{'mode': 2, 'pre': [], 'post': (self.w1, 1)}, {'mode': 2, 'pre': [], 'post': (self.w2, 1)}])

    def test_store_with_no_destination_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_ops(self.ops([(self.w1, 0)]))

    def test_invlpg_before_the_remap_is_seen_forces_the_new_frame(self):
        self.run_ops(self.ops([(self.w2, 2)], mode=1))
        with self.assertRaises(ValueError):
            self.run_ops(self.ops([(self.w1, 1)], mode=1))


if __name__ == '__main__':
    unittest.main()
