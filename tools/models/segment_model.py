"""Independent reference for the descriptor-table, segment-register and far-transfer instructions in 64-bit mode,
written from the Intel SDM, not from natemu's code; it reuses `delivery_model`'s paged bus, fault and descriptor-field
helpers so the event-delivery oracle (X1) and this one read descriptors the same way.

Sources (titles; the repo snapshot of Vol. 3A is `artifacts/research/track01/source-snapshots/2026-09-03/`, Vol. 2 is
not in it, so instruction pseudocode is a recalled transcription): Vol. 2 "LGDT/LIDT", "SGDT", "SIDT", "LLDT", "LTR",
"MOV—Move" (segment register loads, "64-Bit Mode Exceptions"), "JMP", "CALL" (far, protected mode, conforming /
nonconforming code segment), "RET" (far, same and outer privilege level), "IRET"; Vol. 3A "Segment Selectors", "Segment
Descriptors" (accessed bit), "Segment Descriptor Tables", "System Descriptor Types" (IA-32e mode: 2 = LDT, 9/B = 64-bit
TSS available/busy, C = call gate), "TSS Descriptor in 64-bit Mode" and "LDT" (16-byte descriptors, upper type field 0),
"Privilege Checks" (data segment DPL vs max(CPL, RPL); stack segment RPL = DPL = CPL), "Implicit supervisor accesses".

Scope: IA-32e mode, CS a 64-bit code segment (L = 1, D = 0), and what enters compatibility mode from it. M2 task 3a: IRET
whose new CS is a compatibility-mode code segment (L = 0) with RPL 3 is done: the frame's RIP is checked against the 4 GiB
limit of the flat segment natemu runs (#GP(0)), RSP is loaded zero-extended from its low half. natemu runs 32-bit code
only with flat segments (`refused_shape`): where the state after such an IRET (or after a segment load in 32-bit code,
`load`) is one it does not run, its recorded deviation is #GP(the offending selector) before the next instruction (a
`Deviation` at that RIP with the completed state; x86 would run the code, so no x86 reference applies). The other
transfers to compatibility mode are not built (task 5): a far JMP, CALL or RET whose target CS is not a 64-bit code segment
(L = 0), and any transfer to L = D = 1, is `refused`, after the descriptor checks the SDM makes before the mode matters; so
is a 64-bit call gate (not built). Owner decision of November 6: where that code would run at CPL 3 (JMP/CALL far at CPL
3, RET far to RPL 3; a call gate at CPL 3) it is instead the deviation #GP(selector) with nothing changed (a `Deviation`
fault: the result's `deviation` is True). `null_access` and `flat_access` are the two faults of a 32-bit access that
translated code must still raise (a null FS, GS, DS or ES; the 4 GiB limit). An IRET loading TF is
done (its single-step trap after the next instruction is natemu's S4). Paging is delivery_model's abstraction (`memory`: page number -> {'data', 'write',
'user'}); descriptor-table and TSS-descriptor accesses are implicit supervisor accesses (CPL 0 rights, U/S = 0 in a #PF),
operand and stack accesses are made at the CPL.

State dict (delivery_model's keys plus segments): rip (the instruction), length (its length, for CALL's return
address), rsp, rflags, cpl, cs, ss (selectors), cr0, gdtr = (base, limit), idtr, ldtr = (selector, base, limit) or None
(null), tr = (selector, base, limit) or None, seg = {'es'|'ds'|'fs'|'gs'|'ss'|'cs': (selector, attributes)} where
attributes are descriptor bits 47:40 | bits 55:52 << 12 (natemu's `Segment::attributes`; 0 = null), fs_base, gs_base,
memory; optionally shape = {sreg: (base, limit)} of the segments loaded with other than base 0 and a 4 GiB limit (every
load here records it; a state without the key has flat segments). Every operation returns {'outcome': 'done' | 'fault' | 'refused', 'state', 'diff': {address: bytes},
'fault': (vector, error, cr2) or None, 'reason': text for 'refused', 'permitted': frozenset of (outcome, fault)}; a fault
leaves the state as it was (the SDM lets a failed segment load set the accessed bit; the model commits no write: B1).
`outcome`/`fault` are the model's single choice; `permitted` is every result the SDM allows (it differs from
{(outcome, fault)} only at B2, B8, B9 and B10), and a checker must accept any member.

AMBIGUITIES (the model's choice; `permitted` widens B2, B8, B9 and B10, and runners build no case on the others):
B1 A failed instruction commits no write (delivery_model A2).
B2 LGDT/LIDT with a non-canonical base: AMD APM Vol. 3 LGDT/LIDT lists #GP(0); Intel's exception list (recalled) does not;
   KVM raises it, QEMU loads the base. Model: #GP(0); permitted: #GP(0) or the load.
B3 LLDT/LTR: #GP(selector) for a nonzero type field in the upper 8 bytes (bits 12:8; QEMU checks it for LTR only) and
   for a non-canonical 64-bit base (KVM checks; QEMU does not); checked after type and P.
B4 Check order where several fail: as written below. IRETQ: CS, RIP, then SS (delivery_model's order); RET to an outer
   level: CS, SS, then RIP (RET's pseudocode).
B5 A return to an outer level nulls DS/ES/FS/GS that are null or hold data or nonconforming code with DPL < new CPL
   (selector 0, attributes 0); the FS/GS bases stay (RET's pseudocode changes only the selector; QEMU keeps them, and
   leaves an already null FS/GS selector as it is).
B6 A null SS on IRET or an outer RET is allowed only for a 64-bit return to a new CPL < 3 with RPL = the new CPL (SDM
   IRET/RET 64-bit mode exceptions: #GP(0) for a null SS whose RPL != CPL going back to non-CPL3 64-bit mode), as MOV SS.
   delivery_model A7 now states the same rule (X1a).
B7 Loading a null selector into FS or GS clears its base (Intel's behaviour, the one Linux's NULL_SEG bug test reads).
B8 The accessed-bit and LTR busy-bit writes: the SDM gives no access width (a locked read-modify-write; QEMU writes the
   descriptor's high dword, KVM the whole descriptor). Model: byte 5 (CR2 = descriptor + 5 on a read-only GDT page);
   permitted CR2: descriptor + 0, + 4 or + 5.
B10 An access that runs across the 4 GiB end of a segment whose effective limit is FFFFFFFFH (`flat_access`): Vol. 3A
   "Limit Checking" says such accesses "may or may not" raise #GP/#SS, implementation-specific. Model: the fault;
   permitted: the fault or completion.
B9 A not-present SS on IRET or an outer RET: the pseudocode gives #SS(selector) (delivery_model's choice, the model's),
   the exception lists #NP(selector) for "the return code or stack segment" (QEMU's); permitted: either.

THE INTERPRETER'S MODES (M2 Part 2, the boot-interpreter plan sections 5 and 8): `jmp_far_interp`, `ret_far_interp` and
`load_interp` are far JMP (ptr16:16, ptr16:32, m16:16, m16:32), far RET (16- and 32-bit, with or without imm16) and MOV
Sreg in the modes `interpreted_mode` names (R, P16, P32, C16, C32) at CPL 0, from Vol. 2 "JMP" (far jumps: real-address
mode, then protected mode with the conforming and nonconforming code-segment paths), "RET" (real-address mode far
return; protected mode with LMA = 0; IA-32e mode, whose order puts the L = D = 1 check before the RPL checks; return to
the same privilege level) and "MOV" (segment loads, protected-mode exceptions), and Vol. 3A "Limit Checking" and "Type
Checking" (data accesses: #GP(0), #SS(0) through SS). Order of checks, each the SDM's: the operand (segment check, then
the page walk in compatibility mode), the stack slots, null selector #GP(0), table limit #GP(sel), type #GP(sel), with
LMA = 1 L = D = 1 #GP(sel), privilege #GP(sel), present #NP(sel), then the offset against the new CS limit #GP(0)
(none for a 64-bit target, whose 32-bit offset is canonical). Two rules of the 64-bit functions above do not apply
here: a null SS is #GP(0) at every CPL (`_load`'s CPL < 3 allowance is the 64-bit rule, "MOV" 64-bit mode exceptions),
and L is ignored when LMA = 0 (a code descriptor with L = D = 1 is a 32-bit segment there; `far` refuses it as the 64-bit
rule says). natemu's first version (plan section 1) does not interpret gates, task switches, privilege changes or CPL
1-3: those are `refused` after the checks the SDM makes before them (a typed stop for the interpreter). A far transfer
that loads a 64-bit CS with LMA = 1 completes with `handoff` True: the vCPU leaves interpreted state for the translator.
With CR0.PG = 0 there is no #PF: linear = physical, and a page the state's `memory` lacks is unassigned (reads all-ones,
writes ignored: the plan's section 4, layer 3); in compatibility mode the paged bus is delivery_model's.

The hidden descriptor cache is the state's `seg[sreg]` = (selector, attributes) with `shape[sreg]` = (base, limit) (every
interpreted state carries `shape` for each loaded segment); `rip` is EIP (the offset in CS), `rsp` ESP, `efer` holds
LMA (bit 10). Ambiguities, as above:
B11 A real-mode load (MOV Sreg, far JMP, far RET) changes the selector and the base (selector x 16) only; the hidden limit
   and attributes stay (KVM's emulator: "keep limit etc. for unreal mode"; QEMU's real-mode loads write only the selector
   and base). The SDM's return to real mode (Vol. 3A 12.9.2 "Switching Back to Real-Address Mode") loads 64 KiB segments
   in protected mode first and says that "if the segment registers are not reloaded, execution continues using the
   descriptor attributes loaded during protected mode".
B12 RET far in real mode with a 32-bit operand size: the pseudocode checks EIP against the CS limit only for a 16-bit
   operand size; the exception list ("If the return instruction pointer is not within the return code segment limit")
   for both. Model: #GP(0) for both; permitted for the 32-bit form: completion.
B13 Each stack slot of a far RET is checked as the pop that reads it: on a 16-bit stack (SS.B = 0) the second slot's
   offset wraps at 64 KiB ("top n bytes" in the pseudocode). The stack-address size is SS.B in every mode (the
   real-mode pseudocode's "SP := SP + (SRC AND FFFFH)" is the B = 0 case).
B14 Real-mode data accesses check the hidden limit (expand-down when the cached type says so) but no rights: KVM (the
   hardware) checks limits; QEMU's TCG checks none.
B15 The CS that INIT and SIPI leave: Vol. 3A Table 12-1 gives CS "AR = Present, R/W, Accessed"; KVM loads 9BH (code,
   readable, accessed). The model uses 9BH (`reset_state`); a fetch checks the CS limit only, never the cached type
   (rights are checked when CS is loaded), so either value runs the same code.
"""
import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models.delivery_model import (_Bus, Fault, canonical, GP, NP, SS, NT, TF, RF, VM, IF, IOPL, VIF, VIP, IRET_ALWAYS)

SREGS = ('es', 'cs', 'ss', 'ds', 'fs', 'gs')


class Refused(Exception):
    pass


class Deviation(Fault):
    """natemu's recorded deviation where x86 would run compatibility-mode code at CPL 3: #GP(selector). `rip`: where it is
    raised when not at the instruction (after an instruction that completed, whose final state is `state`: an IRET or
    SYSRET into code natemu does not run, at its target; a segment load in 32-bit code, at the next instruction)."""
    def __init__(self, vector, error, rip=None, state=None):
        super().__init__(vector, error)
        self.rip, self.state = rip, state


def refused_shape(s):
    """The selector natemu's deviation names when compatibility-mode code at CPL 3 cannot run under `s` (None: it runs):
    natemu's `Cpu::compat_refused`. 32-bit code (CS.D = 1) on a 32-bit stack (SS.B = 1), CS/SS/DS/ES with base 0 and a 4
    GiB limit, SS/DS/ES writable expand-up data; a null DS or ES runs (x86's fault at its first use: `null_access`)."""
    shape = s.get('shape', {})
    flat = lambda r: shape.get(r, (0, 0xFFFFFFFF)) == (0, 0xFFFFFFFF)
    data = lambda r: flat(r) and s['seg'][r][1] & 0x9E == 0x92
    null = lambda r: not s['seg'][r][1] & 0x80
    cs, ss = s['seg']['cs'], s['seg']['ss']
    if cs[1] >> 13 & 3 != 2 or not flat('cs'):
        return cs[0]
    if not data('ss') or not ss[1] >> 14 & 1:
        return ss[0]
    return next((s['seg'][r][0] for r in ('ds', 'es') if not null(r) and not data(r)), None)


def _loaded(s, sreg, sel, raw):
    """`sreg` := `sel` with the descriptor `raw` (accessed), its base and limit recorded when not flat."""
    s['seg'][sreg] = (sel, attributes(raw) | 1)
    s.setdefault('shape', {})[sreg] = (base32(raw), limit(raw))


def _compat(s):
    return s['cpl'] == 3 and not s['seg']['cs'][1] >> 13 & 1


def _refuse_shape(s, rip):
    """The deviation at `rip` when `s`, a completed state, is compatibility-mode code at CPL 3 natemu does not run."""
    if _compat(s) and refused_shape(s) is not None:
        raise Deviation(GP, refused_shape(s) & 0xFFFC, rip=rip, state=copy.deepcopy(s))


def attributes(raw):
    return (raw >> 40) & 0xFF | ((raw >> 52) & 0xF) << 12


def base32(raw):
    return (raw >> 16) & 0xFFFFFF | (raw >> 56 & 0xFF) << 24


def limit(raw):
    lim = raw & 0xFFFF | (raw >> 48 & 0xF) << 16
    return lim << 12 | 0xFFF if raw >> 55 & 1 else lim


def fields(raw):
    """(s, type, dpl, p, l, d)."""
    return raw >> 44 & 1, raw >> 40 & 0xF, raw >> 45 & 3, raw >> 47 & 1, raw >> 53 & 1, raw >> 54 & 1


def _gp(sel): return Fault(GP, sel & 0xFFFC)
def _np(sel): return Fault(NP, sel & 0xFFFC)
def _ss(sel): return Fault(SS, sel & 0xFFFC)


def descriptor(s, bus, sel, wide=False):
    """(address, raw) of the descriptor `sel` names (GDT, or LDT for TI = 1): #GP(sel) outside the limit or with a null LDTR."""
    if sel & 4:
        if s['ldtr'] is None:
            raise _gp(sel)
        _, base, lim = s['ldtr']
    else:
        base, lim = s['gdtr']
    index = sel & ~7
    if index + (15 if wide else 7) > lim:
        raise _gp(sel)
    addr = base + index
    if not (canonical(addr) and canonical(addr + (15 if wide else 7))):
        raise _gp(sel)
    return addr, int.from_bytes(bus.read(addr, 16 if wide else 8, 0), 'little')


def _descriptor_write(bus, addr, value):
    """Byte 5 of the descriptor at `addr` := value, a supervisor write; a #PF's CR2 may be addr, + 4 or + 5 (B8)."""
    try:
        bus.write(addr + 5, bytes([value]), 0)
    except Fault as f:
        raise _widen(f, [(f.vector, f.error, addr + k) for k in (0, 4, 5)])


def _accessed(bus, addr, raw):
    if not raw >> 40 & 1:
        _descriptor_write(bus, addr, (raw >> 40 & 0xFF) | 1)


def _operand(s, bus, addr, n, write=None, stack=False):
    """Read (or write `write`) n bytes of an explicit operand at the CPL; #SS(0)/#GP(0) when not canonical."""
    if not (canonical(addr) and canonical(addr + n - 1)):
        raise Fault(SS if stack else GP, 0)
    if write is None:
        return bus.read(addr, n, s['cpl'])
    bus.write(addr, write, s['cpl'])


def _widen(fault, faults=(), may_complete=False):
    """`fault` with the other results the SDM permits (B2, B8, B9): alternative (vector, error, cr2), or completion."""
    fault.permitted = frozenset(('fault', f) for f in faults) | ({('done', None)} if may_complete else set())
    return fault


class _PhysBus(_Bus):
    """CR0.PG = 0: linear = physical, no #PF; a page the state's `memory` lacks is unassigned (all-ones, writes ignored)."""

    def check(self, addr, n, write, cpl, first=None):
        pass

    def read(self, addr, n, cpl=0):
        mem = self.state['memory']
        return bytes(self.writes.get(a, mem[a >> 12]['data'][a & 0xFFF] if a >> 12 in mem else 0xFF)
                     for a in range(addr, addr + n))

    def write(self, addr, data, cpl=0):
        for i, b in enumerate(data):
            if (addr + i) >> 12 in self.state['memory']:
                self.writes[addr + i] = b


def _run(state, body):
    s = copy.deepcopy(state)
    bus = (_Bus if s['cr0'] >> 31 & 1 else _PhysBus)(s)
    try:
        body(s, bus)
    except Fault as f:
        fault = (f.vector, f.error, f.cr2 or 0)
        return dict(outcome='fault', state=getattr(f, 'state', None) or copy.deepcopy(state), diff={}, fault=fault, reason=None,
                    permitted=frozenset({('fault', fault)}) | getattr(f, 'permitted', frozenset()),
                    deviation=isinstance(f, Deviation), fault_rip=getattr(f, 'rip', None))
    except Refused as r:
        return dict(outcome='refused', state=copy.deepcopy(state), diff={}, fault=None, reason=str(r),
                    permitted=frozenset({('refused', None)}))
    return dict(outcome='done', state=s, diff=bus.commit(), fault=None, reason=None, permitted=frozenset({('done', None)}))


def lgdt(state, addr, table='gdtr', stack=False):
    def body(s, bus):
        if s['cpl']:
            raise Fault(GP, 0)
        raw = _operand(s, bus, addr, 10, stack=stack)
        lim, base = int.from_bytes(raw[:2], 'little'), int.from_bytes(raw[2:], 'little')
        if not canonical(base):
            raise _widen(Fault(GP, 0), may_complete=True)          # B2
        s[table] = (base, lim)
    return _run(state, body)


def sgdt(state, addr, table='gdtr', stack=False):
    def body(s, bus):
        base, lim = s[table]
        _operand(s, bus, addr, 10, lim.to_bytes(2, 'little') + base.to_bytes(8, 'little'), stack)
    return _run(state, body)


def _system(s, bus, sel, kind):
    if sel & 4:
        raise _gp(sel)
    addr, raw = descriptor(s, bus, sel, wide=True)
    st, t, _, p, _, _ = fields(raw & (1 << 64) - 1)
    if st or t != kind:
        raise _gp(sel)
    if not p:
        raise _np(sel)
    base = base32(raw) | (raw >> 64 & 0xFFFFFFFF) << 32
    if raw >> 104 & 0x1F or not canonical(base):
        raise _gp(sel)                                             # B3
    return addr, raw & (1 << 64) - 1, base


def lldt(state, sel):
    def body(s, bus):
        if s['cpl']:
            raise Fault(GP, 0)
        if sel & 0xFFFC == 0:
            s['ldtr'] = None
            return
        _, raw, base = _system(s, bus, sel, 2)
        s['ldtr'] = (sel, base, limit(raw))
    return _run(state, body)


def ltr(state, sel):
    def body(s, bus):
        if s['cpl']:
            raise Fault(GP, 0)
        if sel & 0xFFFC == 0:
            raise Fault(GP, 0)
        addr, raw, base = _system(s, bus, sel, 9)
        _descriptor_write(bus, addr, (raw >> 40 & 0xFF) | 2)    # busy (B8)
        s['tr'] = (sel, base, limit(raw))
    return _run(state, body)


def _load(s, bus, sreg, sel):
    cpl, rpl = s['cpl'], sel & 3
    if sel & 0xFFFC == 0:
        if sreg == 'ss' and (cpl == 3 or rpl != cpl):
            raise Fault(GP, 0)
        s['seg'][sreg] = (sel, 0)
        if sreg in ('fs', 'gs'):
            s[sreg + '_base'] = 0                                  # B7
        return
    addr, raw = descriptor(s, bus, sel)
    st, t, dpl, p, _, _ = fields(raw)
    code = t & 8
    if sreg == 'ss':
        if rpl != cpl or not st or code or not t & 2 or dpl != cpl:
            raise _gp(sel)
        if not p:
            raise _ss(sel)
    else:
        conforming = code and t & 4
        if not st or (code and not t & 2) or (not conforming and (rpl > dpl or cpl > dpl)):
            raise _gp(sel)
        if not p:
            raise _np(sel)
    _accessed(bus, addr, raw)
    _loaded(s, sreg, sel, raw)
    if sreg in ('fs', 'gs'):
        s[sreg + '_base'] = base32(raw)


def load(state, sreg, sel):
    """MOV sreg, r/m16 with the selector already read (MOV to CS is #UD: not a case here). In 32-bit code at CPL 3 a load
    that leaves segments natemu does not run is its deviation at the next instruction (`refused_shape`)."""
    def body(s, bus):
        _load(s, bus, sreg, sel)
        _refuse_shape(s, s['rip'] + s['length'])
    return _run(state, body)


def _compat(s):
    """The code runs in compatibility mode (CS.L = 0)."""
    return not s['seg']['cs'][1] >> 13 & 1


def null_access(state, sreg):
    """A memory access through FS, GS, DS or ES (`sreg`) in compatibility mode: #GP(0) while it holds a null selector (Vol. 3A
    "Segment Loading Instructions in IA-32e Mode": a null selector's segment faults on use outside 64-bit mode)."""
    def body(s, bus):
        if not s['seg']['cs'][1] >> 13 & 1 and not s['seg'][sreg][1] & 0x80:
            raise Fault(GP, 0)
    return _run(state, body)


def flat_access(state, addr, n, stack=False):
    """An `n`-byte access at offset `addr` of a flat 4 GiB segment in compatibility mode that would run past the limit
    (Vol. 3A "Limit Checking": a doubleword at an offset above the limit - 3): #GP(0), or #SS(0) through SS; but with an
    effective limit of FFFFFFFFH the SDM lets such an access fault or not, implementation-specific (B10). Model: the
    fault (natemu's choice, and the reference AMD machine's: m2-task3a-findings); permitted: also completion, the
    access made wrapped (its own page faults are then not this model's)."""
    def body(s, bus):
        if addr + n > 1 << 32:
            raise _widen(Fault(SS if stack else GP, 0), may_complete=True)
    return _run(state, body)


def _refuse_compatibility(raw, sel, cpl, iret=False):
    """`cpl`: the CPL the target code would run at. `iret`: IRET, which loads a compatibility-mode CS with RPL 3."""
    _, _, _, _, l, d = fields(raw)
    if iret and cpl == 3 and not l:
        return
    if not l or d:
        if cpl == 3:
            raise Deviation(GP, sel & 0xFFFC)
        raise Refused('compatibility-mode code segment')


def far(state, kind, addr, width=8, stack=False):
    """JMP/CALL far through the m16:`width` pointer at `addr` (kind 'jmp' or 'call')."""
    def body(s, bus):
        ptr = _operand(s, bus, addr, width + 2, stack=stack)
        rip, sel = int.from_bytes(ptr[:width], 'little'), int.from_bytes(ptr[width:], 'little')
        if sel & 0xFFFC == 0:
            raise Fault(GP, 0)
        daddr, raw = descriptor(s, bus, sel)
        st, t, dpl, p, l, d = fields(raw)
        cpl = s['cpl']
        if not st and t == 0xC:
            if cpl == 3:
                raise Deviation(GP, sel & 0xFFFC)
            raise Refused('call gate')
        if not st or not t & 8 or ((dpl > cpl) if t & 4 else (sel & 3 > cpl or dpl != cpl)) or (l and d):
            raise _gp(sel)
        if not p:
            raise _np(sel)
        compat = _compat(s)
        if not compat:
            _refuse_compatibility(raw, sel, cpl)
        elif l:
            raise Deviation(GP, sel & 0xFFFC)                      # 32- to 64-bit code: task 5 (natemu's deviation)
        wrap = 0xFFFFFFFF if compat else (1 << 64) - 1             # 32-bit code: ESP and EIP wrap at 4 GiB
        rsp = s['rsp'] - (2 * width if kind == 'call' else 0) & wrap
        if compat and kind == 'call' and rsp + 2 * width > 1 << 32:
            raise Fault(SS, 0)                                     # the frame across the flat stack's limit
        if not compat and kind == 'call' and not (canonical(rsp) and canonical(s['rsp'] - 1)):
            raise Fault(SS, 0)
        if not canonical(rip):
            raise Fault(GP, 0)
        _accessed(bus, daddr, raw)
        if kind == 'call':
            frame = (s['rip'] + s['length'] & wrap).to_bytes(8, 'little')[:width] + s['cs'].to_bytes(8, 'little')[:width]
            bus.write(rsp, frame, cpl)
            s['rsp'] = rsp
        s['cs'] = sel & ~3 | cpl
        _loaded(s, 'cs', s['cs'], raw)
        s['rip'] = rip
    return _run(state, body)


def ret(state, width=8, release=0, iret=False):
    """RET far (imm16 `release`) or IRET with `width`-byte pops."""
    def body(s, bus):
        cpl = s['cpl']
        if iret and s['rflags'] & NT:
            raise Fault(GP, 0)
        # M2 task 3b: IRET from 32-bit code pops three words, and SS:ESP only for an outer return (SDM IRET, IA-32e mode).
        from32 = _compat(s)
        n = 2 if not iret else 3 if from32 else 5
        if from32 and s['rsp'] + n * width > 1 << 32:
            raise Fault(SS, 0)
        raw_words = _operand(s, bus, s['rsp'], n * width, stack=True)
        words = [int.from_bytes(raw_words[k * width:(k + 1) * width], 'little') for k in range(n)]
        rip, sel = words[0], words[1] & 0xFFFF
        rpl = sel & 3
        if sel & 0xFFFC == 0:
            raise Fault(GP, 0)
        caddr, raw = descriptor(s, bus, sel)
        st, t, dpl, p, _, _ = fields(raw)
        if not st or not t & 8 or rpl < cpl or ((dpl > rpl) if t & 4 else (dpl != rpl)):
            raise _gp(sel)
        if not p:
            raise _np(sel)
        if from32 and not iret and fields(raw)[4]:
            raise Deviation(GP, sel & 0xFFFC)                      # RET far from 32- to 64-bit code: task 5
        _refuse_compatibility(raw, sel, rpl, iret or from32)
        compat = not fields(raw)[4]                                # (to RPL 3 only: every other one was refused)
        bad_rip = rip >> 32 != 0 if compat else not canonical(rip)  # the flat 32-bit segment's limit, or 64-bit canonical
        if iret and bad_rip:
            raise Fault(GP, 0)                                     # B4: IRETQ checks RIP before SS
        outer = rpl > cpl
        stack = None
        if n == 5 or outer:
            if n == 5:
                rsp, ss_sel = words[3], words[4] & 0xFFFF
            else:
                tail = _operand(s, bus, s['rsp'] + n * width + release, 2 * width, stack=True)
                rsp, ss_sel = int.from_bytes(tail[:width], 'little'), int.from_bytes(tail[width:], 'little') & 0xFFFF
            ss = None
            if ss_sel & 0xFFFC == 0:
                if rpl == 3 or ss_sel & 3 != rpl:
                    raise Fault(GP, 0)                             # B6
            else:
                saddr, sraw = descriptor(s, bus, ss_sel)
                sst, stype, sdpl, sp, _, _ = fields(sraw)
                if ss_sel & 3 != rpl or not sst or stype & 8 or not stype & 2 or sdpl != rpl:
                    raise _gp(ss_sel)
                if not sp:
                    raise _widen(_ss(ss_sel), [(NP, ss_sel & 0xFFFC, 0)])   # B9
                ss = (saddr, sraw)
            stack = (rsp, ss_sel, ss)
        if not iret and bad_rip:
            raise Fault(GP, 0)                                     # B4: RET checks RIP after SS
        if iret:
            mask = IRET_ALWAYS | (IF if cpl <= (s['rflags'] & IOPL) >> 12 else 0) | (IOPL | VIF | VIP if cpl == 0 else 0)
            flags = (s['rflags'] & ~mask | words[2] & mask) & ~VM | 2
            s['rflags'] = flags                                    # RF from the frame (it suppresses an instruction breakpoint)
        _accessed(bus, caddr, raw)
        if stack and stack[2]:
            _accessed(bus, *stack[2])
        s.update(rip=rip, cs=sel, cpl=rpl)
        _loaded(s, 'cs', sel, raw)
        if stack:
            rsp, ss_sel, ss = stack
            s['rsp'] = rsp + (0 if iret else release)
            if compat:
                s['rsp'] &= 0xFFFFFFFF                             # ESP: the stack pointer of a 32-bit stack segment
            s['ss'] = ss_sel
            s['seg']['ss'] = (ss_sel, 0)
            if ss:
                _loaded(s, 'ss', ss_sel, ss[1])
        else:
            s['rsp'] = s['rsp'] + n * width + release & (0xFFFFFFFF if from32 else (1 << 64) - 1)
        if outer:
            for r in ('es', 'ds', 'fs', 'gs'):                     # B5
                sel_r, a = s['seg'][r]
                if not a & 0x80 or ((not a & 8 or not a & 4) and (a >> 5 & 3) < rpl):
                    s['seg'][r] = (0, 0)
        _refuse_shape(s, rip)
    return _run(state, body)


def iret(state, width=8):
    return ret(state, width, iret=True)


# ---- the interpreter's modes: R, P16, P32, C16, C32 at CPL 0 (boot-interpreter plan sections 5, 8) ----------------

CR0_PE, CR0_PG, EFER_LMA = 1, 1 << 31, 1 << 10


def interpreted_mode(cr0, efer, cs_attributes, cpl):
    """The plan's `Cpu::interpreted_mode`, the predicate "EFER.LMA = 0, or CPL 0 with a non-64-bit code segment": 'R'
    (CR0.PE = 0), 'P16'/'P32' (PE = 1, LMA = 0, by CS.D; L is ignored), 'C16'/'C32' (LMA = 1, CS.L = 0, CPL 0, by CS.D),
    None otherwise (64-bit code, or compatibility-mode code at CPL > 0: the translator's)."""
    d = cs_attributes >> 14 & 1
    if not cr0 & CR0_PE:
        return 'R'
    if not efer & EFER_LMA:
        return 'P32' if d else 'P16'
    if cs_attributes >> 13 & 1 or cpl:
        return None
    return 'C32' if d else 'C16'


def mode_of(s):
    return interpreted_mode(s['cr0'], s['efer'], s['seg']['cs'][1], s['cpl'])


def _cache(s, sreg):
    """(selector, attributes, base, limit) of `sreg`'s hidden descriptor cache."""
    sel, a = s['seg'][sreg]
    base, lim = s.get('shape', {}).get(sreg, (0, 0xFFFFFFFF))
    return sel, a, base, lim


def _within(a, lim, offset, n):
    """[offset, offset + n) inside a segment with attributes `a` and effective limit `lim` (Vol. 3A "Limit Checking"):
    expand-up, offset + n - 1 <= limit; expand-down data (type bit 2), limit < offset and the end within FFFFH or
    FFFFFFFFH by the B flag."""
    end = offset + n - 1
    if a & 0x18 == 0x10 and a & 4:
        return offset > lim and end <= (0xFFFFFFFF if a >> 14 & 1 else 0xFFFF)
    return end <= lim


def _data(s, bus, sreg, offset, n, write=None):
    """An explicit n-byte access at `offset` (wrapped at the address size by the caller) through `sreg`: the bytes read,
    or `write` stored. Protected mode: #GP(0) through a null segment, for a write to code or read-only data and a read of
    execute-only code; outside the limit #GP(0), or #SS(0) through SS (B10 for a flat segment's 4 GiB end). Real mode:
    the limit only (B14). Linear = base + offset, 32 bits."""
    _, a, base, lim = _cache(s, sreg)
    vector = SS if sreg == 'ss' else GP
    if mode_of(s) != 'R':
        code = a & 0x18 == 0x18
        if not a & 0x80:
            raise Fault(GP, 0)
        if (code or not a & 2) if write is not None else (code and not a & 2):
            raise Fault(GP, 0)
    if not _within(a, lim, offset, n):
        raise _widen(Fault(vector, 0), may_complete=lim == 0xFFFFFFFF and offset <= lim)
    linear = base + offset & 0xFFFFFFFF
    if write is None:
        return bus.read(linear, n, s['cpl'])
    bus.write(linear, write, s['cpl'])


def _real_cs(s, sel):
    """Real mode: CS := `sel`, base `sel` x 16, the hidden limit and attributes kept (B11)."""
    _, a, _, lim = _cache(s, 'cs')
    s['cs'] = sel
    s['seg']['cs'] = (sel, a)
    s.setdefault('shape', {})['cs'] = (sel << 4, lim)


def _load_cs(s, bus, sel, daddr, raw, off):
    _accessed(bus, daddr, raw)
    s['cs'] = sel & ~3 | s['cpl']
    _loaded(s, 'cs', s['cs'], raw)
    s['rip'] = off


def _finish(r):
    r['handoff'] = r['outcome'] == 'done' and mode_of(r['state']) is None
    return r


def _interp(s):
    assert mode_of(s), 'not an interpreted state'
    if s['cpl']:
        raise Refused('CPL 1-3: not interpreted')


def jmp_far_interp(state, target=None, pointer=None, osize=32):
    """JMP far in an interpreted mode: `target` = (selector, offset) from the instruction (EA: ptr16:16 with `osize` 16,
    ptr16:32 with 32), or `pointer` = (sreg, offset) of the m16:16 / m16:32 operand (FF /5, read first)."""
    def body(s, bus):
        _interp(s)
        width = osize // 8
        if pointer:
            raw = _data(s, bus, pointer[0], pointer[1], width + 2)
            off, sel = int.from_bytes(raw[:width], 'little'), int.from_bytes(raw[width:], 'little')
        else:
            sel, off = target
        if mode_of(s) == 'R':
            if off > _cache(s, 'cs')[3]:
                raise Fault(GP, 0)                                 # "tempEIP is beyond code segment limit"
            _real_cs(s, sel)
            s['rip'] = off & (1 << osize) - 1
            return
        if sel & 0xFFFC == 0:
            raise Fault(GP, 0)
        daddr, raw = descriptor(s, bus, sel)
        st, t, dpl, p, l, d = fields(raw)
        lma, cpl = s['efer'] & EFER_LMA, s['cpl']
        if not st:
            if t == 0xC or not lma and t in (1, 3, 4, 5, 9, 0xB):
                raise Refused('call gate' if t in (4, 0xC) else 'task gate or TSS: a task switch')
            raise _gp(sel)
        if not t & 8 or lma and l and d:
            raise _gp(sel)
        if (dpl > cpl) if t & 4 else (sel & 3 > cpl or dpl != cpl):
            raise _gp(sel)
        if not p:
            raise _np(sel)
        off &= (1 << osize) - 1
        if not (lma and l) and off > limit(raw):
            raise Fault(GP, 0)                                     # the new CS's limit (none for a 64-bit target)
        _load_cs(s, bus, sel, daddr, raw, off)
    return _finish(_run(state, body))


def ret_far_interp(state, osize=32, release=0):
    """RET far (CB; CA with imm16 `release`) with a 16- or 32-bit operand size in an interpreted mode, to the same
    privilege level (a return to an outer level is refused: natemu's first version has no privilege changes)."""
    def body(s, bus):
        _interp(s)
        width = osize // 8
        _, ssa, _, _ = _cache(s, 'ss')
        mask = 0xFFFFFFFF if ssa >> 14 & 1 else 0xFFFF
        esp = s['rsp'] & mask
        slots = [esp, esp + width & mask]
        for slot in slots:                                         # B13: each pop's slot, before any is read
            _, a, _, lim = _cache(s, 'ss')
            if not _within(a, lim, slot, width):
                raise _widen(Fault(SS, 0), may_complete=lim == 0xFFFFFFFF and slot <= lim)
        off, sel = (int.from_bytes(_data(s, bus, 'ss', slot, width), 'little') for slot in slots)
        sel &= 0xFFFF
        off &= (1 << osize) - 1
        if mode_of(s) == 'R':
            if off > _cache(s, 'cs')[3]:
                raise _widen(Fault(GP, 0), may_complete=osize == 32)   # B12
            _real_cs(s, sel)
            s['rip'] = off
        else:
            if sel & 0xFFFC == 0:
                raise Fault(GP, 0)
            daddr, raw = descriptor(s, bus, sel)
            st, t, dpl, p, l, d = fields(raw)
            lma, cpl, rpl = s['efer'] & EFER_LMA, s['cpl'], sel & 3
            if not st or not t & 8 or lma and l and d:             # IA-32e order: L = D = 1 before the RPL checks
                raise _gp(sel)
            if rpl < cpl or ((dpl > rpl) if t & 4 else (dpl != rpl)):
                raise _gp(sel)
            if not p:
                raise _np(sel)
            if rpl > cpl:
                raise Refused('return to an outer privilege level')
            if not (lma and l) and off > limit(raw):
                raise Fault(GP, 0)
            _load_cs(s, bus, sel, daddr, raw, off)
        s['rsp'] = s['rsp'] & ~mask | esp + 2 * width + release & mask
    return _finish(_run(state, body))


def load_interp(state, sreg, sel):
    """MOV Sreg (not CS) in an interpreted mode. Real mode: selector and base only (B11). Protected and compatibility
    mode: a null selector loads DS/ES/FS/GS as unusable and is #GP(0) for SS at every CPL (the 64-bit allowance of
    `_load` does not apply); otherwise `_load`'s descriptor checks."""
    def body(s, bus):
        assert sreg != 'cs', 'MOV to CS is #UD'
        _interp(s)
        if mode_of(s) == 'R':
            _, a, _, lim = _cache(s, sreg)
            s['seg'][sreg] = (sel, a)
            s.setdefault('shape', {})[sreg] = (sel << 4, lim)
            if sreg == 'ss':
                s['ss'] = sel
            return
        if sel & 0xFFFC == 0:
            if sreg == 'ss':
                raise Fault(GP, 0)
            s['seg'][sreg] = (sel, 0)
            s.setdefault('shape', {})[sreg] = (0, 0)
            return
        _load(s, bus, sreg, sel)
        if sreg == 'ss':
            s['ss'] = sel
    return _finish(_run(state, body))


# ---- builders for tests and runners -----------------------------------------------------------------------------

def segment(base=0, lim=0xFFFF, access=0x93, flags=0):
    """An 8-byte descriptor: `access` is bits 47:40 (P, DPL, S, type), `flags` bits 55:52 (G, D/B, L, AVL); a limit above
    FFFFFH is in bytes with its low 12 bits FFFH and sets G."""
    if lim > 0xFFFFF:
        lim, flags = lim >> 12, flags | 8
    return (lim & 0xFFFF | (base & 0xFFFFFF) << 16 | access << 40 | (lim >> 16 & 0xF) << 48 | flags << 52
            | (base >> 24 & 0xFF) << 56)


def reset_state(memory=None, sipi=None, cd_nw=0x60000000):
    """The INIT state (Vol. 3A Table 12-1, INIT column) in this model's state form: CR0 = CD/NW as kept | ET, EFLAGS 2,
    EIP FFF0H, CS F000H / base FFFF0000H / limit FFFFH (attributes 9BH, B15), the other segments 0 / 0 / FFFFH (93H),
    GDTR and IDTR base 0 limit FFFFH, LDTR and TR 0, EFER 0. `sipi`: the state a SIPI with that vector leaves (CS VV00H,
    base VV000H, EIP 0; the hidden limit and attributes kept)."""
    seg = {r: (0, 0x93) for r in SREGS}
    seg['cs'] = (0xF000, 0x9B)
    shape = {r: (0, 0xFFFF) for r in SREGS}
    shape['cs'] = (0xFFFF0000, 0xFFFF)
    s = dict(rip=0xFFF0, length=0, rsp=0, rflags=2, cpl=0, cs=0xF000, ss=0, cr0=cd_nw & 0x60000000 | 0x10, cr2=0, cr4=0,
             efer=0, gdtr=(0, 0xFFFF), idtr=(0, 0xFFFF), ldtr=None, tr=None, seg=seg, shape=shape, fs_base=0, gs_base=0,
             memory={} if memory is None else memory)
    if sipi is not None:
        s.update(cs=sipi << 8, rip=0)
        seg['cs'] = (sipi << 8, 0x9B)
        shape['cs'] = (sipi << 12, 0xFFFF)
    return s

def system_descriptor(base, lim, kind, dpl=0, present=True, upper_type=0):
    """A 16-byte LDT (kind 2) or 64-bit TSS (kind 9 available, 0xB busy) descriptor as bytes."""
    lo = (lim & 0xFFFF | (base & 0xFFFFFF) << 16 | kind << 40 | dpl << 45 | int(present) << 47
          | (lim >> 16 & 0xF) << 48 | (base >> 24 & 0xFF) << 56)
    hi = base >> 32 & 0xFFFFFFFF | upper_type << 40
    return lo.to_bytes(8, 'little') + hi.to_bytes(8, 'little')


def data(dpl=0, writable=True, present=True, accessed=True, base=0):
    """A flat 8-byte data descriptor (G = 1, D/B = 1) with a 32-bit `base`."""
    access = (2 if writable else 0) | int(accessed) | 0x10 | dpl << 5 | int(present) << 7
    return 0xFFFF | (base & 0xFFFFFF) << 16 | access << 40 | 0xCF << 48 | (base >> 24 & 0xFF) << 56


def code(dpl=0, l=1, d=0, readable=True, conforming=False, present=True, accessed=True):
    access = 8 | (2 if readable else 0) | (4 if conforming else 0) | int(accessed) | 0x10 | dpl << 5 | int(present) << 7
    return 0xFFFF | access << 40 | (0xF | (0x8 | l << 1 | d << 2) << 4) << 48
