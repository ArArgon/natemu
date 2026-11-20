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


def _run(state, body):
    s = copy.deepcopy(state)
    bus = _Bus(s)
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


# ---- builders for tests and runners -----------------------------------------------------------------------------

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
