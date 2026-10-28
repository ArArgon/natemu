"""Per-instruction RFLAGS rules (Intel SDM Vol. 2 "Flags Affected" / "Operation").

A program is described by its instruction annotations; the mask of RFLAGS bits
that may be compared at the end is derived from these rules, never per program.

Meaningful under QEMU Linux-user: the six arithmetic flags (CF PF AF ZF SF OF)
and DF.  Reserved bit 1 reads 1 and IF reads 1 (user-mode POPF cannot clear it);
both are asserted as the fixed pattern 0x202 rather than compared.  TF, NT, RF,
VM, AC, VIF, VIP, ID and IOPL are not representable here and never set.
"""
CF, PF, AF, ZF, SF, DF, OF = 0x1, 0x4, 0x10, 0x40, 0x80, 0x400, 0x800
ARITH = CF | PF | AF | ZF | SF | OF          # 0x8d5: the tracked flags
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
    if reads & undefined:
        raise ValueError(f"{op} {insn.get('cc', '')} reads undefined flag(s) {reads & undefined:#x}")
    if op in NO_FLAGS: return undefined
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
