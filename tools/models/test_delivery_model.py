"""Hand-derived cases for the IA-32e delivery model (SDM references in delivery_model's docstring)."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # tools: runnable by path from the repo root
from models import delivery_model as m

GDT, IDT, TSS = 0x1000, 0x2000, 0x3000
HANDLER = 0xFFFF_FFFF_8000_1000          # vector v's handler is HANDLER + 16 * v
KRIP, URIP = 0xFFFF_FFFF_8100_0000, 0x40_0000
RSP0, RSP1, IST1, IST2 = 0x9000, 0x7000, 0xB000, 0xD008   # IST2 deliberately not 16-byte aligned
TR = 0x50
DESCRIPTORS = {
    0x08: m.segment(0), 0x10: m.segment(0, code=False), 0x18: m.segment(3, code=False), 0x20: m.segment(3),
    0x28: m.segment(1), 0x30: m.segment(1, code=False), 0x38: m.segment(3, l=0, d=1),
    0x40: m.segment(0, present=False), 0x48: m.segment(0, accessed=False), 0x58: m.segment(0, l=0, d=1),
    0x60: m.segment(3, code=False, accessed=False),
}


def machine(user=False, gates=None, tss=None):
    """CPL0 kernel context (or CPL3 user context) with a GDT, a full IDT, a TSS and four stacks."""
    memory = {p: m.page() for p in (0x1, 0x2, 0x3, 0x6, 0x8, 0xA, 0xC)}
    memory[0x4] = m.page(user=True)
    for selector, value in DESCRIPTORS.items():
        m.place(memory, GDT + selector, value.to_bytes(8, 'little'))
    table = {v: dict(dpl=3 if v in (3, 4) else 0) for v in range(256)}
    table[m.DF]['ist'] = 1
    table[m.NMI]['ist'] = 2
    for v, overrides in (gates or {}).items():
        table[v].update(overrides)
    for v, g in table.items():
        args = dict(offset=HANDLER + 16 * v, selector=0x08)
        args.update(g)
        m.place(memory, IDT + 16 * v, m.gate(**args))
    m.place(memory, TSS, tss or m.tss(rsp=(RSP0, RSP1, 0), ist=(IST1, IST2, 0, 0, 0, 0, 0)))
    state = dict(rip=KRIP, rsp=0x8F08, rflags=0x202, cpl=0, cs=0x08, ss=0x10, cs_l=1,
                 cr0=0x8001_0011, cr2=0, cr4=0x20, idtr=(IDT, 0xFFF), gdtr=(GDT, 0x67), tr=(TR, TSS, 0x67),
                 memory=memory, nmi_blocked=False, shadow=None, halted=False)
    if user:
        state.update(rip=URIP, rsp=0x5000, cpl=3, cs=0x23, ss=0x1B)
    return state


def frame(result, words):
    s = result['state']
    return [m.qword(s['memory'], s['rsp'] + 8 * i) for i in range(words)]


def build_iret_frame(state, rip, cs, rflags, rsp, ss):
    for i, v in enumerate((rip, cs, rflags, rsp, ss)):
        m.place(state['memory'], state['rsp'] + 8 * i, v.to_bytes(8, 'little'))
    return state


INTR = lambda v: {'kind': 'intr', 'vector': v}
GPF = {'kind': 'exception', 'vector': m.GP, 'error': 0}


class Gates(unittest.TestCase):
    def test_interrupt_gate_same_cpl_frame_alignment_and_flags(self):
        s = machine()
        s['rflags'] = 0x202 | m.TF | m.NT | m.RF
        r = m.deliver(s, INTR(0x20))
        n = r['state']
        self.assertEqual(r['outcome'], 'delivered')
        self.assertEqual(n['rsp'], 0x8ED8)                         # 0x8F08 aligned to 0x8F00, minus 5 pushes
        self.assertEqual(frame(r, 5), [KRIP, 0x08, 0x14302, 0x8F08, 0x10])   # RIP CS RFLAGS RSP SS
        self.assertEqual((n['rip'], n['cs'], n['ss'], n['cpl']), (HANDLER + 0x200, 0x08, 0x10, 0))
        self.assertEqual(n['rflags'], 0x2)                         # TF NT RF IF cleared
        self.assertEqual(list(r['diff']), [0x8ED8])
        self.assertEqual(len(r['diff'][0x8ED8]), 40)
        self.assertEqual(m.qword(s['memory'], 0x8ED8), 0)          # input state untouched

    def test_trap_gate_keeps_if(self):
        r = m.deliver(machine(gates={0x21: dict(gtype=m.TRAP_GATE)}), INTR(0x21))
        self.assertEqual(r['state']['rflags'], 0x202)

    def test_task_gate_rejected_in_ia32e(self):
        r = m.deliver(machine(gates={0x22: dict(gtype=0x5)}), INTR(0x22))
        self.assertEqual(r['events'], [(0x22, None), (m.GP, 0x22 * 8 + 2 + 1)])
        self.assertEqual(frame(r, 6), [0x113, KRIP, 0x08, 0x10202, 0x8F08, 0x10])   # error code; RF=1 (fault)
        self.assertEqual(r['state']['rip'], HANDLER + 16 * m.GP)

    def test_int_n_from_cpl3_through_dpl0_gate_is_gp(self):
        r = m.deliver(machine(user=True), {'kind': 'int', 'vector': 0x80, 'length': 2})
        self.assertEqual(r['events'], [(0x80, None), (m.GP, 0x80 * 8 + 2)])    # EXT = 0 for INT n
        self.assertEqual(frame(r, 6), [0x402, URIP, 0x23, 0x10202, 0x5000, 0x1B])  # RIP of the INT itself
        n = r['state']
        self.assertEqual((n['rsp'], n['ss'], n['cs'], n['cpl']), (RSP0 - 48, 0, 0x08, 0))

    def test_int3_from_cpl3_through_dpl3_gate(self):
        s = machine(user=True)
        s['rflags'] |= m.RF
        r = m.deliver(s, {'kind': 'int3', 'length': 1})
        self.assertEqual(r['events'], [(m.BP, None)])
        self.assertEqual(frame(r, 5), [URIP + 1, 0x23, 0x202, 0x5000, 0x1B])   # next RIP; RF pushed as 0

    def test_into_is_ud_in_64bit_mode_and_vector_4_in_compatibility_mode(self):
        r = m.deliver(machine(), {'kind': 'into', 'length': 1})
        self.assertEqual((r['events'], frame(r, 1)), ([(m.UD, None)], [KRIP]))
        s = machine(user=True)
        s.update(cs=0x3B, cs_l=0, rflags=0x202 | m.OF_FLAG)
        r = m.deliver(s, {'kind': 'into', 'length': 1})
        self.assertEqual(r['events'], [(m.OF, None)])
        self.assertEqual(frame(r, 2), [URIP + 1, 0x3B])
        self.assertEqual(r['state']['cs_l'], 1)
        s['rflags'] = 0x202
        r = m.deliver(s, {'kind': 'into', 'length': 1})
        self.assertEqual((r['outcome'], r['state']['rip']), ('none', URIP + 1))

    def test_not_present_gate_is_np_with_ext(self):
        s = machine(gates={0x23: dict(present=False)})
        self.assertEqual(m.deliver(s, INTR(0x23))['events'][-1], (m.NP, 0x23 * 8 + 3))
        self.assertEqual(m.deliver(s, {'kind': 'int', 'vector': 0x23, 'length': 2})['events'][-1], (m.NP, 0x11A))
        nmi = m.deliver(machine(gates={m.NMI: dict(present=False)}), {'kind': 'nmi'})
        self.assertEqual(nmi['events'][-1], (m.NP, 2 * 8 + 3))

    def test_idt_limit(self):
        s = machine()
        s['idtr'] = (IDT, 0x20F)                                   # vectors 0..0x20 only
        self.assertEqual(m.deliver(s, INTR(0x21))['events'][-1], (m.GP, 0x10B))

    def test_target_code_segment_checks(self):
        cases = [(dict(selector=0), (m.GP, 1)),                    # NULL: error code holds only EXT
                 (dict(selector=0x10), (m.GP, 0x11)),              # data segment
                 (dict(selector=0x23), (m.GP, 0x21)),              # DPL 3 > CPL 0
                 (dict(selector=0x100), (m.GP, 0x101)),            # beyond GDT limit
                 (dict(selector=0x40), (m.NP, 0x41)),              # not present
                 (dict(selector=0x58), (m.GP, 0x30 * 8 + 3)),      # not a 64-bit segment: vector (7.14.1)
                 (dict(offset=0x0000_8000_0000_0000), (m.GP, 1))]  # non-canonical handler
        for overrides, expected in cases:
            with self.subTest(overrides):
                r = m.deliver(machine(gates={0x30: overrides}), INTR(0x30))
                self.assertEqual(r['events'], [(0x30, None), expected])

    def test_accessed_bit_written_on_cs_load(self):
        r = m.deliver(machine(gates={0x31: dict(selector=0x48)}), INTR(0x31))
        self.assertEqual(r['diff'][GDT + 0x48 + 5], bytes([0x9B]))


class Stacks(unittest.TestCase):
    def test_cpl3_to_cpl0_with_ist(self):
        r = m.deliver(machine(user=True, gates={0x24: dict(ist=1)}), INTR(0x24))
        n = r['state']
        self.assertEqual(n['rsp'], IST1 - 40)
        self.assertEqual(frame(r, 5), [URIP, 0x23, 0x202, 0x5000, 0x1B])
        self.assertEqual((n['cs'], n['ss'], n['cpl'], n['rip']), (0x08, 0, 0, HANDLER + 0x240))

    def test_cpl3_to_cpl0_via_rsp0(self):
        r = m.deliver(machine(user=True), INTR(0x20))
        self.assertEqual((r['state']['rsp'], r['state']['ss']), (RSP0 - 40, 0))

    def test_null_ss_carries_new_cpl_as_rpl(self):
        r = m.deliver(machine(user=True, gates={0x25: dict(selector=0x28)}), INTR(0x25))
        n = r['state']
        self.assertEqual((n['ss'], n['cs'], n['cpl'], n['rsp']), (1, 0x29, 1, RSP1 - 40))

    def test_same_cpl_ist_aligns_and_keeps_ss(self):
        r = m.deliver(machine(), {'kind': 'nmi'})
        n = r['state']
        self.assertEqual((n['rsp'], n['ss'], n['nmi_blocked']), (0xD000 - 40, 0x10, True))
        self.assertEqual(frame(r, 5), [KRIP, 0x08, 0x202, 0x8F08, 0x10])

    def test_tss_limit_is_ts(self):
        s = machine(gates={0x26: dict(ist=1)})
        s['tr'] = (TR, TSS, 0x2B)                                  # IST1 slot 0x24..0x2B needs limit >= 0x2B
        self.assertEqual(m.deliver(s, INTR(0x26))['outcome'], 'delivered')
        s['tr'] = (TR, TSS, 0x2A)
        self.assertEqual(m.deliver(s, INTR(0x26))['events'], [(0x26, None), (m.TS, 0x51)])

    def test_noncanonical_new_rsp_is_ss_then_double_fault(self):
        s = machine(user=True, tss=m.tss(rsp=(0x0000_8000_0000_0000, 0, 0), ist=(IST1,) + (0,) * 6))
        r = m.deliver(s, INTR(0x20))
        self.assertEqual(r['events'], [(0x20, None), (m.SS, 1), (m.SS, 1), (m.DF, 0)])
        self.assertEqual(r['state']['rsp'], IST1 - 48)


class ErrorCodes(unittest.TestCase):
    def test_which_events_push_an_error_code(self):
        for event, words in ((GPF, 6), ({'kind': 'exception', 'vector': m.AC, 'error': 0}, 6),
                             ({'kind': 'exception', 'vector': m.UD}, 5), ({'kind': 'exception', 'vector': m.DF}, 6),
                             ({'kind': 'int', 'vector': m.PF, 'length': 2}, 5), (INTR(m.GP), 5)):
            with self.subTest(event):
                r = m.deliver(machine(), event)
                self.assertEqual((0x8F00 if event['vector'] != m.DF else IST1) - r['state']['rsp'], 8 * words)

    def test_page_fault_sets_cr2_and_pushes_error(self):
        r = m.deliver(machine(), {'kind': 'exception', 'vector': m.PF, 'error': 6, 'cr2': 0x123_4000})
        self.assertEqual(r['state']['cr2'], 0x123_4000)
        self.assertEqual(frame(r, 4), [6, KRIP, 0x08, 0x10202])

    def test_page_fault_error_bits(self):
        s = machine(user=True)
        s['rsp'] = 0x8F00                                          # IRETQ at CPL3 pops from a supervisor page
        r = m.iretq(s)
        self.assertEqual((r['events'][0], r['state']['cr2']), ((m.PF, 5), 0x8F00))   # P, user, read
        s = machine()
        s['memory'][0x8]['write'] = False                          # read-only kernel stack
        self.assertEqual(m.deliver(s, INTR(0x20))['events'][1], (m.PF, 3))   # P, write, supervisor (WP = 1)
        s['cr0'] &= ~m.CR0_WP
        self.assertEqual(m.deliver(s, INTR(0x20))['outcome'], 'delivered')

    def test_ext_bit_set_for_hardware_and_exceptions_clear_for_int_n(self):
        s = machine(gates={0x40: dict(selector=0)})
        self.assertEqual(m.deliver(s, INTR(0x40))['events'][-1], (m.GP, 1))
        self.assertEqual(m.deliver(s, {'kind': 'int', 'vector': 0x40, 'length': 2})['events'][-1], (m.GP, 0))
        s = machine(gates={m.UD: dict(selector=0)})
        self.assertEqual(m.deliver(s, {'kind': 'exception', 'vector': m.UD})['events'][-1], (m.GP, 1))


class NestedFaults(unittest.TestCase):
    def test_double_fault_conditions_table(self):
        table = {('benign', c): 'serial' for c in ('benign', 'contributory', 'page')}
        table.update({('contributory', 'benign'): 'serial', ('contributory', 'contributory'): 'double',
                      ('contributory', 'page'): 'serial', ('page', 'benign'): 'serial',
                      ('page', 'contributory'): 'double', ('page', 'page'): 'double',
                      ('double', 'benign'): 'serial', ('double', 'contributory'): 'shutdown',
                      ('double', 'page'): 'shutdown'})
        for (a, b), outcome in table.items():
            self.assertEqual(m.double_fault_outcome(a, b), outcome, (a, b))

    def test_page_fault_during_frame_push_handled_serially(self):
        s = machine(user=True, gates={m.PF: dict(ist=2)})
        del s['memory'][0x8]                                       # RSP0 stack absent
        r = m.deliver(s, INTR(0x20))
        self.assertEqual(r['events'], [(0x20, None), (m.PF, 2)])   # write, supervisor, not present
        self.assertEqual(r['state']['cr2'], RSP0 - 8)              # first push (SS) faults
        self.assertEqual(frame(r, 2), [2, URIP])

    def test_page_fault_during_frame_push_becomes_double_fault(self):
        s = machine(user=True)
        del s['memory'][0x8]
        r = m.deliver(s, GPF)
        self.assertEqual(r['events'], [(m.GP, 0), (m.PF, 2), (m.PF, 2), (m.DF, 0)])
        self.assertEqual(r['state']['cr2'], RSP0 - 8)
        self.assertEqual(r['state']['rsp'], IST1 - 48)
        self.assertEqual(frame(r, 6), [0, URIP, 0x23, 0x202, 0x5000, 0x1B])   # #DF: RF image = current RF

    def test_contributory_during_contributory_is_double_fault(self):
        r = m.deliver(machine(gates={m.GP: dict(present=False)}), GPF)
        self.assertEqual(r['events'], [(m.GP, 0), (m.NP, m.GP * 8 + 3), (m.DF, 0)])

    def test_double_fault_during_double_fault_is_shutdown(self):
        s = machine(user=True, gates={m.DF: dict(present=False)})
        del s['memory'][0x8]
        r = m.deliver(s, GPF)
        self.assertEqual(r['outcome'], 'shutdown')
        self.assertEqual(r['events'], [(m.GP, 0), (m.PF, 2), (m.PF, 2), (m.DF, 0), (m.NP, 8 * 8 + 3)])
        self.assertEqual((r['diff'], r['state']['cr2']), ({}, RSP0 - 8))

    def test_absent_idt_triple_faults(self):
        s = machine()
        del s['memory'][0x2]
        r = m.deliver(s, INTR(0x20))
        self.assertEqual(r['outcome'], 'shutdown')
        self.assertEqual(r['events'], [(0x20, None), (m.PF, 0), (m.PF, 0), (m.DF, 0), (m.PF, 0)])   # IDT reads: U/S = 0
        self.assertEqual(r['state']['cr2'], IDT + 16 * m.DF)


class Iretq(unittest.TestCase):
    def test_same_cpl_round_trip(self):
        s = machine()
        out = m.deliver(s, INTR(0x20))['state']
        r = m.iretq(out)
        n = r['state']
        self.assertEqual(r['outcome'], 'returned')
        for key in ('rip', 'rsp', 'rflags', 'cs', 'ss', 'cpl', 'cs_l'):
            self.assertEqual(n[key], s[key], key)

    def test_return_to_cpl3_and_to_compatibility_mode(self):
        out = m.deliver(machine(user=True, gates={0x24: dict(ist=1)}), INTR(0x24))['state']
        n = m.iretq(out)['state']
        self.assertEqual((n['rip'], n['cs'], n['ss'], n['rsp'], n['cpl'], n['rflags']),
                         (URIP, 0x23, 0x1B, 0x5000, 3, 0x202))
        s = build_iret_frame(machine(), 0x1000, 0x3B, 0x202, 0x5000, 0x1B)
        n = m.iretq(s)['state']
        self.assertEqual((n['cs'], n['cs_l'], n['cpl']), (0x3B, 0, 3))

    def test_same_cpl_null_ss_allowed_in_64bit_mode(self):
        n = m.iretq(build_iret_frame(machine(), KRIP, 0x08, 0x202, 0x8F00, 0))['state']
        self.assertEqual((n['ss'], n['rsp']), (0, 0x8F00))
        # A7: a NULL SS whose RPL is not the new CPL is #GP(0) (Vol. 2A IRET 64-bit mode exceptions).
        r = m.iretq(build_iret_frame(machine(), KRIP, 0x08, 0x202, 0x8F00, 1))
        self.assertEqual(r['events'][0], (m.GP, 0))

    def test_cs_and_ss_checks(self):
        cases = [(machine(user=True), (URIP, 0x08, 0x202, 0x5000, 0x10), (m.GP, 0x08)),   # RPL < CPL
                 (machine(), (URIP, 0x20, 0x202, 0x5000, 0x18), (m.GP, 0x20)),           # DPL 3 != RPL 0
                 (machine(), (URIP, 0x23, 0x202, 0x5000, 0x10), (m.GP, 0x10)),           # SS RPL 0 != 3
                 (machine(), (URIP, 0x23, 0x202, 0x5000, 0x18), (m.GP, 0x18)),           # SS RPL 0 != 3
                 (machine(), (URIP, 0x23, 0x202, 0x5000, 0x23), (m.GP, 0x20)),           # SS is code
                 (machine(), (URIP, 0x23, 0x202, 0x5000, 0x3), (m.GP, 0)),               # NULL SS to CPL3
                 (machine(), (URIP, 0x43, 0x202, 0x5000, 0x1B), (m.GP, 0x40)),           # 0x40 is DPL 0
                 (machine(), (KRIP, 0x40, 0x202, 0x8F00, 0x10), (m.NP, 0x40)),           # CS not present
                 (machine(), (0x0000_8000_0000_0000, 0x08, 0x202, 0x8F00, 0x10), (m.GP, 0))]  # RIP non-canonical
        for s, words, expected in cases:
            with self.subTest(words):
                if s['cpl'] == 3:
                    s['rsp'] = 0x4F00
                r = m.iretq(build_iret_frame(s, *words))
                self.assertEqual(r['events'][0], expected)
                self.assertEqual(frame(r, 2)[1], s['rip'])         # the fault reports the IRETQ itself

    def test_if_restore_depends_on_cpl_and_iopl(self):
        n = m.iretq(build_iret_frame(machine(), KRIP, 0x08, 0x3002, 0x8F00, 0x10))['state']
        self.assertEqual(n['rflags'], 0x3002)                      # CPL 0: IF and IOPL from the image
        s = machine(user=True)
        s['rsp'] = 0x4F00
        n = m.iretq(build_iret_frame(s, URIP, 0x23, 0x3002 | m.VM, 0x5000, 0x1B))['state']
        self.assertEqual(n['rflags'], 0x202)                       # CPL 3 > IOPL 0: IF, IOPL kept; VM never set

    def test_nt_is_gp(self):
        s = build_iret_frame(machine(), KRIP, 0x08, 0x202, 0x8F00, 0x10)
        s['rflags'] |= m.NT
        self.assertEqual(m.iretq(s)['events'][0], (m.GP, 0))


class Eligibility(unittest.TestCase):
    def test_if_masks_intr(self):
        s = machine()
        self.assertEqual(m.recognise(s, {'intr'}), {'intr'})
        s['rflags'] = 0x2
        self.assertEqual(m.recognise(s, {'intr'}), {None})

    def test_sti_shadow_delays_by_one_instruction(self):
        s = machine()
        s['rflags'] = 0x2
        s = m.after_instruction(s, 'sti')
        self.assertEqual(m.recognise(s, {'intr'}), {None})         # boundary right after STI
        self.assertEqual(m.recognise(s, {'intr', 'nmi'}), {'nmi', None})   # NMI may be inhibited (7.8.1 fn 2)
        self.assertEqual(m.recognise(s, {'smi', 'fetch_fault'}), {'smi', 'fetch_fault'})
        s = m.after_instruction(s, 'other')
        self.assertEqual(m.recognise(s, {'intr'}), {'intr'})
        s = m.after_instruction(machine(), 'sti')                  # IF already 1: no shadow
        self.assertEqual(m.recognise(s, {'intr'}), {'intr'})

    def test_second_consecutive_sti_or_ss_load_may_or_may_not_inhibit(self):
        s = machine()
        s['rflags'] = 0x2
        s = m.after_instruction(m.after_instruction(s, 'sti'), 'sti')
        self.assertEqual(m.recognise(s, {'intr'}), {'intr', None})
        s = m.after_instruction(m.after_instruction(machine(), 'mov_ss'), 'pop_ss')
        self.assertEqual(m.recognise(s, {'intr'}), {'intr', None})
        self.assertEqual(m.recognise(s, {'single_step', 'intr'}), {'single_step', 'intr', None})

    def test_mov_ss_shadow_blocks_intr_nmi_and_debug_events(self):
        s = m.after_instruction(machine(), 'mov_ss')
        self.assertEqual(m.recognise(s, {'intr', 'nmi', 'single_step', 'data_breakpoint', 'code_breakpoint'}),
                         {None})
        self.assertEqual(m.recognise(s, {'intr', 'execute_fault'}), {'execute_fault'})
        self.assertEqual(m.recognise(s, {'init'}), {'init'})

    def test_nmi_blocked_until_iretq(self):
        r = m.deliver(machine(), {'kind': 'nmi'})
        self.assertEqual(m.recognise(r['state'], {'nmi'}), {None})
        n = m.iretq(r['state'])['state']
        self.assertEqual(m.recognise(n, {'nmi'}), {'nmi'})

    def test_nmi_blocked_even_when_nmi_delivery_faults(self):
        r = m.deliver(machine(gates={m.NMI: dict(present=False)}), {'kind': 'nmi'})
        self.assertEqual((r['events'][-1][0], r['state']['nmi_blocked']), (m.NP, True))

    def test_nmi_unblocked_even_when_iretq_faults(self):
        s = build_iret_frame(machine(), 0x0000_8000_0000_0000, 0x08, 0x202, 0x8F00, 0x10)
        s['nmi_blocked'] = True
        r = m.iretq(s)
        self.assertEqual((r['events'][0], r['state']['nmi_blocked']), ((m.GP, 0), False))

    def test_int_2_does_not_block_nmi(self):
        r = m.deliver(machine(), {'kind': 'int', 'vector': 2, 'length': 2})
        self.assertFalse(r['state']['nmi_blocked'])

    def test_priority_among_concurrent_events(self):
        s = machine()
        pending = set(m.PRIORITY)
        for cls in m.CLASSES:                                      # order inside a class is not asserted
            self.assertEqual(m.recognise(s, pending), set(cls))
            pending -= set(cls)
        self.assertEqual(m.recognise(s, pending), {None})
        self.assertEqual(m.recognise(s, {'intr', 'nmi', 'single_step', 'execute_fault'}), {'single_step'})
        self.assertEqual(m.recognise(s, {'intr', 'nmi', 'fetch_fault'}), {'nmi'})

    def test_hlt_wake(self):
        s = machine()
        s['rflags'] = 0x2
        s = m.after_instruction(m.after_instruction(s, 'sti'), 'hlt')   # sti; hlt
        s['rip'] = KRIP + 2                                        # RIP after HLT
        self.assertTrue(s['halted'])
        self.assertEqual(m.recognise(s, {'intr'}), {'intr'})       # STI's shadow ended at HLT
        r = m.deliver(s, INTR(0x20))
        self.assertFalse(r['state']['halted'])
        self.assertEqual(frame(r, 1), [KRIP + 2])
        s['rflags'] = 0x2
        self.assertEqual(m.recognise(s, {'intr', 'fetch_fault'}), {None})   # masked INTR does not wake
        self.assertEqual(m.recognise(s, {'intr', 'nmi'}), {'nmi'})
        s['nmi_blocked'] = True
        self.assertEqual(m.recognise(s, {'nmi'}), {None})
        self.assertEqual(m.recognise(s, {'mc'}), {'mc', None})     # #MC is not a listed wake event (A10)


class Apic(unittest.TestCase):
    def test_ppr_from_tpr_and_isr(self):
        self.assertIsNone(m.apic_deliverable(set(), set(), 0))
        self.assertEqual(m.apic_deliverable({0x31, 0x41}, set(), 0), 0x41)
        self.assertIsNone(m.apic_deliverable({0x41}, set(), 0x40))       # class 4 not above TPR class 4
        self.assertEqual(m.apic_deliverable({0x51}, set(), 0x4F), 0x51)
        self.assertIsNone(m.apic_deliverable({0x45}, {0x41}, 0))         # same class as in-service
        self.assertEqual(m.apic_deliverable({0x51}, {0x41}, 0x20), 0x51)  # PPR = 0x40
        self.assertIsNone(m.apic_deliverable({0x51}, {0x41}, 0x55))      # PPR = TPR = 0x55


class Revisions(unittest.TestCase):
    """Set-valued outcomes (undefined) and the review's added rules."""

    def test_non_64bit_target_cs_error_code_alternatives(self):
        r = m.deliver(machine(gates={0x30: dict(selector=0x58)}), INTR(0x30))
        self.assertEqual(r['undefined']['events'], {1: {(m.GP, 0x183), (m.GP, 0x59)}})
        self.assertEqual(r['undefined']['frame'], {r['state']['rsp']: {0x183, 0x59}})   # the pushed error code

    def test_16bit_gate_error_code_alternatives(self):
        for gtype in (6, 7):
            r = m.deliver(machine(gates={0x30: dict(gtype=gtype)}), INTR(0x30))
            self.assertEqual(r['events'][1], (m.GP, 0))            # 7.14.1: #GP(0)
            self.assertEqual(r['undefined']['events'][1], {(m.GP, 0), (m.GP, 0x183)})

    def test_int1_ext_no_dpl_check_next_rip(self):
        r = m.deliver(machine(user=True), {'kind': 'int1', 'length': 1})   # gate 1 has DPL 0
        self.assertEqual((r['events'], frame(r, 1)), ([(m.DB, None)], [URIP + 1]))
        self.assertEqual(r['undefined']['frame'], {RSP0 - 24: {0x202}})    # RF image {0, current}; current = 0
        r = m.deliver(machine(gates={m.DB: dict(present=False)}), {'kind': 'int1', 'length': 1})
        self.assertEqual(r['events'][1], (m.NP, 1 * 8 + 3))       # EXT = 1

    def test_general_detect_db_pushes_rf(self):
        s = machine()
        self.assertEqual(frame(m.deliver(s, {'kind': 'exception', 'vector': m.DB, 'fault': True}), 3)[2], 0x10202)
        self.assertEqual(frame(m.deliver(s, {'kind': 'exception', 'vector': m.DB}), 3)[2], 0x202)

    def test_straddling_frame_cr2_and_partial_writes(self):
        s = machine()
        s['rsp'] = 0x8010                                          # frame 0x7FE8..0x800F; page 7 absent
        r = m.deliver(s, INTR(0x20))
        self.assertEqual(r['events'], [(0x20, None), (m.PF, 2), (m.PF, 2), (m.DF, 0)])
        u = r['undefined']
        self.assertEqual(r['state']['cr2'], 0x7FF8)                # RFLAGS slot: first push on page 7
        self.assertEqual(u['state'], {'cr2': frozenset(range(0x7FE0, 0x8000))})
        self.assertEqual(u['memory'], frozenset(range(0x8000, 0x8010)))   # SS and RSP slots may be written
        self.assertEqual(u['frame'], {IST1 - 24: {0x202, 0x10202}, IST1 - 32: None, IST1 - 40: None})   # #DF frame

    def test_accessed_bit_of_failed_attempt_is_undefined(self):
        r = m.deliver(machine(gates={0x31: dict(selector=0x48, offset=0x0000_8000_0000_0000)}), INTR(0x31))
        self.assertEqual(r['events'][-1], (m.GP, 1))
        self.assertEqual(r['undefined']['memory'], {GDT + 0x48 + 5})
        self.assertNotIn(GDT + 0x48 + 5, r['diff'])

    def test_iretq_restores_iopl_only_at_cpl0(self):
        s = machine(user=True)
        s.update(rsp=0x4F00, rflags=0x3202)                        # CPL 3 = IOPL 3: IF restored, IOPL not
        n = m.iretq(build_iret_frame(s, URIP, 0x23, 0x2, 0x5000, 0x1B))['state']
        self.assertEqual(n['rflags'], 0x3002)

    def test_iretq_restores_vif_vip_at_cpl0(self):
        n = m.iretq(build_iret_frame(machine(), KRIP, 0x08, 0x202 | m.VIF | m.VIP, 0x8F00, 0x10))['state']
        self.assertEqual(n['rflags'], 0x202 | m.VIF | m.VIP)

    def test_iretq_sets_ss_accessed_bit_and_clears_shadow(self):
        s = build_iret_frame(m.after_instruction(machine(), 'mov_ss'), URIP, 0x23, 0x202, 0x5000, 0x63)
        r = m.iretq(s)
        self.assertEqual(r['diff'], {GDT + 0x60 + 5: bytes([0xF3])})
        self.assertIsNone(r['state']['shadow'])

    def test_ppr_subclass_model_specific_when_classes_equal(self):
        self.assertEqual(m.apic_ppr({0x41}, 0x45), {0x45, 0x40})
        self.assertEqual(m.apic_ppr({0x41}, 0x55), {0x55})
        self.assertEqual(m.apic_ppr({0x51}, 0x45), {0x50})
        self.assertEqual(m.apic_deliverable({0x51}, {0x41}, 0x45), 0x51)


if __name__ == '__main__':
    unittest.main()
