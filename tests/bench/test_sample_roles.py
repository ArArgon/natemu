"""sample_roles on synthetic code objects (crates/abi/src/code_object.rs's layout, built here) and on the host's admission
fixtures: roles from site records (checks reached from their stubs, the stubs, access sites), from relocations (exits) and
from encodings (flags, constants, helpers, load-then-move), the sample-offset roles, and the guest-instruction tests. Each
record-driven role is also shown to go when its record does."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import struct, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sample_roles as R

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRA = [4, 7, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 29, 26, 27]     # RAX..R15, PF (x26), AF (x27)


def site(native, kind, guest=0, g2=0xffff_ffff, insn=None, width=8, base=11, data=(20, 0), cls=0):
    return struct.pack('<IIIIiBBBBBBBBBBbBBBBBBBBB', native, guest, g2, native if insn is None else insn, 0, kind, width, 0, 0, 0,
                       base, 0xff, 0, 0, 0, 0, data[0], data[1], cls, 0, 0, 0, 0, 0, 1)


def reloc(off, kind, reg=0, size=0, value=0):
    return struct.pack('<IBBBBQ', off, kind, reg, size, 0, value)


def code_object(words, sites=(), pcs=((4, 0),), relocs=(), sra=SRA, rip=0x40_1000, bits=64):
    """A code object: header, the code (word 0 its length, as FEX's header word), then the site, PC and relocation tables."""
    code = struct.pack(f'<{len(words) + 1}I', 4 * (len(words) + 1), *words)
    so = 128 + len(code)
    pc = b''.join(struct.pack('<IIB3x', n, g, 0) for n, g in pcs)
    st = b''.join(sites)
    ro = so + len(st) + len(pc)
    ro += -ro % 8
    rl = b''.join(relocs)
    total = ro + len(rl)
    h = struct.pack('<8s4IQ' + 'I' * 14 + '20s4BQ2B2sI', R.CODE_MAGIC, 4, 128, total, bits, rip, 16, 2, 128, len(code), 4,
                    0xffff_ffff, len(code), 0, so, len(sites), so + len(st), len(pcs), ro, len(relocs),
                    bytes(sra) + bytes(20 - len(sra)), len(sra), 1, 2, 4, 0, 0, 0, b'\0\0', 0xffff_ffff)
    obj = h + code + st + pc
    return obj + bytes(ro - len(obj)) + rl


def roles(words, **kw):
    return R.roles(R.Code(code_object(words, **kw)))[1:]


# A64 words used below.
ADD_X1_X11_8, ADD_X0_X1_7, EOR_X0_X0_X1 = 0x9100_2161, 0x9100_1c20, 0xca01_0000
STR_X4_X11_8, LDR_X20_X11_8, MOV_X6_X20 = 0xf900_0564, 0xf940_0574, 0xaa14_03e6
ADR_X0, B_DOT, BRK_10, BRK_1 = 0x10ff_ff00, 0x1400_0000, 0xd420_0140, 0xd420_0020
SVC_LINK, MOVZ_W0, MOVK_W0 = 0xd400_0021, 0x5282_0100, 0x72a0_0800


def tbnz_w0_12(at, to): return 0x3760_0000 | ((to - at) // 4 & 0x3fff) << 5
def cbnz_x0(at, to): return 0xb500_0000 | ((to - at) // 4 & 0x7ffff) << 5
def b(at, to): return 0x1400_0000 | ((to - at) // 4 & 0x3ff_ffff)
def beq(at, to): return 0x5400_0000 | ((to - at) // 4 & 0x7ffff) << 5


class SiteRecords(unittest.TestCase):
    def split_block(self, with_stub=True):
        # 4: add x1, x11, #8; add x0, x1, #7; eor; tbnz w0, #12, 0x1c; str x4, [x11, #8] (0x14); exit (0x18..); stub at 0x24
        w = [ADD_X1_X11_8, ADD_X0_X1_7, EOR_X0_X0_X1, tbnz_w0_12(0x10, 0x24), STR_X4_X11_8, MOVZ_W0, MOVK_W0, SVC_LINK, ADR_X0, B_DOT]
        sites = [site(0x14, R.STORE, insn=4)] + ([site(0x24, R.SPLIT, g2=0x14, insn=4)] if with_stub else [])
        relocs = [reloc(0x18, R.RELOC_GUEST_RIP_MOVE, 0), reloc(0x28, R.RELOC_PROBE_BRANCH)]
        return roles(w, sites=sites, relocs=relocs)

    def test_a_split_check_is_the_words_its_stub_branch_ends(self):
        self.assertEqual(self.split_block(), ['split_check'] * 4 + ['fex_mem'] + ['exit_literal'] * 3 + ['stub'] * 2)

    def test_without_its_stub_record_the_check_is_ordinary_code(self):
        r = self.split_block(with_stub=False)
        self.assertEqual(r[:4], ['fex_alu', 'fex_alu', 'fex_alu', 'fex_branch'])

    def test_stack_align_and_guard_checks_take_one_word_before_their_branch(self):
        for kind, role, first in ((R.STACK, 'stack_check', 0x9240_0900), (R.ALIGN, 'align_check', 0x9240_0d60),
                                  (R.GUARD, 'guard_check', 0xca14_4280)):
            w = [0x9100_2108, first, cbnz_x0(0x0c, 0x10), BRK_10]       # add x8 (not the check's), check word, branch, stub
            r = roles(w, sites=[site(0x10, kind)])
            self.assertEqual(r, ['fex_alu', role, role, 'stub'], kind)

    def test_the_window_stops_at_its_instruction_start(self):
        w = [0x9100_0020, ADD_X0_X1_7, EOR_X0_X0_X1, tbnz_w0_12(0x10, 0x18), STR_X4_X11_8, BRK_1]
        # add x0, x1, #0 belongs to the previous instruction (the PC table starts the store's at 8).
        r = roles(w, sites=[site(0x14, R.STORE, insn=8), site(0x18, R.SPLIT, g2=0x14, insn=8)], pcs=((4, 0), (8, 3)))
        self.assertEqual(r, ['fex_alu', 'split_check', 'split_check', 'split_check', 'fex_mem', 'stub'])

    def test_the_far_form_counts_both_branches(self):
        w = [ADD_X0_X1_7, EOR_X0_X0_X1, beq(0x0c, 0x14), b(0x10, 0x18), STR_X4_X11_8, BRK_1]
        r = roles(w, sites=[site(0x14, R.STORE), site(0x18, R.SPLIT, g2=0x14)])
        self.assertEqual(r, ['split_check'] * 4 + ['fex_mem', 'stub'])


class Exits(unittest.TestCase):
    def test_a_literal_exit_is_its_move_and_svc(self):
        self.assertEqual(roles([MOVZ_W0, MOVK_W0, SVC_LINK], relocs=[reloc(4, R.RELOC_GUEST_RIP_MOVE, 0)]), ['exit_literal'] * 3)

    def test_a_guest_rip_move_into_another_register_is_a_constant(self):
        w = [0x5282_00b4, 0x72a0_0814]          # mov w20, #..; movk w20 (a call's return address)
        self.assertEqual(roles(w, relocs=[reloc(4, R.RELOC_GUEST_RIP_MOVE, 20)]), ['fex_const'] * 2)

    def test_a_computed_exit_is_its_mov_x0_and_branch(self):
        self.assertEqual(roles([0xaa14_03e0, B_DOT], relocs=[reloc(8, R.RELOC_DISPATCH_BRANCH)]), ['exit_computed'] * 2)
        self.assertEqual(roles([0xaa14_03e0, B_DOT]), ['fex_alu', 'fex_branch'])


class Patterns(unittest.TestCase):
    def test_flag_words(self):
        for w in (0xba1f_8682, 0xd500_401f, 0x3a00_080d, 0xfa40_1804, 0x9a9f_27f4, 0xd53b_4200, 0xd51b_4200):
            self.assertEqual(roles([w]), ['flags'], hex(w))

    def test_a_write_of_pf_or_af_is_a_flag_word_only_where_the_object_says_where_they_are(self):
        for w in (0xf100_04fa, 0xaa07_03fb):               # subs x26, x7, #1; mov x27, x7
            self.assertEqual(roles([w]), ['flags'])
            self.assertEqual(roles([w], sra=SRA[:16]), ['fex_alu'])
        self.assertEqual(roles([0xaa1a_03e7]), ['fex_alu'])  # mov x7, x26: the result, read from PF's register
        # 32-bit code: EAX..EDI, then PF and AF (sra_count 10).
        sra32 = [4, 7, 5, 6, 8, 9, 10, 11, 26, 27]
        self.assertEqual(roles([0x7100_04fa, 0xaa07_03fb], sra=sra32, bits=32), ['flags', 'flags'])
        self.assertEqual(roles([0xaa07_03ec], sra=sra32, bits=32), ['fex_alu'])   # mov x12, x7: x12 is R8 only in 64-bit code

    def test_other_encodings(self):
        cases = {0xd53b_4219: 'nzcv_save', 0xd53b_4420: 'fex_fpctl', 0xd51b_443f: 'fex_fpctl', 0x5280_0294: 'fex_const',
                 0x6e23_8e11: 'fex_simd', 0x3dc0_3382: 'fex_state', 0xd503_201f: 'fex_other', 0xd503_3bbf: 'fex_other',
                 0xd53b_e054: 'fex_other', 0x0000_0000: 'literal', 0x8b14_016b: 'fex_alu', BRK_1: 'stub'}
        for w, want in cases.items(): self.assertEqual(roles([w]), [want], hex(w))

    def test_a_helper_call_takes_its_argument_moves(self):
        w = [0xb400_0287, 0xaa05_03e0, 0xaa04_03e1, 0xaa07_03e2, 0xd400_0061, 0xaa00_03f4]
        self.assertEqual(roles(w), ['fex_branch', 'helper', 'helper', 'helper', 'helper', 'fex_alu'])


class LoadThenMove(unittest.TestCase):
    def test_the_move_out_of_a_loads_temporary(self):
        self.assertEqual(roles([LDR_X20_X11_8, MOV_X6_X20], sites=[site(4, R.LOAD)]), ['fex_mem', 'load_move'])
        self.assertEqual(roles([LDR_X20_X11_8, MOV_X6_X20]), ['fex_state', 'fex_alu'])     # no site: no load-then-move

    def test_a_read_modify_write_uses_its_temporary_in_place(self):
        w = [LDR_X20_X11_8, 0x3100_0694, MOV_X6_X20]       # ldr; adds w20, w20, #1; mov x6, x20: x20 is no longer the load's
        self.assertEqual(roles(w, sites=[site(4, R.LOAD)]), ['fex_mem', 'fex_alu', 'fex_alu'])

    def test_a_vector_load_moves_into_its_register(self):
        w = [0xfd40_0162, 0x4ea2_1c50]                      # ldr d2, [x11]; mov v16.16b, v2.16b
        self.assertEqual(roles(w, sites=[site(4, R.LOAD, data=(2, 0), cls=R.CLASS_FPR)]), ['fex_mem', 'load_move'])
        w = [0xa940_5574, 0x9e67_0290, 0x9eaf_02b0]         # ldp x20, x21; fmov d16, x20; fmov v16.d[1], x21
        self.assertEqual(roles(w, sites=[site(4, R.LOAD_PAIR, data=(20, 21))]), ['fex_mem', 'load_move', 'load_move'])

    def test_not_across_its_instruction(self):
        r = roles([LDR_X20_X11_8, MOV_X6_X20], sites=[site(4, R.LOAD)], pcs=((4, 0), (8, 4)))
        self.assertEqual(r, ['fex_mem', 'fex_alu'])


class SampleOffsets(unittest.TestCase):
    def test_role_at(self):
        r = ['entry', 'fex_alu', 'fex_mem']
        self.assertEqual([R.role_at(r, o) for o in (-4, 0, 4, 8, 12, 2, -8, None)],
                         ['entry', 'entry', 'fex_alu', 'fex_mem', 'beyond', 'beyond', 'beyond', 'far'])
        self.assertEqual((R.role_at(r, 8, True), R.role_at(None, 8)), ('probe', 'none'))

    def test_word_instructions_follow_the_pc_table(self):
        c = R.Code(code_object([0] * 5, pcs=((4, 0), (12, 3), (16, 7))))
        self.assertEqual(c.word_insns(), [None, 0, 0, 3, 7, 7])
        self.assertEqual(R.insn_bytes(c, bytes(range(10)), 3), bytes([3, 4, 5, 6]))

    def test_an_object_whose_code_region_lies_outside_it_is_refused(self):
        obj = bytearray(code_object([0, 0]))
        struct.pack_into('<I', obj, 56, 4096)               # code_region_end
        with self.assertRaises(ValueError): R.Code(bytes(obj))


class GuestInstructions(unittest.TestCase):
    def test_rep_string(self):
        for ib, bits, want in ((b'\xf3\xa4', 64, 'movs'), (b'\xf3\x48\xa5', 64, 'movs'), (b'\x66\xf3\xab', 64, 'stos'),
                               (b'\xf3\xaa', 32, 'stos'), (b'\xa4', 64, None), (b'\xf3\xa6', 64, None), (b'\xf2\xae', 64, None),
                               (b'\xf3\x48\x0f\xb8\xc0', 64, None), (b'\xf3\x41\xa4', 32, None)):
            self.assertEqual(R.x86_rep_string(ib, bits), want, ib.hex())

    def test_wide_vector(self):
        for ib, bits, want in ((b'\xc5\xfd\x74\x0f', 64, 'vex256'), (b'\xc5\xf9\x74\x0f', 64, None),
                               (b'\xc4\xe2\x7d\x17\xc1', 64, 'vex256'), (b'\x62\xf1\x7d\x28\x74\xc1', 64, 'evex'),
                               (b'\xc5\x00', 32, None), (b'\xc4\x01\x02', 32, None), (b'\x48\x89\xc0', 64, None)):
            self.assertEqual(R.x86_wide_vector(ib, bits), want, ib.hex())


class AdmissionFixtures(unittest.TestCase):
    """The host's admission fixtures (crates/host/tests/admission: fexc objects the host's tests admit): every word has a
    role, and every access site's word is the load or store its record names."""

    def test_every_word_of_every_fixture_has_a_role(self):
        objs = sorted((ROOT / 'crates/host/tests/admission').glob('*.obj'))
        self.assertGreater(len(objs), 10)
        for f in objs:
            c = R.Code(f.read_bytes())
            r = R.roles(c)
            self.assertNotIn('unknown', r, f.name)
            for s in c.sites:
                if s[5] in R.ACCESS_KINDS and s[0] % 4 == 0:
                    self.assertEqual(r[s[0] // 4], 'fex_mem', f'{f.name} {s[0]:#x}')
                    self.assertTrue(R.is_memory(c.words[s[0] // 4]), f'{f.name} {s[0]:#x}')
                if s[5] in R.STUB_KINDS: self.assertEqual(r[s[0] // 4], 'stub', f'{f.name} {s[0]:#x}')


if __name__ == '__main__':
    unittest.main()
