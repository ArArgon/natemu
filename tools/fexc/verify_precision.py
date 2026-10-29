#!/usr/bin/env python3
"""S1b1 check: the slice-1 precision guarantee, verified statically on the code fexc emits (profile slice1).

usage: verify_precision.py [--before FEXC0] [CORPUS.json ...]      (fexc from $FEXC, else the series15 build)

Corpora: fixtures/slice1/programs.json, the single-instruction matrix of scan_skips.py, the adversarial programs below
(one per precision patch), and every CORPUS.json given (gen_slice1.py format, e.g. a fresh `--seed` corpus).

For every compile, from the disassembly, the sites, the dense PC table and the static register (SRA) map:
  P1  between the native start of a site's guest instruction (its PC-table entry) and the site, no instruction writes a
      guest static register (the SRA map: 16 GPRs, PF x26, AF x27, the xmm registers), or stores to memory (the x28
      frame included); the window holds no other guest instruction's entry;
  P2  no NZCV write in that window unless the site says nzcv=saved, and then the pre-NZCV save `mrs x25, nzcv`
      (state_offsets.natemu_pre_nzcv_register) is the window's first word, and nothing else in the window writes x25;
  P3  a load site's destination is not a static register (DDI0487 D1.4.1.4: UNKNOWN after an abort). The destinations are
      read from the disassembled instruction at the site (every transfer register of an ld*, GPR or FP), not from fexc's
      data_regs; a writeback base (push/pop on RSP) is allowed, the access itself is its only write;
  P4  at most one store-class site (store, store_pair, push) per guest instruction, and it is the instruction's last site;
      no fused pair sites; every site says completed_effects=none;
  P5  no site can be skipped by a forward branch inside its instruction (scan_skips.skips);
  P6  for annotated programs (`insns`, the generator's flag annotations): for each site and each arithmetic flag, the last
      earlier instruction of the block that writes the flag, if it defines it (tools/reference/flags.py rules; an
      instruction that leaves the flag undefined defines nothing, which matches the compiler's InvalidateFlags-as-write
      rule), has a native write of its location in its own range (NZCV for CF/ZF/SF/OF, x26 for PF, x27 for AF): no flag
      write that a later site observes was eliminated (div/idiv leave all six flags undefined, so they define none; FEX
      keeps the incoming flags, which a later site may observe like any other undefined value). COARSE: any NZCV-writing instruction in that range satisfies each of
      CF, ZF, SF and OF (and any x26 / x27 writer PF / AF); it does not check that the right bit holds the right value.
      Values are covered by the QEMU comparison (S1e), not here. P6b (I1): every cld/std stores DF to the guest-state
      page in its own range (df_stores);
  P7  (a) every compile: every native load/store with a base other than x28/sp is a site, every site is on such an
      instruction, and width, base, index, immediate and writeback match the instruction (check_sites.code_vs_sites);
      (b) for programs with `asm` (fixtures, generated, adversarial): check_sites' comparison of expected accesses, sites,
      object and JSON, with no eliminated access.
  P8-P10 (S1b2, E3, see guards()): no access has an address guard (E3: a non-canonical access faults in hardware at
      its site), every store wider than one byte has the split check, every stub is reached only by its check, and every
      indirect exit checks its target before any state write; the split/branch-target stubs are sites, so P1/P2 hold at
      their branches.
  P11 (S1f, see divides()): every div/idiv (width and signedness from its x86 bytes) has exactly its #DE checks in
      their exact per-width form (DIV_FORMS: opcodes, extends, bitfield positions and scratch registers pinned; the far
      form `inverse test; b stub` accepted), the zero check directly before the overflow check, all before FEX's own
      divide (or its 128/64 division `svc`), reading that divide's divisor (directly or through FEX's x1 copy), at 8 bits
      its dividend, and passing the same divisor to the resident in x2, unchanged in between; every divide stub is one of
      these checks; every division `svc` (`svc #3` for div, `svc #4` for idiv) is followed by `cbnz x2` to a divide stub.
      P1/P2 at each divide branch then give the state before the instruction, relying on the resident's contract (only
      x0-x2 change; NZCV kept; the runtime's `division`). Admission (crates/host/src/admission/structure.rs) checks the
      same forms on every block it admits.
  P12 (E1, E1b, see entry_and_branches()): the entry prologue is empty: the block's first word is its first guest
      instruction (no stop check, spill or TF check; the resident stops a vCPU by unlinking the interrupted block's
      exits). No `bl`, `blr`, `br` or `ret` in the block, no sp access and no literal load, and x28 only for a byte of
      the guest-state page's RFLAGS fields (G1, I1: loads of TF/IF/DF/IOPL/NT/RF/VM/AC/VIF/VIP/ID by `pushf`, stores
      only to DF by `cld`/`std`; crates/abi/src/execution.rs `frame`) and loads inside the FS/GS bases (I7: FEX's
      fs_cached/gs_cached, read by every FS-/GS-prefixed access), so the code touches no emulator data;
  P13 (S2c0): every branch in the block goes forward to an instruction of the code region, so a block never loops
      internally: a jcc back to its own entry exits through the gate like any taken edge (no multiblock, no self-loop).
Assumption by construction, NOT checked: every static-register write of guest instruction k-1 is emitted before the PC-table
entry of instruction k. P1 sees only the window from an instruction's start to its site, so a write of an earlier
instruction that was sunk past the next instruction's start would be flagged only if it lands before that site, and one
landing after the site would go unnoticed. It holds because the compiler flushes registers per instruction and the
register-allocation barrier at GuestOpcode (patch 0006) stops a value from being coalesced across the boundary.
Every mnemonic in a P1/P2 window must be classified below; an unknown one is reported, never assumed harmless.
Also prints the cost (native instructions and code bytes per translated guest instruction, and the native words of the
guard, split and branch-target checks and of the stubs), and with --before the totals for an earlier build (e.g. series2,
without the checks) on the same inputs.
Exit status 1 on any violation.
"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / 'tools/reference'))
import check_sites, scan_skips  # noqa: E402
from check_sites import code_vs_sites  # noqa: E402
import flags as F  # noqa: E402

FEXC = os.environ.get('FEXC', '/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series15/obj/fexc')

NZCV_WRITERS = {'adds', 'adcs', 'subs', 'sbcs', 'ands', 'bics', 'negs', 'ngcs', 'cmp', 'cmn', 'tst', 'ccmp', 'ccmn', 'fcmp',
                'fcmpe', 'fccmp', 'fccmpe', 'setf8', 'setf16', 'rmif', 'cfinv', 'axflag', 'xaflag'}
NO_DEST = {'cmp', 'cmn', 'tst', 'ccmp', 'ccmn', 'fcmp', 'fcmpe', 'fccmp', 'fccmpe', 'setf8', 'setf16', 'rmif', 'cfinv', 'axflag',
           'xaflag', 'msr', 'b', 'br', 'ret', 'cbz', 'cbnz', 'tbz', 'tbnz', 'nop', 'dmb', 'dsb', 'isb', 'brk', 'udf', 'hint', 'prfm'}
PAIR_LOADS = {'ldp', 'ldnp', 'ldpsw', 'ldxp', 'ldaxp'}
DATA = {'mov', 'movk', 'movz', 'movn', 'add', 'sub', 'and', 'orr', 'eor', 'bic', 'orn', 'eon', 'lsl', 'lsr', 'asr', 'ror', 'lslv',
        'lsrv', 'asrv', 'rorv', 'mul', 'madd', 'msub', 'mneg', 'smulh', 'umulh', 'smull', 'umull', 'smaddl', 'umaddl', 'sdiv', 'udiv',
        'csel', 'csinc', 'csinv', 'csneg', 'cset', 'csetm', 'cinc', 'cinv', 'cneg', 'sxtb', 'sxth', 'sxtw', 'uxtb', 'uxth', 'ubfx',
        'sbfx', 'ubfiz', 'sbfiz', 'bfi', 'bfxil', 'bfc', 'bfm', 'ubfm', 'sbfm', 'extr', 'rev', 'rev16', 'rev32', 'rbit', 'clz', 'cls',
        'adc', 'sbc', 'neg', 'ngc', 'mvn', 'mrs', 'adr', 'adrp', 'bl', 'blr', 'cnt', 'fmov', 'uaddlv', 'svc'} | NZCV_WRITERS | PAIR_LOADS


def reg(tok):
    """('x', n) for a general register, ('v', n) for a SIMD/FP register, None for zr/sp/anything else."""
    tok = tok.strip().split('.')[0]
    if m := re.fullmatch(r'[wx](\d+)', tok):
        return ('x', int(m[1]))
    if m := re.fullmatch(r'[bhsdqv](\d+)', tok):
        return ('v', int(m[1]))
    return None


def effects(text):
    """(mnemonic, written registers, writes NZCV, native access or None) of one disassembled instruction."""
    mn, _, ops = text.partition(' ')
    pre = ops.split('[')[0]
    toks = [t for t in pre.split(',') if t.strip()]
    acc = check_sites.native_access(text)
    dests = set()
    if mn.startswith('st') and acc:
        if mn.startswith(('stxr', 'stlxr', 'stxp', 'stlxp')):
            dests.add(reg(toks[0]))
    elif mn in PAIR_LOADS:
        dests |= {reg(toks[0]), reg(toks[1])}
    elif mn in ('bl', 'blr'):
        dests.add(('x', 30))
    elif mn == 'svc':   # a division's (svc #3/#4): the resident changes x0-x2; a literal exit's never returns
        dests |= {('x', 0), ('x', 1), ('x', 2)}
    elif mn not in NO_DEST and not mn.startswith('b.') and toks:
        dests.add(reg(toks[0]))
    if acc and acc[5] != 'none':
        dests.add(reg(acc[2]))
    nzcv = mn in NZCV_WRITERS or (mn == 'msr' and 'nzcv' in ops.lower())
    known = mn in DATA or mn in NO_DEST or mn.startswith('b.') or bool(acc)
    return mn, {d for d in dests if d}, nzcv, acc, known


def loaded_regs(text):
    """Registers a disassembled load transfers into (all transfer registers before the address; the base is not one)."""
    return {r for r in map(reg, text.partition(' ')[2].split('[')[0].split(',')) if r}


def flag_writes(a):
    """(written, defined) arithmetic-flag masks of one annotated instruction (flags.py rules); a shift or rotate by an
    unknown count may write nothing, so it defines nothing."""
    op = a['op']
    if op in F.NO_FLAGS | F.NO_FLAGS_I1 | {'lahf', 'pushf'}:
        return 0, 0
    if op == 'cmc':     # FEX flips its CF-inversion state at compile time (no native word; sites record cf_inverted), so
        return 0, 0     # the CF a later site observes is the earlier definer's, inverted: look past cmc to that write
    if op in ('clc', 'stc'):     # I1: CF only; sahf: all but OF
        return F.CF, F.CF
    if op == 'sahf':
        return F.ALL6 & ~F.OF, F.ALL6 & ~F.OF
    if op in F.SHIFTS | F.ROTATES | {'shld', 'shrd'}:
        if a.get('count') is None:
            return F.ALL6, 0
        if not a['count'] & (0x3f if a['width'] == 64 else 0x1f):
            return 0, 0
    w = {'inc': F.ALL6 & ~F.CF, 'dec': F.ALL6 & ~F.CF}.get(op, F.CF | F.OF if op in F.ROTATES else
                                                         F.CF | F.OF | F.SF | F.AF | F.PF if op in F.BITTEST else F.ALL6)
    try:
        return w, w & ~F.step(a, 0)
    except ValueError:
        return w, 0


def stale_flags(d, insns, dis, order):
    """P6 violations: a flag write a later site observes has no native write in its instruction's range."""
    starts = {}
    for e in d['pc_table']['entries']:
        starts[e['guest_offset']] = max(starts.get(e['guest_offset'], -1), e['native_offset'])
    gl = [i['offset'] for i in d['guest']['instructions'] if i['translated']]
    ends = [starts.get(g, None) for g in gl[1:]] + [max(starts.values())]
    wrote = []
    for k, g in enumerate(gl):
        eff = [effects(dis[o]) for o in order if starts[g] <= o < ends[k]]
        wrote.append({'nzcv': any(e[2] for e in eff), 'x26': any(('x', 26) in e[1] for e in eff),
                      'x27': any(('x', 27) in e[1] for e in eff)})
    out = []
    site_insns = sorted({gl.index(s['guest_offset']) for s in d['sites']})
    for k in site_insns:
        for f, loc in ((F.CF, 'nzcv'), (F.ZF, 'nzcv'), (F.SF, 'nzcv'), (F.OF, 'nzcv'), (F.PF, 'x26'), (F.AF, 'x27')):
            i = next((i for i in range(k - 1, -1, -1) if flag_writes(insns[i])[0] & f), None)
            if i is not None and flag_writes(insns[i])[1] & f and not wrote[i][loc]:
                out.append(f'guest +{gl[k]} has a site, but flag {f:#x} defined by guest +{gl[i]} ({insns[i]["op"]}) has no native '
                           f'{loc} write (eliminated)')
    return out


def df_stores(d, insns, dis, order):
    """P6b (I1): every cld/std stores DF to the guest-state page (`strb wN, [x28, #0x3fa]`) in its own native range, so a
    fault at any later site sees it there (the resident banks DF from the page)."""
    starts = {}
    for e in d['pc_table']['entries']:
        starts[e['guest_offset']] = max(starts.get(e['guest_offset'], -1), e['native_offset'])
    gl = [i['offset'] for i in d['guest']['instructions'] if i['translated']]
    ends = [starts.get(g, None) for g in gl[1:]] + [d['code']['layout']['code_region_end']]
    out = []
    for k, g in enumerate(gl):
        if k < len(insns) and insns[k]['op'] in ('cld', 'std') and not any(
                starts[g] <= o < ends[k] and re.fullmatch(r'strb w\d+, \[x28, #0x3fa\]', dis[o]) for o in order):
            out.append(f'guest +{g} ({insns[k]["op"]}) stores no DF in its own range')
    return out


STUBS = check_sites.STUBS
BRANCH = re.compile(r'(b|b\.\w+|cbz|cbnz|tbz|tbnz)\s+(?:([wx]\d+),\s+)?(?:#(0x[0-9a-f]+|\d+),\s+)?0x([0-9a-f]+)$')
STUB_WORD = {'guard': 'brk #0x1', 'split': 'brk #0x2', 'divide': 'brk #0x3'}
M64 = (1 << 64) - 1


def canonical(a):
    return (a & M64) >> 47 in (0, 0x1ffff)


def writes(text):
    return effects(text)[1]


def const_of(reg, lo, hi, dis, order):
    """Value of x<reg> built by a mov/movk sequence ending before `hi` (at or after `lo`), else None."""
    v = None
    for o in (o for o in order if lo <= o < hi):
        t = dis[o]
        if ('x', reg) not in writes(t):
            continue
        if m := re.fullmatch(rf'mov ([wx]){reg}, #(-?(?:0x)?[0-9a-f]+)', t):   # a w destination zero-extends
            v = int(m[2], 0) & (M64 if m[1] == 'x' else 0xffffffff)
        elif (m := re.fullmatch(rf'movk ([wx]){reg}, #((?:0x)?[0-9a-f]+)(?:, lsl #(\d+))?', t)) and v is not None:
            sh = int(m[3] or 0)
            v = (v & ~(0xffff << sh) | int(m[2], 0) << sh) & (M64 if m[1] == 'x' else 0xffffffff)
        else:
            v = None
    return v


def checked_expr(reg, lo, hi, dis, order):
    """The address a guard checks in x<reg>: (base, index, extend, shift, imm), from the guard's own add/sub of [lo, hi)
    when reg is x1 (TMP2), else the register itself. None if it is not one of the guard's forms."""
    if reg != 1:
        return (reg, None, 'none', 0, 0)
    ws = [o for o in order if lo <= o < hi and ('x', 1) in writes(dis[o])]
    if not ws:
        return None
    t = dis[ws[-1]]
    if m := re.fullmatch(r'(add|sub) x1, x(\d+), #((?:0x)?[0-9a-f]+)', t):
        return (int(m[2]), None, 'none', 0, int(m[3], 0) * (1 if m[1] == 'add' else -1))
    if m := re.fullmatch(r'add x1, x(\d+), x1(?:, (?:lsl|sxtx) #0)?', t):
        c = const_of(1, lo, ws[-1], dis, order)
        return None if c is None else (int(m[1]), None, 'none', 0, c - (1 << 64) if c >> 63 else c)
    if m := re.fullmatch(r'add x1, x(\d+), [wx](\d+)(?:, (sxtx|uxtw|sxtw|lsl)(?: #(\d+))?)?', t):
        return (int(m[1]), int(m[2]), {'lsl': 'sxtx', None: 'sxtx'}.get(m[3], m[3]), int(m[4] or 0), 0)
    return None


def guards(d, dis, order):
    """P8-P10 (slice1 split-store check and branch-target check; no address guard since E3) on one compile.
    Returns (violations, {stub: its check branch}, cost {'guard'|'split'|'branch'|'stub': native words}; 'guard' stays 0).
      P8  no access site has a guard stub (the address guard is gone: a non-canonical access faults in hardware at its
          site, where the runtime raises #GP/#SS). Every store wider than one byte (store, store_pair, push; read-modify-write stores too: with both pages shadowed
          only the store faults, and Arm may commit its first part) is preceded by `add x0, xA, #w-1; eor x0, x0, xA;
          tbnz x0, #12, S` on exactly its address (xA = base, or x1 = base + index|imm by the check's own add/sub; or,
          RIP-relative (base last written by a guest-RIP move, no index) and crossing a page, canonical or not, `b S`) with
          S a split stub linked to it, and
          between that branch and the store only x0, x1 or the store's own base are written (the runtime's completion of
          a split store subsumes them). No other site has a split stub.
      P9  every guard/split site is a `brk #1`/`brk #2` word after the body, reached by exactly one branch, which is one of
          the checks above; the body falls into no stub; every brk is a stub site; a stub has its access's guest
          instruction and segment.
      P10 every computed exit (indirect jmp/call/ret: `mov x0, xR` then the dispatch placeholder `b .` at a
          dispatch_branch relocation) is preceded in its guest instruction by `eor x0, xT, xT, lsl #16; tbnz x0, #63, G`,
          G a guard with no access and base_reg T, and R holds T (mov copies followed); the P1/P2 window of that guard
          (instruction start to the tbnz) proves no state write precedes it. Every literal exit (a guest-RIP move into x0,
          then `svc #1`) has a canonical RIP or the block's fall-through RIP."""
    out, check_at, ok = [], {}, set()
    cost = {'guard': 0, 'split': 0, 'branch': 0, 'stub': 0}
    sites = d['sites']
    stubs = {s['native_offset']: s for s in sites if s['kind'] in STUBS}
    refs = {}
    for o in order:
        if (m := BRANCH.match(dis[o])) and int(m[4], 16) in stubs:
            refs.setdefault(int(m[4], 16), []).append(o)
    for at, s in stubs.items():
        if dis.get(at) != STUB_WORD[s['kind']]:
            out.append(f'stub +{at} ({s["kind"]}) is "{dis.get(at)}"')
        if len(refs.get(at, [])) != 1:
            out.append(f'stub +{at} ({s["kind"]}) reached by {len(refs.get(at, []))} branches')
        else:
            check_at[at] = refs[at][0]
    cost['stub'] = len(stubs)
    if {o for o in order if dis[o].startswith('brk')} != set(stubs):
        out.append('brk words and stub sites differ')
    if stubs:
        first = min(stubs)
        prev = max((o for o in order if o < first), default=None)
        if prev is None or not (dis[prev].split()[0] in ('b', 'udf') or dis[prev] == 'svc #0x1'):   # an exit: dispatch or link
            out.append(f'the body falls into the first stub +{first}')
        if any(o > first and o not in stubs and not dis[o].startswith('udf') for o in order):
            out.append('code after the stubs')
    linked = {}
    for at, s in stubs.items():
        linked.setdefault(s['access_native_offset'], []).append(s)
    by_off = {s['native_offset']: s for s in sites if s['kind'] not in STUBS}
    for a, ss in linked.items():
        if a is not None and a not in by_off:
            out.append(f'stub(s) {[s["native_offset"] for s in ss]} linked to +{a}, which is no access site')
        for s in ss:
            if a in by_off and (s['guest_offset'], s['segment']) != (by_off[a]['guest_offset'], by_off[a]['segment']):
                out.append(f'stub +{s["native_offset"]}: guest offset/segment differ from its access +{a}')
    rel = [r for r in d['code']['relocations'] if r['kind'] == 'guest_rip_move']

    def written(reg, lo, hi):
        return any(lo <= o < hi and ('x', reg) in writes(dis[o]) for o in order)

    def cond(t, bit, stub):
        """Offset of the conditional test of a check whose branch to `stub` is at t: `tbnz x0, #bit, stub` itself, or
        the far form `tbz x0, #bit, t+4; b stub` FEX emits after a far-branch restart (blocks over 32 KiB); else None."""
        if t is None:
            return None
        if re.fullmatch(rf'tbnz [wx]0, #{bit:#x}, {stub:#x}', dis.get(t, '')):
            return t
        if dis.get(t) == f'b {stub:#x}' and re.fullmatch(rf'tbz [wx]0, #{bit:#x}, {t + 4:#x}', dis.get(t - 4, '')):
            return t - 4
        return None

    for s in sorted(by_off.values(), key=lambda s: s['native_offset']):
        at, lo = s['native_offset'], s['insn_native_offset']
        tag = f'site +{at} ({s["kind"]}, guest +{s["guest_offset"]})'
        mine = linked.get(at, [])
        gs = [x for x in mine if x['kind'] == 'guard']
        sp = [x for x in mine if x['kind'] == 'split']
        b, ix = s['base_reg'], s['index_reg']
        expr = (b, ix, s['extend'] if ix is not None else 'none', s['shift'] if ix is not None else 0, s['imm'])
        pure = s['kind'] in ('store', 'store_pair', 'push') and s['width'] > 1   # every store wider than 1 byte, rmw too
        bw = [o for o in order if lo <= o < at and ('x', b) in writes(dis[o])]
        r = next((r for r in rel if bw and r['register'] == b and r['offset'] <= bw[-1] < r['offset'] + 16), None)
        if r and ix is None:   # RIP-relative: compile-time address
            A = (int(r['guest_rip'], 16) + s['imm']) & M64
            want_g, want_s = False, pure and bool((A ^ (A + s['width'] - 1)) & 0x1000)
            for kind, want, have in (('guard', want_g, gs), ('split', want_s, sp)):
                if len(have) != want:
                    out.append(f'{tag}: RIP-relative address {A:#x} needs {int(want)} {kind} stub(s), has {len(have)}')
                for x in have:
                    o = check_at.get(x['native_offset'])
                    if o is None or not (lo <= o < at) or dis[o] != f'b {x["native_offset"]:#x}':
                        out.append(f'{tag}: RIP-relative {kind} stub +{x["native_offset"]} is not reached by an unconditional b in the instruction')
                    else:
                        ok.add(o); cost[kind] += 1
            continue
        # P8: no address guard
        if gs:
            out.append(f'{tag}: {len(gs)} address guard stubs')
        # P8 split check
        if pure:
            if len(sp) != 1:
                out.append(f'{tag}: store of {s["width"]} bytes with {len(sp)} split stubs')
            else:
                x = sp[0]
                A = x['base_reg']
                t2 = check_at.get(x['native_offset'])
                c2 = cond(t2, 12, x['native_offset'])
                ce = checked_expr(A, lo, c2 - 8, dis, order) if c2 is not None else None
                if c2 is None or not (lo <= c2 and t2 < at) or dis.get(c2 - 8) != f'add x0, x{A}, #{s["width"] - 1:#x}' or \
                        dis.get(c2 - 4) != f'eor x0, x0, x{A}' or x['imm'] != 0 or ce is None or ce != expr or \
                        written(ce[0], c2 - 8, at) or (ix is not None and written(ix, c2 - 8, at)):
                    out.append(f'{tag}: split stub +{x["native_offset"]} is not `add x0, xA, #w-1; eor x0, x0, xA; tbnz x0, #12` on the store address ({ce} vs {expr})')
                else:
                    ok.add(t2); cost['split'] += 3 + (t2 - c2) // 4
                    q = c2 - 12   # the check's own address words: the run writing only x1 right before the add
                    while A == 1 and q >= lo and writes(dis.get(q, '')) == {('x', 1)}:
                        cost['split'] += 1; q -= 4
                    # The runtime completes a split store from the registers as they are at the stub: nothing between the
                    # check and the store may write anything but the scratch registers x0/x1 (not the base, not the data).
                    for o in (o for o in order if t2 < o < at):
                        if writes(dis[o]) - {('x', 0), ('x', 1)} or check_sites.native_access(dis[o]):
                            out.append(f'{tag}: "{dis[o]}" between the split check and the store')
        elif sp:
            out.append(f'{tag}: split stub on an access that is not a store wider than one byte')

    # P10 exits
    starts = sorted((e['native_offset'], e['guest_offset']) for e in d['pc_table']['entries'])
    fall = (int(d['input']['rip'], 16) + d['guest']['bytes_translated']) & M64
    for r in d['code']['relocations']:
        if r['kind'] == 'guest_rip_move' and r['register'] == 0:
            v = int(r['guest_rip'], 16)
            if not (canonical(v) or v == fall):
                out.append(f'exit +{r["offset"]}: literal RIP {v:#x} is neither canonical nor the fall-through RIP')
    for o in sorted(r['offset'] for r in d['code']['relocations'] if r['kind'] == 'dispatch_branch'):
        m = re.fullmatch(r'mov x0, x(\d+)', dis.get(o - 4, ''))
        if dis.get(o) != f'b {o:#x}' or not m:
            out.append(f'exit +{o}: the dispatch branch is not `mov x0, xR; b .`')
            continue
        R = int(m[1])
        lo, g = max(e for e in starts if e[0] <= o)
        cand = [x for x in stubs.values() if x['kind'] == 'guard' and x['access_native_offset'] is None and x['guest_offset'] == g]
        good = False
        for x in cand:
            t, T = check_at.get(x['native_offset']), x['base_reg']
            c = cond(t, 63, x['native_offset'])
            if c is None or not (lo <= c and t < o - 4) or dis.get(c - 4) != f'eor x0, x{T}, x{T}, lsl #16':
                continue
            alias = {T}   # registers holding the checked value
            for q in (q for q in order if t < q < o - 4):
                mv = re.fullmatch(r'mov x(\d+), x(\d+)', dis[q])
                copy = bool(mv) and int(mv[2]) in alias
                alias -= {r_[1] for r_ in writes(dis[q]) if r_[0] == 'x'}
                if copy:
                    alias.add(int(mv[1]))
            if R in alias:
                good = True
                ok.add(t); cost['branch'] += 2
        if not good:
            out.append(f'exit +{o} (guest +{g}): computed RIP x{R} is not checked by a branch-target guard before any state write')
    dout, dok = divides(d, dis, order, stubs, check_at)
    out += dout
    ok |= dok
    for at, t in check_at.items():
        if t not in ok:
            out.append(f'branch +{t} to stub +{at} is not one of the checks')
    return out, check_at, cost


def x86_divide(code):
    """(signed, operand width) of an x86 div/idiv (F6/F7 /6, /7) from its bytes, else None."""
    i, size16, rex_w = 0, False, False
    while i < len(code) and code[i] in (0x66, 0x67, 0x26, 0x2e, 0x36, 0x3e, 0x64, 0x65, 0xf0, 0xf2, 0xf3):
        size16 |= code[i] == 0x66
        i += 1
    if i < len(code) and code[i] & 0xf0 == 0x40:
        rex_w = bool(code[i] & 8)
        i += 1
    if i + 1 >= len(code) or code[i] not in (0xf6, 0xf7) or (code[i + 1] >> 3 & 7) < 6:
        return None
    return (code[i + 1] >> 3 & 7) == 7, 8 if code[i] == 0xf6 else 64 if rex_w else 16 if size16 else 32


R = r'(?P<{}>\d+)'   # a register number parameter
# The exact #DE checks per (signed, width): kind -> (words before the check branch, the branch, normalized: taken on #DE).
# Registers: d = divisor, h = dividend (8-bit forms; 16-64 read RDX:RAX in x5:x4 directly). Pinned: opcodes, extends,
# bitfield positions, scratch registers x0/x1.
DIV_FORMS = {
    (False, 8): {'high': ([rf'ubfx x0, x{R.format("h")}, #8, #8', rf'sub x0, x0, w{R.format("d")}, uxtb'], r'tbz x0, #0x3f')},
    (False, 16): {'high': ([r'uxth w0, w5', rf'sub x0, x0, w{R.format("d")}, uxth'], r'tbz x0, #0x3f')},
    (False, 32): {'high': ([r'mov w0, w5', rf'sub x0, x0, w{R.format("d")}, uxtw'], r'tbz x0, #0x3f')},
    (False, 64): {'zero': ([], rf'cbz x{R.format("d")}')},
    (True, 8): {'zero': ([rf'sxtb w1, w{R.format("d")}'], r'cbz w1'),
                'overflow': ([rf'sxth w0, w{R.format("h")}', r'sdiv w0, w0, w1', r'sub w0, w0, w0, sxtb'], r'cbnz w0')},
    (True, 16): {'zero': ([rf'sxth w1, w{R.format("d")}'], r'cbz w1'),
                 'overflow': ([r'uxth w0, w4', r'bfi w0, w5, #16, #16', r'sdiv w0, w0, w1', r'sub w0, w0, w0, sxth'], r'cbnz w0')},
    (True, 32): {'zero': ([rf'sxtw x1, w{R.format("d")}'], r'cbz x1'),
                 'overflow': ([r'mov w0, w4', r'bfi x0, x5, #32, #32', r'sdiv x0, x0, x1', r'sub x0, x0, w0, sxtw'], r'cbnz x0')},
    (True, 64): {'zero': ([], rf'cbz x{R.format("d")}'),
                 'overflow': ([r'eor x0, x4, #0x8000000000000000', r'add x1, x5, #0x1', r'orr x0, x0, x1',
                               rf'add x1, x{R.format("d")}, #0x1', r'orr x0, x0, x1'], r'cbz x0')},
}
DIV_WANT = {(False, 64): ['verdict', 'zero'], (True, 64): ['overflow', 'verdict', 'zero']}   # else: high | overflow+zero
DIV_SVC = {'DIV': 'svc #0x3', 'IDIV': 'svc #0x4'}   # the resident's 128/64 division (execution::svc::DIV_U / DIV_S)
SCRATCH = {0, 1, 2, 3, 28, 31}
INVERSE = {'cbz': 'cbnz', 'cbnz': 'cbz', 'tbz': 'tbnz', 'tbnz': 'tbz'}


def divides(d, dis, order, stubs, check_at):
    """P11 on one compile: (violations, the divide check branches)."""
    out, ok = [], set()
    gi = {i['offset']: i for i in d['guest']['instructions'] if i['translated']}
    names = {g: i['name'] for g, i in gi.items()}
    starts = sorted((e['native_offset'], e['guest_offset']) for e in d['pc_table']['entries'])
    body_end = min([o for o in order if dis[o].startswith('brk')] + [d['code']['layout']['code_region_end']])

    def insn_at(o):
        """(guest offset, native start, native end) of the instruction whose range holds native offset o."""
        lo, g = max(e for e in starts if e[0] <= o)
        hi = min([n for n, og in starts if og > g] + [body_end])
        return g, lo, hi

    def cond_to(t, stub):
        """(offset, normalized text) of the check's conditional test reaching `stub` from the branch at t: the test
        itself, or FEX's far form `inverse test to t+4; b stub` normalized to the test taken on #DE."""
        m = re.fullmatch(rf'(cbz|cbnz|tbz|tbnz) (.*), {stub:#x}', dis.get(t, ''))
        if m:
            return t, f'{m[1]} {m[2]}'
        m = re.fullmatch(rf'(cbz|cbnz|tbz|tbnz) (.*), {t + 4:#x}', dis.get(t - 4, ''))
        if dis.get(t) == f'b {stub:#x}' and m:
            return t - 4, f'{INVERSE[m[1]]} {m[2]}'
        return None

    def form(sw, c, cond):
        """(kind, first word, registers) of the exact #DE check whose conditional test is at c, else None."""
        if cond == 'cbnz x2' and dis.get(c - 4) in DIV_SVC.values():
            return 'verdict', c - 8, {}
        for kind, (words, test) in DIV_FORMS.get(sw, {}).items():
            regs = {}
            seq = [(c - 4 * (len(words) - k), w) for k, w in enumerate(words)] + [(c, test)]
            for o, pattern in seq:
                m = re.fullmatch(pattern, cond if o == c else dis.get(o, ''))
                if not m or any(regs.get(k, v) != v for k, v in m.groupdict().items()):
                    break
                regs |= m.groupdict()
            else:
                if all(int(v) not in SCRATCH for v in regs.values()):
                    return kind, seq[0][0], {k: int(v) for k, v in regs.items()}
        return None

    divs = {at: s for at, s in stubs.items() if s['kind'] == 'divide'}
    checks = {}   # guest offset -> [(kind, first word, branch, registers)]
    for at, s in divs.items():
        t = check_at.get(at)
        if t is None:
            continue   # reported by guards()
        cc = cond_to(t, at)
        g, lo, hi = insn_at(cc[0] if cc else t)
        sw = x86_divide(bytes.fromhex(gi[g]['bytes_hex'])) if g in gi else None
        f = cc and sw and form(sw, *cc)
        if not f or g != s['guest_offset'] or not lo <= f[1] or names.get(g) != ('IDIV' if sw[0] else 'DIV'):
            out.append(f'divide stub +{at}: branch +{t} "{dis.get(t)}" is not an exact #DE check of its div/idiv (guest +{s["guest_offset"]})')
            continue
        ok.add(t)
        checks.setdefault(g, []).append((f[0], f[1], t, f[2]))
    for o in (o for o in order if dis[o].startswith('svc') and dis[o] != 'svc #0x1'):
        g, lo, hi = insn_at(o)
        want = DIV_SVC.get(names.get(g))
        nxt = dis.get(o + 4, '')
        cb = re.fullmatch(r'cbnz x2, 0x([0-9a-f]+)', nxt) or re.fullmatch(r'cbz x2, 0x([0-9a-f]+)', nxt)
        stub = int(dis.get(o + 8, 'b 0x0').split()[-1], 16) if cb and nxt.startswith('cbz') else int(cb[1], 16) if cb else None
        if not (want and dis[o] == want and stub in divs and check_at.get(stub) in (o + 4, o + 8) and lo <= o < hi):
            out.append(f'svc +{o} "{dis[o]}" (guest +{g}) is not the 128/64 division of a div/idiv followed by its #DE check')
    for g, name in names.items():
        if name not in ('DIV', 'IDIV'):
            continue
        sw = x86_divide(bytes.fromhex(gi[g]['bytes_hex']))
        lo = min(n for n, og in starts if og == g)
        hi = min([n for n, og in starts if og > g] + [body_end])
        mine = sorted(checks.get(g, []), key=lambda x: x[1])
        kinds = sorted(k for k, *_ in mine)
        want = DIV_WANT.get(sw, ['overflow', 'zero'] if sw and sw[0] else ['high'])
        if not sw or kinds != want:
            out.append(f'{name} at guest +{g}: #DE checks {kinds}, expected {want}')
            continue
        by = {k: (s, t, r) for k, s, t, r in mine}
        if 'overflow' in by and (by['overflow'][0] != by['zero'][1] + 4 or by['overflow'][2].get('d', by['zero'][2]['d']) != by['zero'][2]['d']):
            out.append(f'{name} at guest +{g}: the overflow check does not directly follow its zero check')
        # FEX's divide: the division svc or a udiv/sdiv into another register than the checks' x0; all checks but the
        # svc's verdict come first, and they read its operands.
        def fex_div(o):
            mn, _, ops = dis[o].partition(' ')
            return mn in ('udiv', 'sdiv') and not ops.startswith(('w0,', 'x0,'))
        fex = [o for o in order if lo <= o < hi and (dis[o] in DIV_SVC.values() or fex_div(o))]
        inline = [o for o in fex if dis[o] not in DIV_SVC.values()]
        if not fex or not inline or any(t >= fex[0] for k, s, t, r in mine if k != 'verdict'):
            out.append(f'{name} at guest +{g}: no #DE check before its divide (+{fex[0] if fex else None})')
            continue
        dreg = (by.get('high') or by['zero'])[2]['d']
        hreg = (by.get('high') or by.get('overflow') or (0, 0, {}))[2].get('h')
        mn, ops = dis[inline[0]].split(' ', 1)
        rd, rn, rm = [reg(x)[1] for x in ops.split(', ')]
        x1 = [o for o in order if lo <= o < inline[0] and ('x', 1) in writes(dis[o])]
        copy = rm == 1 and x1 and dis[x1[-1]] in (f'mov w1, w{dreg}', f'sxth w1, w{dreg}', f'sxtw x1, w{dreg}')
        x2 = [o for o in order if lo <= o < fex[0] and ('x', 2) in writes(dis[o])]
        helper = sw[1] != 64 or (x2 and dis[x2[-1]] == f'mov x2, x{dreg}')
        first = min(s for k, s, t, r in mine)
        if not (rm == dreg or copy) or mn != ('sdiv' if sw[0] else 'udiv') or (sw[1] == 8 and rn != hreg) or not helper \
                or any(first <= o < fex[0] and ('x', dreg) in writes(dis[o]) for o in order):
            out.append(f'{name} at guest +{g}: its #DE checks do not read the operands of its divide "{dis[inline[0]]}"')
        # The dividend is RDX:RAX (x5:x4), unchanged from the instruction start to FEX's divide; the 8-bit forms read
        # FEX's copy of AX (`uxth wH, w4` / `sxth xH, w4`), unchanged up to the divide.
        last = max(fex[0], inline[0])
        if sw[1] == 8:
            hw = [o for o in order if lo <= o < first and ('x', hreg) in writes(dis[o])]
            ax = f'sxth x{hreg}, w4' if sw[0] else f'uxth w{hreg}, w4'
            if not hw or dis[hw[-1]] != ax or any(first <= o < inline[0] and ('x', hreg) in writes(dis[o]) for o in order):
                out.append(f'{name} at guest +{g}: its 8-bit #DE checks do not read FEX\'s copy of AX ({ax})')
        if any(lo <= o < last and writes(dis[o]) & {('x', 4), ('x', 5)} for o in order):
            out.append(f'{name} at guest +{g}: RDX:RAX written before its divide')
        # 64 bits: FEX's selector of its inline divide is exactly `cbz x5` (div) or `asr x0, x4, #63; eor x0, x0, x5;
        # cbz x0` (idiv), right after the checks, to that divide.
        if sw[1] == 64:
            after = by['overflow' if sw[0] else 'zero'][1] + 4
            sel, test = (['asr x0, x4, #63', 'eor x0, x0, x5'], 'cbz x0') if sw[0] else ([], 'cbz x5')
            c = after + 4 * len(sel)
            near = re.fullmatch(rf'{test}, 0x([0-9a-f]+)', dis.get(c, ''))
            inv = INVERSE[test.split()[0]] + ' ' + test.split()[1]
            far = dis.get(c) == f'{inv}, {c + 8:#x}' and re.fullmatch(r'b 0x([0-9a-f]+)', dis.get(c + 4, ''))
            target = int((near or far)[1], 16) if (near or far) else None
            if [dis.get(after + 4 * k) for k in range(len(sel))] != sel or target != inline[0]:
                out.append(f'{name} at guest +{g}: FEX\'s 64-bit fast-path selector is not exact')
    return out, ok


# G1 (I1): the guest-state RFLAGS bytes at x28 + 0x3f0 + bit (crates/abi/src/execution.rs `frame`): readable TF, IF,
# DF, IOPL, NT, RF, VM, AC, VIF, VIP, ID; writable DF only.
GUEST_STATE_FLAGS = {0x3f0 + b for b in (8, 9, 10, 12, 14, 16, 17, 18, 19, 20, 21)}
GUEST_STATE_DF = {0x3f0 + 10}
# I7: FEX's gs_cached (0x3e0) and fs_cached (0x3e8), 8 bytes each: loads only (an FS-/GS-prefixed access adds the base).
GUEST_STATE_BASES = ((0x3e0, 8), (0x3e8, 8))


def entry_and_branches(d, dis, order):
    """P12 and P13 on one compile."""
    out = []
    le = d['code']['layout']['entries'][0]
    if (le['entry_native_offset'], le['body_native_offset']) != (4, 4) or d['code']['layout']['spill_bytes']:
        out.append(f'entry prologue is [{le["entry_native_offset"]}, {le["body_native_offset"]}) with {d["code"]["layout"]["spill_bytes"]} spill bytes, not empty at 4 (a stop check, spill or TF check in the prologue)')
    for off in order:
        acc = check_sites.native_access(dis[off])
        mn = dis[off].split(' ')[0]
        guest_state = acc and acc[2] == 'x28' and acc[1] == 1 and acc[3] is None and acc[5] == 'none' and \
            acc[4] in (GUEST_STATE_DF if acc[0] == 'store' else GUEST_STATE_FLAGS)
        guest_state = guest_state or (acc and acc[2] == 'x28' and acc[0] == 'load' and acc[3] is None and acc[5] == 'none' and
                                      any(f <= acc[4] and acc[4] + acc[1] <= f + n for f, n in GUEST_STATE_BASES))
        if acc and acc[2] in ('x28', 'sp') and not guest_state:
            out.append(f'+{off} "{dis[off]}" accesses emulator data ({acc[2]})')
        if re.fullmatch(r'ldr\w* \w+, 0x[0-9a-f]+', dis[off]):
            out.append(f'+{off} "{dis[off]}" is a literal load')
        if mn in ('bl', 'blr', 'br', 'ret'):
            out.append(f'+{off} "{dis[off]}" is a call, return or indirect branch')
        if mn in ('b', 'cbz', 'cbnz', 'tbz', 'tbnz') or mn.startswith('b.'):
            t = int(re.findall(r'0x([0-9a-f]+)$', dis[off])[0], 16)
            dispatch = mn == 'b' and t == off and any(r['kind'] == 'dispatch_branch' and r['offset'] == off for r in d['code']['relocations'])
            if (t <= off or t not in dis) and not dispatch:
                out.append(f'+{off} "{dis[off]}" is not a forward branch to an instruction of the block')
    return out


def verify(d, tmp, insns=None):
    """Violations of P1-P10 for one compile result (P6 needs the generator's annotations `insns`), and the check cost."""
    out = []
    code = bytes.fromhex(d['code']['hex'])
    end = d['code']['layout']['code_region_end']
    lits = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['size']]
    dis = check_sites.disassemble(code, end, tmp, lits)
    order = sorted(dis)
    sabi = d['state_abi']
    sra = {('x', n) for n in sabi['static_registers_gpr_order_rax_rcx_rdx_rbx_rsp_rbp_rsi_rdi_r8_to_r15_then_pf_af']}
    sra |= {('v', n) for n in sabi['static_registers_xmm0_up']}
    pre = sabi['state_offsets'].get('natemu_pre_nzcv_register')
    entries = [(e['native_offset'], e['guest_offset']) for e in d['pc_table']['entries']]
    name = lambda r: ('x%d' if r[0] == 'x' else 'v%d') % r[1]
    gout, check_at, cost_ = guards(d, dis, order)   # P8-P10; check_at: stub -> the branch that reaches it
    out += gout
    out += entry_and_branches(d, dis, order)   # P12, P13
    by_insn = {}
    for s in d['sites']:
        g, at, start = s['guest_offset'], s['native_offset'], s['insn_native_offset']
        tag = f'site +{at} ({s["kind"]}, guest +{g})'
        if s['kind'] in STUBS:   # the state at a stub is the state at its check branch
            if at not in check_at:
                continue   # reported by guards()
            at = check_at[at]
        else:
            by_insn.setdefault(g, []).append(s)
        if (start, g) not in entries:
            out.append(f'{tag}: instruction start +{start} is not a PC-table entry of guest +{g}')
        if any(start < n <= at and og != g for n, og in entries):
            out.append(f'{tag}: another guest instruction starts between +{start} and the site')
        if s['completed_effects'] != 'none':
            out.append(f'{tag}: completed_effects={s["completed_effects"]}')
        if s['kind'] in ('push_pair', 'pop_pair', 'other'):
            out.append(f'{tag}: kind {s["kind"]} (fused or undescribed access)')
        saved, first_nzcv = None, None
        for off in (o for o in order if start <= o < at):
            text = dis[off]
            mn, dests, nzcv, acc, known = effects(text)
            if not known:
                out.append(f'{tag}: unclassified instruction +{off} "{text}"')
            if off == start and pre is not None and text.lower() == f'mrs x{pre}, nzcv':
                saved = off   # the pre-NZCV save, the window's first word
            elif pre is not None and ('x', pre) in dests:
                out.append(f'{tag}: +{off} "{text}" writes x{pre}, the pre-NZCV register, before the site')
            if nzcv and first_nzcv is None:
                first_nzcv = off
            if acc and acc[0] == 'store':
                out.append(f'{tag}: store +{off} "{text}" before the site')
            bad = dests & sra
            if bad:
                out.append(f'{tag}: +{off} "{text}" writes static register {",".join(sorted(map(name, bad)))} before the site')
        if first_nzcv is not None and s['nzcv'] != 'saved':
            out.append(f'{tag}: NZCV written at +{first_nzcv} "{dis[first_nzcv]}" before the site, nzcv={s["nzcv"]}')
        if s['nzcv'] == 'saved' and saved is None:
            out.append(f'{tag}: nzcv=saved but no pre-NZCV save at the instruction start')
        acc = check_sites.native_access(dis.get(at, ''))   # P3 from the disassembly at the site, not from fexc's data_regs
        if acc and acc[0] == 'load' and s['kind'] not in STUBS:
            bad = loaded_regs(dis[at]) & sra
            if bad:
                out.append(f'{tag}: load destination {",".join(sorted(map(name, bad)))} of "{dis[at]}" is a static register')
    for g, ss in by_insn.items():
        stores = [s for s in ss if s['kind'] in ('store', 'store_pair', 'push', 'push_pair')]
        if len(stores) > 1:
            out.append(f'guest +{g}: {len(stores)} store sites')
        if stores and stores[-1]['native_offset'] != max(s['native_offset'] for s in ss):
            out.append(f'guest +{g}: the store site +{stores[-1]["native_offset"]} is not the last access of the instruction')
    out += code_vs_sites(d['sites'], dis)   # P7, code half, on every compile: no unrecorded or misdescribed access
    out += [f'skippable site: {s}' for s in scan_skips.skips(d, tmp)]
    if insns:
        out += stale_flags(d, insns, dis, order)
        out += df_stores(d, insns, dis, order)
    return out, cost_


def cost(d, tmp):
    """(native instructions, code bytes) of the whole code region; literals are not instructions."""
    end = d['code']['layout']['code_region_end']
    lits = sum(r['size'] for r in d['code']['relocations'] if r['size'])
    return (end - lits) // 4, end


def compile_(fexc, hexs, rip):
    r = subprocess.run([fexc, '--profile', 'slice1', '--hex', hexs, '--rip', rip], capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


ADVERSARIAL = {   # one or more per precision patch; registers point into the generator's data/stack pages (not executed)
    'pooled-constant-store-then-gpr': ['mov qword ptr [rbx], 0x12345', 'mov rax, 0x12345'],
    'pooled-constant-twice': ['mov qword ptr [rbx], 0x12345', 'mov qword ptr [rsi], 0x12345', 'mov rax, 0x12345'],
    'cmp-mem-dead-flags': ['cmp qword ptr [rbx], 5', 'add rax, rcx'],
    'test-bt-mem-dead-flags': ['test dword ptr [rsi], 1', 'sub rax, rax', 'bt qword ptr [rdi], 3', 'xor eax, eax'],
    'push-pop-pairs': ['push rax', 'push rbx', 'pop rcx', 'pop rdx', 'push 7', 'push rsp', 'pop rsp'],
    'rmw-add-carry-in-out': ['sub rax, rcx', 'add qword ptr [rbx], rcx', 'adc rdx, 0'],
    'rmw-forms': ['add byte ptr [rbx], 1', 'and qword ptr [rbx], rcx', 'inc qword ptr [rsi]', 'neg dword ptr [rdi]',
                  'sub rax, rdx', 'sbb qword ptr [rbx], rax', 'shl qword ptr [rbx], cl', 'btc qword ptr [rdi], rax', 'not word ptr [rsi]'],
    'flags-before-store-kept': ['add rax, rcx', 'mov qword ptr [rbx], rdx', 'sub rax, rcx'],
    'load-destinations': ['mov rax, qword ptr [rbx]', 'mov ecx, dword ptr [rsi]', 'movzx edx, byte ptr [rdi]', 'pop r8',
                          'cmovb r9, qword ptr [rbx]', 'imul r10, qword ptr [rsi]'],
    'memory-stack-forms': ['pop qword ptr [rbx]', 'push qword ptr [rsi]', 'setb byte ptr [rbx]', 'xchg rax, rcx', 'mov qword ptr [rdi], rax'],
    'killed-by-undefined-then-access': ['add rdx, 5', 'or rsi, rdi', 'mov rax, qword ptr [rbx]', 'sub rax, rcx', 'imul rdx, rcx',
                                        'mov qword ptr [rsi], rax', 'add rax, 1', 'mul qword ptr [rdi]'],
    'undefined-flags-before-access': ['add rax, rcx', 'shl qword ptr [rbx], 3', 'mul qword ptr [rsi]', 'imul rax, qword ptr [rbx]'],
    'division-flags': ['sub rax, rcx', 'div qword ptr [rbx]', 'mov qword ptr [rsi], rdx', 'add r8, 1', 'idiv ecx', 'mov rax, qword ptr [rdi]',
                       'cmp r9, r10', 'div r11b', 'adc qword ptr [rsi], 1'],
}


GUARDED = {   # P8-P10: name -> (rip, asm, compare with the fixture rules); the branch forms end the block
    'address-forms': ('0x100000', ['mov rax, qword ptr [rbx+rcx*8+0x10]', 'mov qword ptr [rbx+0x12345], rax', 'mov ecx, dword ptr [rsi-8]',
                                   'mov word ptr [rdi+rax*2], cx', 'mov byte ptr [rbx], al', 'add dword ptr [rbx+rcx*4], eax',
                                   'inc qword ptr [rsi+0x7fffffff]', 'movzx eax, word ptr [rsp+6]', 'mov qword ptr [rbp-0x10], rax'], True),
    'rip-relative-rmw-crossing': ('0x100000', ['add qword ptr [rip+0xff5], rax'], True),   # 0x100ffc..0x101003
    'rmw-split-forms': ('0x100000', ['add qword ptr [rbx+0xffc], rax', 'inc dword ptr [rsi+rcx*4]', 'not word ptr [rdi-1]',
                                     'shl qword ptr [rbx], cl', 'btc qword ptr [rdi], rax', 'add byte ptr [rbx], 1'], True),
    'rip-relative': ('0x100000', ['mov rax, qword ptr [rip+0x100]', 'mov qword ptr [rip+0xff1], rax', 'mov dword ptr [rip+0x40], 1',
                                  'add qword ptr [rip+0xfe0], rax', 'bt qword ptr [rip+0x40], rax'], True),
    'rip-relative-noncanonical': ('0x7ffffffff000', ['mov rax, qword ptr [rip+0x7fffffff]', 'mov qword ptr [rip+0x7ffffff0], rax'], True),
    'large-displacements': ('0x100000', ['mov rax, qword ptr [rbx+0x1000]', 'neg qword ptr [rsp+rbp*8+0x2000]',
                                         'mov qword ptr [rsi-0x1001], rcx', 'add dword ptr [rdi+0x7ffff000], eax'], True),
    'stack-forms': ('0x100000', ['push rax', 'push rsp', 'pop qword ptr [rbx]', 'push qword ptr [rsp+8]', 'pop rsp', 'push 7'], True),
    'division-forms': ('0x100000', ['div cl', 'div byte ptr [rbx]', 'idiv bh', 'div word ptr [rsi+rcx*2]', 'idiv r9w', 'div ecx',
                                    'idiv dword ptr [rsp+8]', 'div rcx', 'idiv qword ptr [rbx+0x10]', 'div qword ptr [rip+0x100]', 'idiv r15',
                                    'idiv qword ptr [rbp-8]'], True),
    'ret': ('0x100000', ['add rax, rcx', 'ret'], False),
    'ret-imm': ('0x100000', ['ret 8'], False),
    'call-reg': ('0x100000', ['call rax'], False),
    'call-rsp': ('0x100000', ['call rsp'], False),   # with RSP % 4096 in 1..7 the push splits: no base copy may sit between check and store
    'push-base-forms': ('0x100000', ['push rsp', 'push qword ptr [rsp]', 'push qword ptr [rsp+8]', 'call qword ptr [rsp+8]'], False),
    'call-mem': ('0x100000', ['call qword ptr [rax+8]'], False),
    'call-mem-rsp': ('0x100000', ['call qword ptr [rsp]'], False),
    'jmp-reg': ('0x100000', ['mov rax, 0x123456789', 'jmp rax'], False),
    'jmp-mem': ('0x100000', ['jmp qword ptr [rip+0x100]'], False),
    'jmp-mem-index': ('0x100000', ['jmp qword ptr [rbx+rcx*8]'], False),
    'jcc': ('0x100000', ['cmp rax, rcx', '.byte 0x75, 0x40'], False),   # jne +0x40
    # I7: FS/GS overrides; the base is a register load from the guest-state page added into the address, so the split
    # check and the site see the linear address; an RSP/RBP base under FS/GS is FS/GS (#GP), not SS.
    'segment-forms': ('0x100000', ['mov rax, qword ptr gs:[rbx+8]', 'mov qword ptr fs:[rsi+rcx*4+0xffc], rax',
                                   'add dword ptr gs:[rsp+16], 1', 'mov rcx, qword ptr fs:[rbp-8]', 'mov rax, qword ptr gs:[0x28]',
                                   'mov qword ptr fs:[rip+0xff5], rax', 'add qword ptr gs:[rip+0x100], rcx', 'push qword ptr fs:[rbx]',
                                   'pop qword ptr gs:[rsp+8]', 'xchg qword ptr fs:[rdi], rdx', 'cmpxchg qword ptr gs:[rbx], rcx',
                                   'xadd dword ptr fs:[rsi], eax', 'btc qword ptr gs:[rdi], rax', 'shld qword ptr fs:[rbx], rcx, 5',
                                   'movnti qword ptr gs:[rsi], rcx', 'div qword ptr fs:[rbx+0x10]', 'andn rax, rcx, qword ptr gs:[rbx]',
                                   'lea rax, fs:[rbx+8]', 'movabs rax, qword ptr gs:[0x1122334455667788]'], True),
    'segment-call-mem': ('0x100000', ['call qword ptr gs:[rax+8]'], False),
    'segment-jmp-mem-rip': ('0x100000', ['jmp qword ptr fs:[rip+0x100]'], False),
}


def adversarial():
    import ref, gen_slice1
    progs = []
    for name, asm in ADVERSARIAL.items():
        progs.append({'name': name, 'code': ref.assemble('\n'.join(asm)).hex(), 'rip': '0x100000', 'asm': asm,
                      'insns': [gen_slice1.annotate(a, 0 if a.endswith(', cl') else None) | ({'count': None} if a.endswith(', cl') else {})
                                for a in asm]})
    for name, (rip, asm, fixture_rules) in GUARDED.items():
        progs.append({'name': name, 'code': ref.assemble('\n'.join(asm)).hex(), 'rip': rip} | ({'asm': asm} if fixture_rules else {}))
    return progs


def run_corpus(label, progs, tmp, before=None):
    """Verify every program; returns (violations, compiled, totals)."""
    bad, ok, sites = 0, 0, 0
    tot = {'g': 0, 'i': 0, 'b': 0, 'g0': 0, 'i0': 0, 'b0': 0, 'guard': 0, 'split': 0, 'branch': 0, 'stub': 0}
    for p in progs:
        d = compile_(FEXC, p['code'], p['rip'])
        if d['status'] != 'ok':
            continue
        ok += 1
        sites += len(d['sites'])
        problems, c = verify(d, tmp, p.get('insns'))
        for k, v in c.items():
            tot[k] += v
        if 'asm' in p:
            check_sites.FEXC, check_sites.PROFILE = FEXC, ['--profile', 'slice1']
            cp, _ = check_sites.check_program(p, tmp)
            problems += [f'{k}: {m}' for k, m in cp if k != 'NARROWED' and m not in problems]   # code_vs_sites already ran in verify()
        for m in problems:
            print(f'  {label} {p["name"]}: {m}')
        bad += bool(problems)
        g = d['guest']['instructions_translated']
        i, b = cost(d, tmp)
        tot['g'] += g; tot['i'] += i; tot['b'] += b
        if before:
            d0 = compile_(before, p['code'], p['rip'])
            if d0['status'] == 'ok' and d0['guest']['instructions_translated'] == g:
                i0, b0 = cost(d0, tmp)
                tot['g0'] += g; tot['i0'] += i0; tot['b0'] += b0
    line = f'{label}: {len(progs)} programs, {ok} compiled, {sites} sites, {bad} with violations'
    if tot['g']:
        line += f'; per guest insn {tot["i"] / tot["g"]:.2f} native insns, {tot["b"] / tot["g"]:.1f} code bytes'
    if before and tot['g0']:
        line += f' (before: {tot["i0"] / tot["g0"]:.2f} insns, {tot["b0"] / tot["g0"]:.1f} bytes over {tot["g0"]} guest insns)'
    if tot['g']:
        line += '; checks per guest insn: ' + ', '.join(f'{k} {tot[k] / tot["g"]:.2f}' for k in ('guard', 'split', 'branch', 'stub'))
    print(line)
    return bad


def main():
    global FEXC
    args = sys.argv[1:]
    before = None
    if '--before' in args:
        k = args.index('--before'); before = args[k + 1]; del args[k:k + 2]
    scan_skips.FEXC = FEXC
    fixtures = check_sites.fixture_programs()
    matrix = [{'name': n, 'code': h, 'rip': '0x102000'} for n, h in scan_skips.matrix().items()]
    # I7: every memory form of the matrix again with an FS and a GS override (implicit-stack forms are cut: no operand).
    matrix += [{'name': f'{seg} {n}', 'code': pre + h, 'rip': '0x102000'} for n, h in scan_skips.matrix().items() if '[' in n
               for seg, pre in (('fs', '64'), ('gs', '65'))]
    corpora = [('fixtures', fixtures), ('matrix', matrix), ('adversarial', adversarial())]
    for a in args:
        corpora.append((Path(a).name, json.loads(Path(a).read_text())['programs']))
    bad = 0
    with tempfile.TemporaryDirectory() as t:
        for label, progs in corpora:
            bad += run_corpus(label, progs, Path(t), before if label != 'matrix' else None)
    print('PASS' if not bad else f'FAIL: {bad} programs with violations')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
