"""Hand-derived cases for the 64-bit segment model (SDM references in segment_model's docstring), and its IRETQ against
delivery_model's."""
import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models import segment_model as m, delivery_model as dm

GDT, RO, LDT, STACK, PTR = 0x1000, 0x2000, 0x3000, 0x5000, 0x6000
TSS_BASE = 0xFFFF_8000_0000_0000
D = {0x08: m.code(), 0x10: m.data(), 0x18: m.data(3), 0x20: m.code(3), 0x28: m.data(present=False),
     0x30: m.code(readable=False), 0x38: m.code(l=0, d=1), 0x40: m.data(writable=False), 0x48: m.data(accessed=False),
     0x50: m.data(base=0x1234_5678), 0x58: m.code(3, conforming=True), 0x60: m.code(present=False),
     0x68: m.data(present=False)}
WIDE = {0x70: m.system_descriptor(TSS_BASE, 0x67, 9), 0x80: m.system_descriptor(0x4000, 0x67, 0xB),
        0x90: m.system_descriptor(LDT, 0x17, 2), 0xA0: m.system_descriptor(0x4000, 0x67, 9, present=False),
        0xB0: m.system_descriptor(0x4000, 0x67, 9, upper_type=1), 0xC0: m.system_descriptor(0x8000_0000_0000, 0x67, 9)}
CALL_GATE = 0xD0


def machine(cpl=0, ro=False):
    memory = {p: dm.page(user=True) for p in (1, 3, 5, 6)}
    memory[2] = dm.page(write=False)
    base = RO if ro else GDT
    for sel, raw in D.items():
        dm.place(memory, base + sel, raw.to_bytes(8, 'little'))
    for sel, raw in WIDE.items():
        dm.place(memory, base + sel, raw)
    dm.place(memory, base + CALL_GATE, dm.gate(0x1000, 0x08, gtype=0xC))
    dm.place(memory, LDT + 8, m.data(base=0x4242).to_bytes(8, 'little'))
    seg = {'es': (0x10, 0xC093), 'cs': (0x08, 0xA09B), 'ss': (0x10, 0xC093),
           'ds': (0x10, 0xC093), 'fs': (0, 0), 'gs': (0, 0)}
    if cpl:
        seg.update(cs=(0x23, 0xA0FB), ss=(0x1B, 0xC0F3), ds=(0, 0), es=(0, 0))
    return dict(rip=0x40_0000, length=3, rsp=STACK + 0x800, rflags=0x202, cpl=cpl, cs=seg['cs'][0], ss=seg['ss'][0],
                cs_l=1, cr0=0x8001_0011, cr2=0, cr4=0x20, gdtr=(base, 0xFF), idtr=(0, 0xFFF), ldtr=None, tr=None,
                seg=seg, fs_base=0x77, gs_base=0x88, memory=memory, nmi_blocked=False, shadow=None, halted=False)


def push(state, *words):
    """Place `words` (first = lowest address) at the stack top."""
    for k, w in enumerate(words):
        dm.place(state['memory'], state['rsp'] + 8 * k, w.to_bytes(8, 'little'))
    return state


class Loads(unittest.TestCase):
    def fault(self, r): return r['outcome'], r['fault'] and r['fault'][:2]

    def test_data_segment_loads(self):
        s = machine()
        r = m.load(s, 'ds', 0x48)
        self.assertEqual((r['outcome'], r['diff']), ('done', {GDT + 0x48 + 5: bytes([0x93])}))   # accessed bit set
        self.assertEqual(r['state']['seg']['ds'], (0x48, 0xC093))
        self.assertEqual(self.fault(m.load(s, 'ds', 0x28)), ('fault', (dm.NP, 0x28)))
        self.assertEqual(self.fault(m.load(s, 'es', 0x30)), ('fault', (dm.GP, 0x30)))     # execute-only code
        self.assertEqual(self.fault(m.load(s, 'ds', 0x13)), ('fault', (dm.GP, 0x10)))     # RPL 3 > DPL 0
        self.assertEqual(self.fault(m.load(s, 'ds', 0x100)), ('fault', (dm.GP, 0x100)))   # beyond the limit
        self.assertEqual(self.fault(m.load(s, 'ds', 0x0C)), ('fault', (dm.GP, 0x0C)))     # TI = 1, null LDTR (error keeps TI)
        self.assertEqual(m.load(s, 'ds', 0x08)['outcome'], 'done')                       # readable code
        u = machine(cpl=3)
        self.assertEqual(self.fault(m.load(u, 'ds', 0x10)), ('fault', (dm.GP, 0x10)))     # CPL 3 > DPL 0
        self.assertEqual(m.load(u, 'ds', 0x5B)['outcome'], 'done')                       # conforming code: no DPL check
        self.assertEqual(m.load(u, 'ds', 0x1B)['state']['seg']['ds'], (0x1B, 0xC0F3))

    def test_stack_segment_loads(self):
        s = machine()
        self.assertEqual(m.load(s, 'ss', 0)['state']['seg']['ss'], (0, 0))               # null SS at CPL 0
        self.assertEqual(self.fault(m.load(s, 'ss', 1)), ('fault', (dm.GP, 0)))           # null, RPL != CPL
        self.assertEqual(self.fault(m.load(machine(cpl=3), 'ss', 3)), ('fault', (dm.GP, 0)))
        self.assertEqual(self.fault(m.load(s, 'ss', 0x40)), ('fault', (dm.GP, 0x40)))     # read-only data
        self.assertEqual(self.fault(m.load(s, 'ss', 0x18)), ('fault', (dm.GP, 0x18)))     # DPL 3 != CPL 0
        self.assertEqual(self.fault(m.load(s, 'ss', 0x08)), ('fault', (dm.GP, 0x08)))     # code
        self.assertEqual(self.fault(m.load(s, 'ss', 0x68)), ('fault', (dm.SS, 0x68)))     # not present: #SS

    def test_fs_gs_bases(self):
        s = machine()
        self.assertEqual(m.load(s, 'fs', 0x50)['state']['fs_base'], 0x1234_5678)
        self.assertEqual(m.load(s, 'gs', 0)['state']['gs_base'], 0)
        self.assertEqual(m.load(s, 'ds', 0)['state']['gs_base'], 0x88)

    def test_read_only_gdt(self):
        s = machine(ro=True)
        self.assertEqual(m.load(s, 'ds', 0x10)['outcome'], 'done')                       # A already set: no write
        self.assertEqual(m.load(s, 'ds', 0x48)['fault'], (dm.PF, 3, RO + 0x48 + 5))        # supervisor write, P = 1
        self.assertEqual(m.ltr(s, 0x70)['fault'], (dm.PF, 3, RO + 0x70 + 5))
        s['cr0'] &= ~dm.CR0_WP                                                           # WP = 0: supervisor writes pass
        self.assertEqual(m.ltr(s, 0x70)['outcome'], 'done')


class Tables(unittest.TestCase):
    def test_lgdt_sgdt(self):
        s = machine()
        dm.place(s['memory'], PTR, (0x37).to_bytes(2, 'little') + (0xFFFF_8000_1234_0000).to_bytes(8, 'little'))
        r = m.lgdt(s, PTR)
        self.assertEqual(r['state']['gdtr'], (0xFFFF_8000_1234_0000, 0x37))
        self.assertEqual(m.sgdt(r['state'], PTR + 0x10)['diff'], {PTR + 0x10: bytes.fromhex('3700') + (0xFFFF_8000_1234_0000).to_bytes(8, 'little')})
        dm.place(s['memory'], PTR, (0x37).to_bytes(2, 'little') + (0x0000_8000_0000_0000).to_bytes(8, 'little'))
        self.assertEqual(m.lgdt(s, PTR)['fault'][:2], (dm.GP, 0))                            # non-canonical base (B2)
        self.assertEqual(m.lgdt(machine(cpl=3), PTR)['fault'][:2], (dm.GP, 0))
        self.assertEqual(m.sgdt(machine(cpl=3), PTR, 'idtr')['outcome'], 'done')            # no UMIP

    def test_ltr_lldt(self):
        s = machine()
        r = m.ltr(s, 0x70)
        self.assertEqual((r['state']['tr'], r['diff']), ((0x70, TSS_BASE, 0x67), {GDT + 0x75: bytes([0x8B])}))
        for sel, want in ((0x80, (dm.GP, 0x80)), (0x90, (dm.GP, 0x90)), (0xA0, (dm.NP, 0xA0)), (0xB0, (dm.GP, 0xB0)),
                          (0xC0, (dm.GP, 0xC0)), (0x08, (dm.GP, 0x08)), (0, (dm.GP, 0)), (0x74, (dm.GP, 0x74)),
                          (0xF8, (dm.GP, 0xF8))):
            self.assertEqual(m.ltr(s, sel)['fault'][:2], want, hex(sel))
        self.assertEqual(m.ltr(machine(cpl=3), 0x70)['fault'][:2], (dm.GP, 0))
        r = m.lldt(s, 0x90)
        self.assertEqual(r['state']['ldtr'], (0x90, LDT, 0x17))
        l = m.load(r['state'], 'es', 0x0C)                                                 # LDT entry 1
        self.assertEqual(l['state']['seg']['es'], (0x0C, 0xC093))
        self.assertEqual(m.load(r['state'], 'es', 0x1C)['fault'][:2], (dm.GP, 0x1C))       # beyond the LDT limit
        self.assertEqual(m.lldt(s, 0x70)['fault'][:2], (dm.GP, 0x70))
        self.assertIsNone(m.lldt(r['state'], 0)['state']['ldtr'])


class Transfers(unittest.TestCase):
    def pointer(self, s, rip, sel, width=8):
        dm.place(s['memory'], PTR, rip.to_bytes(width, 'little') + sel.to_bytes(2, 'little'))
        return s

    def test_far_jump_and_call(self):
        s = self.pointer(machine(), 0x40_1000, 0x08)
        r = m.far(s, 'jmp', PTR)
        self.assertEqual((r['state']['rip'], r['state']['cs']), (0x40_1000, 0x08))
        r = m.far(s, 'call', PTR)
        self.assertEqual((r['state']['rip'], r['state']['rsp']), (0x40_1000, STACK + 0x7F0))
        self.assertEqual(r['diff'], {STACK + 0x7F0: (0x40_0003).to_bytes(8, 'little') + (0x08).to_bytes(8, 'little')})
        # M2 Part 2: CPL-0 compatibility-mode code (0x38: L = 0, D = 1) is the boot-time interpreter's: the JMP completes.
        r = m.far(self.pointer(machine(), 0x1000, 0x38), 'jmp', PTR)
        self.assertEqual((r['outcome'], r['state']['rip'], r['state']['cs']), ('done', 0x1000, 0x38))
        self.assertEqual(m.far(self.pointer(machine(), 0x1000, CALL_GATE), 'jmp', PTR)['outcome'], 'refused')
        for sel, want in ((0x10, (dm.GP, 0x10)), (0x20, (dm.GP, 0x20)), (0x60, (dm.NP, 0x60)), (0x0B, (dm.GP, 0x08)),
                          (0x70, (dm.GP, 0x70)), (0, (dm.GP, 0))):
            self.assertEqual(m.far(self.pointer(machine(), 0x1000, sel), 'jmp', PTR)['fault'][:2], want, hex(sel))
        self.assertEqual(m.far(self.pointer(machine(), 1 << 47, 0x08), 'jmp', PTR)['fault'][:2], (dm.GP, 0))
        self.assertEqual(m.far(self.pointer(machine(), 0x1000, 0x08, 4), 'jmp', PTR, 4)['state']['rip'], 0x1000)

    def test_far_returns(self):
        r = m.ret(push(machine(), 0x40_2000, 0x08))
        self.assertEqual((r['state']['rip'], r['state']['rsp'], r['state']['cpl']), (0x40_2000, STACK + 0x810, 0))
        r = m.ret(push(machine(), 0x40_2000, 0x08, 0, 0), release=16)
        self.assertEqual(r['state']['rsp'], STACK + 0x820)
        r = m.ret(push(machine(), 0x40_2000, 0x23, 0x7000, 0x1B))                          # to CPL 3
        st = r['state']
        self.assertEqual((st['cpl'], st['rsp'], st['ss'], st['seg']['ds'], st['seg']['es'], st['fs_base']),
                         (3, 0x7000, 0x1B, (0, 0), (0, 0), 0x77))
        self.assertEqual(m.ret(push(machine(), 0x40_2000, 0x23, 0x7000, 0x03))['fault'][:2], (dm.GP, 0))  # null SS at CPL 3
        self.assertEqual(m.ret(push(machine(), 0x40_2000, 0x23, 0x7000, 0x13))['fault'][:2], (dm.GP, 0x10))
        self.assertEqual(m.ret(push(machine(cpl=3), 0x40_2000, 0x08))['fault'][:2], (dm.GP, 0x08))  # RPL < CPL
        r = m.ret(push(machine(), 0x40_2000, 0x38))                                         # to CPL-0 32-bit code
        self.assertEqual((r['outcome'], r['state']['rip'], r['state']['cs']), ('done', 0x40_2000, 0x38))
        self.assertEqual(m.iret(push(machine(), 0x40_2000, 0x38, 2, STACK, 0x10))['outcome'], 'refused')   # IRET: refused
        # The compatibility-mode deviation: a return to RPL 3 compatibility-mode code is #GP(selector), at CPL 0 too.
        s = machine()
        dm.place(s['memory'], GDT + 0x48, m.code(3, l=0, d=1).to_bytes(8, 'little'))
        r = m.ret(push(s, 0x40_2000, 0x4B, 0x7000, 0x1B))
        self.assertEqual((r['fault'][:2], r['deviation']), ((dm.GP, 0x48), True))
        self.assertEqual(m.ret(push(machine(), 1 << 47, 0x08))['fault'][:2], (dm.GP, 0))

    def compat(self, cs=None, **over):
        """A CPL 0 machine with DPL 3 code at 0x48 (32-bit unless `cs`), DS = ES = the flat DPL 3 data segment 0x1B."""
        s = machine()
        dm.place(s['memory'], GDT + 0x48, (cs or m.code(3, l=0, d=1)).to_bytes(8, 'little'))
        s['seg'].update(ds=(0x1B, 0xC0F3), es=(0x1B, 0xC0F3))
        s['seg'].update(over)
        return s

    def test_iret_into_32_bit_code(self):
        # M2 task 3a: IRETQ to a 32-bit code segment with RPL 3 is a real load; RSP is zero-extended from ESP.
        frame = (0x40_2000, 0x4B, 0x202, 0x1_0000_7000, 0x1B)
        r = m.iret(push(self.compat(), *frame))
        s = r['state']
        self.assertEqual((r['outcome'], s['rip'], s['cpl'], s['seg']['cs'], s['seg']['ss'], s['rsp'], s['seg']['ds']),
                         ('done', 0x40_2000, 3, (0x4B, 0xC0FB), (0x1B, 0xC0F3), 0x7000, (0x1B, 0xC0F3)))
        # The frame's RIP against the 4 GiB limit: #GP(0) at the IRET, nothing changed.
        r = m.iret(push(self.compat(), 1 << 32, 0x4B, 0x202, 0x7000, 0x1B))
        self.assertEqual((r['fault'][:2], r['deviation'], r['state']['cpl']), ((dm.GP, 0), False, 0))
        # Segments natemu does not run 32-bit code with: the deviation, #GP(selector) at the target after the IRET.
        deviation = lambda r: (r['fault'][:2], r['deviation'], r['fault_rip'], r['state']['cpl'], r['state']['rsp'])
        for state, selector in ((self.compat(cs=m.code(3, l=0, d=0)), 0x48),              # a 16-bit code segment
                                (self.compat(es=(0x1B, 0xC0F1)), 0x18),                   # ES read-only
                                (dict(self.compat(), shape={'ds': (0x1000, 0xFFFFFFFF)}), 0x18)):   # DS with a base
            self.assertEqual(deviation(m.iret(push(state, 0x40_2000, 0x4B, 0x202, 0x7000, 0x1B))),
                             ((dm.GP, selector), True, 0x40_2000, 3, 0x7000))
        # A DS the return nulls (DPL 0) runs: its first use faults (null_access).
        r = m.iret(push(self.compat(ds=(0x10, 0xC093)), 0x40_2000, 0x4B, 0x202, 0x7000, 0x1B))
        self.assertEqual((r['outcome'], r['state']['seg']['ds'], m.null_access(r['state'], 'ds')['fault'][:2]), ('done', (0, 0), (dm.GP, 0)))
        # A stack segment with a base (0x50 at DPL 3): the same, naming SS.
        s = self.compat()
        dm.place(s['memory'], GDT + 0x50, m.data(3, base=0x1234_5678).to_bytes(8, 'little'))
        self.assertEqual(deviation(m.iret(push(s, 0x40_2000, 0x4B, 0x202, 0x7000, 0x53)))[:3], ((dm.GP, 0x50), True, 0x40_2000))
        # RET far to it is still not built (task 5), and L = D = 1 is never loaded.
        self.assertEqual(m.ret(push(self.compat(), 0x40_2000, 0x4B, 0x7000, 0x1B))['fault'][:2], (dm.GP, 0x48))
        self.assertEqual(m.iret(push(self.compat(cs=m.code(3, l=1, d=1)), *frame))['fault'][:2], (dm.GP, 0x48))

    def test_32_bit_code(self):
        # In 32-bit code at CPL 3: a load that leaves a segment natemu does not run is its deviation at the next instruction.
        s = self.compat()
        s.update(cpl=3, cs=0x4B, ss=0x1B)
        s['seg'].update(cs=(0x4B, 0xC0FB), ss=(0x1B, 0xC0F3))
        dm.place(s['memory'], GDT + 0x50, m.data(3, base=0x1234_5678).to_bytes(8, 'little'))
        r = m.load(s, 'ds', 0x53)
        self.assertEqual((r['fault'][:2], r['deviation'], r['fault_rip'], r['state']['seg']['ds']),
                         ((dm.GP, 0x50), True, 0x40_0003, (0x53, 0xC0F3)))
        self.assertEqual(m.load(s, 'gs', 0x53)['outcome'], 'done')                       # FS and GS may have a base
        self.assertEqual(m.load(s, 'ds', 0x1B)['outcome'], 'done')
        # A null FS or GS faults on use there, not in 64-bit mode; an access may not run past 4 GiB.
        self.assertEqual(m.null_access(s, 'fs')['fault'][:2], (dm.GP, 0))
        self.assertEqual(m.null_access(m.load(s, 'gs', 0x53)['state'], 'gs')['outcome'], 'done')
        self.assertEqual(m.null_access(machine(3), 'fs')['outcome'], 'done')
        self.assertEqual(m.flat_access(s, 0xFFFF_FFFC, 4)['outcome'], 'done')
        r = m.flat_access(s, 0xFFFF_FFFE, 4)                                           # B10: the fault, or not
        self.assertEqual((r['fault'][:2], r['permitted']), ((dm.GP, 0), {('fault', (dm.GP, 0, 0)), ('done', None)}))
        self.assertEqual(m.flat_access(s, 0xFFFF_FFFD, 4, stack=True)['fault'][:2], (dm.SS, 0))

    def test_32_bit_far_transfers(self):
        # M2 task 3b, in 32-bit code at CPL 3: far transfers to 32-bit code run, with 4-byte slots and ESP wrapping; to
        # 64-bit code they are natemu's deviation (task 5); IRET pops three words at the same level.
        s = self.compat()
        s.update(cpl=3, cs=0x4B, ss=0x1B, rip=0x40_0000, length=6, rsp=0)
        s['seg'].update(cs=(0x4B, 0xC0FB), ss=(0x1B, 0xC0F3))
        for page in (0, 0xFFFFF):
            s['memory'][page] = dm.page(user=True)
        r = m.far(self.pointer(s, 0x40_1000, 0x4B, 4), 'call', PTR, 4)
        self.assertEqual((r['outcome'], r['state']['rip'], r['state']['rsp']), ('done', 0x40_1000, 0xFFFF_FFF8))
        self.assertEqual(r['diff'], {0xFFFF_FFF8: (0x40_0006).to_bytes(4, 'little') + (0x4B).to_bytes(4, 'little')})
        r = m.far(self.pointer(dict(s, rsp=4), 0x40_1000, 0x4B, 4), 'call', PTR, 4)
        self.assertEqual(r['fault'][:2], (dm.SS, 0))                                  # the frame across 4 GiB
        r = m.far(self.pointer(s, 0x1000, 0x23, 4), 'jmp', PTR, 4)
        self.assertEqual((r['fault'][:2], r['deviation']), ((dm.GP, 0x20), True))
        frame = lambda *w: b''.join(x.to_bytes(4, 'little') for x in w)
        s = dict(s, rsp=0x5800)
        dm.place(s['memory'], 0x5800, frame(0x40_2000, 0x4B, 0x8D7))
        r = m.iret(s, 4)
        self.assertEqual((r['outcome'], r['state']['rip'], r['state']['rsp'], r['state']['rflags']), ('done', 0x40_2000, 0x580C, 0xAD7))
        dm.place(s['memory'], 0x5800, frame(0x40_2000, 0x23, 0x202))
        r = m.iret(s, 4)
        self.assertEqual((r['outcome'], r['state']['rsp'], r['state']['cs']), ('done', 0x580C, 0x23))   # to 64-bit code: no SS:ESP
        r = m.ret(s, 4)
        self.assertEqual((r['fault'][:2], r['deviation']), ((dm.GP, 0x20), True))
        dm.place(s['memory'], 0x5800, frame(0x40_2000, 0x4B))
        self.assertEqual(m.ret(s, 4, release=8)['state']['rsp'], 0x5810)

    def test_iretq_agrees_with_the_delivery_model(self):
        frames = [(0x40_2000, 0x08, 0x246, STACK + 0x100, 0x10), (0x40_2000, 0x23, 0x3246, 0x7000, 0x1B),
                  (0x40_2000, 0x08, 0x202, STACK, 0), (0x40_2000, 0x23, 0x202, 0x7000, 3), (0x40_2000, 0x10, 2, 0, 0x10),
                  (0x40_2000, 0x08, 2, 0, 0x18), (0x40_2000, 0x60, 2, 0, 0x10), (1 << 47, 0x08, 2, 0, 0x10),
                  (0x40_2000, 0x08, 2, 0, 0x68), (0x40_2000, 0x0B, 2, 0, 0x10), (0x40_2000, 0x08, 0x40202, 0, 0x10),
                  (0x40_2000, 0x23, 0x180202, 0x7000, 0x1B), (0x40_2000, 0x08, 0x180202, STACK, 0x10)]
        for frame in frames:
            for cpl in (0, 3):
                s = push(machine(cpl), *frame)
                ours, theirs = m.iret(s), dm.iretq(s)
                if theirs['outcome'] == 'returned':
                    want = {k: theirs['state'][k] for k in ('rip', 'rsp', 'cs', 'ss', 'cpl', 'rflags')}
                    self.assertEqual(ours['outcome'], 'done', frame)
                    self.assertEqual({k: ours['state'][k] for k in want}, want, (frame, cpl))
                else:
                    self.assertEqual(ours['fault'][:2], theirs['events'][0], (frame, cpl))
        s = push(machine(), 0x40_2000, 0x08, 0x302, STACK, 0x10)
        self.assertEqual(m.iret(s)['state']['rflags'], 0x302)                             # TF loaded (its trap: S4)

    def test_iretq_flags_and_null_ss(self):
        # VIF/VIP are restored only at CPL 0 (SDM IRET); at CPL 3 the frame's bits are ignored.
        r = m.iret(push(machine(cpl=3), 0x40_2000, 0x23, 0x180202, 0x7000, 0x1B))
        self.assertEqual(r['state']['rflags'], 0x202)
        self.assertEqual(m.iret(push(machine(), 0x40_2000, 0x08, 0x180202, STACK, 0x10))['state']['rflags'], 0x180202)
        # B6: a null SS whose RPL != the new CPL (< 3) is #GP(0); RPL = CPL is loaded.
        self.assertEqual(m.iret(push(machine(), 0x40_2000, 0x08, 2, STACK, 1))['fault'][:2], (dm.GP, 0))
        self.assertEqual(m.iret(push(machine(), 0x40_2000, 0x08, 2, STACK, 0))['state']['ss'], 0)

    def test_permitted_alternatives(self):
        s = machine()
        dm.place(s['memory'], PTR, (0x37).to_bytes(2, 'little') + (1 << 47).to_bytes(8, 'little'))
        self.assertEqual(m.lgdt(s, PTR)['permitted'], {('fault', (dm.GP, 0, 0)), ('done', None)})          # B2
        r = m.load(machine(ro=True), 'ds', 0x48)
        self.assertEqual(r['permitted'], {('fault', (dm.PF, 3, RO + 0x48 + k)) for k in (0, 4, 5)})       # B8
        self.assertEqual(m.ltr(machine(ro=True), 0x70)['permitted'], {('fault', (dm.PF, 3, RO + 0x70 + k)) for k in (0, 4, 5)})
        r = m.iret(push(machine(), 0x40_2000, 0x08, 2, STACK, 0x68))
        self.assertEqual(r['permitted'], {('fault', (dm.SS, 0x68, 0)), ('fault', (dm.NP, 0x68, 0))})      # B9
        self.assertEqual(m.load(s, 'ds', 0x28)['permitted'], {('fault', (dm.NP, 0x28, 0))})

    def test_ret_checks_ss_before_rip(self):
        # B4: an outer RET with both a non-canonical RIP and a bad SS raises the SS fault (pseudocode order).
        r = m.ret(push(machine(), 1 << 47, 0x23, 0x7000, 0x13))
        self.assertEqual(r['fault'][:2], (dm.GP, 0x10))


# ---- the interpreter's modes (R, P16, P32, C32 at CPL 0): far JMP, far RET, MOV Sreg ---------------------------------

IGDT, ISTACK, IPTR = 0x7000, 0x9000, 0xA000
IDESC = {0x08: m.segment(0, 0xFFFFFFFF, 0x9B, 4),            # flat 32-bit code
         0x10: m.segment(0, 0xFFFFFFFF, 0x93, 4),            # flat data
         0x18: m.segment(0x10000, 0xFFFF, 0x9B),             # 16-bit code, base 10000H, 64 KiB
         0x20: m.segment(0, 0xFFFFFFFF, 0x9B, 6),            # L = D = 1: 32-bit code while LMA = 0
         0x28: m.segment(0, 0xFFFF, 0x9B, 2),                # L = 1, D = 0: 16-bit code while LMA = 0
         0x30: m.segment(0, 0xFFFF, 0x1B),                   # not present
         0x38: m.segment(0, 0xFFFF, 0xFB),                   # DPL 3, nonconforming
         0x40: m.segment(0, 0xFFFF, 0x9F),                   # conforming, DPL 0
         0x48: m.segment(0, 0xFFFFFFFF, 0x9A, 4),            # code, accessed bit clear
         0x50: m.segment(0x1000, 0x67, 0x89),                # 32-bit TSS
         0x58: m.segment(0, 0, 0x8C),                        # call gate (32-bit, or 64-bit under LMA)
         0x60: m.segment(0x2000, 0x17, 0x82),                # LDT
         0x68: m.segment(0, 0, 0x86),                        # 16-bit interrupt gate
         0x70: m.segment(0, 0xFFFFFFFF, 0x9B, 2),            # 64-bit code (L = 1, D = 0)
         0x78: m.segment(0, 0x0FFF, 0x93),                   # data, limit FFFH
         0x80: m.segment(0, 0xFFFFFFFF, 0x91, 4),            # read-only data
         0x88: m.segment(0, 0xFFFFFFFF, 0x85),               # task gate
         0x90: m.segment(0, 0xFFFFFFFF, 0xFB, 4)}            # DPL 3 32-bit code


def interp(mode, cpl=0):
    """An interpreted state: 'R' (after a SIPI with vector 9), 'P16' (PE just set under that CS), 'P32' (flat), 'C32'
    (compatibility mode under the delivery bus's paging: every page present and writable)."""
    memory = {p: dm.page() for p in range(16)}
    for sel, raw in IDESC.items():
        dm.place(memory, IGDT + sel, raw.to_bytes(8, 'little'))
    s = m.reset_state(memory, sipi=9)
    s.update(gdtr=(IGDT, 0x97), rsp=ISTACK)
    if mode != 'R':
        s['cr0'] |= 1
    if mode in ('P32', 'C32'):
        flat = {r: (0x10, 0xC093) for r in m.SREGS}
        flat['cs'] = (0x08 | cpl, 0xC09B if cpl == 0 else 0xC0FB)
        s.update(seg=flat, shape={r: (0, 0xFFFFFFFF) for r in m.SREGS}, cs=flat['cs'][0], ss=0x10, cpl=cpl,
                 rip=0x1000)
    if mode == 'C32':
        s.update(cr0=s['cr0'] | 1 << 31, cr4=0x20, efer=0x500)
    return s


def frame(s, *words, width=4):
    """Place `words` (first = lowest) at SS:ESP, a 16-bit stack's offsets wrapping at 64 KiB."""
    big = s['seg']['ss'][1] >> 14 & 1
    for k, w in enumerate(words):
        off = s['rsp'] + k * width
        dm.place(s['memory'], s['shape']['ss'][0] + (off if big else off & 0xFFFF), w.to_bytes(width, 'little'))
    return s


class Interpreted(unittest.TestCase):
    def f(self, r): return r['outcome'], r['fault'] and r['fault'][:2]

    def test_modes(self):
        self.assertEqual([m.mode_of(interp(x)) for x in ('R', 'P16', 'P32', 'C32')], ['R', 'P16', 'P32', 'C32'])
        self.assertIsNone(m.interpreted_mode(0x80000011, 0x500, 0xA09B, 0))   # 64-bit code
        self.assertIsNone(m.interpreted_mode(0x80000011, 0x500, 0xC0FB, 3))   # compatibility mode at CPL 3: translated
        self.assertEqual(m.interpreted_mode(0x11, 0, 0xE09B, 0), 'P32')        # L ignored while LMA = 0
        self.assertEqual(m.interpreted_mode(0x11, 0, 0x209B, 0), 'P16')

    def test_reset_and_sipi_state(self):
        r = m.reset_state()
        self.assertEqual((r['cr0'], r['rflags'], r['rip'], r['seg']['cs'], r['shape']['cs']),
                         (0x60000010, 2, 0xFFF0, (0xF000, 0x9B), (0xFFFF0000, 0xFFFF)))
        self.assertEqual(m.reset_state(cd_nw=0)['cr0'], 0x10)                   # INIT keeps CD and NW
        s = m.reset_state(sipi=0x9A)
        self.assertEqual((s['seg']['cs'][0], s['shape']['cs'], s['rip']), (0x9A00, (0x9A000, 0xFFFF), 0))

    def test_real_mode_far_jump(self):
        s = interp('R')
        r = m.jmp_far_interp(s, (0x1000, 0x10), osize=16)
        self.assertEqual((r['outcome'], r['state']['seg']['cs'], r['state']['shape']['cs'], r['state']['rip']),
                         ('done', (0x1000, 0x9B), (0x10000, 0xFFFF), 0x10))   # B11: limit and attributes kept
        self.assertEqual(r['diff'], {})                                          # no descriptor, no accessed bit
        self.assertEqual(self.f(m.jmp_far_interp(s, (0x1000, 0x12345))), ('fault', (dm.GP, 0)))   # beyond CS limit
        u = interp('R')
        u['shape']['cs'] = (0x9000, 0xFFFFFFFF)                                  # a stale 4 GiB CS limit
        self.assertEqual(m.jmp_far_interp(u, (0x1000, 0x12345))['state']['rip'], 0x12345)
        dm.place(s['memory'], 0x200, (0x4321).to_bytes(4, 'little') + (0x2000).to_bytes(2, 'little'))
        r = m.jmp_far_interp(s, pointer=('ds', 0x200))                           # m16:32 through DS base 0
        self.assertEqual((r['state']['cs'], r['state']['shape']['cs'][0], r['state']['rip']), (0x2000, 0x20000, 0x4321))
        self.assertEqual(self.f(m.jmp_far_interp(s, pointer=('ds', 0xFFFC))), ('fault', (dm.GP, 0)))  # crosses DS limit
        self.assertEqual(self.f(m.jmp_far_interp(s, pointer=('ss', 0xFFFC))), ('fault', (dm.SS, 0)))

    def test_real_mode_far_return_and_loads(self):
        s = frame(interp('R'), 0x1234, 0x5000)
        r = m.ret_far_interp(s, release=8)
        self.assertEqual((r['state']['cs'], r['state']['shape']['cs'][0], r['state']['rip'], r['state']['rsp']),
                         (0x5000, 0x50000, 0x1234, ISTACK + 16))
        r = m.ret_far_interp(frame(interp('R'), 0x10000, 0x5000))
        self.assertEqual(self.f(r), ('fault', (dm.GP, 0)))                       # B12: EIP beyond the CS limit
        self.assertIn(('done', None), r['permitted'])
        u = frame(interp('R'), 0x2000, 0x5000, width=2)
        u['shape']['cs'] = (0x9000, 0xFFF)                                       # (a stale 4 KiB CS limit)
        r = m.ret_far_interp(u, osize=16)
        self.assertEqual((self.f(r), r['permitted']), (('fault', (dm.GP, 0)), {('fault', (dm.GP, 0, 0))}))
        w = interp('R')
        w['rsp'] = 0xFFFE                                                        # B13: the second slot wraps to 0
        dm.place(w['memory'], 0xFFFE, (0x40).to_bytes(2, 'little'))
        dm.place(w['memory'], 0, (0x7000).to_bytes(2, 'little'))
        r = m.ret_far_interp(w, osize=16)
        self.assertEqual((r['state']['cs'], r['state']['rip'], r['state']['rsp']), (0x7000, 0x40, 2))
        w['rsp'] = 0xFFFF
        self.assertEqual(self.f(m.ret_far_interp(w)), ('fault', (dm.SS, 0)))     # a 4-byte slot across 64 KiB
        r = m.load_interp(interp('R'), 'ds', 0x1234)
        self.assertEqual((r['state']['seg']['ds'], r['state']['shape']['ds']), ((0x1234, 0x93), (0x12340, 0xFFFF)))
        u = interp('R')
        u['shape']['ds'] = (0, 0xFFFFFFFF)                                       # big real mode survives a load
        self.assertEqual(m.load_interp(u, 'ds', 0x10)['state']['shape']['ds'], (0x100, 0xFFFFFFFF))
        self.assertEqual(m.load_interp(interp('R'), 'ss', 0)['outcome'], 'done')  # no null rule in real mode
        r = m.load_interp(interp('R'), 'fs', 0x1234)['state']
        self.assertEqual((r['shape']['fs'][0], r['fs_base']), (0x12340, 0x12340))   # the base kept in both places
        p = m.load_interp(m.load_interp(interp('P32'), 'gs', 0x10)['state'], 'gs', 0)['state']
        self.assertEqual((p['shape']['gs'], p['gs_base']), ((0, 0), 0))

    def test_protected_far_jump_checks(self):
        s = interp('P16')
        r = m.jmp_far_interp(s, (0x08, 0x1234))                                  # 66 EA ptr16:32: into P32
        self.assertEqual((r['outcome'], r['state']['seg']['cs'], r['state']['shape']['cs'], m.mode_of(r['state'])),
                         ('done', (0x08, 0xC09B), (0, 0xFFFFFFFF), 'P32'))
        r = m.jmp_far_interp(s, (0x18, 0x10), osize=16)                          # EA ptr16:16: a 16-bit segment
        self.assertEqual((r['state']['shape']['cs'], m.mode_of(r['state'])), ((0x10000, 0xFFFF), 'P16'))
        for sel, want in ((0, (dm.GP, 0)), (3, (dm.GP, 0)), (0x98, (dm.GP, 0x98)), (0x0C, (dm.GP, 0x0C)),
                          (0x10, (dm.GP, 0x10)), (0x30, (dm.NP, 0x30)), (0x38, (dm.GP, 0x38)), (0x0B, (dm.GP, 0x08)),
                          (0x60, (dm.GP, 0x60)), (0x68, (dm.GP, 0x68))):
            self.assertEqual(self.f(m.jmp_far_interp(s, (sel, 0))), ('fault', want), hex(sel))
        dm.place(s['memory'], 8, m.segment(0x10000, 0xFFFF, 0x9B).to_bytes(8, 'little'))
        r = m.jmp_far_interp(s, (0x0C, 0x20), osize=16)                           # B16: the INIT LDT, base 0
        self.assertEqual((r['outcome'], r['state']['shape']['cs']), ('done', (0x10000, 0xFFFF)))
        self.assertEqual((s['ldtr'], s['tr'], s['tr_attributes'] & 0xF), ((0, 0, 0xFFFF), (0, 0, 0xFFFF), 0xB))
        r = m.jmp_far_interp(s, (0x43, 0x10))                                    # conforming: RPL ignored, RPL := CPL
        self.assertEqual(r['state']['cs'], 0x40)
        self.assertEqual(self.f(m.jmp_far_interp(s, (0x18, 0x10000))), ('fault', (dm.GP, 0)))   # the new CS's limit
        self.assertEqual(self.f(m.jmp_far_interp(s, (0x18, 0x10000), osize=16))[0], 'done')    # offset & FFFFH
        self.assertEqual(m.mode_of(m.jmp_far_interp(s, (0x20, 0))['state']), 'P32')  # L = D = 1, LMA = 0: 32-bit
        self.assertEqual(m.mode_of(m.jmp_far_interp(s, (0x28, 0))['state']), 'P16')  # L = 1, D = 0: 16-bit
        for sel in (0x50, 0x58, 0x88):
            self.assertEqual(m.jmp_far_interp(s, (sel, 0))['outcome'], 'refused', hex(sel))
        for access in (0x8B, 0x83, 0x0B):                                       # a busy TSS (32-, 16-bit; not present)
            b = copy.deepcopy(s)
            dm.place(b['memory'], IGDT + 0x50, m.segment(0x1000, 0x67, access).to_bytes(8, 'little'))
            self.assertEqual(self.f(m.jmp_far_interp(b, (0x50, 0))), ('fault', (dm.GP, 0x50)), hex(access))
        r = m.jmp_far_interp(s, (0x48, 0))
        self.assertEqual(r['diff'], {IGDT + 0x48 + 5: bytes([0x9B])})          # the accessed bit
        self.assertEqual(self.f(m.jmp_far_interp(interp('P32', cpl=3), (0x93, 0)))[0], 'refused')

    def test_protected_far_jump_through_memory(self):
        s = interp('P32')
        dm.place(s['memory'], IPTR, (0x2222).to_bytes(4, 'little') + (0x18).to_bytes(2, 'little'))
        r = m.jmp_far_interp(s, pointer=('ds', IPTR))
        self.assertEqual((r['state']['cs'], r['state']['rip'], m.mode_of(r['state'])), (0x18, 0x2222, 'P16'))
        r = m.jmp_far_interp(s, pointer=('ds', IPTR), osize=16)                  # m16:16: the selector is bytes 2-3
        self.assertEqual(self.f(r), ('fault', (dm.GP, 0)))                       # (0: the offset's high half)
        s['seg']['ds'], s['shape']['ds'] = (0x78, 0x9093), (0, 0xFFF)
        self.assertEqual(self.f(m.jmp_far_interp(s, pointer=('ds', 0xFFC))), ('fault', (dm.GP, 0)))
        s['seg']['ds'] = (0, 0)                                                  # a null DS used
        self.assertEqual(self.f(m.jmp_far_interp(s, pointer=('ds', IPTR))), ('fault', (dm.GP, 0)))
        s['seg']['es'] = (0x08, 0xC098)                                          # execute-only code: no read
        self.assertEqual(self.f(m.jmp_far_interp(s, pointer=('es', IPTR))), ('fault', (dm.GP, 0)))
        u = interp('P32')
        u['memory'].pop(IPTR >> 12)                                              # paging off: unassigned, all-ones
        self.assertEqual(self.f(m.jmp_far_interp(u, pointer=('ds', IPTR))), ('fault', (dm.GP, 0xFFFC)))

    def test_protected_far_return(self):
        r = m.ret_far_interp(frame(interp('P32'), 0x3000, 0x18), release=4)
        self.assertEqual((r['state']['cs'], r['state']['rip'], r['state']['rsp'], m.mode_of(r['state'])),
                         (0x18, 0x3000, ISTACK + 12, 'P16'))
        self.assertEqual(self.f(m.ret_far_interp(frame(interp('P32'), 0x10000, 0x18))), ('fault', (dm.GP, 0)))
        self.assertEqual(self.f(m.ret_far_interp(frame(interp('P32'), 0, 0))), ('fault', (dm.GP, 0)))
        self.assertEqual(self.f(m.ret_far_interp(frame(interp('P32'), 0, 0x38))), ('fault', (dm.GP, 0x38)))  # DPL != RPL
        self.assertEqual(self.f(m.ret_far_interp(frame(interp('P32'), 0, 0x30))), ('fault', (dm.NP, 0x30)))
        self.assertEqual(self.f(m.ret_far_interp(frame(interp('P32'), 0, 0x10))), ('fault', (dm.GP, 0x10)))
        self.assertEqual(m.ret_far_interp(frame(interp('P32'), 0, 0x93))['outcome'], 'refused')   # to CPL 3
        self.assertEqual(m.mode_of(m.ret_far_interp(frame(interp('P32'), 0, 0x20))['state']), 'P32')  # L ignored
        s = frame(interp('P32'), 0x3000, 0x08)
        s['seg']['ss'], s['shape']['ss'] = (0x78, 0xC093), (0, ISTACK + 4)       # the CS slot beyond the SS limit
        self.assertEqual(self.f(m.ret_far_interp(s)), ('fault', (dm.SS, 0)))
        r = m.ret_far_interp(frame(interp('P16'), 0x44, 0x18, width=2), osize=16)  # 16-bit RET on the SIPI stack
        self.assertEqual((r['state']['cs'], r['state']['rip'], r['state']['rsp']), (0x18, 0x44, ISTACK + 4))

    def test_protected_loads(self):
        s = interp('P32')
        self.assertEqual(self.f(m.load_interp(s, 'ss', 0)), ('fault', (dm.GP, 0)))   # null SS: #GP(0) outside 64-bit
        self.assertEqual(m.load(machine(), 'ss', 0)['outcome'], 'done')                # (64-bit mode, CPL 0: allowed)
        r = m.load_interp(s, 'ds', 0)
        self.assertEqual((r['outcome'], r['state']['seg']['ds']), ('done', (0, 0)))
        self.assertEqual(self.f(m.load_interp(s, 'ds', 0x30)), ('fault', (dm.NP, 0x30)))
        self.assertEqual(self.f(m.load_interp(s, 'ss', 0x80)), ('fault', (dm.GP, 0x80)))  # read-only data as SS
        r = m.load_interp(s, 'ss', 0x78)
        self.assertEqual((r['state']['ss'], r['state']['shape']['ss']), (0x78, (0, 0xFFF)))
        p = interp('P16')                                                        # the stale real-mode DS is usable
        self.assertEqual(m.load_interp(p, 'es', 0x10)['state']['shape']['es'], (0, 0xFFFFFFFF))

    def test_compatibility_mode(self):
        s = interp('C32')
        r = m.jmp_far_interp(s, (0x70, 0x8000_1000))                             # to 64-bit code: the hand-off
        self.assertEqual((r['outcome'], r['handoff'], r['state']['rip']), ('done', True, 0x8000_1000))
        self.assertEqual(self.f(m.jmp_far_interp(s, (0x20, 0))), ('fault', (dm.GP, 0x20)))  # L = D = 1 with LMA = 1
        self.assertFalse(m.jmp_far_interp(s, (0x08, 0x10))['handoff'])
        self.assertEqual(self.f(m.jmp_far_interp(s, (0x50, 0))), ('fault', (dm.GP, 0x50)))  # a TSS is no target
        self.assertEqual(m.jmp_far_interp(s, (0x58, 0))['outcome'], 'refused')   # (type CH: a 64-bit call gate)
        r = m.ret_far_interp(frame(interp('C32'), 0x4000, 0x70))
        self.assertEqual((r['handoff'], r['state']['rip'], r['state']['rsp']), (True, 0x4000, ISTACK + 8))
        self.assertEqual(self.f(m.ret_far_interp(frame(interp('C32'), 0, 0x20))), ('fault', (dm.GP, 0x20)))
        u = interp('C32')
        u['memory'].pop(IPTR >> 12)                                              # paging on: the pointer's #PF
        r = m.jmp_far_interp(u, pointer=('ds', IPTR))
        self.assertEqual(r['fault'], (dm.PF, 0, IPTR))


if __name__ == '__main__':
    unittest.main()
