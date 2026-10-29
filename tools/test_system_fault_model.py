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

    def test_cr4_features_the_profile_lacks(self):
        # Rederived from the CPUID profile (CPU 16): VME/PVI (CPUID.1.EDX.VME), MCE (MCE/MCA not exposed), UMIP, LA57, PKE,
        # CET (CPUID.7), VMXE, SMXE (CPUID.1.ECX) were valid in the previous hand-written list and are now reserved.
        for name, bit in (('VME', 0), ('PVI', 1), ('MCE', 6), ('UMIP', 11), ('LA57', 12), ('VMXE', 13), ('SMXE', 14), ('PKE', 22), ('CET', 23)):
            self.check('0f22e0', GP, rax=CR4 | 1 << bit)
        for bit in (2, 3, 4, 7, 8, 9, 10, 16, 18, 20):        # TSD DE PSE PGE PCE OSFXSR OSXMMEXCPT FSGSBASE OSXSAVE SMEP
            self.check('0f22e0', None, rax=CR4 | 1 << bit)

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
