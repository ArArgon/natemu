"""Independent reference for SYSCALL/SYSRET, SWAPGS, RD/WR{FS,GS}BASE, LAR/LSL/VERR/VERW, RDRAND/RDSEED/RDPID and the
cache-control instructions in 64-bit mode, written from the Intel SDM, not from natemu's code. It reuses
`segment_model`'s state, `_run`, descriptor lookup and field helpers, and `delivery_model`'s paged bus and faults.

Sources (titles; Vol. 2 is not in the repo snapshot, so pseudocode is a recalled transcription): Vol. 2B "SYSCALL—Fast
System Call", "SYSRET—Return From Fast System Call", "SWAPGS—Swap GS Base Register", "RDFSBASE/RDGSBASE—Read FS/GS
Segment Base", "WRFSBASE/WRGSBASE—Write FS/GS Segment Base", "RDRAND—Read Random Number", "RDSEED—Read Random SEED",
"RDPID—Read Processor ID", "VERR/VERW—Verify a Segment for Reading or Writing", "WBINVD—Write Back and Invalidate Cache",
"WBNOINVD—Write Back and Do Not Invalidate Cache"; Vol. 2A "LAR—Load Access Rights Byte" (table "Segment and Gate Types":
valid in IA-32e mode), "LSL—Load Segment Limit" (same table), "INVD—Invalidate Internal Caches", "CLFLUSH—Flush Cache
Line", "CLFLUSHOPT", "CLWB"; Vol. 3A "System Descriptor Types", "Segment Descriptors"; Vol. 3B "Debug Exceptions" (RF).

Scope: 64-bit mode (EFER.LMA = 1, CS.L = 1), CPL 0 or 3, GenuineIntel profile (`cpu_profile.json`: RDRAND, FSGSBASE,
CLFSH advertised; RDSEED, RDPID, CLFLUSHOPT, CLWB, WBNOINVD, SMAP clear). Feature #UDs are read from the profile.
LOCK/REP-prefix #UDs and VM exits are not modelled.

State: segment_model's keys, plus gpr = {'rax'..'r15': int} (only those used), efer, cr4, msrs = {'star', 'lstar',
'fmask', 'kernel_gs_base'}. RFLAGS bit 1 is always set. `width` is the operand size in bytes (2, 4 or 8); a 2-byte
register write keeps bits 63:16, a 4-byte one zero-extends. Results are segment_model's ({'outcome', 'state', 'diff',
'fault', 'reason', 'permitted'}; a fault or refusal leaves the state unchanged) plus 'alternatives': a list of other
complete final states the SDM allows for a 'done' outcome (empty unless C1, C6 or C9 applies); a checker must accept
`state` or any member.

AMBIGUITIES (the model's choice; `permitted`/`alternatives` widen C1, C6, C9, C11; runners build no case on C2, C8, C10):
C1  SYSCALL's R11 image of RF: pseudocode "R11 := RFLAGS" (model: RF as it was); QEMU's helper_syscall clears RF in R11.
    alternatives: R11 with RF = 0. The new RFLAGS has RF = 0 (RF is cleared when an instruction completes, Vol. 3B).
C2  SYSCALL SS selector = STAR[47:32] + 8, RPL bits not masked (SDM pseudocode; AMD APM and KVM agree); QEMU masks it to
    RPL 0. Not widened (the SDM is explicit), but QEMU differs: compare against QEMU only with STAR[33:32] = 0.
C3  Single-step (not built): SYSCALL with TF = 1 on entry, and SYSRET with TF = 1 before or after, are 'refused' after
    every fault check (whether SYSCALL traps when FMASK clears TF is not stated in the SYSCALL page).
C4  SYSRET to 64-bit mode with non-canonical RCX: #GP(0) at CPL 0 before any change (Intel). AMD completes SYSRET and
    faults at CPL 3 on the fetch; AMD also leaves stale SS attributes. Neither widened (Intel profile).
C5  SYSRET whose new RFLAGS.AC = 1 with CR0.AM = 1 completes (alignment checking, built by X2, then applies to the
    accesses at CPL 3 that follow: the next misaligned one is #AC(0)). Not set-valued: the SDM gives SYSRET no AC check.
C6  LAR's "00FxFF00H" mask: bits 19:16 ("x", limit 19:16) are not defined. Model: 0 (QEMU); alternatives: the 15 others.
C7  LAR/LSL system types valid in IA-32e mode (SDM table): LAR 2 (LDT), 9/B (64-bit TSS), C (call gate); LSL 2, 9, B.
    16-bit TSS (1, 3), 16-bit call gate (4), task gate (5), interrupt/trap gates and reserved types give ZF = 0. QEMU
    accepts 1, 3 (and 4, 5 for LAR) in every mode: not widened.
C8  LAR/LSL read 8 bytes of a system descriptor (limit check on index + 7); the upper 8 bytes are neither read nor
    type-checked. Undecided, not settled: the SDM text at hand does not say whether IA-32e LAR/LSL check the upper half
    (its type field, its limit, its page), so this choice is not a finding and nothing tests it. Runners build no case
    whose upper half is beyond the table limit, on a faulting page, or has a nonzero type field.
C9  P is not checked by LAR, LSL, VERR or VERW (pseudocode; LAR returns P). VERR/VERW's description equates the check
    with a DS load (which would #NP): for a not-present segment that verifies, alternatives: ZF = 0.
C10 A null selector (index 0, TI 0) gives ZF = 0 (QEMU tests it first; the pseudocode would read GDT[0]); runners build
    no case with a valid descriptor in GDT[0]. TI = 1 with a null LDTR, or an offset beyond the limit: ZF = 0, no fault.
    A non-canonical table address: ZF = 0 here; runners build no case on it.
C11 WBNOINVD (F3 0F 09) is not advertised (CPUID.80000008H:EBX[9] = 0). Its exception list (recalled) has no
    CPUID #UD line; AMD's APM and Linux's wbnoinvd() fallback note that older processors ignore the F3 prefix and
    execute WBINVD. Model: WBINVD's behaviour; permitted also #UD.
C12 INVD's #UD under active SGX memory protections does not apply (SGX clear).
"""
import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models.delivery_model import Fault, canonical, GP, UD, TF, RF
from models.segment_model import Refused, _run as _segment_run, _operand, descriptor, fields, limit
from models import cpu_profile

PROFILE = cpu_profile.load()
CF, PF_FLAG, AF, ZF, SF, OF = 1, 1 << 2, 1 << 4, 1 << 6, 1 << 7, 1 << 11
AC = 1 << 18
SCE, LMA = 1, 1 << 10
CR4_FSGSBASE, CR0_AM = 1 << 16, 1 << 18
SYSRET_FLAGS = 0x3C7FD7
LAR_SYSTEM, LSL_SYSTEM = {2, 9, 0xB, 0xC}, {2, 9, 0xB}                   # C7


def _run(state, body):
    """segment_model's _run; `body(s, bus, alts)` may append alternative final states (C1, C6, C9)."""
    alts = []
    r = _segment_run(state, lambda s, bus: body(s, bus, alts))
    r['alternatives'] = alts if r['outcome'] == 'done' else []
    return r


def _ud_unless(feature):
    if not PROFILE.feature(feature):
        raise Fault(UD)


def _mode64(s):
    if not (s['efer'] & LMA and s['seg']['cs'][1] >> 13 & 1):
        raise Fault(UD)


def _set(s, reg, value, width):
    old = s['gpr'].get(reg, 0)
    s['gpr'][reg] = old & ~0xFFFF | value & 0xFFFF if width == 2 else value & (1 << 8 * width) - 1


def _zf(s, z):
    s['rflags'] = s['rflags'] & ~ZF | (ZF if z else 0)


# ---- SYSCALL / SYSRET / SWAPGS ----------------------------------------------------------------------------------

def syscall(state):
    """Vol. 2B SYSCALL: #UD unless 64-bit mode and EFER.SCE; RCX := next RIP, R11 := RFLAGS, RFLAGS &= ~FMASK,
    RIP := LSTAR, flat CS (STAR[47:32] & FFFCH) / SS (STAR[47:32] + 8), CPL := 0.
    Reads rip, length, rflags, efer, msrs, seg; writes gpr rcx/r11, rip, rflags, cs, ss, seg cs/ss, cpl."""
    def body(s, bus, alts):
        _mode64(s)
        if not s['efer'] & SCE:
            raise Fault(UD)
        flags = s['rflags']
        if flags & TF:
            raise Refused('TF (single-step)')                       # C3
        sel = s['msrs']['star'] >> 32 & 0xFFFF
        s['gpr']['rcx'] = s['rip'] + s['length']
        s['gpr']['r11'] = flags                                     # C1
        s['rflags'] = flags & ~(s['msrs']['fmask'] & 0xFFFFFFFF) & ~RF | 2
        s['rip'] = s['msrs']['lstar']
        s['cs'], s['ss'], s['cpl'] = sel & 0xFFFC, sel + 8 & 0xFFFF, 0   # C2
        s['seg']['cs'] = (s['cs'], 0xA09B)                          # type B, S, DPL 0, P; G, L
        s['seg']['ss'] = (s['ss'], 0xC093)                          # type 3, S, DPL 0, P; G, B
        if flags & RF:
            a = copy.deepcopy(s)
            a['gpr']['r11'] = flags & ~RF
            alts.append(a)
    return _run(state, body)


def sysret(state, width=8):
    """Vol. 2B SYSRET: #UD unless 64-bit mode and EFER.SCE; #GP(0) at CPL != 0; 64-bit: #GP(0) for non-canonical RCX,
    RIP := RCX, RFLAGS := (R11 & 3C7FD7H) | 2, CS := (STAR[63:48] + 16) | 3, SS := (STAR[63:48] + 8) | 3, CPL := 3.
    width 4 (to compatibility mode) is 'refused' after the CPL check. Reads gpr rcx/r11, rflags, efer, msrs, cr0, cpl;
    writes rip, rflags, cs, ss, seg cs/ss, cpl."""
    def body(s, bus, alts):
        _mode64(s)
        if not s['efer'] & SCE:
            raise Fault(UD)
        if s['cpl']:
            raise Fault(GP, 0)
        if width != 8:
            raise Refused('SYSRET to compatibility mode')
        rcx = s['gpr']['rcx']
        if not canonical(rcx):
            raise Fault(GP, 0)                                      # C4
        flags = s['gpr']['r11'] & SYSRET_FLAGS | 2
        if (s['rflags'] | flags) & TF:
            raise Refused('TF (single-step)')                       # C3
        base = s['msrs']['star'] >> 48 & 0xFFFF
        s['rip'], s['rflags'], s['cpl'] = rcx, flags, 3
        s['cs'], s['ss'] = (base + 16 | 3) & 0xFFFF, (base + 8 | 3) & 0xFFFF
        s['seg']['cs'] = (s['cs'], 0xA0FB)                          # type B, S, DPL 3, P; G, L
        s['seg']['ss'] = (s['ss'], 0xC0F3)                          # type 3, S, DPL 3, P; G, B
    return _run(state, body)


def swapgs(state):
    """Vol. 2B SWAPGS: #UD outside 64-bit mode, #GP(0) at CPL != 0; exchange gs_base and IA32_KERNEL_GS_BASE.
    Reads/writes gs_base, msrs['kernel_gs_base']."""
    def body(s, bus, alts):
        _mode64(s)
        if s['cpl']:
            raise Fault(GP, 0)
        s['gs_base'], s['msrs']['kernel_gs_base'] = s['msrs']['kernel_gs_base'], s['gs_base']
    return _run(state, body)


def fsgsbase(state, op, reg, width=8):
    """Vol. 2B RDFSBASE/RDGSBASE, WRFSBASE/WRGSBASE: #UD unless 64-bit mode and CR4.FSGSBASE; any CPL; RD width 4 writes
    base[31:0] zero-extended; WR width 4 writes the zero-extended low 32 bits; WR width 8 #GP(0) when non-canonical.
    Reads cr4, fs_base/gs_base or gpr[reg]; writes the other."""
    def body(s, bus, alts):
        _mode64(s)
        _ud_unless('7.0.ebx.FSGSBASE')
        if not s['cr4'] & CR4_FSGSBASE:
            raise Fault(UD)
        key = op[2:4] + '_base'
        if op.startswith('rd'):
            _set(s, reg, s[key], width)
            return
        value = s['gpr'][reg] & (1 << 8 * width) - 1
        if not canonical(value):
            raise Fault(GP, 0)
        s[key] = value
    assert op in ('rdfsbase', 'rdgsbase', 'wrfsbase', 'wrgsbase') and width in (4, 8)
    return _run(state, body)


# ---- LAR / LSL / VERR / VERW ------------------------------------------------------------------------------------

def _lookup(s, bus, sel):
    """The 8-byte descriptor `sel` names, or None where these instructions clear ZF without a fault (C8, C10)."""
    if sel & 0xFFFC == 0:
        return None
    try:
        return descriptor(s, bus, sel)[1]                           # #PF: implicit supervisor read
    except Fault as f:
        if f.vector != GP:
            raise
        return None


def _accessible(s, sel, raw, system_types):
    st, t, dpl, _, _, _ = fields(raw)
    if not st and t not in system_types:
        return False
    conforming = st and t & 0xC == 0xC
    return conforming or (s['cpl'] <= dpl and sel & 3 <= dpl)


def lar(state, sel, reg, width=4):
    """Vol. 2A LAR: ZF := 1 and reg := descriptor dword 1 & 00FxFF00H (width 4/8, zero-extended) or & FF00H (width 2,
    bits 63:16 kept) for an accessible descriptor of a valid type (C7); else ZF := 0, reg unchanged. Reads gdtr, ldtr,
    cpl, memory; writes rflags.ZF, gpr[reg]."""
    def body(s, bus, alts):
        raw = _lookup(s, bus, sel)
        ok = raw is not None and _accessible(s, sel, raw, LAR_SYSTEM)
        _zf(s, ok)
        if ok:
            hi = raw >> 32
            _set(s, reg, hi & (0xFF00 if width == 2 else 0x00F0FF00), width)
            if width != 2:
                for x in range(1, 16):                              # C6
                    a = copy.deepcopy(s)
                    _set(a, reg, hi & 0x00F0FF00 | x << 16, width)
                    alts.append(a)
    return _run(state, body)


def lsl(state, sel, reg, width=4):
    """Vol. 2A LSL: ZF := 1 and reg := the byte-granular limit (G scales: limit << 12 | FFFH), low 16 bits for width 2
    (63:16 kept), zero-extended for 4/8; else ZF := 0, reg unchanged. Reads/writes as LAR."""
    def body(s, bus, alts):
        raw = _lookup(s, bus, sel)
        ok = raw is not None and _accessible(s, sel, raw, LSL_SYSTEM)
        _zf(s, ok)
        if ok:
            _set(s, reg, limit(raw), width)
    return _run(state, body)


def _verify(state, sel, write):
    def body(s, bus, alts):
        raw = _lookup(s, bus, sel)
        ok = raw is not None and _accessible(s, sel, raw, ())       # system descriptors never verify
        if ok:
            _, t, _, p, _, _ = fields(raw)
            ok = (t & 0xA) == 2 if write else (not t & 8 or t & 2)  # writable data / data or readable code
        _zf(s, ok)
        if ok and not p:
            a = copy.deepcopy(s)
            _zf(a, False)
            alts.append(a)                                          # C9
    return _run(state, body)


def verr(state, sel):
    """Vol. 2B VERR: ZF := 1 for an accessible data segment or readable code segment, else 0. Writes rflags.ZF."""
    return _verify(state, sel, False)


def verw(state, sel):
    """Vol. 2B VERW: ZF := 1 for an accessible writable data segment, else 0. Writes rflags.ZF."""
    return _verify(state, sel, True)


# ---- random numbers, processor ID -------------------------------------------------------------------------------

def rdrand(state, reg, width=8, value=None):
    """Vol. 2B RDRAND: reg := `value`, CF := 1; `value` None = no random data: reg := 0, CF := 0; OF SF ZF AF PF := 0.
    Writes gpr[reg], rflags."""
    def body(s, bus, alts):
        _ud_unless('1.ecx.RDRAND')
        _set(s, reg, value or 0, width)
        s['rflags'] = s['rflags'] & ~(CF | PF_FLAG | AF | ZF | SF | OF) | (CF if value is not None else 0)
    return _run(state, body)


def rdseed(state, reg, width=8, value=None):
    """Vol. 2B RDSEED: #UD (CPUID.07H:EBX.RDSEED = 0 in the profile)."""
    return _run(state, lambda s, bus, alts: _ud_unless('7.0.ebx.RDSEED'))


def rdpid(state, reg):
    """Vol. 2B RDPID: #UD (CPUID.07H:ECX.RDPID = 0 in the profile)."""
    return _run(state, lambda s, bus, alts: _ud_unless('7.0.ecx.RDPID'))


# ---- cache control ----------------------------------------------------------------------------------------------

def cache(state, op):
    """Vol. 2B WBINVD, Vol. 2A INVD, Vol. 2B WBNOINVD (C11): #GP(0) at CPL != 0, otherwise no state change."""
    assert op in ('wbinvd', 'invd', 'wbnoinvd')

    def body(s, bus, alts):
        if s['cpl']:
            raise Fault(GP, 0)
    r = _run(state, body)
    if op == 'wbnoinvd':
        r['permitted'] |= {('fault', (UD, None, 0))}                # C11
    return r


def clflush(state, addr, stack=False):
    """Vol. 2A CLFLUSH: faults of a byte load at the CPL (#PF with a read error code; #GP(0), or #SS(0) for an SS
    reference, when non-canonical); no change otherwise. Reads cpl, memory."""
    def body(s, bus, alts):
        _ud_unless('1.edx.CLFSH')
        _operand(s, bus, addr, 1, stack=stack)
    return _run(state, body)


def clflushopt(state, addr, stack=False):
    """Vol. 2A CLFLUSHOPT: #UD (CPUID.07H:EBX.CLFLUSHOPT = 0 in the profile)."""
    return _run(state, lambda s, bus, alts: _ud_unless('7.0.ebx.CLFLUSHOPT'))


def clwb(state, addr, stack=False):
    """Vol. 2A CLWB: #UD (CPUID.07H:EBX.CLWB = 0 in the profile)."""
    return _run(state, lambda s, bus, alts: _ud_unless('7.0.ebx.CLWB'))
