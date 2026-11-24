"""The role of each word of a served code object (docs/restructure/el0-sampler-plan.md chunk 1): what a sampled word is for,
from the object's own records first (crates/abi/src/code_object.rs: its sites, PC table and relocations), patterns of the A64
encodings fexc emits under profile slice1 for the rest (the anatomy of docs/restructure/guest-code-review-20261114.md
section 2). No disassembler: the classes come from the encodings.

Roles, by who emits them (`GROUPS`):
- FEX's body: `fex_alu`, `fex_mem` (a recorded access site), `fex_simd`, `fex_const` (movz/movk), `fex_state` (a load or
  store that is no access site: the guest-state page through x28), `fex_branch` (a branch that reaches no check stub),
  `fex_fpctl` (FPSR and FPCR moves), `fex_other` (nop, barriers, other system words such as RDTSC's counter read); and its
  flag words, `flags` (rmif, setf, cfinv, ccmp, cset, NZCV moves,
  a write of the PF or AF register).
- natemu's slice-1 words: the checks, each reached by the branch to its stub (`split_check` with its address word,
  `stack_check`, `align_check`, `guard_check` (a computed target's), `divide_check`, `fp_check` (SIMD, x87, pending, denormal
  and FMA checks), `x87_check` (the x87 stack tag, guessed stack and FLDCW tests), `segment_check`); the stubs themselves
  (`stub`: a `brk`, or a probing split stub's `adr` and `b`); `probe` (a sample in a shared probe, charged to its store's
  block); `load_move` (the move out of a load's temporary, slice 1's precise loads); `nzcv_save` (`mrs x25, nzcv`); `helper`
  (an `svc` to the resident's divide, x87 or string helpers); the exits (`exit_literal`: the guest-RIP move and `svc #LINK`,
  whose first word becomes the link `b` or its trampoline; `exit_computed`: `mov x0, xR; b <dispatcher>`); `entry` (a PIC
  block's full entry: its `and` at native offset -4 and its `str` in the code header's word 0, `Block::full_entry`; word 0
  runs only there).
- data: `literal` (a guest-RIP literal slot), `beyond` (past the code region: FEX's stripped tail, which
  admission does not place; a sample there is a fault of the report or the sampler, counted), `far` (the offset did not
  fit the record), `none` (no object for the block), `unknown` (no class: the corpus case requires none).
"""
import struct

CODE_MAGIC = b'NFXOBJ\0\x01'
HEADER = struct.Struct('<8s4IQ' + 'I' * 14 + '20s4BQ2B2sI')
SITE = struct.Struct('<IIIIiBBBBBBBBBBbBBBBBBBBB')
PC = struct.Struct('<IIB3x')
RELOC = struct.Struct('<IBBBBQ')
assert (HEADER.size, SITE.size, PC.size, RELOC.size) == (128, 40, 12, 16)

# code_object.rs `kind`
LOAD, STORE, LOAD_PAIR, STORE_PAIR, PUSH, POP, GUARD, SPLIT, PUSH_PAIR, POP_PAIR, OTHER, DIVIDE, SWAP, ATOMIC, ALIGN, SIMD, \
    X87, PENDING, FMA, DENORM, STACK, FLDCW, X87TAG, X87GUESS, X87MAP, SEGMENT = range(26)
STUB_KINDS = {SEGMENT, GUARD, SPLIT, DIVIDE, ALIGN, SIMD, X87, PENDING, DENORM, STACK, FLDCW, X87TAG, X87GUESS}
ACCESS_KINDS = {LOAD, STORE, LOAD_PAIR, STORE_PAIR, PUSH, POP, PUSH_PAIR, POP_PAIR, OTHER, SWAP, ATOMIC}
LOAD_KINDS = {LOAD, LOAD_PAIR, POP, POP_PAIR}
CHECK_ROLE = {SPLIT: 'split_check', STACK: 'stack_check', ALIGN: 'align_check', GUARD: 'guard_check', DIVIDE: 'divide_check',
              SIMD: 'fp_check', X87: 'fp_check', PENDING: 'fp_check', DENORM: 'fp_check', X87TAG: 'x87_check',
              X87GUESS: 'x87_check', FLDCW: 'x87_check', SEGMENT: 'segment_check'}
# The most words before a check's branch that belong to it (they write only x0-x2): the split check's address word with
# a constant's moves, the segment check's record walk, the x87 guess's loads.
CHECK_WORDS = {SPLIT: 6, STACK: 1, ALIGN: 1, GUARD: 1, DIVIDE: 2, SIMD: 3, X87: 7, PENDING: 4, DENORM: 1, X87TAG: 1,
               X87GUESS: 4, FLDCW: 3, SEGMENT: 12}
RELOC_GUEST_RIP_LITERAL, RELOC_NAMED_SYMBOL_LITERAL, RELOC_GUEST_RIP_MOVE, RELOC_DISPATCH_BRANCH, RELOC_PROBE_BRANCH = 1, 2, 3, 4, 5
SVC_LINK = 0xd400_0021
MRS_X25_NZCV = 0xd53b_4219
CLASS_FPR = 1

GROUPS = {}
for r in ('fex_alu', 'fex_mem', 'fex_simd', 'fex_const', 'fex_state', 'fex_branch', 'fex_fpctl', 'fex_other'): GROUPS[r] = 'fex_body'
GROUPS['flags'] = 'fex_flags'
for r in ('split_check', 'stack_check', 'align_check', 'guard_check', 'divide_check', 'fp_check', 'x87_check', 'segment_check',
          'stub', 'probe', 'load_move', 'nzcv_save', 'helper', 'exit_literal', 'exit_computed', 'entry'): GROUPS[r] = 'natemu'
for r in ('literal', 'beyond', 'far', 'none', 'unknown'): GROUPS[r] = 'other'
ROLES = list(GROUPS)


class Code:
    """A code object's header, code region and tables (code_object.rs), or ValueError when the bytes are not one."""

    def __init__(self, obj: bytes):
        if len(obj) < HEADER.size or obj[:8] != CODE_MAGIC: raise ValueError('not a code object')
        h = HEADER.unpack_from(obj, 0)
        (_, self.version, _, self.total_size, self.bitness, self.rip, self.guest_bytes, self.guest_insns, self.code_offset,
         self.code_size, self.body_offset, self.end_marker, self.region_end, _, so, sc, po, pc, ro, rc, sra, self.sra_count,
         *_) = h
        self.sra = list(sra[:self.sra_count])
        if self.code_offset + self.region_end > len(obj) or self.region_end > self.code_size or self.region_end % 4:
            raise ValueError('code region outside the object')
        self.code = obj[self.code_offset:self.code_offset + self.region_end]
        self.words = [w for (w,) in struct.iter_unpack('<I', self.code)]

        def table(s, off, n):
            if off + n * s.size > len(obj): raise ValueError('table outside the object')
            return [s.unpack_from(obj, off + i * s.size) for i in range(n)]
        self.sites = table(SITE, so, sc)
        self.pcs = sorted((n, g) for n, g, _ in table(PC, po, pc))
        self.relocs = table(RELOC, ro, rc)
        # The PF and AF registers: the two entries after the general registers (RAX..R15 in 64-bit code, EAX..EDI in 32-bit
        # code), when the object has them.
        gprs = {18: 16, 10: 8}.get(self.sra_count)
        self.flag_regs = set(self.sra[gprs:gprs + 2]) if gprs else set()

    def word_insns(self):
        """Per word, the guest offset of the instruction its native offset lies in (its PC-table entry; None before the first)."""
        out, k, g = [], 0, None
        for i in range(len(self.words)):
            while k < len(self.pcs) and self.pcs[k][0] <= 4 * i: g = self.pcs[k][1]; k += 1
            out.append(g)
        return out


# --- A64 encodings ---------------------------------------------------------------------------------------------------
def _branch_target(w, at):
    """The target of a direct branch word at `at` (b, bl, b.cond, cbz/cbnz, tbz/tbnz), or None."""
    if w & 0x7c00_0000 == 0x1400_0000:                       # b, bl
        return at + (((w & 0x3ff_ffff) ^ 0x200_0000) - 0x200_0000) * 4
    if w & 0xff00_0010 == 0x5400_0000:                       # b.cond
        return at + ((((w >> 5) & 0x7ffff) ^ 0x40000) - 0x40000) * 4
    if w & 0x7e00_0000 == 0x3400_0000:                       # cbz, cbnz
        return at + ((((w >> 5) & 0x7ffff) ^ 0x40000) - 0x40000) * 4
    if w & 0x7e00_0000 == 0x3600_0000:                       # tbz, tbnz
        return at + ((((w >> 5) & 0x3fff) ^ 0x2000) - 0x2000) * 4
    return None


def is_branch(w):
    return _branch_target(w, 0) is not None or w & 0xfe1f_fc1f == 0xd61f_0000      # br/blr/ret


def is_memory(w):
    return w & 0x0a00_0000 == 0x0800_0000                     # op0 x1x0: loads and stores


def dest(w):
    """The register a data-processing or load word writes (Rd/Rt), or None for words that write no general register the
    window test should see (stores, branches, system words but mrs, SIMD data processing)."""
    op0 = w >> 25 & 0xf
    if op0 in (0b1000, 0b1001): return w & 31                 # data processing, immediate
    if op0 in (0b0101, 0b1101):                               # data processing, register
        return w & 31
    if is_memory(w):
        if w & 0x3b00_0000 == 0x3900_0000 or w & 0x3b20_0000 == 0x3820_0000 or w & 0x3b20_0c00 == 0x3800_0000 \
                or w & 0x3b20_0400 == 0x3800_0400:            # single register, general: a load writes Rt
            if w >> 22 & 3 in (1, 2, 3) and not w & 0x0400_0000: return w & 31
        return None
    if w & 0xfff0_0000 == 0xd530_0000: return w & 31          # mrs
    return None


def is_mov_from(w, src):
    """`mov xD, xS` / `mov wD, wS` (orr with the zero register) reading `src`."""
    return w & 0x7fe0_ffe0 == 0x2a00_03e0 and (w >> 16) & 31 == src


def is_fp_move_from(w, src, fpr):
    """A move out of `src`: from a general register into a SIMD&FP one (fmov d/s, fmov v.d[1]) or, when `fpr`, between
    SIMD&FP registers (mov v.16b/8b, fmov d/s)."""
    n = (w >> 5) & 31
    if not fpr:
        return n == src and (w & 0xffff_fc00 in (0x9e67_0000, 0x1e27_0000, 0x9eaf_0000))
    if w & 0xbfe0_fc00 == 0x0ea0_1c00: return n == src and (w >> 16) & 31 == src     # orr v.t (mov)
    return n == src and w & 0xffff_fc00 in (0x1e60_4000, 0x1e20_4000)                # fmov d, d / s, s


def flag_word(w, flag_regs):
    if w & 0xffe0_7c10 == 0xba00_0400: return True           # rmif
    if w & 0xffff_bc1f == 0x3a00_080d: return True           # setf8, setf16
    if w in (0xd500_401f, 0xd500_403f, 0xd500_405f): return True   # cfinv, xaflag, axflag
    if w & 0xffff_ffe0 in (0xd53b_4200, 0xd51b_4200): return True  # mrs, msr nzcv
    if w & 0x3fe0_0010 == 0x3a40_0000: return True           # ccmp, ccmn
    if w & 0x7fe0_0c00 in (0x1a80_0400, 0x5a80_0000) and (w >> 5) & 31 == 31 and (w >> 16) & 31 == 31:
        return True                                          # cset, csetm
    d = dest(w)
    return d is not None and d in flag_regs and not is_memory(w)


def pattern(w, flag_regs):
    """A word's role by its encoding alone (the words no record or check window claimed)."""
    if w == MRS_X25_NZCV: return 'nzcv_save'
    if w & 0xffe0_001f == 0xd400_0001: return 'exit_literal' if w == SVC_LINK else 'helper'
    if w & 0xffe0_001f == 0xd420_0000: return 'stub'          # brk
    if flag_word(w, flag_regs): return 'flags'
    if w & 0xfff0_0000 in (0xd530_0000, 0xd510_0000) and (w >> 5) & 0x7fff in (0x5a21, 0x5a20):
        return 'fex_fpctl'                                   # mrs/msr fpsr, fpcr
    if w & 0xffc0_0000 == 0xd500_0000: return 'fex_other'    # system: nop and hints, barriers, mrs/msr of others
    if is_branch(w): return 'fex_branch'
    if is_memory(w): return 'fex_state'
    op0 = w >> 25 & 0xf
    if op0 in (0b1000, 0b1001):
        if w & 0x1f80_0000 == 0x1280_0000: return 'fex_const'   # movn, movz, movk
        return 'fex_alu'
    if op0 in (0b0101, 0b1101): return 'fex_alu'
    if op0 in (0b0111, 0b1111): return 'fex_simd'
    if op0 == 0 and w >> 16 == 0: return 'literal'           # udf: padding, a zeroed slot
    return 'unknown'


def roles(code: Code):
    """One role per word of the code region."""
    words, n = code.words, len(code.words)
    out = [None] * n
    if n: out[0] = 'entry'                                   # the code header's word: a PIC block's full entry
    starts = {no for no, _ in code.pcs}

    def put(off, role):
        i = off // 4
        if 0 <= i < n and out[i] is None: out[i] = role; return True
        return False
    for off, kind, _, _, size, _ in code.relocs:
        if kind in (RELOC_GUEST_RIP_LITERAL, RELOC_NAMED_SYMBOL_LITERAL) and size:
            for o in range(off - off % 4, off + size, 4): put(o, 'literal')
    stubs = {}
    probe_b = {off for off, kind, *_ in code.relocs if kind == RELOC_PROBE_BRANCH}
    for s in code.sites:
        no, kind = s[0], s[5]
        if kind in STUB_KINDS and no % 4 == 0:
            stubs[no] = kind
            put(no, 'stub')
            if no + 4 in probe_b: put(no + 4, 'stub')
    # Exits: a literal exit's guest-RIP move into x0 up to its svc #LINK; a computed exit's mov x0 and its b.
    for off, kind, reg, size, _, _ in code.relocs:
        if kind == RELOC_GUEST_RIP_MOVE and reg == 0:
            o = off
            while o // 4 < n:
                w = words[o // 4]
                put(o, 'exit_literal')
                if w == SVC_LINK or o - off >= 20: break
                o += 4
        elif kind == RELOC_DISPATCH_BRANCH:
            put(off, 'exit_computed')
            if off >= 4 and words[off // 4 - 1] & 0xffe0_ffff == 0xaa00_03e0: put(off - 4, 'exit_computed')
    # Checks: the branch to a stub (and the far form's skip over it), then the words before it that write only x0-x2.
    for i, w in enumerate(words):
        at = i * 4
        t = _branch_target(w, at)
        if t is None or t not in stubs or out[i] is not None: continue
        kind = stubs[t]
        role = CHECK_ROLE[kind]
        out[i] = role
        first = i
        if w & 0xfc00_0000 == 0x1400_0000 and i > 0 and _branch_target(words[i - 1], at - 4) == at + 4:
            out[i - 1] = role; first = i - 1                   # `!cond +8; b stub`
        j, room = first, CHECK_WORDS[kind]
        while room and j > 0 and j * 4 not in starts:
            p = words[j - 1]
            d = dest(p)
            if out[j - 1] is not None or is_branch(p) or d is None or d > 2: break
            if is_memory(p) and (p >> 5) & 31 not in (0, 1, 2, 28): break
            j -= 1; room -= 1
            out[j] = role
    # Access sites, then the moves out of a load's temporary in the same guest instruction.
    site_regs = []
    for s in code.sites:
        no, kind = s[0], s[5]
        if no % 4: continue                                   # an x87 map record: no word
        if kind in ACCESS_KINDS:
            put(no, 'fex_mem')
            if kind in LOAD_KINDS:
                regs = [s[16]] + ([s[17]] if kind in (LOAD_PAIR, POP_PAIR) else [])
                site_regs.append((no, regs, s[18] == CLASS_FPR))
        elif kind == FMA: put(no, 'fp_check')
    nexts = sorted(starts)
    for no, regs, fpr in site_regs:
        end = next((x for x in nexts if x > no), n * 4)
        for r in regs:
            if r == 31: continue
            for o in range(no + 4, min(end, no + 28), 4):
                w = words[o // 4]
                if out[o // 4] is None and (is_fp_move_from(w, r, fpr) or (not fpr and is_mov_from(w, r))):
                    out[o // 4] = 'load_move'; break
                if not fpr and dest(w) == r: break
    # A helper call's arguments: the moves into x0-x2 right before its svc (the 128/64 divide's, an x87 or string helper's
    # function code), in its instruction.
    for i, w in enumerate(words):
        if w & 0xffe0_001f != 0xd400_0001 or w == SVC_LINK or out[i] is not None: continue
        out[i] = 'helper'
        j = i
        while j > 0 and i - j < 4 and j * 4 not in starts and out[j - 1] is None:
            p = words[j - 1]
            if is_branch(p) or is_memory(p) or dest(p) is None or dest(p) > 2: break
            j -= 1
            out[j] = 'helper'
    for i, w in enumerate(words):
        if out[i] is None: out[i] = pattern(w, code.flag_regs)
    return out


def role_at(code_roles, offset, place_probe=False):
    """A sample's role: `offset` its native offset (None: FAR), `code_roles` the object's roles (None: no object)."""
    if place_probe: return 'probe'
    if code_roles is None: return 'none'
    if offset is None: return 'far'
    if offset == -4: return 'entry'
    if offset < 0 or offset // 4 >= len(code_roles) or offset % 4: return 'beyond'
    return code_roles[offset // 4]


# --- guest instructions ----------------------------------------------------------------------------------------------
LEGACY = set(b'\x26\x2e\x36\x3e\x64\x65\x66\x67\xf0\xf2\xf3')


def x86_rep_string(insn: bytes, bits=64):
    """'movs' or 'stos' for a REP MOVS/STOS (F3 among the legacy prefixes, REX in 64-bit code, then A4/A5/AA/AB), else None."""
    i, rep = 0, False
    while i < len(insn) and insn[i] in LEGACY:
        rep |= insn[i] == 0xf3; i += 1
    if bits == 64 and i < len(insn) and 0x40 <= insn[i] <= 0x4f: i += 1
    if not rep or i >= len(insn): return None
    return {0xa4: 'movs', 0xa5: 'movs', 0xaa: 'stos', 0xab: 'stos'}.get(insn[i])


def x86_wide_vector(insn: bytes, bits=64):
    """'vex256' for a VEX instruction with L set, 'evex' for an EVEX one, else None (32-bit code: C4/C5/62 are VEX/EVEX only
    with a register-form next byte)."""
    i = 0
    while i < len(insn) and insn[i] in LEGACY: i += 1
    if bits == 64 and i < len(insn) and 0x40 <= insn[i] <= 0x4f: return None    # REX before VEX is #UD
    if i + 2 >= len(insn): return None
    b, n1 = insn[i], insn[i + 1]
    if bits != 64 and b in (0xc4, 0xc5, 0x62) and n1 >> 6 != 3: return None
    if b == 0xc5: return 'vex256' if n1 & 4 else None
    if b == 0xc4: return 'vex256' if insn[i + 2] & 4 else None
    if b == 0x62: return 'evex'
    return None


def insn_bytes(code: Code, guest: bytes, guest_off):
    """The guest bytes of the instruction at guest offset `guest_off` (to the next PC-table entry's offset, or the end)."""
    offs = sorted({g for _, g in code.pcs})
    nxt = next((g for g in offs if g > guest_off), len(guest))
    return guest[guest_off:nxt]
