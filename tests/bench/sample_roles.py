"""The role of each word of a served code object (docs/restructure/el0-sampler-plan.md chunk 1): what a sampled word is for,
from the object's own records first (crates/abi/src/code_object.rs: its sites, PC table and relocations), patterns of the A64
encodings fexc emits under profile slice1 for the rest (the anatomy of docs/restructure/guest-code-review-20261114.md
section 2). No disassembler: the classes come from the encodings.

Roles, by who emits them (`GROUPS`):
- FEX's body: `fex_alu`, `fex_mem` (a recorded access site), `fex_simd`, `fex_const` (movz/movk), `fex_state` (a load or
  store that is no access site: the guest-state page through x28), `fex_branch` (a branch that reaches no check stub),
  `fex_fpctl` (FPSR and FPCR moves), `fex_other` (nop, barriers, other system words such as RDTSC's counter read); a bulk
  REP STOS loop (`--bulk`, kernel code) is FEX body too (its guest instruction counts in the report's REP share); and its
  flag words, `flags` (rmif, setf, cfinv, ccmp, cset, NZCV moves, the PF/AF bookkeeping: a write of PF's or AF's register
  by a word that sets no flags, unless it is the result its instruction moves on; a flag-setting `adds`/`subs` is the
  instruction's own work, `fex_alu`, whatever it writes).
- natemu's slice-1 words: the checks, each reached by the branch to its stub (`split_check` with its address word,
  `stack_check`, `align_check`, `guard_check` (a computed target's), `divide_check`, `fp_check` (SIMD, x87, pending, denormal
  and FMA checks), `x87_check` (the x87 stack tag, guessed stack and FLDCW tests), `segment_check`); `x87_run` (the test for
  an open x87 run, its start and its end: FPSR, FPCR and the run byte); the stubs themselves (`split_stub`: a probing split
  stub's `adr` and `b`; `stub`: a `brk`); `probe` (a sample in a shared probe, charged to its store's
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
# a constant's moves, the segment check's record walk, the x87 guess's loads, idiv's overflow test (RAX ^ INT_MIN, RDX + 1
# and RCX + 1 or-ed).
CHECK_WORDS = {SPLIT: 6, STACK: 1, ALIGN: 1, GUARD: 1, DIVIDE: 5, SIMD: 3, X87: 7, PENDING: 4, DENORM: 1, X87TAG: 1,
               X87GUESS: 4, FLDCW: 3, SEGMENT: 12}
RELOC_GUEST_RIP_LITERAL, RELOC_NAMED_SYMBOL_LITERAL, RELOC_GUEST_RIP_MOVE, RELOC_DISPATCH_BRANCH, RELOC_PROBE_BRANCH = 1, 2, 3, 4, 5
SVC_LINK = 0xd400_0021
MRS_X25_NZCV = 0xd53b_4219
CLASS_FPR = 1

GROUPS = {}
for r in ('fex_alu', 'fex_mem', 'fex_simd', 'fex_const', 'fex_state', 'fex_branch', 'fex_fpctl', 'fex_other'): GROUPS[r] = 'fex_body'
GROUPS['flags'] = 'fex_flags'
for r in ('split_check', 'split_stub', 'stack_check', 'align_check', 'guard_check', 'divide_check', 'fp_check', 'x87_check',
          'segment_check', 'x87_run', 'stub', 'probe', 'load_move', 'nzcv_save', 'helper', 'exit_literal', 'exit_computed', 'entry'): GROUPS[r] = 'natemu'
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
    if flag_only(w): return None                             # rmif, setf, ccmp: the low bits are a mask, no register
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


def flag_only(w):
    """rmif, setf8/16, ccmp/ccmn: words that write NZCV only."""
    return w & 0xffe0_7c10 == 0xba00_0400 or w & 0xffff_bc1f == 0x3a00_080d or w & 0x3fe0_0010 == 0x3a40_0000


def sets_flags(w):
    """A data-processing word with its S bit (adds, subs, ands, bics, adcs, sbcs; cmp, cmn, tst): the guest instruction's own
    result and flags, whatever register it writes."""
    s = w >> 29 & 1
    return bool((w & 0x1f00_0000 == 0x1100_0000 and s) or (w & 0x1f80_0000 == 0x1200_0000 and w >> 29 & 3 == 3)
                or (w & 0x1f00_0000 == 0x0b00_0000 and s) or (w & 0x1f00_0000 == 0x0a00_0000 and w >> 29 & 3 == 3)
                or (w & 0x1fe0_0000 == 0x1a00_0000 and s))


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
    # FEX's PF and AF: PF's register holds a result whose low byte's parity is PF, AF's an operand xor whose bit 4 with the
    # result's is AF. A flag-setting word is the instruction's own work even when it writes PF's register (cmp's `subs x26`,
    # add's `adds x26` whose result is then moved to the guest register); any other write of either register is the
    # bookkeeping (AF's `eor x27` and `mov x27`, a narrow compare's `sub w26`). roles() takes back a PF write whose value its
    # instruction then moves into another register (the result, computed there).
    d = dest(w)
    return d is not None and d in flag_regs and not is_memory(w) and not sets_flags(w)


# The x87 run's exact words: execution.rs RUN_END (frame::X87_RUN's byte at 0x414, the exception word at 0x4b4, the saved
# FPCR at 0xb0), and the start's (the SIMD flag byte at 0x411, FCW at 0x4b0; `movz w1, #6`, or #0 without AFP).
X87_RUN_TEST = 0x3950_5380                                   # ldrb w0, [x28, #X87_RUN] (then cbnz w0)
RUN_END = [0xd53b_4420, 0x1200_1c00, 0xb944_b781, 0x2a00_0021, 0xb904_b781, 0xb940_b380, 0xd51b_4400, 0xd51b_443f, 0x3910_539f]
RUN_START = [0xd53b_4420, None, 0x3950_4781, 0x2a00_0021, 0x3910_4781, 0xd51b_443f, 0x5280_0020, 0x3910_5380, 0xd53b_4400,
             0xb900_b380, 0x7949_6380, 0x5ac0_0000, 0x5314_5400, (0x5280_00c1, 0x5280_0001), 0x2a00_5820, 0xd51b_4400]


def x87_run_words(words, i):
    """How many words of the x87 run's test, start or end begin at word i (0: none)."""
    def at(seq, j):
        if j + len(seq) > len(words): return False
        for k, want in enumerate(seq):
            w = words[j + k]
            if want is None: ok = w & 0xff00_001f == 0x3400_0000                 # cbz w0
            elif isinstance(want, tuple): ok = w in want
            else: ok = w == want
            if not ok: return False
        return True
    if words[i] == X87_RUN_TEST and i + 1 < len(words) and words[i + 1] & 0xff00_001f == 0x3500_0000:
        return 2                                                                  # the test: ldrb w0; cbnz w0
    if at(RUN_END, i):
        return len(RUN_END) + (i + 9 < len(words) and words[i + 9] & 0xfc00_0000 == 0x1400_0000)
    if at(RUN_START, i): return len(RUN_START)
    return 0


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
    for off, kind, _, size, _, _ in code.relocs:      # (offset, kind, register, size, reserved, value)
        if kind in (RELOC_GUEST_RIP_LITERAL, RELOC_NAMED_SYMBOL_LITERAL) and size:
            for o in range(off - off % 4, off + size, 4): put(o, 'literal')
    stubs = {}
    probe_b = {off for off, kind, *_ in code.relocs if kind == RELOC_PROBE_BRANCH}
    for s in code.sites:
        no, kind = s[0], s[5]
        if kind in STUB_KINDS and no % 4 == 0:
            stubs[no] = kind
            role = 'split_stub' if kind == SPLIT else 'stub'
            put(no, role)
            if no + 4 in probe_b: put(no + 4, role)
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
    # The x87 run's words (M2 task 4, tools/fexc/shim/fexc_sites.inc FexcX87RunStart, FexcHoistEmit, FexcX87RunEnd): the
    # test for an open run, its start (FPSR's flags kept, the run byte set, FPCR switched to the x87's) and its end
    # (execution.rs RUN_END, with the `b` back after it when it is the top test's out-of-line end).
    i = 0
    while i < n:
        k = x87_run_words(words, i)
        for j in range(i, i + k):
            if out[j] is None: out[j] = 'x87_run'
        i += max(k, 1)
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
    # A PF-register write whose value its instruction moves into another register is the result (alu), and so is the move.
    for i, w in enumerate(words):
        d = dest(w)
        if out[i] is not None or d is None or d not in code.flag_regs or sets_flags(w) or flag_only(w): continue
        end = next((x for x in nexts if x > 4 * i), n * 4)
        for o in range(4 * i + 4, end, 4):
            u = words[o // 4]
            if is_mov_from(u, d) and dest(u) not in code.flag_regs: out[i] = 'fex_alu'; break
            if dest(u) == d: break
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


# The one-word skid correction (the report's `skid` shares). The sampler's validity case shows where the samples of a stall
# land: in guest-sampler-check-sample's chase phase (a loop of dependent cache-missing loads; M2 Max, run
# 20261123-194520-5367) 58% fell on the linked back-edge word just before the slow load in execution order, 18% on the load,
# the rest on cheap words after it. So a sample is also counted on the next word executed: the next word of its block for
# a straight-line word, the target block's first body word for a linked literal exit (its RIP from the exit's guest-RIP
# move). Branches with two successors, computed exits and the places with no word stay where they are.
NOT_MOVED = {'fex_branch', 'exit_computed', 'probe', 'none', 'far', 'beyond'}


def exit_targets(code: Code):
    """{word index: target RIP} of each literal exit's words (the guest-RIP move into x0 up to its svc #LINK)."""
    out, n = {}, len(code.words)
    for off, kind, reg, _, _, value in code.relocs:
        if kind != RELOC_GUEST_RIP_MOVE or reg != 0: continue
        o = off
        while o // 4 < n:
            out[o // 4] = value
            if code.words[o // 4] == SVC_LINK or o - off >= 20: break
            o += 4
    return out


def next_word(code_roles, offset):
    """The word index a straight-line sample at `offset` moves to under the skid correction, or None (stays)."""
    if code_roles is None or offset is None: return None
    i = 0 if offset == -4 else offset // 4 + 1
    return i if 0 <= offset + 4 and i < len(code_roles) and offset % 4 == 0 else None


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
