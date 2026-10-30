"""Hand-derived cases for syscall_model (SDM references in its docstring); expectations are computed by hand."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models import syscall_model as m, segment_model as sm, delivery_model as dm
from models.test_segment_model import machine as segment_machine, LDT

UD, GP, SS, PF = (dm.UD, None, 0), (dm.GP, 0, 0), (dm.SS, 0, 0), dm.PF
STAR = 0x0023_0010_0000_0000                           # Linux: SYSRET base 0x23, SYSCALL CS 0x10
LSTAR, FMASK, KGS = 0xFFFF_FFFF_8100_0000, 0x47700, 0xFFFF_8880_0000_0000
EXTRA = {0xE0: 0x67 | 1 << 40 | 1 << 47,                 # available 16-bit TSS (type 1)
         0xE8: int.from_bytes(dm.gate(0x1000, 0x08)[:8], 'little'),   # 64-bit interrupt gate (type E)
         0xF8: sm.code(0, conforming=True)}


def machine(cpl=0, ro=False):
    s = segment_machine(cpl, ro)
    base = s['gdtr'][0]
    for sel, raw in EXTRA.items():
        dm.place(s['memory'], base + sel, raw.to_bytes(8, 'little'))
    s.update(efer=0xD01, length=2, gpr={'rax': 0xFFFF_FFFF_FFFF_FFFF, 'rbx': 0, 'rcx': 0, 'r11': 0},
             msrs={'star': STAR, 'lstar': LSTAR, 'fmask': FMASK, 'kernel_gs_base': KGS})
    return s


def done(t, r):
    t.assertEqual((r['outcome'], r['diff']), ('done', {}))
    return r['state']


class FastCalls(unittest.TestCase):
    def test_syscall_sysret_round_trip(self):
        # Two derivations agree: a Linux user at CPL 3 with AC|OF|DF|IF|ZF|PF set enters and returns.
        s = machine(cpl=3)
        s['rflags'] = 0x40E46
        k = done(self, m.syscall(s))
        self.assertEqual((k['gpr']['rcx'], k['gpr']['r11'], k['rflags'], k['rip']), (0x40_0002, 0x40E46, 0x846, LSTAR))
        self.assertEqual((k['cs'], k['ss'], k['cpl'], k['seg']['cs'], k['seg']['ss']),
                         (0x10, 0x18, 0, (0x10, 0xA09B), (0x18, 0xC093)))
        self.assertEqual((k['rsp'], k['seg']['ds'], k['gs_base']), (s['rsp'], s['seg']['ds'], s['gs_base']))
        k['gpr']['r11'] |= 0x10000 | 0x20000 | 0x8      # RF, VM and reserved bit 3 are dropped by 3C7FD7H
        u = done(self, m.sysret(k))
        self.assertEqual((u['rip'], u['rflags'], u['cpl'], u['seg']['cs'], u['seg']['ss']),
                         (0x40_0002, 0x40E46, 3, (0x33, 0xA0FB), (0x2B, 0xC0F3)))

    def test_syscall_faults_and_quirks(self):
        s = machine(cpl=3)
        s['efer'] = 0xD00
        self.assertEqual(m.syscall(s)['fault'], UD)                                     # SCE = 0
        s = machine(cpl=3)
        s['efer'] = 0x101                                                               # LMA = 0
        self.assertEqual(m.syscall(s)['fault'], UD)
        s = machine(cpl=3)
        s['seg']['cs'] = (0x23, 0xC0FB)                                                 # compatibility mode
        self.assertEqual(m.syscall(s)['fault'], UD)
        s = machine(cpl=3)
        s['rflags'] = 0x302
        self.assertEqual(m.syscall(s)['outcome'], 'done')                               # TF (C3): FMASK decides
        s = machine(cpl=3)
        s['msrs']['star'] = 0x0023_0013_0000_0000                                       # C2: SS keeps the RPL bits
        k = m.syscall(s)['state']
        self.assertEqual((k['cs'], k['ss']), (0x10, 0x1B))
        s = machine(cpl=3)
        s['rflags'] = 0x10202                                                           # RF set (C1)
        r = m.syscall(s)
        self.assertEqual((r['state']['gpr']['r11'], r['state']['rflags']), (0x10202, 0x2))
        self.assertEqual([a['gpr']['r11'] for a in r['alternatives']], [0x202])

    def test_sysret_faults(self):
        s = machine()
        s['gpr'].update(rcx=0x40_0000, r11=0x202)
        u = done(self, m.sysret(s))
        self.assertEqual((u['rip'], u['rflags'], u['cs'], u['ss']), (0x40_0000, 0x202, 0x33, 0x2B))
        self.assertEqual(m.sysret(dict(s, efer=0xD00))['fault'], UD)
        self.assertEqual(m.sysret(dict(s, efer=0xD00, cpl=3))['fault'], UD)               # #UD before the CPL check
        self.assertEqual(m.sysret(dict(s, cpl=3))['fault'], GP)
        self.assertEqual(m.sysret(dict(s, cpl=3), width=4)['fault'], GP)
        self.assertEqual(m.sysret(s, width=4)['outcome'], 'refused')                    # compatibility mode
        bad = machine()
        bad['gpr'].update(rcx=0x0000_8000_0000_0000, r11=0x302)
        self.assertEqual(m.sysret(bad)['fault'], GP)                                    # non-canonical before TF
        self.assertEqual(m.sysret(bad, width=4)['outcome'], 'refused')                  # SYSRETL: no RCX check
        s['gpr']['r11'] = 0x302
        self.assertEqual(done(self, m.sysret(s))['rflags'], 0x302)                     # TF after (C3)
        s['gpr']['r11'] = 0x202
        self.assertEqual(done(self, m.sysret(dict(s, rflags=0x302)))['rflags'], 0x202)  # TF before (C3)
        s['gpr']['r11'] = 0x40202
        self.assertEqual(m.sysret(s)['outcome'], 'done')                                # AC, CR0.AM = 0
        self.assertEqual(m.sysret(dict(s, cr0=0x8005_0011))['outcome'], 'done')         # AC with AM: completes (C5)

    def test_swapgs(self):
        u = done(self, m.swapgs(machine()))
        self.assertEqual((u['gs_base'], u['msrs']['kernel_gs_base'], u['fs_base']), (KGS, 0x88, 0x77))
        self.assertEqual(m.swapgs(machine(cpl=3))['fault'], GP)


class Bases(unittest.TestCase):
    def test_fsgsbase(self):
        s = machine(cpl=3)
        self.assertEqual(m.fsgsbase(s, 'rdfsbase', 'rax')['fault'], UD)                 # CR4.FSGSBASE = 0
        s['cr4'] |= 1 << 16
        s['fs_base'] = 0xFFFF_8000_1234_5678
        self.assertEqual(done(self, m.fsgsbase(s, 'rdfsbase', 'rax'))['gpr']['rax'], 0xFFFF_8000_1234_5678)
        self.assertEqual(done(self, m.fsgsbase(s, 'rdfsbase', 'rax', 4))['gpr']['rax'], 0x1234_5678)
        self.assertEqual(done(self, m.fsgsbase(s, 'rdgsbase', 'rbx'))['gpr']['rbx'], 0x88)
        s['gpr']['rbx'] = 0xFFFF_FFFF_8000_0000
        self.assertEqual(done(self, m.fsgsbase(s, 'wrgsbase', 'rbx'))['gs_base'], 0xFFFF_FFFF_8000_0000)
        s['gpr']['rbx'] = 0x0000_8000_DEAD_BEEF
        self.assertEqual(m.fsgsbase(s, 'wrfsbase', 'rbx')['fault'], GP)                 # non-canonical
        self.assertEqual(done(self, m.fsgsbase(s, 'wrfsbase', 'rbx', 4))['fs_base'], 0xDEAD_BEEF)
        s['seg']['cs'] = (0x23, 0xC0FB)
        self.assertEqual(m.fsgsbase(s, 'rdfsbase', 'rax')['fault'], UD)                 # compatibility mode


class Verify(unittest.TestCase):
    def setUp(self):
        self.s = machine()
        self.s['rflags'] = 0x2C3                       # IF, SF, ZF, CF: only ZF may change

    def check(self, fn, sel, z, value=None, s=None, width=4):
        s = s or self.s
        r = fn(s, sel, 'rax', width) if fn in (m.lar, m.lsl) else fn(s, sel)
        u = done(self, r)
        self.assertEqual(u['rflags'], s['rflags'] & ~0x40 | (0x40 if z else 0), hex(sel))
        if fn in (m.lar, m.lsl):
            self.assertEqual(u['gpr']['rax'], value if z else s['gpr']['rax'], hex(sel))
        return r

    def test_lar(self):
        r = self.check(m.lar, 0x08, 1, 0x00A0_9B00)                                     # dword 00AF9B00 & 00F0FF00
        self.assertEqual(sorted(a['gpr']['rax'] for a in r['alternatives']),
                         [0x00A0_9B00 | x << 16 for x in range(1, 16)])                 # C6
        self.check(m.lar, 0x08, 1, 0xFFFF_FFFF_FFFF_9B00, width=2)
        self.check(m.lar, 0x08, 1, 0x00A0_9B00, width=8)
        self.check(m.lar, 0x10, 1, 0x00C0_9300)
        for sel, value in ((0x70, 0x8900), (0x80, 0x8B00), (0x90, 0x8200), (0xD0, 0x8C00), (0xA0, 0x0900)):
            self.check(m.lar, sel, 1, value)                                            # TSS, LDT, call gate; P = 0
        for sel in (0, 3, 0x100, 0x0C, 0x13, 0xE0, 0xE8, 0x78):
            self.check(m.lar, sel, 0)                    # null, limit, null LDTR, RPL > DPL, types 1 and E, type 0
        u = machine(cpl=3)
        for sel, z, value in ((0x08, 0, None), (0x73, 0, None), (0x1B, 1, 0x00C0_F300), (0xFB, 1, 0x00A0_9F00),
                              (0x5B, 1, 0x00A0_FF00)):
            self.check(m.lar, sel, z, value, u)          # DPL check; conforming code exempt
        self.check(m.lar, 0x0F, 0, s=dict(u, ldtr=(0x90, LDT, 0x17)))                  # LDT entry 1 is DPL 0
        self.s['ldtr'] = (0x90, LDT, 0x17)
        self.check(m.lar, 0x0C, 1, 0x00C0_9300)                                         # data(base=0x4242)
        self.check(m.lar, 0x1C, 0)                                                      # beyond the LDT limit

    def test_lsl(self):
        self.check(m.lsl, 0x08, 1, 0xFFFF_FFFF)
        self.check(m.lsl, 0x08, 1, 0xFFFF_FFFF_FFFF_FFFF, width=2)
        self.check(m.lsl, 0x70, 1, 0x67, width=8)
        self.check(m.lsl, 0x70, 1, 0xFFFF_FFFF_FFFF_0067, width=2)
        self.check(m.lsl, 0x90, 1, 0x17)
        for sel in (0xD0, 0xE0, 0xE8, 0x13, 0x100, 0):
            self.check(m.lsl, sel, 0)                    # call gate, 16-bit TSS, interrupt gate, RPL, limit, null
        self.check(m.lsl, 0x70, 0, s=machine(cpl=3))

    def test_verr_verw(self):
        for sel, z in ((0x10, 1), (0x08, 1), (0x30, 0), (0x40, 1), (0x70, 0), (0x13, 0), (0, 0), (0x100, 0)):
            self.check(m.verr, sel, z)
        for sel, z in ((0x10, 1), (0x40, 0), (0x08, 0), (0x70, 0), (0xF8, 0)):
            self.check(m.verw, sel, z)
        u = machine(cpl=3)
        for sel, z in ((0x13, 0), (0x5B, 1), (0xFB, 1), (0x1B, 1)):
            self.check(m.verr, sel, z, s=u)
        self.check(m.verw, 0x1B, 1, s=u)
        r = self.check(m.verr, 0x28, 1)                                                 # not present (C9)
        self.assertEqual([a['rflags'] for a in r['alternatives']], [0x283])

    def test_descriptor_page_faults(self):
        s = machine(cpl=3)
        s['gdtr'] = (0x9000, 0xFF)                                                      # absent page
        for fn in (m.lar, m.lsl):
            self.assertEqual(fn(s, 0x08, 'rax')['fault'], (PF, 0, 0x9008))              # implicit supervisor: U/S = 0
        self.assertEqual(m.verw(s, 0x13)['fault'], (PF, 0, 0x9010))
        r = machine(cpl=3, ro=True)                                                     # GDT on a supervisor page
        self.assertEqual(m.verr(r, 0x1B)['outcome'], 'done')


class Misc(unittest.TestCase):
    def test_rdrand(self):
        s = machine(cpl=3)
        s['rflags'] = 0x2 | 0x200 | 0x400 | 0x8D5        # IF, DF, OF SF ZF AF PF CF
        cases = ((2, 0x1234_5678, 0xFFFF_FFFF_FFFF_5678, 0x603), (4, 0x1234_5678, 0x1234_5678, 0x603),
                 (8, 0xDEAD, 0xDEAD, 0x603), (4, None, 0, 0x602), (2, None, 0xFFFF_FFFF_FFFF_0000, 0x602))
        for width, value, rax, flags in cases:
            u = done(self, m.rdrand(s, 'rax', width, value))
            self.assertEqual((u['gpr']['rax'], u['rflags']), (rax, flags), (width, value))
        self.assertEqual(m.rdseed(s, 'rax')['fault'], UD)
        self.assertEqual(m.rdpid(s, 'rax')['fault'], UD)

    def test_cache(self):
        for op in ('wbinvd', 'invd', 'wbnoinvd'):
            s = machine()
            self.assertEqual(done(self, m.cache(s, op)), s)
            self.assertEqual(m.cache(machine(cpl=3), op)['fault'], GP)
        self.assertEqual(m.cache(machine(), 'wbnoinvd')['permitted'], {('done', None), ('fault', UD)})   # C11
        self.assertEqual(m.cache(machine(cpl=3), 'wbnoinvd')['permitted'], {('fault', GP), ('fault', UD)})

    def test_clflush(self):
        u = machine(cpl=3)
        self.assertEqual(done(self, m.clflush(u, 0x5123)), u)
        self.assertEqual(m.clflush(u, 0x2010)['fault'], (PF, 5, 0x2010))               # user read of a supervisor page
        self.assertEqual(m.clflush(u, 0x9000)['fault'], (PF, 4, 0x9000))
        self.assertEqual(m.clflush(machine(), 0x9000)['fault'], (PF, 0, 0x9000))
        self.assertEqual(done(self, m.clflush(machine(), 0x2010))['cpl'], 0)            # read-only page: a read
        self.assertEqual(m.clflush(u, 0x0000_8000_0000_0000)['fault'], GP)
        self.assertEqual(m.clflush(u, 0x0000_8000_0000_0000, stack=True)['fault'], SS)
        self.assertEqual(m.clflushopt(u, 0x5000)['fault'], UD)
        self.assertEqual(m.clwb(u, 0x5000)['fault'], UD)


if __name__ == '__main__':
    unittest.main()
