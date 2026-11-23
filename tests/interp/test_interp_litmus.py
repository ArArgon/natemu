"""The litmus patterns between an interpreting and a translated vCPU without a VM (interp_litmus.py): the outcome classes
the model gives, the programs' forms, the machine descriptions, and how a run's results are read and judged."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import collections
import re
import unittest

import interp_cases as G
import interp_litmus as L
from models import ordering_model as model

# The forms A's programs may use: the interpreter's first version (plan section 1).
FIRST_VERSION = {'mov', 'shl', 'shr', 'xor', 'and', 'cmp', 'jae', 'jmp', 'dec', 'jns', 'add', 'inc', 'jbe'}


def sections(text):
    """The description's shared lines and its `cpu` sections, each a list of (word, rest)."""
    out = [[]]
    for line in text.splitlines():
        if line == 'cpu':
            out.append([])
            continue
        word, _, rest = line.partition(' ')
        out[-1].append((word, rest))
    return out[0], out[1:]


def fake_run(cell, k, res, done=None, stops=(0, 0)):
    """A driver output of `cell` with result areas `res` ({'A': bytes, 'B': bytes}) and done words."""
    done = done or {'A': k, 'B': k}
    sync = bytearray(L.SYNC_BYTES)
    for s in 'AB':
        sync[L.DONE[s]:L.DONE[s] + 4] = done[s].to_bytes(4, 'little')
    cpus = [{'stop': 'reached' if stops[i] == 0 else 'resident', 'reason': 'x',
             'machine': {'stop': stops[i], 'window0': res[s].hex(), **({'window1': bytes(sync).hex()} if s == 'B' else {})}}
            for i, s in enumerate('AB')]
    return {'cpus': cpus}


class Classes(unittest.TestCase):
    def test_the_models_sets(self):
        mp, sb, lb = (L.classes(model.profile(n)) for n in ('mp', 'sb', 'lb'))
        self.assertEqual(mp, ({(0, 0), (0, 1), (1, 1)}, {(0, 1)}, set()))
        self.assertEqual(sb, ({(0, 0), (0, 1), (1, 0), (1, 1)}, {(1, 1)}, {(0, 0)}))
        self.assertEqual(lb, ({(0, 0), (0, 1), (1, 0)}, {(0, 0)}, set()))

    def test_every_pattern_has_a_forbidden_or_relaxed_outcome_and_an_overlap(self):
        for cell in L.CELLS:
            allowed, overlap, relaxed = L.classes(model.profile(cell[1]))
            self.assertTrue(overlap, cell)
            self.assertTrue(len(allowed) < 4 or relaxed, cell)


class Programs(unittest.TestCase):
    def test_a_uses_only_first_version_forms(self):
        for cell in L.CELLS:
            lines = [re.sub(r'^\s*\.\w+:\s*', '', x).strip() for x in L.cpu_text(model.profile(cell[1]), cell[2], 'A')]
            self.assertLessEqual({x.split()[0] for x in lines if x}, FIRST_VERSION, cell)
            self.assertFalse(re.search(r'\br(?:[abcd]x|[sd]i|[bs]p|\d+)\b', '\n'.join(lines)), cell)

    def test_each_side_runs_its_participants_accesses_in_order(self):
        for cell in L.CELLS:
            test = model.profile(cell[1])
            for side, p in (('A', cell[2]), ('B', 1 - cell[2])):
                text = '\n'.join(L.cpu_text(test, p, side))
                body = text[text.index('.dl:'):]
                where = [body.index(f'+{L.LOC[op[1] if op[0] == "store" else op[2]]:#x}]') for op in test.programs[p]]
                self.assertEqual(where, sorted(where), (cell, side))
            self.assertTrue(all(L.programs(cell).values()), cell)


class Description(unittest.TestCase):
    def test_a_interprets_and_b_runs_translated_code(self):
        for cell in L.CELLS:
            shared, (a, b) = sections(L.description(cell, 100))
            self.assertEqual(shared[0], ('machine', ''))
            code = L.programs(cell)
            mem = {int(r.split()[0], 16): r.split()[1] for w, r in shared if w == 'memory'}
            self.assertEqual(mem[L.CODE['A']], code['A'].hex())
            self.assertEqual(mem[L.CODE['B']], code['B'].hex())
            for s, sec in (('A', a), ('B', b)):
                one = {w: r for w, r in sec if w not in ('seg', 'window')}
                segs = {r.split()[0]: [int(v, 16) for v in r.split()[1:]] for w, r in sec if w == 'seg'}
                windows = [r for w, r in sec if w == 'window']
                self.assertEqual(int(one['stop'], 16), L.CODE[s] + len(code[s]))
                self.assertEqual(int(one['rip'], 16), L.CODE[s])
                self.assertEqual(windows[0], f'{L.RES[s]:#x} {L.STRIDE * 100:#x}')
                gpr = [int(v, 16) for v in one['gpr'].split()]
                self.assertEqual([gpr[G.GPRS.index(r)] for r in ('edx', 'esi', 'edi')], [100, L.RES[s], L.SYNC])
                if s == 'A':       # P32: PE without PG, no long mode, a 32-bit CS
                    self.assertEqual((int(one['cr0'], 16), int(one['efer'], 16)), (G.CR0_ET | G.CR0_PE, 0))
                    self.assertEqual(segs['cs'][3] & 0x6000, 0x4000)
                else:              # 64-bit mode: IA-32e active, CS.L
                    self.assertTrue(int(one['efer'], 16) & G.EFER_LMA and int(one['cr0'], 16) & G.CR0_PG)
                    self.assertEqual(segs['cs'][3] & 0x6000, 0x2000)
                    self.assertEqual(windows[1], f'{L.SYNC:#x} {L.SYNC_BYTES:#x}')

    def test_k_bounded_by_the_result_areas(self):
        with self.assertRaises(ValueError):
            L.description(L.CELLS[0], (L.RES['B'] - L.RES['A']) // L.STRIDE + 1)


class Judge(unittest.TestCase):
    def test_outcomes_read_each_participants_bytes(self):
        k = 3
        # mp-a-reads: A is the reader (participant 1): its bytes are (flag, data) per iteration.
        res = {'A': bytes([0, 1, 1, 1, 0, 0]), 'B': bytes(2 * k)}
        hist, diffs = L.judge(L.CELLS[1], k, fake_run(L.CELLS[1], k, res))
        self.assertEqual((hist, diffs), (collections.Counter({(0, 1): 1, (1, 1): 1, (0, 0): 1}), []))
        # sb: A's r0 and B's r1, one byte each.
        res = {'A': bytes([1, 0, 0, 0, 1, 0]), 'B': bytes([1, 0, 1, 0, 0, 0])}
        hist, _ = L.judge(L.CELLS[2], k, fake_run(L.CELLS[2], k, res))
        self.assertEqual(hist, collections.Counter({(1, 1): 1, (0, 1): 1, (1, 0): 1}))

    def test_a_run_that_did_not_finish_counts_nothing(self):
        k, res = 2, {'A': bytes(4), 'B': bytes(4)}
        for out in (fake_run(L.CELLS[0], k, res, done={'A': 1, 'B': 2}), fake_run(L.CELLS[0], k, res, stops=(None, 0)), {}):
            hist, diffs = L.judge(L.CELLS[0], k, out)
            self.assertEqual(hist, collections.Counter())
            self.assertTrue(diffs)

    def test_verdict(self):
        cell = L.CELLS[0]
        self.assertEqual(L.verdict(cell, collections.Counter({(0, 0): 500, (0, 1): 100, (1, 1): 400}), 1000), [])
        forbidden = L.verdict(cell, collections.Counter({(0, 0): 500, (0, 1): 100, (1, 0): 1, (1, 1): 399}), 1000)
        self.assertEqual(len(forbidden), 1)
        self.assertIn('forbidden outcomes {(1, 0): 1}', forbidden[0])
        control = L.verdict(cell, collections.Counter({(0, 0): 600, (0, 1): 4, (1, 1): 396}), 1000)
        self.assertEqual(len(control), 1)
        self.assertIn('control', control[0])
        self.assertTrue(L.verdict(cell, collections.Counter({(0, 0): 500, (0, 1): 100}), 1000))
        # Store buffering's both-old outcome is allowed: counted, never a failure.
        self.assertEqual(L.verdict(L.CELLS[2], collections.Counter({(0, 0): 900, (1, 1): 100}), 1000), [])


if __name__ == '__main__':
    unittest.main()
