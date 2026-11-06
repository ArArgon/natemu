"""Contracts §3 item 15: WRMSR EFER in the modes model (Intel profile)."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # tools: runnable by path from the repo root
from models import modes_model as m


def last(profile, edx=0):
    return m.model(profile, 0, m.data(profile, [0] * 0x400), edx)['records'][-1]


class WrmsrEfer(unittest.TestCase):
    def test_nonzero_edx_raises_gp_with_no_effect(self):
        # Profile 22 writes EFER = 0x100 (LME, legal) as its fourth instruction.
        ok = m.model(22, 0, m.data(22, [0] * 0x400))['records'][3]
        self.assertEqual((ok['source'], ok['vector'], ok['efer_after']), (b'\x0f\x30', 0, 0x100))
        for edx in (1, 0x8000_0000):
            r = last(22, edx)
            self.assertEqual((r['index'], r['vector'], r['efer_after'], r['next_rip']), (3, 13, 0, r['rip']))

    def test_reserved_eax_bits_raise_gp(self):
        self.assertEqual(last(29)['vector'], 13)                 # EAX = 0xffffffff
        self.assertEqual(m.EFER_DEFINED, 1 | 1 << 8 | 1 << 10 | 1 << 11)


if __name__ == '__main__':
    unittest.main()
