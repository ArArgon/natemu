"""Independent reference for x86 event delivery in IA-32e mode, written from the Intel SDM, not from natemu's code.

Numbering follows the repo's snapshot `artifacts/research/track01/source-snapshots/2026-09-03/intel-sdm-vol3a.pdf`
(interrupts = Vol. 3A chapter 7, APIC = chapter 13, paging = chapter 5); other volumes are cited by section title.
- Vol. 3A §7.3 (sources), §7.7.2 "NMI Blocking", §7.8.1 "Masking Maskable Hardware Interrupts" (STI, footnote 2),
  §7.8.3 "Masking Exceptions and Interrupts When Switching Stacks", §7.9 Table 7-2 "Priority Among Concurrent
  Events", §7.12.1 (handler procedures; §7.12.1.3 flag usage), §7.13 "Error Code", §7.14.1 "64-Bit Mode IDT",
  §7.14.2 "64-Bit Mode Stack Frame", §7.14.3 "IRET in IA-32e Mode", §7.14.4 "Stack Switching in IA-32e Mode",
  §7.14.5 "Interrupt Stack Table", §7.15 per-vector reference with Table 7-4 "Interrupt and Exception Classes" and
  Table 7-5 "Conditions for Generating a Double Fault";
- Vol. 3A §3.4.5 "Segment Descriptors" (accessed bit), §6.2.1 "Code-Segment Descriptor in 64-bit Mode", §10.7 "Task
  Management in 64-bit Mode" (64-bit TSS layout), §5.7 "Page-Fault Exceptions" (error code, CR2), the paging chapter's
  access-rights section (implicit supervisor accesses, CR0.WP);
- Vol. 3A §13.8.3.1 "Task and Processor Priorities", §13.8.4 "Interrupt Acceptance for Fixed Interrupts";
- Vol. 3B §20.3.1.1 "Instruction-Breakpoint Exception Condition" (RF image pushed);
- Vol. 2A "INT n/INTO/INT3/INT1—Call to Interrupt Procedure" and "IRET/IRETD/IRETQ—Interrupt Return" (Operation,
  IA-32e-MODE paths); Vol. 2B "STI", "HLT", "MOV" (SS load); Vol. 3C VMCS "Guest Non-Register State"
  (interruptibility-state table: blocking by MOV-SS covers maskable and nonmaskable interrupts).

Processor profile: the accepted CPU profile (INV-03-01, "Selected identity, topology and instruction surface"):
GenuineIntel family 6 model 0x3c, linear width 48 (no LA57), no SMAP, no CET, no FRED, no PKS. So: no shadow-stack or
#CP effects, no FRED frames, implicit supervisor accesses to user pages are allowed, canonical = 48-bit sign extension.

Scope. Only IA-32e mode (EFER.LMA = 1) with an interrupted context in 64-bit or compatibility mode; descriptors come
from the GDT (an LDT selector, TI = 1, is out of limits: the model has a null LDTR). Paging is abstracted: `memory`
maps linear page numbers to {'data': bytearray(4096), 'write': bool, 'user': bool}; an absent page is not present.
DS/ES/FS/GS are not modelled (so IRET's nulling of data segments on a return to an outer level is not either).

State dict keys: rip, rsp, rflags, cpl, cs, ss (selectors), cs_l (current CS.L: 1 = 64-bit mode, 0 = compatibility),
cr0, cr2, cr4, idtr = (base, limit), gdtr = (base, limit), tr = (selector, base, limit) (TR's hidden part, as LTR
loaded it), memory, nmi_blocked, shadow (None, 'sti', 'mov_ss', or 'sti?' / 'mov_ss?' for a shadow that may or may
not be in effect, see A9), halted. `state['rip']` is the RIP an exception frame holds: the faulting instruction for
faults, the next one for traps and at an interrupt boundary (a halted CPU's RIP is the instruction after HLT).

Events: {'kind': 'exception', 'vector': v, 'error': e, 'cr2': a (for #PF), 'mid_string': bool, 'fault': bool} |
{'kind': 'intr', 'vector': v, 'mid_string': bool} | {'kind': 'nmi'} | {'kind': 'int', 'vector': n, 'length': l} |
{'kind': 'int3' | 'into' | 'int1', 'length': l}. 'fault': True marks a fault-class #DB (general detect, Table 7-3);
leave it unset for instruction-breakpoint and trap-class #DB.

`deliver(state, event)` and `iretq(state)` are pure and return
{'outcome': 'delivered' | 'returned' | 'none' | 'shutdown', 'state', 'diff': {linear address: bytes},
 'events': [(vector, error or None), ...], 'undefined': {...}}.
`events` lists every event recognised, in order: the original event, each fault detected while delivering the
previous entry, and #DF where Table 7-5 generates one. The last entry is the one delivered, or, after 'shutdown', the
fault detected while delivering #DF.
`undefined` lists where the SDM permits other results than the model's single choice. A checker must accept, for
  'memory': frozenset of addresses whose bytes may hold anything a failed attempt may have written (old or new);
  'frame': {slot address: frozenset of permitted qwords, or None = any value};
  'state': {field: container of permitted values, or None = any};
  'events': {index into events: frozenset of permitted (vector, error)}.
`recognise(state, pending)` returns the frozenset of events the CPU may take at a boundary (None = take none).

RULE FOR RUNNERS: build no case in which more than one delivery or IRETQ check fails at once (two defects in one
gate/descriptor/stack, or one access over two bad pages). The model's check order is a transcription (A5, A7), not
oracle-grade, and such cases are outside what it certifies.

AMBIGUITIES (choice made, and what `undefined` widens):
A1 Frame pushes go SS, RSP, RFLAGS, CS, RIP, error code at decreasing addresses; CR2 is the slot of the first push
   that faults; undefined['state']['cr2'] = every frame byte on a faulting page.
A2 A failed attempt commits no writes in the model; undefined['memory'] holds every byte it might have written (frame
   bytes on writable pages, the CS/SS accessed byte once that descriptor was read) unless a later success rewrote it.
A3 Frame accesses are made at the new CPL; IDT/GDT/TSS reads are implicit supervisor accesses (U/S = 0).
A4 A descriptor or pop read that crosses into an absent page: CR2 = first faulting byte; undefined = all of them.
A5 Delivery check order: IDT limit, gate type, software DPL, gate P, CS null, CS limit, CS type/DPL, CS P, CS L/D,
   TSS limit, new RSP canonical, handler RIP canonical, push canonical, push #PF, accessed bit. The stack checks
   precede the RIP check as the INT n pseudocode's INTER-/INTRA-PRIVILEGE-LEVEL-INTERRUPT paths place them (recalled
   transcription; Vol. 2A is not in the repo snapshot, so unverified there). Multi-failure: see RULE.
A6 #DF: saved CS and RIP are undefined (§7.15 interrupt 8): model pushes the original context, undefined = any;
   RF image ∈ {current RF, 1} (abort; §20.3.1.1 does not list aborts).
A7 IRETQ order: NT, pops, CS null/limit/type/RPL/DPL/P, RIP canonical (or CS limit), SS checks. IRETQ to the same CPL
   reloads SS from the frame with the outer-return checks. A NULL SS is accepted only for a 64-bit CS, a new CPL < 3
   and RPL = the new CPL: Vol. 2A IRET, 64-Bit Mode Exceptions, #GP(0) "if the stack segment selector is NULL going
   back to compatibility mode", "going back to CPL3 64-bit mode" and "if the stack segment selector RPL is not equal to
   the RPL of the return code segment selector" (as MOV SS and RET; segment_model B6). Not set-valued: the SDM states
   it.
A8 IRETQ releases NMI blocking even when it faults (§7.7.2). NMI blocking starts before NMI delivery and stays even if
   that delivery faults (§7.7.2).
A9 Shadows: STI (IF 0 -> 1) blocks INTR and may block NMI and SMI (§7.8.1 footnote 2); MOV/POP SS blocks INTR and NMI
   (Vol. 2B MOV; Vol. 3C interruptibility state), suppresses an instruction breakpoint on the next instruction and a
   single-step trap after it, and holds a data breakpoint on itself pending until the following boundary (§7.8.3).
   Callers discard 'single_step' and 'code_breakpoint' when not taken in a MOV SS shadow; they keep 'data_breakpoint'
   pending. A shadow-setting instruction executed inside a shadow gives a '?' shadow: "only the first is guaranteed".
A10 HLT wakes on an enabled INTR, NMI, SMI, INIT, RESET or debug exception (Vol. 2B HLT); a blocked NMI or masked INTR
   does not; #MC is not listed, so a pending #MC may or may not wake it.
A11 #GP error codes §7.14.1 states differently from the INT n pseudocode: a 16-bit gate (06H/07H) -> #GP(0) per
   §7.14.1, also permitted vector*8+2+EXT; a non-64-bit target CS -> #GP(vector*8+2+EXT) per §7.14.1, also
   permitted selector|EXT.
A12 INT1's RF image: model = current RF; permitted {0, current} (§20.3.1.1 names INT n, INT3, INTO only).
A13 PPR[3:0] when TPR[7:4] = ISRV[7:4] is model-specific (§13.8.3.1): `apic_ppr` returns both.
"""
import copy

DE, DB, NMI, BP, OF, BR, UD, NM, DF, TS, NP, SS, GP, PF, MF, AC, MC, XM, VE, CP = (
    0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 16, 17, 18, 19, 20, 21)
# #CP (21) is kept for completeness of Table 7-4; the no-CET profile never raises it.
ERROR_CODE_VECTORS = {DF, TS, NP, SS, GP, PF, AC, CP}          # §7.15, per-vector "Exception Error Code"
FAULT_VECTORS = {DE, BR, UD, NM, TS, NP, SS, GP, PF, MF, AC, XM, VE, CP}   # Table 7-1 type "Fault"
CONTRIBUTORY = {DE, TS, NP, SS, GP, CP}                        # Table 7-4
PAGE_FAULTS = {PF, VE}                                         # Table 7-4
SOFTWARE = {'int', 'int3', 'into'}                              # EXT = 0 and gate-DPL check (not INT1), Vol. 2A INT n

TF, IF, IOPL, NT, RF, VM, AC_FLAG, VIF, VIP = 1 << 8, 1 << 9, 3 << 12, 1 << 14, 1 << 16, 1 << 17, 1 << 18, 1 << 19, 1 << 20
OF_FLAG = 1 << 11
# Flags IRET always restores (Vol. 2A IRET): CF PF AF ZF SF TF DF OF NT RF AC ID.
IRET_ALWAYS = 1 | 4 | 16 | 64 | 128 | TF | 1 << 10 | OF_FLAG | NT | RF | AC_FLAG | 1 << 21
INTERRUPT_GATE, TRAP_GATE = 0xE, 0xF
TSS_RSP0, TSS_IST1 = 4, 0x24                                   # 64-bit TSS (§10.7)
CR0_WP = 1 << 16


class Fault(Exception):
    def __init__(self, vector, error=None, cr2=None, error_alts=None):
        super().__init__(vector, error, cr2)
        self.vector, self.error, self.cr2, self.error_alts = vector, error, cr2, error_alts
        self.cr2_alts, self.maybe = None, frozenset()


def canonical(a):
    return (a >> 47) in (0, 0x1FFFF)                             # 48-bit linear profile


# ---- memory and descriptors ------------------------------------------------------------------------------------

class _Bus:
    """Paged linear memory with buffered writes; reads see the buffer. `maybe`: bytes a failed attempt may write."""

    def __init__(self, state):
        self.state, self.writes, self.maybe = state, {}, set()

    def fault(self, addr, write, cpl):
        page = self.state['memory'].get(addr >> 12)
        user = cpl == 3
        error = (2 if write else 0) | (4 if user else 0)
        if page is None:
            return Fault(PF, error, addr)
        if (user and not page['user']) or (write and not page['write'] and (user or self.state['cr0'] & CR0_WP)):
            return Fault(PF, error | 1, addr)                      # §5.7: P = 1, protection violation
        return None

    def check(self, addr, n, write, cpl, first=None):
        """Raise the #PF of an access to [addr, addr+n); `first` picks the reported byte (default lowest)."""
        bad = [a for a in range(addr, addr + n) if self.fault(a, write, cpl)]
        if bad:
            f = self.fault(first(bad) if first else bad[0], write, cpl)
            f.cr2_alts = frozenset(bad)                            # A1, A4
            raise f

    def read(self, addr, n, cpl=0):
        self.check(addr, n, False, cpl)
        mem = self.state['memory']
        return bytes(self.writes.get(a, mem[a >> 12]['data'][a & 0xFFF]) for a in range(addr, addr + n))

    def write(self, addr, data, cpl=0):
        self.check(addr, len(data), True, cpl)
        for i, b in enumerate(data):
            self.writes[addr + i] = b

    def commit(self):
        """Apply the buffered writes to the state's memory; return the diff as {address: bytes} runs."""
        diff, run = {}, None
        for a in sorted(self.writes):
            self.state['memory'][a >> 12]['data'][a & 0xFFF] = self.writes[a]
            if run is not None and a == run + len(diff[run]):
                diff[run] += bytes([self.writes[a]])
            else:
                run = a
                diff[a] = bytes([self.writes[a]])
        return diff


def _descriptor(bus, selector, ext):
    """(address, fields) of a GDT segment descriptor; #GP(selector) when outside the table (§3.4.5)."""
    base, limit = bus.state['gdtr']
    index = selector & ~7
    if selector & 4 or index + 7 > limit:
        raise Fault(GP, (selector & 0xFFFC) | ext)
    raw = int.from_bytes(bus.read(base + index, 8), 'little')
    access, flags = raw >> 40 & 0xFF, raw >> 52 & 0xF
    lim = raw & 0xFFFF | (raw >> 48 & 0xF) << 16
    if not access & 1:
        bus.maybe.add(base + index + 5)                            # A2: the accessed bit may be set by a failed load
    return base + index, dict(type=access & 0xF, s=access >> 4 & 1, dpl=access >> 5 & 3, p=access >> 7,
                              l=flags >> 1 & 1, d=flags >> 2 & 1,
                              limit=(lim << 12 | 0xFFF) if flags & 8 else lim, accessed=access & 1, access=access)


def _is_code(d): return d['s'] and d['type'] & 8
def _conforming(d): return d['type'] & 4
def _writable_data(d): return d['s'] and not d['type'] & 8 and d['type'] & 2


def _set_accessed(bus, addr, d):
    if not d['accessed']:                                          # §3.4.5: set on load if clear
        bus.write(addr + 5, bytes([d['access'] | 1]))


# ---- event delivery ---------------------------------------------------------------------------------------------

def event_class(event):
    """Table 7-4: 'benign', 'contributory', 'page' or 'double'. INT n/INT3/INTO, INTR and NMI are benign."""
    if event['kind'] != 'exception':
        return 'benign'
    v = event['vector']
    return 'double' if v == DF else 'contributory' if v in CONTRIBUTORY else 'page' if v in PAGE_FAULTS else 'benign'


def double_fault_outcome(first, second):
    """Table 7-5 (plus §7.15 interrupt 8: a contributory or page fault while delivering #DF = shutdown):
    'serial', 'double' or 'shutdown' for a `second` class detected while delivering a `first`."""
    if first == 'double':
        return 'shutdown' if second in ('contributory', 'page') else 'serial'
    if (first, second) in (('contributory', 'contributory'), ('page', 'contributory'), ('page', 'page')):
        return 'double'
    return 'serial'


def _vector(event):
    return {'int3': BP, 'into': OF, 'int1': DB, 'nmi': NMI}.get(event['kind'], event.get('vector'))


def _deliver_once(s, event):
    """One delivery attempt from `s` (mutated only on success): (diff, undefined frame slots), or raises Fault."""
    bus = _Bus(s)
    try:
        return _attempt(s, bus, event)
    except Fault as f:
        f.maybe = frozenset(bus.maybe)
        raise


def _attempt(s, bus, event):
    kind, vector = event['kind'], _vector(event)
    ext = 0 if kind in SOFTWARE else 1                             # §7.13 EXT
    cpl = s['cpl']
    idt_base, idt_limit = s['idtr']
    gate_error = vector << 3 | 2 | ext                             # §7.13: IDT = 1
    if vector * 16 + 15 > idt_limit:
        raise Fault(GP, gate_error)
    raw = bus.read(idt_base + vector * 16, 16)
    lo, hi = int.from_bytes(raw[:8], 'little'), int.from_bytes(raw[8:], 'little')
    gtype, dpl, present = lo >> 40 & 0x1F, lo >> 45 & 3, lo >> 47 & 1
    selector, ist = lo >> 16 & 0xFFFF, lo >> 32 & 7
    offset = lo & 0xFFFF | (lo >> 48 & 0xFFFF) << 16 | (hi & 0xFFFFFFFF) << 32
    if gtype in (6, 7):                                            # 16-bit gates: #GP(0) per §7.14.1 (A11)
        raise Fault(GP, 0, error_alts=frozenset({0, gate_error}))
    if gtype not in (INTERRUPT_GATE, TRAP_GATE):                   # task gates, other system types, S = 1: §7.14.1
        raise Fault(GP, gate_error)
    if kind in SOFTWARE and dpl < cpl:                             # §7.12.1.2; INT1 is exempt
        raise Fault(GP, vector << 3 | 2)
    if not present:
        raise Fault(NP, gate_error)
    if selector & 0xFFFC == 0:
        raise Fault(GP, ext)
    cs_addr, cs = _descriptor(bus, selector, ext)
    cs_error = (selector & 0xFFFC) | ext
    if not _is_code(cs) or cs['dpl'] > cpl:
        raise Fault(GP, cs_error)
    if not cs['p']:
        raise Fault(NP, cs_error)
    if not cs['l'] or cs['d']:                                     # §7.14.1: must be 64-bit; error = vector (A11)
        raise Fault(GP, gate_error, error_alts=frozenset({gate_error, cs_error}))
    new_cpl = cpl if _conforming(cs) else cs['dpl']
    if ist or new_cpl != cpl:                                      # §7.14.4, §7.14.5: RSPn or ISTn from the TSS
        tr_selector, tss_base, tss_limit = s['tr']
        slot = TSS_IST1 + 8 * (ist - 1) if ist else TSS_RSP0 + 8 * new_cpl
        if slot + 7 > tss_limit:
            raise Fault(TS, (tr_selector & 0xFFFC) | ext)
        rsp = int.from_bytes(bus.read(tss_base + slot, 8), 'little')
        if not canonical(rsp):
            raise Fault(SS, ext)
    else:
        rsp = s['rsp']
    if not canonical(offset):                                      # A5: after the stack checks
        raise Fault(GP, ext)
    rsp &= ~0xF                                                    # §7.14.2: 16-byte alignment, every case
    image = s['rflags']
    if kind in SOFTWARE:
        rf = 0                                                     # §20.3.1.1: RF cleared at INT start
    elif event.get('mid_string') or (kind == 'exception' and (vector in FAULT_VECTORS or event.get('fault'))):
        rf = 1                                                     # fault class, or mid-REP: RF image = 1
    else:
        rf = image >> 16 & 1
    image = image & ~RF | rf << 16
    rip = s['rip'] + event.get('length', 0) if kind in SOFTWARE | {'int1'} else s['rip']
    frame = [s['ss'], s['rsp'], image, s['cs'], rip]
    error = None
    if kind == 'exception' and vector in ERROR_CODE_VECTORS:       # §7.13: never for INT n / INTR / NMI
        error = event.get('error', 0)
        frame.append(error)
    top, low = rsp, rsp - 8 * len(frame)
    for a in range(top - 8, low - 1, -8):
        if not canonical(a):
            raise Fault(SS, ext)
    bus.maybe |= {a for a in range(low, top) if not bus.fault(a, True, new_cpl)}
    bus.check(low, top - low, True, new_cpl, first=lambda bad: max(bad) & ~7)   # A1: first push to fault, SS first
    for i, value in enumerate(frame):
        bus.write(top - 8 * (i + 1), value.to_bytes(8, 'little'), new_cpl)      # A3: at the new CPL
    _set_accessed(bus, cs_addr, cs)
    diff = bus.commit()
    undefined = {}
    if kind == 'int1':
        undefined[top - 24] = frozenset({image & ~RF, image})     # A12
    if kind == 'exception' and vector == DF:
        undefined.update({top - 24: frozenset({image, image | RF}), top - 32: None, top - 40: None})   # A6
    if error is not None and event.get('error_alts'):
        undefined[top - 48] = event['error_alts']
    s.update(rip=offset, rsp=low, cpl=new_cpl, cs=(selector & 0xFFFC) | new_cpl, cs_l=1, shadow=None, halted=False)
    if new_cpl != cpl:
        s['ss'] = new_cpl                                          # NULL selector with RPL = new CPL (§7.14.4)
    s['rflags'] &= ~(TF | NT | RF | VM | (IF if gtype == INTERRUPT_GATE else 0))   # §7.12.1.3
    return diff, undefined


def deliver(state, event, _maybe=frozenset()):
    """Deliver `event` through the 64-bit IDT; chain nested faults per Table 7-5."""
    s = copy.deepcopy(state)
    assert s['cr0'] & 1 and s['cr0'] >> 31, 'IA-32e mode only'
    undefined = dict(memory=set(_maybe), frame={}, state={}, events={})
    if event['kind'] == 'into':
        if s['cs_l']:
            event = {'kind': 'exception', 'vector': UD}            # Vol. 2A INTO: #UD in 64-bit mode
        elif not s['rflags'] & OF_FLAG:
            s['rip'] += event['length']
            return dict(outcome='none', state=s, diff={}, events=[], undefined=_freeze(undefined))
    if event['kind'] == 'nmi':
        s['nmi_blocked'] = True                                    # §7.7.2: blocked before delivery commences
    vector = _vector(event)
    events = [(vector, event.get('error') if event['kind'] == 'exception' and vector in ERROR_CODE_VECTORS else None)]
    if event['kind'] == 'exception' and event['vector'] == PF:
        s['cr2'] = event['cr2']                                    # §7.15 interrupt 14: CR2 before delivery
        _cr2_alternatives(undefined, event.get('cr2_alts'))
    while True:
        try:
            diff, undefined['frame'] = _deliver_once(s, event)
            undefined['memory'] -= {a + i for a, data in diff.items() for i in range(len(data))}
            return dict(outcome='delivered', state=s, diff=diff, events=events, undefined=_freeze(undefined))
        except Fault as f:
            events.append((f.vector, f.error))
            if f.error_alts:
                undefined['events'][len(events) - 1] = frozenset((f.vector, e) for e in f.error_alts)
            undefined['memory'] |= f.maybe
            nested = {'kind': 'exception', 'vector': f.vector, 'error': f.error, 'cr2': f.cr2,
                      'error_alts': f.error_alts}
            outcome = double_fault_outcome(event_class(event), event_class(nested))
            if f.vector == PF:
                s['cr2'] = f.cr2                                   # a detected #PF writes CR2 whatever follows
                _cr2_alternatives(undefined, f.cr2_alts)
            if outcome == 'shutdown':
                return dict(outcome='shutdown', state=s, diff={}, events=events, undefined=_freeze(undefined))
            if outcome == 'double':
                events.append((DF, 0))
            event = nested if outcome == 'serial' else {'kind': 'exception', 'vector': DF, 'error': 0}


def _cr2_alternatives(undefined, alts):
    if alts and len(alts) > 1:
        undefined['state']['cr2'] = alts
    else:
        undefined['state'].pop('cr2', None)


def _freeze(undefined):
    return dict(undefined, memory=frozenset(undefined['memory']))


# ---- IRETQ ------------------------------------------------------------------------------------------------------

def _iretq_once(s, bus):
    """Vol. 2A IRET, IA-32e-MODE with 64-bit operand size; mutates `s` only on success."""
    cpl = s['cpl']
    if s['rflags'] & NT:
        raise Fault(GP, 0)                                         # no task return in IA-32e mode
    words = []
    for i in range(5):                                             # pop order: RIP, CS, RFLAGS, RSP, SS
        a = s['rsp'] + 8 * i
        if not canonical(a):
            raise Fault(SS, 0)
        words.append(int.from_bytes(bus.read(a, 8, cpl), 'little'))
    rip, cs_sel, flags, rsp, ss_sel = words[0], words[1] & 0xFFFF, words[2], words[3], words[4] & 0xFFFF
    rpl = cs_sel & 3
    if cs_sel & 0xFFFC == 0:
        raise Fault(GP, 0)
    cs_addr, cs = _descriptor(bus, cs_sel, 0)
    if not _is_code(cs) or rpl < cpl:
        raise Fault(GP, cs_sel & 0xFFFC)
    if (cs['dpl'] > rpl) if _conforming(cs) else (cs['dpl'] != rpl):
        raise Fault(GP, cs_sel & 0xFFFC)
    if not cs['p']:
        raise Fault(NP, cs_sel & 0xFFFC)
    to_64 = cs['l'] and not cs['d']
    if (not canonical(rip)) if to_64 else (rip > cs['limit']):
        raise Fault(GP, 0)
    ss_addr = ss = None
    if ss_sel & 0xFFFC == 0:
        if not to_64 or rpl == 3 or ss_sel & 3 != rpl:             # NULL SS: 64-bit CS, CPL < 3, RPL = CPL (A7)
            raise Fault(GP, 0)
    else:
        ss_addr, ss = _descriptor(bus, ss_sel, 0)
        if ss_sel & 3 != rpl or not _writable_data(ss) or ss['dpl'] != rpl:
            raise Fault(GP, ss_sel & 0xFFFC)
        if not ss['p']:
            raise Fault(SS, ss_sel & 0xFFFC)
    mask = IRET_ALWAYS
    if cpl <= (s['rflags'] & IOPL) >> 12:
        mask |= IF
    if cpl == 0:
        mask |= IOPL | VIF | VIP
    _set_accessed(bus, cs_addr, cs)
    if ss is not None:
        _set_accessed(bus, ss_addr, ss)
    diff = bus.commit()
    s.update(rip=rip, cs=cs_sel, cs_l=int(to_64), cpl=rpl, rsp=rsp, ss=ss_sel, shadow=None,
             rflags=(s['rflags'] & ~mask | flags & mask) & ~VM | 2)
    return diff


def iretq(state):
    """Execute IRETQ at `state` (state['rip'] is the IRETQ). NMI blocking is released first (A8)."""
    s = copy.deepcopy(state)
    assert s['cs_l'], 'IRETQ exists only in 64-bit mode'
    s['nmi_blocked'] = False                                       # §7.7.2
    bus = _Bus(s)
    try:
        diff = _iretq_once(s, bus)
        return dict(outcome='returned', state=s, diff=diff, events=[],
                    undefined=dict(memory=frozenset(), frame={}, state={}, events={}))
    except Fault as f:
        r = deliver(s, {'kind': 'exception', 'vector': f.vector, 'error': f.error, 'cr2': f.cr2,
                        'cr2_alts': f.cr2_alts}, _maybe=bus.maybe)
        return r


# ---- recognition at instruction boundaries ----------------------------------------------------------------------

# Table 7-2 classes, highest first; order inside a class is implementation-dependent (§7.9). FLUSH/STOPCLK omitted.
# 'execute_fault' (faults while executing the next instruction) is a modelling extension: Table 7-2 has 9 classes.
CLASSES = (('reset', 'mc'), ('task_trap',), ('smi', 'init'), ('single_step', 'data_breakpoint'), ('nmi',), ('intr',),
           ('code_breakpoint',), ('fetch_fault',), ('decode_fault',), ('execute_fault',))
PRIORITY = tuple(name for cls in CLASSES for name in cls)
WAKES_HLT = {'reset', 'init', 'smi', 'nmi', 'intr', 'single_step', 'data_breakpoint'}  # A10


def _blocked_by(state, *kinds):
    shadow = state['shadow']
    if shadow in kinds:
        return 'no'
    return 'maybe' if shadow and shadow.rstrip('?') in kinds else 'yes'


def _eligible(state, name):
    """'yes', 'no' or 'maybe' (A9, A10)."""
    if state['halted'] and name not in WAKES_HLT:
        return 'maybe' if name == 'mc' else 'no'
    if name == 'intr':
        return _blocked_by(state, 'sti', 'mov_ss') if state['rflags'] & IF else 'no'
    if name == 'nmi':
        if state['nmi_blocked']:
            return 'no'
        verdict = _blocked_by(state, 'mov_ss')
        return 'maybe' if verdict == 'yes' and state['shadow'] in ('sti', 'sti?') else verdict
    if name == 'smi':
        return 'maybe' if state['shadow'] else 'yes'
    if name in ('single_step', 'data_breakpoint', 'code_breakpoint'):
        return _blocked_by(state, 'mov_ss')
    return 'yes'


def recognise(state, pending):
    """Frozenset of the pending events the CPU may take at this boundary (Table 7-2); None = may take none."""
    out = set()
    for cls in CLASSES:
        verdicts = {name: _eligible(state, name) for name in cls if name in pending}
        out |= {name for name, v in verdicts.items() if v != 'no'}
        if 'yes' in verdicts.values():
            return frozenset(out)
    return frozenset(out | {None})


def after_instruction(state, instruction):
    """State at the boundary after `instruction` ('sti', 'cli', 'mov_ss', 'pop_ss', 'hlt' or 'other') retires."""
    s = copy.deepcopy(state)
    inside, shadow = s['shadow'] is not None, None
    if instruction == 'sti':
        if inside:
            shadow = 'sti?'                                        # only the first is guaranteed (A9)
        elif not s['rflags'] & IF:
            shadow = 'sti'                                         # §7.8.1: only when IF was 0
        s['rflags'] |= IF
    elif instruction == 'cli':
        s['rflags'] &= ~IF
    elif instruction in ('mov_ss', 'pop_ss'):
        shadow = 'mov_ss?' if inside else 'mov_ss'                 # §7.8.3
    elif instruction == 'hlt':
        s['halted'] = True
    s['shadow'] = shadow
    return s


# ---- local APIC -------------------------------------------------------------------------------------------------

def apic_ppr(isr, tpr):
    """Permitted PPR values (§13.8.3.1); PPR[3:0] is model-specific when the classes are equal (A13)."""
    isrv = max(isr, default=0)
    if tpr >> 4 & 0xF > isrv >> 4:
        return frozenset({tpr & 0xFF})
    if tpr >> 4 & 0xF < isrv >> 4:
        return frozenset({isrv & 0xF0})
    return frozenset({tpr & 0xFF, tpr & 0xF0})


def apic_deliverable(irr, isr, tpr):
    """Vector the LAPIC presents to the core, or None (§13.8.3.1, §13.8.4). `irr`/`isr`: sets of vectors."""
    if not irr:
        return None
    vector, ppr = max(irr), max(apic_ppr(isr, tpr))               # every permitted PPR has the same PPR[7:4]
    return vector if vector >> 4 > ppr >> 4 else None


# ---- builders for tests and runners -----------------------------------------------------------------------------

def segment(dpl, code=True, l=1, d=0, present=True, conforming=False, writable=True, accessed=True):
    """An 8-byte flat code or data descriptor as an int (§3.4.5)."""
    t = (0x8 | (4 if conforming else 0) | 2) if code else (2 if writable else 0)
    access = t | (1 if accessed else 0) | 0x10 | dpl << 5 | (0x80 if present else 0)
    flags = 8 | (l << 1 if code else 0) | d << 2
    return 0xFFFF | access << 40 | 0xF << 48 | flags << 52


def gate(offset, selector, dpl=0, gtype=INTERRUPT_GATE, ist=0, present=True):
    """A 16-byte 64-bit IDT gate (§7.14.1, Figure 7-8)."""
    lo = (offset & 0xFFFF | selector << 16 | ist << 32 | gtype << 40 | dpl << 45 | int(present) << 47
          | (offset >> 16 & 0xFFFF) << 48)
    return lo.to_bytes(8, 'little') + (offset >> 32 & 0xFFFFFFFF).to_bytes(8, 'little')


def tss(rsp=(0, 0, 0), ist=(0,) * 7):
    """A 104-byte 64-bit TSS image (§10.7)."""
    image = bytearray(104)
    for i, v in enumerate(rsp):
        image[TSS_RSP0 + 8 * i:TSS_RSP0 + 8 * i + 8] = v.to_bytes(8, 'little')
    for i, v in enumerate(ist):
        image[TSS_IST1 + 8 * i:TSS_IST1 + 8 * i + 8] = v.to_bytes(8, 'little')
    return bytes(image)


def page(write=True, user=False):
    return {'data': bytearray(4096), 'write': write, 'user': user}


def place(memory, addr, data):
    for i, b in enumerate(data):
        memory[(addr + i) >> 12]['data'][(addr + i) & 0xFFF] = b


def qword(memory, addr):
    return int.from_bytes(bytes(memory[(addr + i) >> 12]['data'][(addr + i) & 0xFFF] for i in range(8)), 'little')
