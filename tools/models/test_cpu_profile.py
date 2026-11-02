"""cpu_profile.json against the accepted records (CPU 9/10/12/13/16/17), written from the record text, not from the JSON."""
import copy
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # tools: runnable by path from the repo root
from models import cpu_profile as cp

P = cp.load()
RAW = json.loads(cp.PATH.read_text())


def bitset(*b): return sum(1 << i for i in b)


class Cpuid(unittest.TestCase):
    """CPU 16 table, leaf by leaf (bit positions from SDM Vol.2A Table 3-8)."""

    def test_leaf0_1(self):
        self.assertEqual(P.cpuid(0), (0x15, 0x756e6547, 0x6c65746e, 0x49656e69))     # GenuineIntel
        eax, ebx, ecx, edx = P.cpuid(1)
        self.assertEqual(eax, 0x000306C3)                                              # family 6, model 0x3c, stepping 3
        # CPU 16: EDX FPU DE PSE TSC MSR PAE MTRR CX8 APIC(dyn) SEP PGE CMOV PAT PSE36 CLFSH MMX FXSR SSE SSE2; HT conditional
        self.assertEqual(edx, bitset(0, 2, 3, 4, 5, 6, 8, 11, 12, 13, 15, 16, 17, 19, 23, 24, 25, 26) | 1 << 9)
        # ECX SSE3 PCLMUL SSSE3 FMA CX16 SSE4.1 SSE4.2 x2APIC MOVBE POPCNT AES XSAVE AVX F16C RDRAND hypervisor; OSXSAVE dynamic
        self.assertEqual(ecx, bitset(0, 1, 9, 12, 13, 19, 20, 21, 22, 23, 25, 26, 28, 29, 30, 31))

    def test_leaf1_dynamic(self):
        _, ebx, ecx, edx = P.cpuid(1, n=4, vcpu=3, cr4=1 << 18, apic_en=False)
        self.assertEqual((ebx >> 24, ebx >> 16 & 0xFF, ebx >> 8 & 0xFF), (3, 4, 8))
        self.assertTrue(ecx >> 27 & 1)                      # OSXSAVE follows CR4
        self.assertFalse(edx >> 9 & 1)                      # APIC follows IA32_APIC_BASE.EN
        self.assertTrue(edx >> 28 & 1)                      # HT iff N > 1
        self.assertFalse(P.cpuid(1)[3] >> 28 & 1)
        self.assertFalse(P.cpuid(1)[2] >> 27 & 1)

    def test_cache_and_topology(self):
        self.assertEqual(P.cpuid(2), (0xff01, 0, 0, 0))
        l1d, l1i, l2, l3, end = (P.cpuid(4, i) for i in range(5))
        self.assertEqual(l1d, (0x121, 0x01c0003f, 63, 1))   # 32 KiB 8-way 64 sets, type 1 level 1, self-init
        self.assertEqual((l1i[0] & 0x1f, l1i[2]), (2, 63))
        self.assertEqual(((l2[1] >> 22) + 1, l2[2] + 1, l2[0] >> 5 & 7), (8, 512, 2))      # 256 KiB 8-way
        self.assertEqual(((l3[1] >> 22) + 1, l3[2] + 1, l3[0] >> 5 & 7), (16, 8192, 3))    # 8 MiB 16-way
        self.assertEqual(end, (0, 0, 0, 0))
        self.assertEqual(P.cpuid(4, 3, n=6)[0] >> 14 & 0xFFF, 5)       # L3 shared by N; L1 private
        self.assertEqual(P.cpuid(4, 0, n=6)[0] >> 14 & 0xFFF, 0)
        self.assertEqual(P.cpuid(4, 0, n=6)[0] >> 26, 5)               # package core count - 1
        self.assertEqual(P.cpuid(6), (4, 0, 0, 0))                     # ARAT only
        self.assertEqual(P.cpuid(0xB, 0, n=5, vcpu=2), (0, 1, 0x100, 2))
        self.assertEqual(P.cpuid(0xB, 1, n=5, vcpu=2), (3, 5, 0x201, 2))     # ceil(log2 5)
        self.assertEqual(P.cpuid(0xB, 2, vcpu=2), (0, 0, 2, 2))              # terminating level
        self.assertEqual(P.cpuid(0xB, 9, vcpu=2), (0, 0, 9, 2))

    def test_leaf7_and_extended_state(self):
        self.assertEqual(P.cpuid(7, 0), (0, bitset(0, 3, 5, 7, 8), 0, 0))   # FSGSBASE BMI1 AVX2 SMEP BMI2 only
        self.assertEqual(P.cpuid(7, 1), (0, 0, 0, 0))
        self.assertEqual(P.cpuid(0xD, 0, xcr0=7), (7, 832, 832, 0))
        self.assertEqual(P.cpuid(0xD, 0, xcr0=3)[1], 576)
        self.assertEqual(P.cpuid(0xD, 0, xcr0=1)[1], 576)
        self.assertEqual(P.cpuid(0xD, 1), (0, 0, 0, 0))
        self.assertEqual(P.cpuid(0xD, 2), (256, 576, 0, 0))
        self.assertEqual(P.cpuid(0xD, 3), (0, 0, 0, 0))

    def test_time_hypervisor_extended(self):
        self.assertEqual(P.cpuid(0x15), (3, 125, 24000000, 0))
        self.assertEqual(P.cpuid(0x40000000)[1:], (0x6574614e, 0x614e756d, 0x756d6574))      # "NatemuNatemu"
        self.assertEqual(P.cpuid(0x80000000)[0], 0x80000008)
        _, _, ecx, edx = P.cpuid(0x80000001)
        self.assertEqual(ecx, bitset(0, 5))                                  # LAHF, LZCNT only (no PREFETCHW)
        self.assertEqual(edx, bitset(20, 27, 29) | 1 << 11)                  # NX RDTSCP LM, SYSCALL in 64-bit mode
        self.assertEqual(P.cpuid(0x80000001, mode64=False)[3], bitset(20, 27, 29))
        brand = b''.join(r.to_bytes(4, 'little') for l in (2, 3, 4) for r in P.cpuid(0x80000000 + l))
        self.assertEqual(brand, b'Natemu Virtual x86-64 CPU'.ljust(48, b'\0'))
        self.assertEqual(P.cpuid(0x80000005), (0, 0, 0, 0))
        self.assertEqual(P.cpuid(0x80000006), (0, 0, 0x01006040, 0))
        self.assertEqual(P.cpuid(0x80000007), (0, 0, 0, 0x100))             # invariant TSC
        self.assertEqual(P.cpuid(0x80000008), (0x3028, 0, 0, 0))            # 40 / 48

    def test_holes_and_out_of_range(self):
        for leaf in (3, 5, 9, 0xA, 0x14):
            self.assertEqual(P.cpuid(leaf), (0, 0, 0, 0), hex(leaf))        # CPU 16: holes return zero, PMU 0AH zero
        self.assertEqual(P.cpuid(0x16), P.cpuid(0x15))                      # highest basic leaf (Intel), leaf 16 not advertised
        self.assertEqual(P.cpuid(0x80000009), P.cpuid(0x15))

    def test_misc_enable_limit(self):
        self.assertEqual(P.cpuid(0, misc_enable=0x1801 | 1 << 22)[0], 2)    # CPU 17 bit 22
        self.assertEqual(P.cpuid(4, misc_enable=1 << 22), P.cpuid(2))       # basic query above 2: limited highest-basic response
        self.assertEqual(P.cpuid(0x80000008, misc_enable=1 << 22)[0], 0x3028)   # extended namespace unaffected
        self.assertEqual(P.cpuid(0x40000000, misc_enable=1 << 22)[1], 0x6574614e)


class ControlBits(unittest.TestCase):
    def test_masks(self):
        self.assertEqual(P.cr0_valid_mask(), bitset(0, 1, 2, 3, 4, 5, 16, 18, 29, 30, 31))
        # plan section 1 CR4 row: DE TSD PSE PAE PGE PCE OSFXSR OSXMMEXCPT FSGSBASE OSXSAVE SMEP
        self.assertEqual(P.cr4_valid_mask(), bitset(2, 3, 4, 5, 7, 8, 9, 10, 16, 18, 20))
        self.assertEqual(P.efer_valid_mask(), bitset(0, 8, 10, 11))
        for name in ('VME', 'PVI', 'MCE', 'UMIP', 'LA57', 'VMXE', 'SMXE', 'PCIDE', 'SMAP', 'PKE', 'CET', 'PKS', 'UINTR', 'KL'):
            self.assertFalse(RAW['control']['cr4']['bits'][name]['valid'], name)

    def test_xcr0(self):
        self.assertEqual([v for v in range(16) if P.xcr0_ok(v)], [1, 3, 7])   # bit 0 required, bit 2 needs bit 1, mask 0x7


class Msrs(unittest.TestCase):
    def ok(self, idx, val, **kw): self.assertTrue(P.msr_write_ok(idx, val, **kw), hex(idx))
    def gp(self, idx, val, **kw): self.assertFalse(P.msr_write_ok(idx, val, **kw), hex(idx))

    def test_absent_is_gp(self):
        for idx in (0xce, 0x140, 0x79, 0x3b, 0x6e0, 0xda0, 0x48, 0x10a, 0x1d9, 0x179, 0x12345678, 0xc0000104):
            self.assertFalse(P.msr_read_ok(idx), hex(idx))
            self.gp(idx, 0)

    def test_every_listed_msr_reads_and_takes_its_reset_value(self):
        for e in RAW['msrs']:
            if 'index_last' in e: continue
            i = int(e['index'], 16)
            if e['access'] == 'none':                    # 0x79: #GP both ways (CPU 17)
                self.assertFalse(P.msr_read_ok(i)); self.gp(i, 0); continue
            self.assertTrue(P.msr_read_ok(i), e['name'])
            if e['access'] == 'rw': self.ok(i, int(e['reset'], 16))
            else: self.gp(i, 0)
        self.assertEqual(len([e for e in RAW['msrs'] if e['index'].startswith('0x2') and 'MTRR' in e['name']]), 16 + 11 + 1)

    def test_canonical(self):
        bad, good = 0x0000800000000000, 0xFFFF800000000000
        for idx in (0xC0000100, 0xC0000101, 0xC0000102, 0xC0000082, 0x175, 0x176):      # SDM WRMSR canonical list
            self.gp(idx, bad); self.ok(idx, good); self.ok(idx, 0x00007fffffffffff)
        self.ok(0xC0000083, bad)             # CSTAR: no canonical rule in CPU 9/16
        self.ok(0xC0000081, bad)             # STAR holds selectors

    def test_efer(self):
        self.ok(0xC0000080, 0x500); self.ok(0xC0000080, 0xd01)
        self.gp(0xC0000080, 0x502); self.gp(0xC0000080, 1 << 14); self.gp(0xC0000080, 1 << 32)
        self.gp(0xC0000080, 0xc00, cr0=0x80010011, efer=0xd00)      # LME cleared with paging on (CPU 10)
        self.ok(0xC0000080, 0xd01, cr0=0x80010011, efer=0xd00)      # SCE change only
        self.ok(0xC0000080, 0xd00 ^ 0x100, cr0=0x11, efer=0xd00)    # paging off: LME may change

    def test_tsc_aux_and_ucode(self):
        self.ok(0xC0000103, 0xFFFFFFFF); self.gp(0xC0000103, 1 << 32)
        self.ok(0x8b, 0); self.ok(0x8b, 1 << 32); self.gp(0x8b, 1)
        self.gp(0x17, 0); self.gp(0xfe, 0)

    def test_misc_enable(self):
        for v in (0x1801, 0x401801, 0x1800, 0x1881):
            self.ok(0x1a0, v)
        for bit in (1, 3, 6, 8, 16, 18, 23, 34, 38, 63):
            self.gp(0x1a0, 0x1801 | 1 << bit)
        self.assertEqual(P.misc_enable_after_write(0), 0x1800)          # bits 11/12 fixed 1, bit 0 cleared
        self.assertEqual(P.misc_enable_after_write(0x401881), 0x401801) # status bit 7 reads 0

    def test_memory_types(self):
        self.assertEqual(P.msr(0x277)['reset'], '0x7040600070406')
        self.ok(0x277, 0x0007040600070406); self.ok(0x277, 0x0101010101010101)
        for v in (2, 3, 8, 0xff):
            self.gp(0x277, v << 24 | 6)
        self.ok(0x2ff, 0xc06); self.ok(0x2ff, 0); self.gp(0x2ff, 0x206); self.gp(0x2ff, 0xc02); self.gp(0x2ff, 1 << 12)
        self.ok(0x200, 0x12345000 | 6); self.gp(0x200, 0x12345000 | 2); self.gp(0x200, 0x100 | 6); self.gp(0x200, 1 << 40 | 6)
        self.ok(0x201, 0xFFF00800); self.gp(0x201, 0x400); self.gp(0x201, 1 << 40)
        self.ok(0x250, 0x0606060606060606); self.gp(0x250, 0x0606060606060206); self.gp(0x26f, 0x0300000000000000)
        self.assertEqual(int(P.msr(0xfe)['reset'], 16), 0x508)          # VCNT 8, FIX, WC

    def test_apic_base(self):
        self.ok(0x1b, 0xfee00900 & ~0x100); self.ok(0x1b, 0xfee00800 | 1 << 10)
        self.gp(0x1b, 0xfee00000 | 1 << 10)                              # x2APIC without global enable
        self.gp(0x1b, 0xfee00800 | 1 << 9); self.gp(0x1b, 1 << 40 | 1 << 11)

    def test_x2apic_is_the_lapic_owners(self):
        self.assertIsNotNone(P.msr(0x808))
        self.assertRaises(ValueError, P.msr_read_ok, 0x808)


class Consistency(unittest.TestCase):
    """The validator must actually fail when the profile contradicts itself."""

    def mutate(self, fn):
        d = copy.deepcopy(RAW); fn(d)
        return cp.Profile(d).problems()

    def leaf(self, d, leaf, sub=None):
        return next(e for e in d['cpuid'] if int(e['leaf'], 16) == leaf and e['subleaf'] == sub)

    def test_clean(self):
        self.assertEqual(P.problems(), [])

    def test_avx_needs_xcr0_bit2(self):
        def f(d): self.leaf(d, 0xD, 0)['eax'] = '0x3'
        self.assertTrue(any('XCR0' in m or 'EAX' in m for m in self.mutate(f)))
        def g(d): d['control']['xcr0']['supported_mask'] = '0x5'
        self.assertTrue(any('XCR0' in m for m in self.mutate(g)))

    def test_cr4_bit_without_feature(self):
        def f(d): d['control']['cr4']['bits']['UMIP']['valid'] = True
        self.assertTrue(any('UMIP' in m for m in self.mutate(f)))
        def g(d): self.leaf(d, 1)['ecx'] = hex(int(self.leaf(d, 1)['ecx'], 16) & ~(1 << 26))   # XSAVE off, OSXSAVE still valid
        self.assertTrue(any('OSXSAVE' in m for m in self.mutate(g)))

    def test_feature_without_msr(self):
        def f(d): d['msrs'] = [m for m in d['msrs'] if m['index'] != '0xc0000103']
        self.assertTrue(any('RDTSCP' in m for m in self.mutate(f)))
        def g(d): self.leaf(d, 1)['ecx'] = hex(int(self.leaf(d, 1)['ecx'], 16) | 1 << 24)      # TSC-deadline advertised
        self.assertTrue(any('TSC_DEADLINE' in m for m in self.mutate(g)))

    def test_unnamed_or_misdescribed_bit(self):
        def f(d): self.leaf(d, 7, 0)['ebx'] = hex(int(self.leaf(d, 7, 0)['ebx'], 16) | 1 << 18)   # RDSEED set, field says clear
        self.assertTrue(any('RDSEED' in m for m in self.mutate(f)))

    def test_kernel_site_would_fault(self):
        def f(d): d['absent_msrs'].append({'index': '0xc0000103', 'name': 'x'}); d['msrs'] = [m for m in d['msrs'] if m['index'] != '0xc0000103']
        self.assertTrue(any('0xc0000103' in m for m in self.mutate(f)))

    def test_every_unconditional_unsafe_kernel_site_is_a_present_msr(self):
        sites = [s for s in RAW['kernel_msr_sites'] if not s['safe'] and s['gate'] is None]
        self.assertGreaterEqual(len(sites), 15)
        for s in sites: self.assertIsNotNone(P.msr(int(s['msr'], 16)), s)

    def test_flags_cover_the_requested_features(self):
        slugs = {f['slug'] for f in RAW['census_flags']}
        for want in ('bmi1-bmi2-lzcnt-tzcnt', 'cet-shadow-stack', 'rdseed', 'smap-clac-stac', 'popcnt', 'rdtscp-tsc-aux', 'invpcid-pcid'):
            self.assertIn(want, slugs)

    def test_mtrr_initial_is_the_owners_choice(self):
        m = RAW['parameters']['mtrr_initial']
        self.assertEqual(m['selected'], 'enabled')                       # owner decision Oct 28
        self.assertEqual(set(m['choices']), {'disabled', 'enabled'})
        self.assertIn('0xc06', P.mtrr_initial('enabled')['IA32_MTRR_DEF_TYPE'])

    def test_mtrr_initial_matches_the_m69_map(self):
        st = P.mtrr_state()
        mt = lambda a: P.memory_type(st, a)
        WB, UC = 6, 0
        # M69 table: RAM 0x1000..0xa0000, 0x1000000..L (L = 2 GiB), optional high RAM; holes 0xa0000..0x100000, L..4 GiB
        for a in (0, 0x1000, 0x9ffff, 0x100000, 0x200000, 0x600000, 0x7ffffff, 0x1000000, 0x7fffffff, 0x100000000, 0x200000000):
            self.assertEqual(mt(a), WB, hex(a))
        for a in (0xa0000, 0xbffff, 0xc0000, 0xfffff, 0x80000000, 0xf0000000, 0xf0002fff, 0xfec00000, 0xfee00000, 0xffffffff):
            self.assertEqual(mt(a), UC, hex(a))                         # VGA/legacy hole, virtio-MMIO, IOAPIC, LAPIC
        self.assertEqual(P.memory_type({}, 0x1000), UC)                  # MTRRs disabled: UC (SDM 11.11.2.1)

    def test_mtrr_initial_for_other_ram_limits(self):
        for L in (1 << 30, 3 << 30, 0xC0000000, 0x70000000):               # configurable 1..3 GiB (M69)
            st = P.mtrr_state(L)
            self.assertLessEqual(len([k for k in st if 0x200 <= k < 0x210 and k % 2 == 0]), 3)
            for a in (0x1000000, L - 1): self.assertEqual(P.memory_type(st, a), 6, hex(a))
            for a in (L, 0xf0000000, 0xfee00000, 0xffffffff): self.assertEqual(P.memory_type(st, a), 0, (hex(L), hex(a)))
            for k, v in st.items(): self.assertTrue(P.msr_write_ok(k, v), hex(k))
        self.assertEqual(cp.uc_pairs(1 << 30, 1 << 32), [(1 << 30, 1 << 30), (2 << 30, 2 << 30)])

    def test_mtrr_initial_follows_every_ram_size(self):
        """M2 addendum: for guest RAM of every 2 MiB multiple to 3 GiB (M1) the state fits IA32_MTRRCAP.VCNT pairs, WRMSR
        accepts it, RAM is WB and the rest below 4 GiB UC (every 2 MiB block at 1, 2 and 3 GiB; the edges at the others)."""
        WB, UC, MiB = 6, 0, 1 << 20
        for L in range(2 * MiB, (3 << 30) + 1, 2 * MiB):
            st = P.mtrr_state(L)
            self.assertLessEqual(len([k for k in st if 0x200 <= k < 0x210 and k % 2 == 0]), 8, hex(L))
            for k, v in st.items(): self.assertTrue(P.msr_write_ok(k, v), (hex(L), hex(k)))
            full = L in (1 << 30, 2 << 30, 3 << 30)
            blocks = range(0, 1 << 32, 2 * MiB) if full else {0, L - 2 * MiB, L, (L + (1 << 32)) // 2 & ~(2 * MiB - 1), (1 << 32) - 2 * MiB}
            for a in blocks:
                for x in {max(a, MiB), a + 2 * MiB - 0x1000}:
                    self.assertEqual(P.memory_type(st, x), WB if x < L else UC, (hex(L), hex(x)))
            for a in (0x1000, 0x9f000, 0xa0000, 0xff000, 0xfec00000, 0xfee00000):
                self.assertEqual(P.memory_type(st, a), WB if a < 0xa0000 else UC, (hex(L), hex(a)))


if __name__ == '__main__':
    unittest.main()
