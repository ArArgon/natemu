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

Scope: IA-32e mode, CS a 64-bit code segment (L = 1, D = 0). Compatibility mode is not built (natemu's owner decision 3):
a far transfer or return whose target CS is not a 64-bit code segment (L = 0, or L = D = 1) is `refused`, after the
descriptor checks the SDM makes before the mode matters; so is a 64-bit call gate (not built) and an IRET that would set
TF (single-step: natemu's S4). Paging is delivery_model's abstraction (`memory`: page number -> {'data', 'write',
'user'}); descriptor-table and TSS-descriptor accesses are implicit supervisor accesses (CPL 0 rights, U/S = 0 in a #PF),
operand and stack accesses are made at the CPL.

State dict (delivery_model's keys plus segments): rip (the instruction), length (its length, for CALL's return
address), rsp, rflags, cpl, cs, ss (selectors), cr0, gdtr = (base, limit), idtr, ldtr = (selector, base, limit) or None
(null), tr = (selector, base, limit) or None, seg = {'es'|'ds'|'fs'|'gs'|'ss'|'cs': (selector, attributes)} where
attributes are descriptor bits 47:40 | bits 55:52 << 12 (natemu's `Segment::attributes`; 0 = null), fs_base, gs_base,
memory. Every operation returns {'outcome': 'done' | 'fault' | 'refused', 'state', 'diff': {address: bytes},
'fault': (vector, error, cr2) or None, 'reason': text for 'refused', 'permitted': frozenset of (outcome, fault)}; a fault
leaves the state as it was (the SDM lets a failed segment load set the accessed bit; the model commits no write: B1).
`outcome`/`fault` are the model's single choice; `permitted` is every result the SDM allows (it differs from
{(outcome, fault)} only at B2, B8 and B9), and a checker must accept any member.

AMBIGUITIES (the model's choice; `permitted` widens B2, B8 and B9, and runners build no case on the others):
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
        return dict(outcome='fault', state=copy.deepcopy(state), diff={}, fault=fault, reason=None,
                    permitted=frozenset({('fault', fault)}) | getattr(f, 'permitted', frozenset()))
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
    s['seg'][sreg] = (sel, attributes(raw) | 1)
    if sreg in ('fs', 'gs'):
        s[sreg + '_base'] = base32(raw)


def load(state, sreg, sel):
    """MOV sreg, r/m16 with the selector already read (MOV to CS is #UD: not a case here)."""
    return _run(state, lambda s, bus: _load(s, bus, sreg, sel))


def _refuse_compatibility(raw):
    _, _, _, _, l, d = fields(raw)
    if not l or d:
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
            raise Refused('call gate')
        if not st or not t & 8 or ((dpl > cpl) if t & 4 else (sel & 3 > cpl or dpl != cpl)) or (l and d):
            raise _gp(sel)
        if not p:
            raise _np(sel)
        _refuse_compatibility(raw)
        rsp = s['rsp'] - (2 * width if kind == 'call' else 0)
        if kind == 'call' and not (canonical(rsp) and canonical(s['rsp'] - 1)):
            raise Fault(SS, 0)
        if not canonical(rip):
            raise Fault(GP, 0)
        _accessed(bus, daddr, raw)
        if kind == 'call':
            frame = (s['rip'] + s['length']).to_bytes(8, 'little')[:width] + s['cs'].to_bytes(8, 'little')[:width]
            bus.write(rsp, frame, cpl)
            s['rsp'] = rsp
        s['cs'] = sel & ~3 | cpl
        s['seg']['cs'] = (s['cs'], attributes(raw) | 1)
        s['rip'] = rip
    return _run(state, body)


def ret(state, width=8, release=0, iret=False):
    """RET far (imm16 `release`) or IRET with `width`-byte pops."""
    def body(s, bus):
        cpl = s['cpl']
        if iret and s['rflags'] & NT:
            raise Fault(GP, 0)
        n = 5 if iret else 2
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
        _refuse_compatibility(raw)
        if iret and not canonical(rip):
            raise Fault(GP, 0)                                     # B4: IRETQ checks RIP before SS
        outer = rpl > cpl
        stack = None
        if iret or outer:
            if iret:
                rsp, ss_sel = words[3], words[4] & 0xFFFF
            else:
                tail = _operand(s, bus, s['rsp'] + 2 * width + release, 2 * width, stack=True)
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
        if not iret and not canonical(rip):
            raise Fault(GP, 0)                                     # B4: RET checks RIP after SS
        if iret:
            mask = IRET_ALWAYS | (IF if cpl <= (s['rflags'] & IOPL) >> 12 else 0) | (IOPL | VIF | VIP if cpl == 0 else 0)
            flags = (s['rflags'] & ~mask | words[2] & mask) & ~VM | 2
            if flags & TF:
                raise Refused('TF (single-step)')
            s['rflags'] = flags & ~RF                              # natemu drops RF (no breakpoints before S4)
        _accessed(bus, caddr, raw)
        if stack and stack[2]:
            _accessed(bus, *stack[2])
        s.update(rip=rip, cs=sel, cpl=rpl)
        s['seg']['cs'] = (sel, attributes(raw) | 1)
        if stack:
            rsp, ss_sel, ss = stack
            s['rsp'] = rsp + (0 if iret else release)
            s['ss'] = ss_sel
            s['seg']['ss'] = (ss_sel, attributes(ss[1]) | 1 if ss else 0)
        else:
            s['rsp'] += 2 * width + release
        if outer:
            for r in ('es', 'ds', 'fs', 'gs'):                     # B5
                sel_r, a = s['seg'][r]
                if not a & 0x80 or ((not a & 8 or not a & 4) and (a >> 5 & 3) < rpl):
                    s['seg'][r] = (0, 0)
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
