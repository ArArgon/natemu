"""Contracts §3 item 15: WRMSR EFER in the modes model (Intel profile); every program's ending, derived by hand from the
SDM rules in the model's docstring; the boot-time transitions (boot-interpreter plan section 6)."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # tools: runnable by path from the repo root
from models import modes_model as m


def last(profile, edx=0):
    return m.model(profile, 0, m.data(profile, [0] * 0x400), edx)['records'][-1]


def records(profile):
    return m.model(profile, 0, m.data(profile, [0] * 0x400))['records']


# (records, index of the last, vector, error, stop) of each program; several for 34 and 35 (M1: a TLB may keep the
# cleared PTE's translation, access by access), the fresh-walk history first.
ENDINGS = {
    22: [(15, 14, 0, 0, None)],                   # P32 -> activation -> 64-bit -> compatibility -> PG cleared -> P32
    23: [(20, 19, 0, 0, None)],                   # the same through PAE paging first (natemu: a stop, M2)
    24: [(11, 0, 0, 0, None)],                    # the ABA loop, cut at 11 records
    25: [(10, 9, 13, 0, None)],                   # WRMSR clearing LME with paging on: #GP(0)
    26: [(4, 3, 13, 0, None)],                    # activation without PAE: #GP(0)
    27: [(10, 9, 13, 0, None)],                   # CR4.PAE cleared with LMA = 1: #GP(0)
    28: [(11, 10, 13, 0, None)],                  # CR0.PG cleared in 64-bit mode: #GP(0)
    29: [(2, 1, 13, 0, None)],                    # reserved EFER bits: #GP(0)
    30: [(10, 9, 6, 0, None)],                    # JMP ptr16:32 in 64-bit mode: #UD
    31: [(9, 8, 13, 0x38, None)],                 # selector beyond the GDT limit: #GP(selector)
    32: [(9, 8, 11, 0x18, None)],                 # code segment not present: #NP(selector)
    33: [(9, 8, 13, 0x18, None)],                 # L = D = 1 with LMA = 1: #GP(selector)
    34: [(11, 10, 14, 0, None), (12, 11, 14, 0, None), (12, 11, 0, 0, None)],   # DATA's PTE cleared: #PF or the old entry
    35: [(10, 9, 14, 0, None), (10, 9, 0, 0, None)],                        # SOURCE's PTE cleared: fetch #PF or old entry
    36: [(8, 7, 13, 0, None)],                    # activation with a 16-bit TSS in TR: #GP(0)
    37: [(9, 8, 13, 0x10, None)],                 # far JMP to a data segment: #GP(selector)
    38: [(11, 10, 13, 0, None)],                  # m16:64 to a non-canonical offset: #GP(0)
    39: [(8, 7, 0, 0, None)],                     # P32 -> P16 -> real mode -> P16 -> P32
    40: [(2, 1, 13, 0, None)],                    # PG = 1 with PE = 0: #GP(0)
    41: [(2, 1, 13, 0, None)],                    # NW = 1 with CD = 0: #GP(0)
    42: [(21, 20, 0, 0, None)],                   # deactivation, LME cleared and set under PG = 0, activation
    43: [(9, 8, 13, 0, None)],                    # activation with CS.L = 1 (a 32-bit segment while LMA = 0): #GP(0)
    44: [(8, 7, 13, 0, None)],                    # Linux's CR4 bits and PAE under PG = 0 run; LA57 (not offered): #GP(0)
    45: [(10, 9, 13, 0, None)],                   # PE cleared with PG = 1: #GP(0)
    46: [(2, 1, 0, 0, 'paging without long mode')],   # legacy paging: architecturally legal, natemu's typed stop
    47: [(16, 15, 0, 0, None)],                   # CR4.PAE and EFER.LME set in real mode, then activation from P32
    48: [(11, 10, 0, 0, None)],                   # LIDT and near RET in compatibility mode
    49: [(9, 8, 14, 0, None)],                    # LIDT's operand on a page not present: #PF (read, supervisor)
}


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


class Programs(unittest.TestCase):
    def test_every_ending(self):
        self.assertEqual(sorted(ENDINGS), list(m.PROFILES))
        for profile, want in ENDINGS.items():
            self.assertEqual(m.outcomes(profile), want, profile)

    def test_a_fault_changes_nothing(self):
        for profile in m.PROFILES:
            r = last(profile)
            if r['vector']:
                self.assertEqual((r['after'], r['efer_after'], r['cs_after'], r['next_rip']),
                                 (r['before'], r['efer_before'], r['cs_before'], r['rip']), profile)

    def test_activation_round_trip_modes(self):
        self.assertEqual([r['imode'] for r in records(22)],
                         ['P32'] * 8 + ['C32', None, None, 'C32', 'C32', 'C32', 'P32'])
        r = records(22)
        self.assertEqual((r[7]['efer_after'], r[13]['efer_after']), (0x500, 0x100))   # LMA set, then cleared

    def test_real_mode_round_trip(self):
        r = records(39)
        self.assertEqual([x['imode'] for x in r], ['P32', 'P16', 'P16', 'R', 'R', 'R', 'P16', 'P32'])
        self.assertEqual(r[0]['cs_after'], [m.CODE16, m.CODE16_BASE, 0xFFFF, 0x9B])       # a 16-bit segment
        self.assertEqual((r[2]['after'][0], r[2]['cs_after']), (0x10010, r[1]['cs_after']))  # PE cleared: CS stale
        self.assertEqual(r[3]['cs_after'], [m.REAL_CS, m.REAL_CS << 4, 0xFFFF, 0x9B])     # real mode: base, limit kept
        self.assertEqual(r[3]['next_rip'], r[4]['rip'])
        self.assertEqual((r[5]['imode_after'], r[5]['cs_after'][0]), ('P16', m.REAL_CS))  # PE set: the stale real CS
        self.assertEqual((r[6]['cs_after'], r[7]['vector']), (m.CS32, 0))
        self.assertEqual([x['next_rip'] for x in r[:-1]], [x['rip'] for x in r[1:]])

    def test_lme_under_paging_off(self):
        r = records(42)
        self.assertEqual([(x['index'], x['efer_after']) for x in r[13:20]],
                         [(13, 0x100), (14, 0x100), (15, 0), (16, 0), (17, 0x100), (18, 0x100), (19, 0x500)])
        r = records(47)
        self.assertEqual([(x['imode'], x['after'][2], x['efer_after']) for x in r[5:9]],
                         [('R', 0x620, 0), ('R', 0x620, 0), ('R', 0x620, 0x100), ('R', 0x620, 0x100)])

    def test_l_is_ignored_while_lma_is_0(self):
        r = records(43)
        self.assertEqual((r[6]['vector'], r[6]['cs_after'][3], r[6]['imode_after']), (0, 0xE09B, 'P32'))
        self.assertEqual(r[8]['vector'], 13)                     # activation with CS.L = 1

    def test_cr4_under_paging_off(self):
        r = records(44)
        self.assertEqual([x['after'][2] for x in r[3:6]], [m.LINUX_CR4, m.LINUX_CR4, 0x600])   # PAE cleared, LME = 1
        self.assertEqual(m.LINUX_CR4 & ~m.CR4_VALID, 0)
        self.assertNotEqual((1 << 12) & m.CR4_VALID, 1 << 12)    # LA57 is not offered

    def test_lidt_and_near_ret_in_compatibility_mode(self):
        r = records(48)
        self.assertEqual([x['imode'] for x in r[8:]], ['C32', 'C32', 'C32'])
        self.assertEqual(r[8]['idtr_after'], [0x12345678, 0xFFF])               # m16&32: 16-bit limit, 32-bit base
        self.assertEqual((r[9]['next_rip'], r[9]['esp_after']), (r[10]['rip'], m.STACK + 4))
        self.assertEqual((records(49)[-1]['linear'], records(49)[-1]['vector']), (m.DATA + 0x1000, 14))

    def test_paging_without_long_mode_is_natemu_stop(self):
        self.assertEqual([(x['index'], x.get('stop')) for x in records(23)][3], (3, 'paging without long mode'))
        self.assertEqual(sum('stop' in x for x in records(23)), 1)
        self.assertNotIn('stop', records(22)[7])                 # PG with LME: activation, no stop


if __name__ == '__main__':
    unittest.main()
