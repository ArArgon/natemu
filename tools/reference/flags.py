"""Per-instruction RFLAGS rules (Intel SDM Vol. 2 "Flags Affected" / "Operation").

A program is described by its instruction annotations; the mask of RFLAGS bits
that may be compared at the end is derived from these rules, never per program.

Meaningful under QEMU Linux-user: the six arithmetic flags (CF PF AF ZF SF OF)
and DF.  Reserved bit 1 reads 1 and IF reads 1 (user-mode POPF cannot clear it);
both are asserted as the fixed pattern 0x202 rather than compared.  TF, NT, RF,
VM, AC, VIF, VIP, ID and IOPL are not representable here and never set.
"""
CF, PF, AF, ZF, SF, DF, OF = 0x1, 0x4, 0x10, 0x40, 0x80, 0x400, 0x800
ARITH = CF | PF | AF | ZF | SF | OF          # 0x8d5: the arithmetic flags
TRACKED = ARITH | DF                          # compared (DF: cld/std since I1; initial DF is 0)
FIXED = 0x202                                 # bit1 and IF, constant in Linux-user
ALL6 = ARITH

# Flags read by each condition code (setcc / cmovcc).
CC_READS = {'o': OF, 'no': OF, 'b': CF, 'ae': CF, 'e': ZF, 'ne': ZF, 'be': CF | ZF, 'a': CF | ZF,
            's': SF, 'ns': SF, 'p': PF, 'np': PF, 'l': SF | OF, 'ge': SF | OF,
            'le': ZF | SF | OF, 'g': ZF | SF | OF}

NO_FLAGS = {'mov', 'movzx', 'movsx', 'lea', 'push', 'pop', 'not', 'setcc', 'cmovcc', 'xchg', 'bswap',
            'cbw', 'cwde', 'cdqe', 'cwd', 'cdq', 'cqo',
            'jcc', 'jmp', 'call', 'ret', 'loop', 'loope', 'loopne', 'jrcxz'}      # control flow: reads flags (below), writes none
ADD_LIKE = {'add', 'adc', 'sub', 'sbb', 'cmp', 'neg'}          # all six defined
LOGIC = {'and', 'or', 'xor', 'test'}                           # CF=OF=0; AF undefined
SHIFTS = {'shl', 'shr', 'sar'}
ROTATES = {'rol', 'ror'}
MULS = {'mul', 'imul'}                                          # SF ZF AF PF undefined
DIVS = {'div', 'idiv'}                                          # all six undefined
BITTEST = {'bt', 'bts', 'btr', 'btc'}                           # CF = selected bit; OF SF AF PF undefined; ZF unaffected
BITSCAN = {'bsf', 'bsr'}                                        # ZF = (src == 0); CF OF SF AF PF undefined
# I1 forms (SDM Vol. 2 "Flags Affected").
NO_FLAGS_I1 = {'cld', 'std', 'leave', 'enter', 'mulx', 'rorx', 'sarx', 'shlx', 'shrx', 'prefetcht0', 'prefetcht1', 'prefetcht2',
               'prefetchnta', 'movnti'}                         # arithmetic flags untouched (cld/std: DF only)
ZCNT = {'tzcnt', 'lzcnt'}                                       # CF = (src == 0), ZF = (result == 0); OF SF PF AF undefined
BMI_LOGIC = {'andn': ZF | SF, 'bextr': ZF, 'blsi': ZF | SF, 'blsmsk': SF, 'blsr': ZF | SF, 'bzhi': ZF | SF}
#   defined: ZF/SF as given by the result (blsmsk: ZF cleared), CF (andn, bextr: cleared; blsi: src != 0; blsmsk, blsr:
#   src == 0; bzhi: index > size-1), OF cleared; AF and PF undefined, and SF too where not listed.
# I2 string instructions. movs/stos/lods write no flags; cmps/scas define all six like cmp (each element), so a REP form
# with an initial count of 0 (annotation `count`) leaves them untouched.
STRING_MOVES = {'movs', 'stos', 'lods'}
STRING_COMPARES = {'cmps', 'scas'}


def step(insn, undefined):
    """Return the undefined-flag set after one instruction.

    insn: dict with 'op', 'width' (bits) and, for shifts/rotates, 'count' (the
    count value in effect, before masking) and, for setcc/cmovcc, 'cc'.
    Raises ValueError if the instruction consumes a currently undefined flag.
    """
    op, width = insn['op'], insn.get('width', 64)
    reads = 0
    if op in ('adc', 'sbb'): reads = CF
    elif op in ('setcc', 'cmovcc', 'jcc'): reads = CC_READS[insn['cc']]
    elif op in ('loope', 'loopne'): reads = ZF
    elif op == 'cmc': reads = CF
    elif op == 'lahf': reads = SF | ZF | AF | PF | CF               # copied into AH
    elif op == 'pushf': reads = ARITH                                # the pushed image holds every flag
    if reads & undefined:
        raise ValueError(f"{op} {insn.get('cc', '')} reads undefined flag(s) {reads & undefined:#x}")
    # I5a: SSE forms (annotation `sse`) write no arithmetic flag but COMISS/UCOMISS/COMISD/UCOMISD and PTEST, which define
    # all six (ZF PF CF, or ZF CF, by the operands; the rest cleared).
    if insn.get('sse'): return undefined & ~ALL6 if op in ('comiss', 'ucomiss', 'comisd', 'ucomisd', 'ptest') else undefined
    if op in NO_FLAGS | NO_FLAGS_I1 | STRING_MOVES | {'lahf', 'pushf'}: return undefined
    if op in STRING_COMPARES: return undefined if insn.get('count') == 0 else undefined & ~ALL6
    if op in ('clc', 'stc', 'cmc'): return undefined & ~CF
    if op == 'sahf': return undefined & ~(SF | ZF | AF | PF | CF)    # OF unaffected
    if op in ('cmpxchg', 'xadd', 'popcnt'): return undefined & ~ALL6   # cmp / add; popcnt: ZF by source, the rest cleared
    if op in ('cmpxchg8b', 'cmpxchg16b'): return undefined & ~ZF       # I3: ZF = equal; CF PF AF SF OF unaffected
    # I3: a LOCK prefix (annotation `lock`) changes no flag rule: each locked form has its unlocked form's flags.
    if op in ZCNT: return (undefined & ~ALL6) | OF | SF | PF | AF
    if op in BMI_LOGIC:
        # blsi/blsmsk/blsr: CF is not compared. The pinned QEMU (7.2) leaves cc_src stale for them (BLSI src = 0 gives
        # CF = 1; fixed upstream in 8.0, "target/i386: Fix C flag for BLSI, BLSMSK, BLSR"); FEX sets CF per the SDM.
        stale = CF if op.startswith('bls') else 0
        return (undefined & ~ALL6) | stale | (AF | PF | (SF & ~BMI_LOGIC[op] if op != 'blsmsk' else 0))
    if op in ('shld', 'shrd'):
        if width not in (32, 64): raise ValueError(f'{op}: 16-bit forms are not generated (result undefined above 16)')
        count = insn['count'] & (0x3f if width == 64 else 0x1f)
        if count == 0: return undefined                  # flags and destination untouched
        return (undefined & ~ALL6) | AF | (0 if count == 1 else OF)
    if op in ADD_LIKE: return undefined & ~ALL6
    if op in ('inc', 'dec'): return undefined & ~(ALL6 & ~CF)
    if op in LOGIC: return (undefined & ~ALL6) | AF
    if op in MULS: return (undefined & ~ALL6) | SF | ZF | AF | PF
    if op in DIVS: return ALL6
    if op in BITTEST: return (undefined & ~CF) | OF | SF | AF | PF
    if op in BITSCAN:
        # The destination is undefined for a zero source (Intel; AMD leaves it unchanged), so the annotation must
        # assert the source is nonzero; the generator guarantees it by construction.
        if not insn.get('src_nonzero'): raise ValueError(f'{op} needs src_nonzero (destination undefined for a zero source)')
        return (undefined & ~ZF) | CF | OF | SF | AF | PF
    if op in SHIFTS | ROTATES:
        count = insn['count'] & (0x3f if width == 64 else 0x1f)
        if count == 0: return undefined                 # flags untouched
        if op in ROTATES:                                # only CF, OF written
            undef_new = 0 if count == 1 else OF         # OF defined only for count 1
            return (undefined & ~(CF | OF)) | undef_new
        undef_new = AF | (0 if count == 1 else OF)       # AF always; OF only defined for count 1
        if op != 'sar' and count >= width: undef_new |= CF   # SDM: CF undefined for SHL/SHR when count >= operand size (SAR: sign bit)
        return (undefined & ~ALL6) | undef_new
    raise ValueError(f'no flag rule for {op}')


def analyze(insns):
    """Undefined RFLAGS bits (within ARITH) after the whole sequence."""
    undefined = 0
    for insn in insns: undefined = step(insn, undefined)
    return undefined
