"""Hand-derived cases for the 64-bit segment model (SDM references in segment_model's docstring), and its IRETQ against
delivery_model's."""
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
        self.assertEqual(m.far(self.pointer(machine(), 0x1000, 0x38), 'jmp', PTR)['outcome'], 'refused')
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
        self.assertEqual(m.ret(push(machine(), 0x40_2000, 0x38))['outcome'], 'refused')
        self.assertEqual(m.ret(push(machine(), 1 << 47, 0x08))['fault'][:2], (dm.GP, 0))

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
        self.assertEqual(m.iret(s)['outcome'], 'refused')                                 # TF: single-step is S4's

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


if __name__ == '__main__':
    unittest.main()
