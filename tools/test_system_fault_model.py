import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from system_fault_model import DB, GP, UD, dr_write, fault  # noqa: E402

CR0, CR4, EFER = 0x80010011, 0x20, 0xd00


def regs(**r):
    g = [0] * 16
    for k, v in r.items(): g['rax rcx rdx rbx'.split().index(k)] = v
    return g


class SystemFaultModel(unittest.TestCase):
    def check(self, code, want, cpl=0, cr4=CR4, dr7=0x400, **r):
        self.assertEqual(fault(bytes.fromhex(code), regs(**r), cpl, CR0, cr4, EFER, dr7), want, code)

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

    def test_cr4_features_the_profile_lacks(self):
        # Rederived from the CPUID profile (CPU 16): VME/PVI (CPUID.1.EDX.VME), MCE (MCE/MCA not exposed), LA57, PKE,
        # CET (CPUID.7), VMXE, SMXE (CPUID.1.ECX) were valid in the previous hand-written list and are now reserved. UMIP is
        # valid since CPUID.7.0:ECX.UMIP is advertised (owner decision November 3).
        for name, bit in (('VME', 0), ('PVI', 1), ('MCE', 6), ('LA57', 12), ('VMXE', 13), ('SMXE', 14), ('PKE', 22), ('CET', 23)):
            self.check('0f22e0', GP, rax=CR4 | 1 << bit)
        for bit in (2, 3, 4, 7, 8, 9, 10, 11, 16, 18, 20):    # TSD DE PSE PGE PCE OSFXSR OSXMMEXCPT UMIP FSGSBASE OSXSAVE SMEP
            self.check('0f22e0', None, rax=CR4 | 1 << bit)

    def test_s2a_control_registers(self):
        for bit in (1, 2, 3, 5, 18):                          # MP EM TS NE AM: no check on the load
            self.check('0f22c0', None, rax=CR0 | 1 << bit)
        self.check('0f22c0', None, rax=CR0 | 3 << 29)         # CD with NW
        self.check('0f22c0', None, rax=CR0 | 1 << 30)         # CD alone
        self.check('0f22d0', None, rax=0x8000000000000001)    # mov cr2, rax: any value
        self.check('0f20d0', None)                            # mov rax, cr2
        self.check('440f22c0', None, rax=15)                  # mov cr8, rax
        self.check('440f22c0', GP, rax=16)                    # bits 63:4 reserved
        self.check('440f22c0', GP, rax=1 << 63)
        self.check('440f20c0', None)
        self.check('440f20c0', GP, cpl=3)
        self.check('0f20d0', GP, cpl=3)

    def test_debug_registers(self):
        for dr in range(8):
            self.check(f'0f21{0xc0 | dr << 3:02x}', None)     # mov rax, drN (DE = 0: DR4/DR5 alias DR6/DR7)
            self.check(f'0f23{0xc0 | dr << 3:02x}', None, rax=0xffffffff)
            self.check(f'0f21{0xc0 | dr << 3:02x}', GP, cpl=3)
        for dr in (4, 5):
            self.check(f'0f21{0xc0 | dr << 3:02x}', UD, cr4=CR4 | 8)
            self.check(f'0f23{0xc0 | dr << 3:02x}', UD, cr4=CR4 | 8)
            self.check(f'0f21{0xc0 | dr << 3:02x}', GP, cpl=3, cr4=CR4 | 8)     # privilege before CR4.DE
        self.check('0f21f0', None, cr4=CR4 | 8)               # DR6 with DE
        for dr in (0, 3):
            self.check(f'0f23{0xc0 | dr << 3:02x}', None, rax=1 << 63)          # DR0..DR3: any value
        for dr in (4, 5, 6, 7):
            self.check(f'0f23{0xc0 | dr << 3:02x}', GP, rax=1 << 32)            # DR6/DR7 bits 63:32
        self.check('440f21c0', UD)                            # mov rax, dr8 (REX.R)
        self.check('440f21c0', UD, cpl=3)                     # decode-class before privilege
        self.check('f00f21c0', UD)
        for code in ('0f21c0', '0f21f8', '0f23c0', '0f23f8'):
            self.check(code, DB, dr7=0x2400)                  # general detect, reads and writes
        self.check('0f23f8', DB, dr7=0x2400, rax=1 << 32)     # #DB before the value check
        self.check('0f21e0', UD, dr7=0x2400, cr4=CR4 | 8)     # DE #UD before general detect
        self.check('0f21c0', GP, cpl=3, dr7=0x2400)

    def test_dr6_dr7_read_back(self):
        self.assertEqual(dr_write(6, 0), 0xFFFF0FF0)
        self.assertEqual(dr_write(6, 0x600F), 0xFFFF6FFF)
        self.assertEqual(dr_write(6, 1 << 12), 0xFFFF0FF0)            # bit 12 reads 0
        self.assertEqual(dr_write(6, 0xFFFFFFFF), 0xFFFFEFFF)
        self.assertEqual(dr_write(7, 0), 0x400)
        for bit in (11, 12, 14, 15):                                   # reserved, read 0
            self.assertEqual(dr_write(7, 1 << bit), 0x400)
        self.assertEqual(dr_write(7, 0xFFFF2300), 0xFFFF2700)          # R/W, LEN, LE, GE, GD kept

    def test_rdpmc_without_counters(self):
        for cpl in (0, 3):
            for cr4 in (CR4, CR4 | 1 << 8):
                self.check('0f33', GP, cpl=cpl, cr4=cr4, rcx=0)
        self.check('f00f33', UD, cpl=3)

    def test_msrs_from_the_profile(self):
        self.check('0f30', None, rcx=0xC0000100, rax=0x1000)           # FS_BASE canonical
        self.check('0f30', GP, rcx=0xC0000100, rax=0, rdx=0x8000)       # 0x0000800000000000 (CPU 9 counterexample)
        self.check('0f30', None, rcx=0xC0000103, rax=0xFFFFFFFF)        # TSC_AUX
        self.check('0f30', GP, rcx=0xC0000103, rdx=1)                   # upper half reserved
        self.check('0f30', GP, rcx=0x277, rax=2)                        # PAT type 2 is reserved
        self.check('0f30', GP, rcx=0x1a0, rax=1 << 16)                  # MISC_ENABLE: EIST not implemented (CPU 17)
        self.check('0f30', GP, rcx=0x79)                                # BIOS_UPDT_TRIG
        self.check('0f32', GP, rcx=0x6e0)                               # TSC_DEADLINE not advertised
        self.check('0f32', None, rcx=0x1a0)
        self.assertRaises(ValueError, fault, bytes.fromhex('0f32'), regs(rcx=0x808), 0, CR0, CR4, EFER)   # x2APIC: LAPIC owner

    def test_ud_before_privilege(self):
        self.check('f00f22e0', UD, cpl=3)                     # lock
        self.check('0f22c8', UD, cpl=3)                       # cr1
        self.check('0f0138', None)                            # invlpg [rax]

    def test_msrs(self):
        self.check('0f32', None, rcx=0xC0000080)
        self.check('0f32', None, rcx=0x1b)                    # IA32_APIC_BASE is in the profile (CPU 16); was #GP in the EFER-only model
        self.check('0f32', GP, rcx=0xce)                      # MSR_PLATFORM_INFO is absent (CPU 17)
        self.check('0f30', None, rcx=0xC0000080, rax=0x500)   # NXE off
        self.check('0f30', GP, rcx=0xC0000080, rax=0x500, rdx=1)
        self.check('0f30', GP, rcx=0xC0000080, rax=0xd02)
        self.check('0f30', GP, rcx=0xC0000080, rax=0xc00)     # LME cleared with paging on


if __name__ == '__main__':
    unittest.main()
