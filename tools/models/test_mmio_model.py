"""Hand-derived checks for mmio_model (expected values worked out from the SDM, not by running the model)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mmio_model import run  # noqa: E402

DL = 0x10000
RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI = range(8)


def go(code, regs=None, mem=None, rip=0x20000, fs=0, gs=0):
    g = [0] * 16
    for k, v in (regs or {}).items():
        g[k] = v
    d = bytearray(4096)
    for off, b in (mem or {}).items():
        d[off:off + len(b)] = b
    return run(bytes.fromhex(code) if isinstance(code, str) else code, rip, g, fs, gs, DL, bytes(d))


class T(unittest.TestCase):
    def test_high_byte_and_rex_low_byte(self):
        # 8A 23: mov ah,[rbx]   (mod 00 reg 100 rm 011); with REX 40 reg 4 is SPL
        r = go('8a23', {RAX: 0x1122334455667788, RBX: DL + 0x10}, {0x10: b'\x5a'})
        self.assertEqual(r['accesses'], [(0x10, 1, 0, 0x5A)])
        self.assertEqual(r['gpr'][RAX], 0x112233445566_5A88)
        r = go('408a23', {RSP: 0xAABB, RBX: DL + 0x10}, {0x10: b'\x5a'})
        self.assertEqual(r['gpr'][RSP], 0xAA5A)
        self.assertEqual(r['gpr'][RAX], 0)

    def test_movsx_word_to_64(self):
        # 48 0F BF 43 02: movsx rax, word [rbx+2]
        r = go('480fbf4302', {RBX: DL + 0x100}, {0x102: b'\x00\x80'})
        self.assertEqual(r['accesses'], [(0x102, 2, 0, 0x8000)])
        self.assertEqual(r['gpr'][RAX], 0xFFFFFFFFFFFF8000)

    def test_movzx_riprel(self):
        # 0F B6 0D disp32: next rip = 0x10107, target 0x10020, disp = -0xE7 = 0xFFFFFF19
        r = go('0fb60d19ffffff', {RCX: M}, {0x20: b'\xc3'}, rip=0x10100)
        self.assertEqual(r['accesses'], [(0x20, 1, 0, 0xC3)])
        self.assertEqual(r['gpr'][RCX], 0xC3)
        self.assertEqual(r['rip'], 0x10107)

    def test_store_imm_sign_extended(self):
        # 48 C7 43 08 FE FF FF FF: mov qword [rbx+8], -2
        r = go('48c74308feffffff', {RBX: DL + 0x40})
        self.assertEqual(r['accesses'], [(0x48, 8, 1, 0xFFFFFFFFFFFFFFFE)])
        self.assertEqual(r['device_bytes'][0x48:0x50], b'\xfe' + b'\xff' * 7)

    def test_moffs(self):
        a1 = (DL + 0x30).to_bytes(8, 'little').hex()
        a2 = (DL + 0x38).to_bytes(8, 'little').hex()
        r = go('48a1' + a1 + 'a2' + a2, {}, {0x30: bytes(range(1, 9))})
        self.assertEqual(r['accesses'], [(0x30, 8, 0, 0x0807060504030201), (0x38, 1, 1, 0x01)])
        self.assertEqual(r['gpr'][RAX], 0x0807060504030201)
        self.assertEqual(r['device_bytes'][0x38], 1)

    def test_segment_bases(self):
        # 64 8B 03: mov eax,[fs:rbx]; 3E prefix is ignored
        r = go('648b03', {RAX: M, RBX: 0x10}, {0x90: b'\x78\x56\x34\x12'}, fs=DL + 0x80)
        self.assertEqual(r['accesses'], [(0x90, 4, 0, 0x12345678)])
        self.assertEqual(r['gpr'][RAX], 0x12345678)
        with self.assertRaises(ValueError):  # 3E: no base, rbx=0x10 is outside the page
            go('3e8b03', {RBX: 0x10})

    def test_sib_forms(self):
        # 8B 44 8B 08: mov eax,[rbx+rcx*4+8]
        r = go('8b448b08', {RBX: DL + 0x100, RCX: 0x10}, {0x148: b'\xdd\xcc\xbb\xaa'})
        self.assertEqual(r['accesses'], [(0x148, 4, 0, 0xAABBCCDD)])
        # 65 8B 04 CD 10 00 00 00: mov eax,[gs:rcx*8+0x10], no base
        r = go('658b04cd10000000', {RCX: 0x20}, {0x110: b'\x11\x22\x33\x44'}, gs=DL)
        self.assertEqual(r['accesses'], [(0x110, 4, 0, 0x44332211)])
        # 42 8B 04 4B: mov eax,[rbx+r9*2] (REX.X selects r9)
        r = go('428b044b', {RBX: DL + 0x200, 9: 0x10}, {0x220: b'\x01\x00\x00\x00'})
        self.assertEqual(r['accesses'], [(0x220, 4, 0, 1)])

    def test_counter_reads(self):
        # 48 8B 03 x2: mov rax,[rbx]; 8B 4B 04: mov ecx,[rbx+4]; 66 8B 13: mov dx,[rbx]
        r = go('488b03' '488b03' '8b4b04' '668b13', {RBX: DL + 0xF00, RDX: 0xABCD0000})
        self.assertEqual(r['accesses'], [(0xF00, 8, 0, 0), (0xF00, 8, 0, 1), (0xF04, 4, 0, 0), (0xF00, 2, 0, 3)])
        self.assertEqual(r['gpr'][RDX], 0xABCD0003)

    def test_straddle_is_memory(self):
        # 8 bytes at 0xEFC..0xF03 is plain memory and does not count as a counter read
        r = go('488b03' '488b03', {RBX: DL + 0xEFC}, {0xEFC: bytes(range(1, 9))})
        self.assertEqual(r['accesses'][0], (0xEFC, 8, 0, 0x0807060504030201))
        r = go('488b03' '8b03', {RBX: DL + 0xEFC, RAX: 0}, {0xEFC: bytes(range(1, 9))})
        self.assertEqual(r['accesses'][1], (0xEFC, 4, 0, 0x04030201))  # fully before the counter: memory
        r = go('488b03' '488b4304', {RBX: DL + 0xEFC})
        self.assertEqual(r['accesses'][1], (0xF00, 8, 0, 0))

    def test_sequence_order_error(self):
        # mov qword [rbx+8],5 (wrong, seq 0); mov qword [rbx+8],1 (right); mov rax,[rbx+8] -> 2
        r = go('48c743080500000048c74308010000004 88b4308'.replace(' ', ''), {RBX: DL + 0xF00})
        self.assertEqual(r['accesses'], [(0xF08, 8, 1, 5), (0xF08, 8, 1, 1), (0xF08, 8, 0, 2)])
        self.assertEqual(r['order_errors'], 1)
        self.assertEqual(r['gpr'][RAX], 2)

    def test_movsxd(self):
        r = go('486303' '6303', {RBX: DL}, {0: b'\x00\x00\x00\x80'})
        self.assertEqual(r['accesses'], [(0, 4, 0, 0x80000000)] * 2)
        self.assertEqual(r['gpr'][RAX], 0x80000000)  # last instruction: 32-bit move zero-extends
        r = go('486303', {RBX: DL}, {0: b'\x00\x00\x00\x80'})
        self.assertEqual(r['gpr'][RAX], 0xFFFFFFFF80000000)

    def test_refusals(self):
        for c in ('0103', 'f3 8b03', '678b03', '89c3', 'c64b0001'):
            with self.assertRaises(ValueError, msg=c):
                go(c.replace(' ', ''), {RBX: DL})
        with self.assertRaises(ValueError):  # outside the page
            go('8b03', {RBX: DL + 4094})


M = (1 << 64) - 1

if __name__ == '__main__':
    unittest.main()
