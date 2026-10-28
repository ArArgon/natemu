import unittest

from system_fault_model import GP, UD, fault

CR0, CR4, EFER = 0x80010011, 0x20, 0xd00


def regs(**r):
    g = [0] * 16
    for k, v in r.items(): g['rax rcx rdx rbx'.split().index(k)] = v
    return g


class SystemFaultModel(unittest.TestCase):
    def check(self, code, want, cpl=0, **r):
        self.assertEqual(fault(bytes.fromhex(code), regs(**r), cpl, CR0, CR4, EFER), want, code)

    def test_mov_to_control_registers(self):
        self.check('0f22c0', None, rax=CR0 & ~0x10000)       # mov cr0, rax: WP off
        self.check('0f22c0', GP, rax=CR0 | 1 << 32)           # bits 63:32
        self.check('0f22c0', GP, rax=CR0 & ~1)                # PG without PE
        self.check('0f22c0', GP, rax=CR0 & ~(1 << 31))        # PG cleared in 64-bit mode
        self.check('0f22c0', GP, rax=CR0 | 1 << 29)           # NW without CD
        self.check('0f22e0', None, rax=CR4 | 1 << 20)         # mov cr4, rax: SMEP
        for bit in (5, 15, 17, 21, 32):                       # PAE cleared; reserved, PCIDE, SMAP, bit 32
            self.check('0f22e0', GP, rax=CR4 ^ 1 << bit)
        self.check('0f22d8', None, rax=0x1000 | 0xfff)        # mov cr3, rax: low bits
        self.check('0f22d8', GP, rax=1 << 40)
        self.check('0f22d8', GP, cpl=3, rax=0x1000)

    def test_ud_before_privilege(self):
        self.check('f00f22e0', UD, cpl=3)                     # lock
        self.check('0f22c8', UD, cpl=3)                       # cr1
        self.check('0f0138', None)                            # invlpg [rax]

    def test_msrs(self):
        self.check('0f32', None, rcx=0xC0000080)
        self.check('0f32', GP, rcx=0x1b)
        self.check('0f30', None, rcx=0xC0000080, rax=0x500)   # NXE off
        self.check('0f30', GP, rcx=0xC0000080, rax=0x500, rdx=1)
        self.check('0f30', GP, rcx=0xC0000080, rax=0xd02)
        self.check('0f30', GP, rcx=0xC0000080, rax=0xc00)     # LME cleared with paging on


if __name__ == '__main__':
    unittest.main()
