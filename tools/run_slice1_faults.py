#!/usr/bin/env python3
"""Precise guest faults in the VM against QEMU prefix runs (slice 1, S1e).

    tools/with-env python3 tools/run_slice1_faults.py --fexc FEXC --out DIR [--max-block N] [--no-paging] [PROGRAMS.json ...]

Cases are derived from program descriptions, never written per program:

* Page mutations (default inputs: fixtures/slice1/programs.json; any corpus with `asm` works). For every program,
  every data page an access touches, and each mutation (`unmap`: clear P; `readonly`: clear R/W), the expected
  fault is the first access in program order the mutation affects (readonly: the first write-intent access).
  Effective addresses come from the generator's `asm` text evaluated in QEMU's prefix state at that instruction
  (tools/reference/ref.py `prefix`), never from fexc.
* Paging cells (S3a2; `--no-paging` leaves them out): the controls CPL 3/0, CR0.WP, CR4.SMEP (also with supervisor
  code pages), EFER.NXE (CONTROLS) crossed with a mutation (`unmap`: P; `readonly`: R/W; `supervisor`: U/S; `reserved`:
  bit 51, PS in a PML4E/PDPTE or bit 13 of a 2 MiB leaf; `np-garbage`: every bit but P; `noexec`: XD) of the entry at
  level 1-4 of a touched data page's walk or of the code page's, with that page's 2 MiB range one PDE leaf or not.
  Each corpus program takes the next cells of one rotation per target (data/code), so the corpus covers every
  combination. The access-form programs (`paging_programs`: DATA_FORMS, pop/ret reads and page-crossing accesses) take,
  for each page they touch, every control x mutation of its leaf, then the next cells of a rotation over the upper levels
  and 2 MiB leaves, at three placements (`PLACEMENTS`; QEMU runs the program low and its results move with it).
  In each, the code, the data/stack pages and the store base `rdi` (`SCR`, a page of its own) sit under three different
  PML4 entries, so a mutated entry of the page under test never takes the prelude's store or the fetch first: all in
  the lower half; all in the upper canonical half (the pointer registers move with the pages); and the code alone in
  the upper half with lower-half operands. Every such program must fault at its own access in at least one cell
  (`access_cells`, checked before any VM run). A cell without a fault expects the program's QEMU full run. A fetch
  fault at the first instruction must come before any compile.
  The reference runs are QEMU user-mode, i.e. CPL 3; the CPL 0 cells compare the VM against them, which is valid only
  while the corpus has no popf or other privileged or CPL-dependent instruction (pushf, admitted since I1, pushes the
  same image at both: IF = 1, IOPL = 0, AC = 0).
* I1: DATA_FORMS also holds the I1 memory forms (shld/shrd, cmpxchg and xadd without LOCK, popcnt/tzcnt, BMI loads,
  movnti, pushf, enter, leave) and a prefetch hint, which never faults (SDM PREFETCHh): its window and non-canonical
  probes expect the QEMU full run (`NOFAULT`). shld/shrd with a memory destination and a count masking to 0 is a
  write-intent read-modify-write like a shift by 0 (#PF W on a read-only page): the SDM gives the value ("no
  operation") but not the access; the pinned QEMU faults with error 7 for shld/shrd by 0, 64 (64-bit), 32 (32-bit) and
  CL = 0 alike (October 28), and fexc series14 writes the value back. The guest-state page (G1) is a documented
  deviation: `guest_state_probes`. Any corpus of `tools/reference/gen_i1.py` works as input too.
* I7: an FS/GS-overridden operand's address is the program's `fsbase`/`gsbase` + EA (mod 2^64) and it is never a stack
  reference (#GP(0), not #SS(0), when non-canonical, whatever its base register); `segment_probes` cover every memory form
  at a far, a negative and a wrapping base with a not-present page (CR2 = base + EA), a page-crossing access, a
  non-canonical address, a canonical straddle and the private window, and indirect branches through FS/GS operands to a
  non-canonical target. Any corpus of `tools/reference/gen_i7.py` works as input (its page mutations fault at base + EA).
  CS/DS/ES/SS prefixes take Intel's class (fexc series27; SDM Vol. 3A 4.5.1): SS-class with an SS prefix or an RSP/RBP
  base and no prefix, else DS-class; `null_segment_probes` put CS/DS/ES on RSP/RBP bases and SS on others.
* I3: DATA_FORMS also holds LOCK forms (one atomic each, write intent: a read-only page faults W even when LOCK CMPXCHG's
  comparison fails, the SDM's write cycle; LOCK NEG a load then a compare-and-swap; LOCK BTS FEX's byte). CMPXCHG8B/16B are
  probes only (the pinned QEMU's full runs keep stale flags after them; the prefix state is right): not-present and
  read-only pages, CMPXCHG16B not 16-byte aligned (#GP(0) before the page walk, QEMU's order), split locks (every LOCK
  form and CMPXCHG8B, locked or not, crossing 16 bytes, a line or a page: `unsupported_atomic` with the QEMU prefix state),
  and LOCK on forms that are not lockable (#UD, nothing executed: a register destination, a non-lockable opcode, a system
  instruction). Any corpus of `tools/reference/gen_i3.py` works as input (bt* with a register bit offset is skipped).
* Built-in probes (PROBES below): non-canonical data and stack addresses (#GP(0)/#SS(0)), stack accesses across the
  canonical gap (#SS(0)), E3's negative cases (`window_probes`; `--no-window` leaves them out): every class of the
  former private window (frame, state, tables, stacks, staging, resident text, shadow pool, guest-RAM alias, code area,
  dispatcher, code-write alias, metadata, lookup, global), both holes and poison values, each with every data form,
  page-crossing stores and pushes, and a call's push, plus both canonical straddles (first byte canonical and last not,
  and the reverse) with loads, stores, RMW, XCHG and stack forms, and RIP-relative accesses into the low hole and
  across into it: all fault in hardware at their site (no address guard), branches to non-canonical targets (#GP(0) at the branch), instruction fetch from a
  not-present code page, and divide errors (#DE: zero divisor and quotient overflow per width and signedness,
  register and memory divisors, a not-present divisor page before #DE, 64-bit divisions whose dividend needs 128 bits
  after a run of such divisions that do not fault).

The expected exception, from the instruction text and the prefix state alone, follows x86 and the contract's natemu
policies: per access in order, a non-canonical access (any byte) is #SS(0) for an RSP/RBP base or stack operation,
else #GP(0); then each page it touches in address order, #PF with CR2 = the first byte of the access in that page,
error code U | W (write intent; a read-modify-write's load is write intent) | P (protection) | RSVD. A branch target is
checked after the accesses that produce it and before a call's push. div/idiv: after its memory divisor is read, #DE
(no error code) when the divisor is zero or the quotient does not fit the destination (SDM DIV/IDIV), computed from the
prefix state. Fetch #PF: CR2 = the first instruction byte in the faulting page, error U | I (with NXE or SMEP) | P | RSVD.
bt-family memory forms access one byte (natemu policy, tools/fexc/README.md). Instruction fetch is checked before the
instruction's data accesses. Page-walk outcomes and error codes follow SDM vol. 3 ch. 4 (`Machine`); A/D: natemu's
walker policy (`Machine`), leaf A/D bits compared per page (data exactly; code pages A where fetched, else either).

The VM must stop with exactly that exception record (vector, CR2, error code, RIP = start of the faulting instruction),
delivered through the driver's logging IDT since X1a (the state is the one its frame saved; the frame, handler RSP and
CPL are judged by tools/models/delivery_model.py, `delivery_check`),
or reach the program's end when nothing faults, its GPRs, masked RFLAGS and memory equal to the QEMU prefix (or full)
run (nothing later ran, no partial store),
and the FEX frame intact. A stop at a declared slice limit before the expected fault (unsupported_form)
is "unsupported". Writes DIR/faults.json; exit status 1 on any failure."""
import argparse, json, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
import ref, gen_slice1 as gen, gen_i1, run_slice1_vm as vm, delivery_check  # noqa: E402

M64, PAGE = 2**64 - 1, 4096
SIZES = {'byte': 1, 'word': 2, 'dword': 4, 'qword': 8, 'xmmword': 16}
STACK_BASES = {'rsp', 'rbp'}

def canonical(a): return a >> 47 in (0, 0x1ffff)

def split_operands(text):
    rest = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ''
    ops, depth, cur = [], 0, ''
    for c in rest:
        depth += (c == '[') - (c == ']')
        if c == ',' and depth == 0: ops.append(cur.strip()); cur = ''
        else: cur += c
    return [o for o in ops + [cur.strip()] if o]

MEM = re.compile(r'(?:(byte|word|dword|qword|xmmword) ptr )?(?:([cdefgs]s):)?\[(.*)\]')

def address(expr, regs, next_rip):
    """Intel address expression `base + index*scale + disp` (64-bit registers, rip) -> (address, base register)."""
    total, base = 0, None
    for sign, term in re.findall(r'([+-]?)\s*([^+-]+)', expr.replace(' ', '')):
        if '*' in term: r, s = term.split('*'); v = regs[r] * int(s)
        elif term == 'rip': v = next_rip
        elif term in ref.R64: v = regs[term]; base = base or term
        else: v = int(term, 0)
        total += -v if sign == '-' else v
    return total & M64, base

class Unsupported(Exception): pass

def reg_value(name, regs):
    """Value of a 64/32/16/8-bit register name (incl. ah..bh) from the 64-bit registers."""
    if name in ref.HI: return (regs[ref.R64[ref.HI.index(name)]] >> 8) & 0xff
    for names in (ref.R64, ref.R32, ref.R16, ref.R8):
        if name in names: return regs[ref.R64[names.index(name)]] & ((1 << ref.WIDTH[name]) - 1)
    raise Unsupported(name)

def divide_error(signed, w, divisor, rax, rdx):
    """x86 #DE of a w-byte div (unsigned) / idiv (signed): zero divisor or a quotient outside the destination."""
    bits = 8 * w; mask = (1 << bits) - 1
    n = rax & 0xffff if w == 1 else (rdx & mask) << bits | rax & mask
    d = divisor & mask
    if d == 0: return True
    if not signed: return n // d > mask
    n -= (n >> (2 * bits - 1)) << (2 * bits); d -= (d >> (bits - 1)) << bits
    q = abs(n) // abs(d) * (1 if (n < 0) == (d < 0) else -1)
    return not -(1 << (bits - 1)) <= q < 1 << (bits - 1)

STRING = re.compile(r'(?:(rep|repe|repne)\s+)?(movs|stos|lods|scas|cmps)([bwdq]?)\b(.*)')

def string_events(text, regs):
    """I2: the accesses of ONE element of a string instruction at the registers `regs` (the element they point at), or None
    if `text` is no string instruction. In architectural order: movs reads [RSI] then writes [RDI], cmps reads [RDI] then
    [RSI] (QEMU's and fexc's order; the SDM gives none); the DS:RSI source adds an FS/GS base (`regs['fs']`/`regs['gs']`),
    ES:RDI takes no override; a REP form with RCX = 0 accesses nothing. Never a stack reference (#GP(0) when non-canonical)."""
    x = STRING.fullmatch(text.strip())
    if not x: return None
    rep, op, suffix, rest = x.groups()
    w = {'b': 1, 'w': 2, 'd': 4, 'q': 8}.get(suffix) or SIZES[re.search(r'(byte|word|dword|qword) ptr', rest)[1]]
    if rep and regs['rcx'] == 0: return []
    seg = re.search(r'([fg]s):\[rsi\]', rest)
    src, dst = (regs['rsi'] + (regs.get(seg[1], 0) if seg else 0)) & M64, regs['rdi']
    order = {'movs': [(src, False), (dst, True)], 'stos': [(dst, True)], 'lods': [(src, False)], 'scas': [(dst, False)],
             'cmps': [(dst, False), (src, False)]}[op]
    return [('access', a, w, write, False) for a, write in order]

# I3 (SDM Vol.2 "LOCK"): the forms LOCK may prefix, each with a memory destination (xchg: either operand).
LOCKABLE = {'add', 'adc', 'and', 'btc', 'btr', 'bts', 'cmpxchg', 'cmpxchg8b', 'cmpxchg16b', 'dec', 'inc', 'neg', 'not', 'or', 'sbb',
            'sub', 'xor', 'xadd', 'xchg'}

def events(text, regs, next_rip):
    """One instruction's guest data accesses and branch-target check, in architectural order:
    ('access', address, bytes, write intent, stack segment), ('target', register value | ('mem', address)) and
    ('divide', signed, bytes, divisor value | ('mem', address), rax, rdx); I3: ('ud',) for LOCK on a form that is not
    lockable (#UD before anything), ('align',) for CMPXCHG16B on an operand not 16-byte aligned (#GP(0) before any access)."""
    if text.startswith('lock '):
        text = text[5:]
        m, ops = text.split()[0], split_operands(text)
        if m not in LOCKABLE or not any(MEM.fullmatch(o) for o in (ops if m == 'xchg' else ops[:1])): return [('ud',)]
    m, ops = text.split()[0], split_operands(text)
    if m == 'cmpxchg16b' and MEM.fullmatch(ops[0]):
        x = MEM.fullmatch(ops[0]); a, _ = address(x[3], regs, next_rip)
        if x[2] in ('fs', 'gs'): a = (a + regs.get(x[2], 0)) & M64
        if a & 15: return [('align',)]
    # I2: a single string instruction is one element; a REP form's later elements depend on the earlier ones (counted
    # per element by tools/run_slice2_faults.py from QEMU's signal context), so it is never guessed here.
    if (one := string_events(text, regs)) is not None:
        if STRING.fullmatch(text.strip())[1]: raise Unsupported(text)
        return one
    def mem(op):
        # I7: an FS/GS override adds that segment's base (`regs['fs']`/`regs['gs']`, from the program) and makes the
        # reference an FS/GS one (#GP(0) when non-canonical, whatever the base register); CS/DS/ES/SS add nothing but the
        # class (Intel SDM Vol. 3A 4.5.1): SS-class (#SS(0)) with an SS override or an RSP/RBP base and no override.
        x = MEM.fullmatch(op)
        if not x: return None
        a, base = address(x[3], regs, next_rip)
        if x[2] in ('fs', 'gs'): return (a + regs.get(x[2], 0)) & M64, SIZES[x[1]] if x[1] else None, False
        return a, SIZES[x[1]] if x[1] else None, x[2] == 'ss' or (not x[2] and base in STACK_BASES)
    def reg_width(op, default=8): return ref.WIDTH[op] // 8 if op in ref.WIDTH else default
    rsp = regs['rsp']
    def operand_width(op):     # push/pop operand: memory with its size (qword or word), r64/r16, or an immediate (8)
        x = mem(op)
        w = (x[1] if x else ref.WIDTH.get(op, 64) // 8 if op in ref.WIDTH else 8 if m == 'push' else None)
        if w not in (2, 8): raise Unsupported(text)
        return w
    if m == 'push' and len(ops) == 1:
        x, w = mem(ops[0]), operand_width(ops[0])
        return ([('access', x[0], w, False, x[2])] if x else []) + [('access', (rsp - w) & M64, w, True, True)]
    if m == 'pop' and len(ops) == 1:
        w, out = operand_width(ops[0]), [('access', rsp, operand_width(ops[0]), False, True)]
        if MEM.fullmatch(ops[0]):     # the destination address uses RSP after the increment
            x = MEM.fullmatch(ops[0]); a, base = address(x[3], dict(regs, rsp=(rsp + w) & M64), next_rip)
            seg = x[2] in ('fs', 'gs')
            out.append(('access', (a + regs.get(x[2], 0)) & M64 if seg else a, w, True, x[2] == 'ss' or (not x[2] and base in STACK_BASES)))
        return out
    # I1: pushf and enter (nesting level 0) push one qword; leave reads [RBP] (a stack access; RSP and RBP change after it);
    # a prefetch hint never faults (SDM PREFETCHh: no exceptions but #UD for LOCK), whatever its address.
    if m in ('pushfq', 'enter') and (m == 'pushfq' or len(ops) == 2 and int(ops[1], 0) == 0):
        return [('access', (rsp - 8) & M64, 8, True, True)]
    if m == 'leave' and not ops: return [('access', regs['rbp'], 8, False, True)]
    if m.startswith('prefetch'): return []
    if m == 'ret' and (not ops or len(ops) == 1 and re.fullmatch(r'\d+|0x[0-9a-f]+', ops[0])):    # ret / ret imm16
        return [('access', rsp, 8, False, True), ('target', ('mem', rsp))]
    if m in ('jmp', 'call') and len(ops) == 1 and (mem(ops[0]) or ops[0] in ref.R64):
        x = mem(ops[0])
        if x and x[1] not in (None, 8): raise Unsupported(text)
        out = [('access', x[0], 8, False, x[2]), ('target', ('mem', x[0]))] if x else [('target', regs[ops[0]])]
        return out + ([('access', (rsp - 8) & M64, 8, True, True)] if m == 'call' else [])     # the push follows the check
    # Anything else that transfers control (jcc, direct jmp/call, loop, ...) is not straight-line: never guessed.
    if m in ('jmp', 'call', 'ret', 'push', 'pop', 'enter', 'pushf', 'popf', 'popfq') or m.startswith('j') or m.startswith('loop') or m in ('iret', 'iretq', 'int', 'syscall'):
        raise Unsupported(text)
    if m in ('div', 'idiv') and len(ops) == 1:      # the divisor is read, then checked
        x = mem(ops[0])
        if x and not x[1]: raise Unsupported(text)
        w = x[1] if x else reg_width(ops[0])
        return ([('access', x[0], w, False, x[2])] if x else []) + \
            [('divide', m == 'idiv', w, ('mem', x[0]) if x else reg_value(ops[0], regs), regs['rax'], regs['rdx'])]
    mems = [i for i, o in enumerate(ops) if MEM.fullmatch(o)]
    if m in ('lea', 'nop') or not mems: return []
    i = mems[0]; a, w, ss = mem(ops[i])
    w = w or reg_width(ops[1 - i] if len(ops) > 1 else '', 0)
    if not w: raise Unsupported(text)
    if m in gen.flags.BITTEST:      # natemu policy: the one byte holding the bit (immediate bit offsets only)
        if not re.fullmatch(r'-?\d+|0x[0-9a-f]+', ops[1]): raise Unsupported(text)
        return [('access', (a + (int(ops[1], 0) % (8 * w)) // 8) & M64, 1, m != 'bt', ss)]
    # A memory destination is written (mov/setcc) or read-modify-written (write intent); cmp/test only read, and
    # so does the implicit-destination one-operand group (mul/imul/div/idiv). xchg writes its memory operand either way.
    write = (i == 0 or m == 'xchg') and m not in ('cmp', 'test', 'mul', 'imul', 'div', 'idiv')
    return [('access', a, w, write, ss)]

# The driver's guest tables (natemu-host run-program), entry bits.
P, RW, US, PS, XD = 1, 2, 4, 1 << 7, 1 << 63
RSVD = 0x000f_ff00_0000_0000        # bits 51:40: MAXPHYADDR 40 (the walker's profile)
LARGE, HIGH = 0x20_0000, 0xffff_8000_0000_0000

def entry_key(level, va): return level, (va & M64) >> (12 + 9 * (level - 1))

class Machine:
    """One case's guest tables and controls, as the driver builds them, and the expected walks.

    Tables: every entry present and user; code-page leaves read-only and executable, data-page leaves writable and,
    with NXE, not executable (a page in both: writable and executable); a `large` 2 MiB range is one PDE leaf
    (writable if a data page is in it, not executable unless a code page is); then the edits, in order: `unmap`/
    `readonly` clear P/R/W in a page's leaf, `entry` (va, level, set, clear) any entry of the walk. Controls: CPL (0|3),
    CR0.WP, CR4.SMEP, EFER.NXE (defaults 3, 1, 0, 1).

    `walk` follows SDM vol. 3 ch. 4 for 4-level paging: a non-present entry faults (P=0) whatever its other bits; a
    present entry with a reserved bit (51:40 under MAXPHYADDR 40, XD without NXE, PS in a PML4E or PDPTE: no 1 GiB
    pages, 20:13 in a 2 MiB leaf) faults with RSVD=1, P=1; access rights (4.6) from R/W and U/S of every level: CPL3
    needs U everywhere, a write needs R/W everywhere unless CPL0 with WP=0, a fetch needs XD clear everywhere (NXE) and,
    at CPL0 with SMEP, a supervisor page. Error code: P, W (write intent), U (CPL3), RSVD, I (fetch, with NXE or SMEP).
    natemu's A/D policy (the walker's, QEMU's order): a leaf gets A, and D for write intent, when its access passes;
    the accesses of an instruction and the pages of one access go in order, so an earlier page keeps A/D when a later
    one faults.

    Walker refusals (the resident halts instead of guessing; a refusal, never a wrong result; boot-phase prerequisites,
    docs/restructure/slice3-contract.md): any PWT, PCD or PAT bit makes a page non-write-back and the resident halts
    (`shadow status=1 detail=7`), although under the default PAT some of them are write-back on x86, so the model and
    the corpus never set them; PS in a PDPTE is reserved (no 1 GiB pages: the boot-phase CPUID must report Page1GB=0);
    the A bit of a non-leaf entry is set by the walk but not compared."""
    def __init__(self, code, data, mut):
        self.cpl, self.wp, self.smep, self.nxe = (mut.get(k, d) for k, d in (('cpl', 3), ('wp', 1), ('smep', 0), ('nxe', 1)))
        self.large = {r & ~(LARGE - 1) for r in mut.get('large', [])}
        self.e, self.a, self.d = {}, set(), set()
        for pg in sorted(set(code) | set(data)):
            for level in (4, 3, 2): self.e.setdefault(entry_key(level, pg), P | RW | US)
            if pg & ~(LARGE - 1) not in self.large:
                self.e[entry_key(1, pg)] = P | US | (RW if pg in data else 0) | (XD if self.nxe and pg not in code else 0)
        for r in self.large:
            inside = [pg for pg in set(code) | set(data) if pg & ~(LARGE - 1) == r]
            self.e[entry_key(2, r)] = P | US | PS | (RW if set(inside) & set(data) else 0) \
                | (XD if self.nxe and not set(inside) & set(code) else 0)
        edits = [(pg, None, 0, P) for pg in mut.get('unmap', [])] + [(pg, None, 0, RW) for pg in mut.get('readonly', [])]
        for va, level, set_, clear in edits + list(mut.get('entry', [])):
            k = self.leaf(va) if level is None else entry_key(level, va)
            if k not in self.e: raise ValueError(f'{va:#x}: no level-{k[0]} entry to edit')
            self.e[k] = (self.e[k] | set_) & ~clear
    def leaf(self, va): return entry_key(2 if va & ~(LARGE - 1) in self.large else 1, va)
    def walk(self, va, kind):
        """(None, error code) for a #PF of a `kind` ('r', 'w' or 'x') access to `va`, else (leaf, 0) with A/D set."""
        error = (2 if kind == 'w' else 0) | (4 if self.cpl == 3 else 0) | (16 if kind == 'x' and (self.nxe or self.smep) else 0)
        rw, us, xd = True, True, False
        for level in (4, 3, 2, 1):
            e = self.e.get(entry_key(level, va), 0)
            if not e & P: return None, error
            large = level == 2 and e & PS
            if e & RSVD or (not self.nxe and e & XD) or (level >= 3 and e & PS) or (large and e & 0x1fe000):
                return None, error | 9
            rw, us, xd = rw and e & RW, us and e & US, xd or e & XD
            if level == 1 or large:
                if (self.cpl == 3 and not us) or (kind == 'w' and not rw and (self.cpl == 3 or self.wp)) \
                        or (kind == 'x' and (xd or (self.cpl == 0 and self.smep and us))):
                    return None, error | 1
                k = entry_key(level, va)
                self.a.add(k)
                if kind == 'w': self.d.add(k)
                return k, 0
    def fetch(self, start, length):
        """The exception of fetching an instruction's bytes, or None: #GP(0) at a non-canonical byte, else #PF with
        CR2 = its first byte in the faulting page, pages in address order. Only the first 15 bytes are fetched: a longer
        instruction is #GP(0) (SDM Vol.3 Interrupt 13 (#GP), "Exceeding the instruction length limit of 15 bytes"),
        after a fetch fault in its first 15 bytes (Vol.3 Table 7-2). Not fetching the 16th is natemu policy: the SDM is
        silent; Bochs does the same, QEMU fetches the next page first (#PF wins there)."""
        length = min(length, 15)
        for page in sorted({start & ~(PAGE - 1), (start + length - 1) & ~(PAGE - 1)}):
            if not canonical(max(start, page)): return (13, 0, 0)
            leaf, error = self.walk(max(start, page), 'x')
            if leaf is None: return (14, max(start, page), error)
        return None

def first_fault(evs, machine, read):
    """The x86 exception of one instruction's events on the `Machine`, or None (its accesses set A/D)."""
    for e in evs:
        if e[0] in ('ud', 'align'): return (6, 0, 0) if e[0] == 'ud' else (13, 0, 0)
        if e[0] == 'divide':
            _, signed, w, src, rax, rdx = e
            d = int.from_bytes(read(src[1], w), 'little') if isinstance(src, tuple) else src
            if divide_error(signed, w, d, rax, rdx): return (0, 0, 0)
            continue
        if e[0] == 'target':
            value = int.from_bytes(read(e[1][1], 8), 'little') if isinstance(e[1], tuple) else e[1]
            if not canonical(value): return (13, 0, 0)
            continue
        _, a, w, write, ss = e
        last = (a + w - 1) & M64
        if not (canonical(a) and canonical(last)): return (12 if ss else 13, 0, 0)
        for page in sorted({a & ~(PAGE - 1), last & ~(PAGE - 1)}, key=lambda p: (p - (a & ~(PAGE - 1))) & M64):
            leaf, error = machine.walk(max(a, page), 'w' if write else 'r')
            if leaf is None: return (14, max(a, page), error)
    return None

def pages(a, n): return range(a & ~(PAGE - 1), a + n, PAGE)

def layout(p, mutations):
    """The test driver's guest tables for program `p` (tools/run_slice1_vm.py descriptions, natemu-host run-program)
    with the case's mutations and controls (`Machine`)."""
    rip = ref.num(p['rip'])
    data = {pg for m in p['memory'] for pg in pages(ref.num(m['addr']), len(m['data']) // 2)}
    return Machine(set(pages(rip, len(p['code']) // 2)), data, mutations)

def describe(line, evs, f, fetched=False):
    m = line.split()[0]
    acc = [e for e in evs if e[0] == 'access']
    if f[0] == 6: return f'#UD {line}'
    if f[0] == 13 and not evs and not fetched: return '#GP instruction too long'
    if any(e[0] == 'align' for e in evs): return f'#GP align {line}'
    if f[0] == 0: return f'#DE {m}{8 * next(e[2] for e in evs if e[0] == "divide")}' + (' mem' if acc else '')
    crossing = ' crossing' if any((a ^ (a + w - 1)) >> 12 for _, a, w, _, _ in acc) else ''
    if fetched: what = 'fetch'
    elif m in ('push', 'pop', 'ret', 'call', 'jmp'): what = m
    elif not any(e[3] for e in acc): what = 'load'
    else: what = 'store' if m in ('mov', 'movabs') or m.startswith('set') else 'rmw'
    return {14: '#PF', 13: '#GP', 12: '#SS'}[f[0]] + ' ' + what + crossing + (' (div)' if m in ('div', 'idiv') else '')

def offsets(lines):
    """(code, instruction start offsets and the end) from assembling the lines."""
    code, labels = ref.assemble('\n'.join(f'{l}\ne{i}:' for i, l in enumerate(lines)), labels=True)
    return code, [0] + [labels[f'e{i}'] for i in range(len(lines))]

def reader(result):
    q = result['qemu']
    regions = [(ref.num(m['addr']), bytes.fromhex(m['data'])) for m in q['memory']]
    def read(a, n):
        for base, data in regions:
            if base <= a and a + n <= base + len(data): return data[a - base:a - base + n]
        raise Unsupported(f'read of {a:#x} outside the regions')
    return read

def state(result): return dict(zip(ref.REGS, (ref.num(v) for v in result['qemu']['gpr'])))
def bases(p): return {'fs': ref.num(p.get('fsbase', 0)) & M64, 'gs': ref.num(p.get('gsbase', 0)) & M64}    # I7

# Built-in probes: (name, registers, lines, index of the faulting line, mutations).
D, S = gen.DATA, gen.STACK
PRIVATE = 0x0000_8000_4000_0000
SCR, SLOT = 0x400000, 1 << 39        # the access-form programs' store base (a page of its own); one PML4 entry's span
NONCANONICAL = [PRIVATE, PRIVATE + 0x23000 + 2920, 0x0000_8000_0000_0000, 0x8000_0000_0000_0000, 0xffff_7fff_ffff_f000,
                0x1234_5678_9abc_def0]
PRELUDE = ['add rcx, rdx', 'mov qword ptr [rdi], rcx', 'sub rdx, 3']                       # flags and a store that stand
AFTER = ['mov qword ptr [rdi + 8], rax', 'inc rdx']                                        # must not run
DATA_FORMS = [('load', 'rbx', 'mov rax, qword ptr [rbx + 8]'), ('store', 'rbx', 'mov dword ptr [rbx], ecx'),
              ('rmw', 'rbx', 'add qword ptr [rbx], 1'), ('read-index', 'rsi', 'cmp word ptr [rsi + r8*2], 5'),
              ('push-mem', 'rbx', 'push qword ptr [rbx]'), ('rbp-load', 'rbp', 'mov rax, qword ptr [rbp + 16]'),
              ('rsp-store', 'rsp', 'mov qword ptr [rsp + 8], rax'), ('push', 'rsp', 'push rax'), ('pop', 'rsp', 'pop rcx'),
              ('rmw-byte', 'rbp', 'not byte ptr [rbp]'), ('ret', 'rsp', 'ret'),
              ('xchg', 'rbx', 'xchg qword ptr [rbx], rax'), ('xchg-byte', 'rbp', 'xchg cl, byte ptr [rbp + 3]'),
              # I1 memory forms: RMW (shld/shrd, cmpxchg and xadd without LOCK), loads (popcnt/tzcnt, BMI), a store (movnti),
              # stack forms (pushf, enter, leave) and a prefetch hint (never faults)
              ('shld', 'rbx', 'shld qword ptr [rbx], rcx, 5'), ('shld-zero', 'rbx', 'shld qword ptr [rbx], rcx, 64'), ('shrd', 'rsi', 'shrd dword ptr [rsi + r8*2], ecx, 31'),
              ('cmpxchg', 'rbx', 'cmpxchg dword ptr [rbx], ecx'), ('xadd', 'rbp', 'xadd qword ptr [rbp + 8], rcx'),
              ('popcnt', 'rsi', 'popcnt rax, qword ptr [rsi + r8*2]'), ('tzcnt', 'rbx', 'tzcnt eax, dword ptr [rbx + 4]'),
              ('andn', 'rbx', 'andn rax, rcx, qword ptr [rbx]'), ('shrx', 'rbp', 'shrx eax, dword ptr [rbp + 4], ecx'),
              ('movnti', 'rbx', 'movnti qword ptr [rbx], rcx'), ('pushf', 'rsp', 'pushfq'), ('enter', 'rsp', 'enter 16, 0'),
              ('leave', 'rbp', 'leave'), ('prefetch', 'rbx', 'prefetcht0 byte ptr [rbx]'),
              # I3 LOCK forms: one atomic each (write intent, nothing written at an abort; a failing CMPXCHG compare still
              # faults as a write, SDM: the destination always receives a write cycle), LOCK NEG a load then a compare-and-swap,
              # LOCK BTS FEX's byte. CMPXCHG8B/16B are probes (PAIR_PROBES): the pinned QEMU's full runs keep stale flags there.
              ('lock-add', 'rbx', 'lock add qword ptr [rbx], rcx'), ('lock-adc-byte', 'rbp', 'lock adc byte ptr [rbp + 3], cl'),
              ('lock-xadd', 'rbp', 'lock xadd dword ptr [rbp + 8], ecx'), ('lock-cmpxchg', 'rbx', 'lock cmpxchg qword ptr [rbx], rcx'),
              ('lock-neg', 'rsi', 'lock neg word ptr [rsi + r8*2]'), ('lock-not', 'rbp', 'lock not byte ptr [rbp]'),
              ('lock-bts', 'rbx', 'lock bts qword ptr [rbx], 5'), ('lock-dec', 'rbx', 'lock dec dword ptr [rbx + 4]'),
              ('lock-xchg', 'rbx', 'lock xchg qword ptr [rbx], rax')]
# Mnemonics that access memory without a bracketed operand (the stack forms).
IMPLICIT_ACCESS = ('push', 'pop', 'call', 'ret', 'pushfq', 'enter', 'leave')
# Forms that never fault (a prefetch hint): their probes expect the QEMU full run.
NOFAULT = {'prefetch': {'nofault': True}}
REGS = {'rax': 0x1122334455667788, 'rcx': 0x7fffffffffffffff, 'rdx': 5, 'rbx': D + 0x100, 'rsi': D + 0x200, 'rdi': D + 0x300,
        'rbp': D + 0x400, 'rsp': S + 0x1000, 'r8': 3}
SS_SPAN = [(0x8000_0000_0004, 'push', 'push rax'), (0xffff_8000_0000_0004, 'push', 'push rax'), (0x8000_0000_0004, 'pop', 'pop rcx'),
           (0x7fff_ffff_fffc, 'pop', 'pop rcx'), (0xffff_7fff_ffff_fffc, 'pop', 'pop rcx')]
# E3 (no address guard): every class of the former private window and both holes, each a hardware fault at the access
# site that must give #GP(0), or #SS(0) for a stack access, with the exact state. (name, page; abi `PRIVATE` layout.)
WINDOW = [('tables', PRIVATE + 0x1_0000), ('shared', PRIVATE + 0x2_0000), ('state', PRIVATE + 0x2_2000), ('frame', PRIVATE + 0x2_3000),
          ('stack', PRIVATE + 0x5_3000), ('exception-stack', PRIVATE + 0x6_3000), ('staging', PRIVATE + 0x7_0000),
          ('text', PRIVATE + 0xa_0000), ('pool', PRIVATE + 0xe_0000), ('guest-ram', PRIVATE + 0x4000_0000 + D), ('ram-tables', PRIVATE + 0x2_5000), ('pool-high', PRIVATE + 0x1f_f000),
          # G1: the guest-state page's former EL0 alias (unmapped since the alias left the window) and its tables' pages.
          ('old-guest-state', PRIVATE + 0x2_4000), ('guest-state-tables', PRIVATE + 0x2_8000),
          ('code', PRIVATE + 0x1003_1000), ('dispatch', PRIVATE + 0x1001_0000), ('code-write', PRIVATE + 0x2003_1000),
          ('meta', PRIVATE + 0x3000_0000), ('lookup', PRIVATE + 0x3800_0000), ('global', PRIVATE + 0x3c00_0000),
          ('low-hole', 0x0000_ffff_ffff_e000), ('low-hole-kernel', 0x0000_8880_1234_5000),
          ('high-hole', 0xffff_0000_0000_0000), ('high-hole-mid', 0xffff_4000_0000_0000), ('poison', 0xdead_0000_0000_0000)]
# Forms besides DATA_FORMS (address offset from the page): a page-crossing store and push (split stubs), a call's push.
WINDOW_FORMS = [('split-store', 'rbx', 0, 'mov qword ptr [rbx + 0xffc], rax'), ('push-split', 'rsp', 0x1004, 'push rax'),
                ('call', 'rsp', 0x800, 'call rdx')]
# Both canonical straddles: the first byte canonical and the last not, and the reverse; (form, base, address offset, line).
STRADDLES = [0x0000_7fff_ffff_fffc, 0xffff_7fff_ffff_fffc]
STRADDLE_FORMS = [('load', 'rbx', 0, 'mov rax, qword ptr [rbx]'), ('store', 'rbx', 0, 'mov qword ptr [rbx], rax'),
                  ('rmw', 'rbx', 0, 'add qword ptr [rbx], 1'), ('read-index', 'rsi', -3, 'cmp word ptr [rsi + r8*2], 5'),
                  ('xchg', 'rbx', 0, 'xchg qword ptr [rbx], rax'), ('rbp-load', 'rbp', 0, 'mov rax, qword ptr [rbp]'),
                  ('rsp-store', 'rsp', 0, 'mov qword ptr [rsp], rax'), ('push', 'rsp', 8, 'push rax'), ('pop', 'rsp', 0, 'pop rcx'),
                  ('ret', 'rsp', 0, 'ret'), ('call', 'rsp', 8, 'call rdx'), ('push-mem', 'rbx', 0, 'push qword ptr [rbx]'),
                  ('lock-add', 'rbx', 0, 'lock add qword ptr [rbx], rcx'), ('lock-cmpxchg16b', 'rbx', 0, 'lock cmpxchg16b xmmword ptr [rbx]')]

# G1: the guest-state page's EL0 alias (abi GUEST_STATE_VA), outside the window in the upper half's x86 non-canonical
# range: an access inside it reaches the vCPU's own guest state (documented deviation): a non-field byte reads 0, the IF
# byte the banked IF (1), and a store to a non-field qword reads back (then restored to 0, as the frame check needs). Its
# extent is pinned by straddles into it (from the unmapped page below in its slot) and out of it, and by the page above:
# each #GP. The window's former alias (PRIVATE + 0x24000) is a WINDOW class above.
GUEST_STATE = 0xffff_4000_0000_1000
GUEST_STATE_EDGES = [GUEST_STATE - 4, GUEST_STATE + 0xffc, GUEST_STATE + 0x1000]
GUEST_STATE_FORMS = [('load', 'mov rax, qword ptr [rbx]'), ('store', 'mov qword ptr [rbx], rax'), ('rmw', 'add qword ptr [rbx], 1')]

def guest_state_probes():
    out = [('window-guest-state', REGS, PRELUDE + [f'movabs rbx, {GUEST_STATE}', 'mov rax, qword ptr [rbx]',
                                                    'movzx ecx, byte ptr [rbx + 0x3f9]'], len(PRELUDE) + 1,
            {'deviation': {'rax': 0, 'rcx': 1}}),
           ('window-guest-state-store', REGS, PRELUDE + [f'movabs rbx, {GUEST_STATE}', 'mov qword ptr [rbx + 8], rdx',
                                                          'mov rcx, qword ptr [rbx + 8]', 'mov qword ptr [rbx + 8], 0'], len(PRELUDE) + 1,
            {'deviation': {'rcx': 2}})]
    for a in GUEST_STATE_EDGES:
        for name, line in GUEST_STATE_FORMS:
            out.append((f'guest-state-edge-{a:#x}-{name}', REGS, PRELUDE + [f'movabs rbx, {a}', line] + AFTER, len(PRELUDE) + 1, {}))
    return out

def window_probes():
    out = guest_state_probes()
    for cls, page in WINDOW:
        for name, base, line in DATA_FORMS:
            out.append((f'window-{cls}-{name}', REGS, PRELUDE + [f'movabs {base}, {page + 0x800}', line] + AFTER, len(PRELUDE) + 1, NOFAULT.get(name, {})))
    for cls, page in WINDOW + [(f'{a:#x}', a & ~(PAGE - 1)) for a in NONCANONICAL]:
        for name, base, off, line in WINDOW_FORMS:
            out.append((f'window-{cls}-{name}', REGS, PRELUDE + [f'movabs {base}, {page + off}', line] + AFTER, len(PRELUDE) + 1, {}))
    for a in STRADDLES:
        for name, base, off, line in STRADDLE_FORMS:
            out.append((f'straddle-{a:#x}-{name}', REGS, PRELUDE + [f'movabs {base}, {(a + off) & M64}', line] + AFTER, len(PRELUDE) + 1, {}))
    # RIP-relative (a guest-RIP move, no stub but a page-crossing store's `b split`): code at the top of the lower half
    # addressing the low hole, and straddling into it.
    for name, line in [('load', 'mov rax, qword ptr [rip + {d}]'), ('store', 'mov qword ptr [rip + {d}], rax'),
                       ('rmw', 'add qword ptr [rip + {d}], 1'), ('xchg', 'xchg qword ptr [rip + {d}], rax')]:
        for where, target in [('hole', RIP_TOP + 0x1_0000), ('straddle', 0x8000_0000_0000 - 4)]:
            lines = PRELUDE + [line.format(d=0x1000)] + AFTER
            d = target - (RIP_TOP + offsets(lines)[1][len(PRELUDE) + 1])
            lines[len(PRELUDE)] = line.format(d=hex(d))
            out.append((f'rip-{where}-{name}', REGS, lines, len(PRELUDE), {'rip': RIP_TOP}))
    return out
RIP_TOP = 0x7fff_ffff_e000          # the RIP-relative probes' code page (the last two of the lower half)

def probes(window=True):
    out = window_probes() if window else []
    for name, base, line in DATA_FORMS:
        for a in NONCANONICAL:
            out.append((f'noncanonical-{name}-{a:#x}', REGS, PRELUDE + [f'movabs {base}, {a}', line] + AFTER, len(PRELUDE) + 1, NOFAULT.get(name, {})))
    # Canonical start, non-canonical end: a page-crossing store (split stub) and load (abort in the gap).
    for name, line in [('store', 'mov qword ptr [rbx], rax'), ('load', 'mov rax, qword ptr [rbx]'), ('rmw', 'add dword ptr [rbx + 2], ecx')]:
        out.append((f'noncanonical-end-{name}', REGS, PRELUDE + ['movabs rbx, 0x7ffffffffffc', line] + AFTER, len(PRELUDE) + 1, {}))
    # Stack accesses across the canonical gap are #SS(0), whichever end is non-canonical: a push below the end of the lower
    # half and above the start of the upper half, a pop starting in the gap or at the last bytes before it.
    for a, name, line in SS_SPAN:
        out.append((f'noncanonical-ss-span-{name}-{a:#x}', REGS, PRELUDE + [f'movabs rsp, {a}', line] + AFTER, len(PRELUDE) + 1, {}))
    # A memory XCHG whose operand crosses 16 bytes (inside a line, across a line, across a page) is a split lock: the
    # VM stops `unsupported_atomic` at it with nothing written (the QEMU prefix state); expected by `atomic`, not a fault.
    for name, line in [('16', 'xchg qword ptr [rbx + 0xc], rax'), ('line', 'xchg dword ptr [rbx + 0x3e], ecx'),
                       ('page', 'xchg qword ptr [rbx + 0xefc], rax'), ('16-word', 'xchg word ptr [rbp + 0xf], dx')]:
        out.append((f'xchg-crossing-{name}', REGS, PRELUDE + [line] + AFTER, len(PRELUDE), {'atomic': True}))
    # I3: every LOCK form crossing 16 bytes (owner decision: LOCKed crossing accesses stop) and CMPXCHG8B, locked or not (one
    # caspal either way: an unlocked one crossing 16 bytes stops too, a documented deviation).
    for name, line in [('add-16', 'lock add qword ptr [rbx + 0xc], rcx'), ('xadd-line', 'lock xadd dword ptr [rbx + 0x3e], ecx'),
                       ('cmpxchg-page', 'lock cmpxchg qword ptr [rbx + 0xefc], rcx'), ('neg-16', 'lock neg word ptr [rbp + 0xf]'),
                       ('inc-16', 'lock inc dword ptr [rsi + 0xe]'), ('sbb-16', 'lock sbb qword ptr [rbx + 0x9], rax'),
                       ('cmpxchg8b-16', 'lock cmpxchg8b qword ptr [rbx + 0xc]'), ('cmpxchg8b-unlocked-line', 'cmpxchg8b qword ptr [rbx + 0x3c]')]:
        out.append((f'lock-crossing-{name}', REGS, PRELUDE + [line] + AFTER, len(PRELUDE), {'atomic': True}))
    # I3: CMPXCHG8B/16B faults (prefix-state references only: see DATA_FORMS), CMPXCHG16B's alignment #GP(0) before its page
    # walk, LOCK on forms that are not lockable (#UD; a register destination, a non-lockable opcode, a system instruction).
    for name, line, mut in [('cmpxchg16b-unmapped', 'lock cmpxchg16b xmmword ptr [rbx]', {'unmap': [D]}),
                            ('cmpxchg16b-readonly', 'lock cmpxchg16b xmmword ptr [rbx]', {'readonly': [D]}),
                            ('cmpxchg16b-unlocked-readonly', 'cmpxchg16b xmmword ptr [rbx + 0x10]', {'readonly': [D]}),
                            ('cmpxchg8b-unmapped', 'cmpxchg8b qword ptr [rbp + 8]', {'unmap': [D]}),
                            ('cmpxchg8b-readonly', 'lock cmpxchg8b qword ptr [rbx + 4]', {'readonly': [D]}),
                            ('cmpxchg16b-misaligned-8', 'lock cmpxchg16b xmmword ptr [rbx + 8]', {}),
                            ('cmpxchg16b-misaligned-unmapped', 'lock cmpxchg16b xmmword ptr [rbx + 1]', {'unmap': [D]}),
                            ('cmpxchg16b-unlocked-misaligned', 'cmpxchg16b xmmword ptr [rsi + 4]', {}),
                            ('neg-unmapped', 'lock neg qword ptr [rbx]', {'unmap': [D]}),
                            ('cmpxchg-readonly-unequal', 'lock cmpxchg dword ptr [rbx], ecx', {'readonly': [D]}),
                            ('ud-mov', 'lock mov qword ptr [rbx], rax', {}), ('ud-register', 'lock add rax, rcx', {}),
                            ('ud-cmp', 'lock cmp qword ptr [rbx], 1', {}), ('ud-bt', 'lock bt qword ptr [rbx], 1', {}),
                            ('ud-xchg-register', 'lock xchg rax, rcx', {}), ('ud-nop', 'lock nop', {}), ('ud-hlt', 'lock hlt', {}),
                            ('ud-cpuid', 'lock cpuid', {}), ('ud-inc-register', 'lock inc rax', {})]:
        out.append((f'lock-{name}', dict(REGS, rdi=S + 0x300), PRELUDE + [line] + AFTER, len(PRELUDE), mut))   # (the prelude's store off D)
    # Page order of a crossing access (first page tested first, also when its first byte is near the page end).
    out.append(('crossing-first-page-readonly', dict(REGS, rbx=D, rdi=S + 0x300), PRELUDE + ['mov qword ptr [rbx + 0xffc], rax'] + AFTER, len(PRELUDE),
                {'readonly': [D], 'unmap': [D + 0x1000]}))
    out.append(('crossing-first-page-unmapped', dict(REGS, rsp=S + 0xfff), PRELUDE + ['mov rax, qword ptr [rsp + 0x1000]'] + AFTER,
                len(PRELUDE), {'unmap': [S + 0x1000]}))
    for a in [0x8000_0000_0000_0000, 0x0000_8000_0000_0000, PRIVATE]:
        for name, setup, line in [('ret', ['push rax'], 'ret'), ('jmp-reg', [], 'jmp rax'), ('call-reg', [], 'call rax'),
                                  ('jmp-mem', ['mov qword ptr [rbx], rax'], 'jmp qword ptr [rbx]'),
                                  ('call-mem', ['mov qword ptr [rbx], rax'], 'call qword ptr [rbx]')]:
            lines = PRELUDE + [f'movabs rax, {a}'] + setup + [line] + AFTER
            out.append((f'branch-{name}-{a:#x}', REGS, lines, len(lines) - len(AFTER) - 1, {}))
    # Fetch: code from the end of a page into a not-present page, at and across the boundary.
    for name, pad in [('at-boundary', 0), ('straddling', 2)]:      # the first three lines take 11 bytes
        lines = ['add rcx, rdx', 'mov qword ptr [rdi], rcx', 'mov eax, 0x11223344'] + AFTER
        out.append((f'fetch-{name}', REGS, lines, None, {'unmap': [0x101000], 'rip': 0x101000 - 11 - pad}))
        # Falling through (or straddling) the end of the lower canonical half: #GP at the next fetch.
        out.append((f'fetch-noncanonical-{name}', REGS, lines, None, {'rip': 0x8000_0000_0000 - 11 - pad}))
    return out + division_probes() + segment_probes() + null_segment_probes() + straddle_probes()

# An instruction straddling a page end (fexc series28): (its bytes in the first page, a line before it, the instruction,
# natemu refuses the form (1; 2: before its fetch completes), program keys). The bytes before the page end cover every
# prefix whose substituted zero bytes FEX decodes as invalid (0f, 0f 3a, 0f 0f, 0f c7, 66 0f, f3 0f, c4, c4 e2, c5, 90 0f),
# others that stop mid-field, VEX/EVEX and multi-prefix forms. The Debian kernel's: jne rel32 with 0f the page's last byte.
SSE, SSSE3, POPCNT = {'sse': 1}, {'sse': 1, 'cpu': 'qemu64,+ssse3'}, {'cpu': gen_i1.CPU}
JNE = {'insn': {'op': 'jcc', 'cc': 'ne'}}
def sse(op, keys): return dict(keys, insn={'op': op, 'sse': True})
STRADDLE_FETCH = [
    ('0f', [], '{disp32} jne {next}', 0, JNE), ('0f85', [], '{disp32} jne {next}', 0, JNE), ('0f8500', [], '{disp32} jne {next}', 0, JNE),
    ('0f', ['nop'], '{disp32} jne {next}', 0, JNE), ('0f3a', [], 'palignr mm0, mm1, 8', 0, sse('palignr', SSSE3)),
    ('660f3a', [], 'palignr xmm0, xmm1, 8', 0, sse('palignr', SSSE3)), ('0f0f', [], 'pfadd mm0, mm1', 1, sse('pfadd', {})),
    ('0fc7', [], 'cmpxchg8b qword ptr [rbx]', 0, {}), ('660f', [], 'movdqa xmm0, xmm1', 0, sse('movdqa', SSE)),
    ('f30f', [], 'popcnt eax, ecx', 0, POPCNT), ('c4', [], 'andn eax, ecx, eax', 0, POPCNT), ('c4e2', [], 'andn eax, ecx, eax', 0, POPCNT),
    # I8: AVX forms, here without CR4.OSXSAVE (QEMU's qemu64: no AVX): #UD at the instruction once it is fetched.
    ('c4e27d', [], 'vbroadcastss ymm0, xmm0', 0, sse('vbroadcastss', {'expect': (6, 0, 0)})),
    ('c5', [], 'vzeroupper', 0, sse('vzeroupper', {'expect': (6, 0, 0)})), ('c5f8', [], 'vzeroupper', 0, sse('vzeroupper', {'expect': (6, 0, 0)})),
    ('0fae', [], 'mfence', 0, sse('mfence', {})), ('0f38', [], 'movbe eax, dword ptr [rbx]', 0, sse('movbe', {'cpu': 'qemu64,+movbe'})),
    ('660f38', [], 'pshufb xmm0, xmm1', 0, sse('pshufb', SSSE3)), ('66', [], 'add ax, cx', 0, {}), ('48', [], 'add rax, rcx', 0, {}),
    ('8f', [], 'pop qword ptr [rbx]', 0, {}), ('e8', [], 'call {next}', 0, {}), ('66f30f', [], 'popcnt ax, cx', 0, POPCNT),
    ('f048', [], 'lock add qword ptr [rbx], rcx', 0, {}), ('3e488b', [], 'mov rax, qword ptr ds:[rbx]', 0, {}),
    ('0f01', [], 'xgetbv', 0, sse('xgetbv', {'expect': (6, 0, 0)})),       # without CR4.OSXSAVE (QEMU's qemu64: no XSAVE): #UD
    ('62', [], 'vaddps zmm0, zmm0, zmm0', 2, sse('vaddps', {}))]

def straddle_probes():
    """Each STRADDLE_FETCH instruction with its first bytes the last of page 0x100000: with the next page present it
    runs as one instruction (the QEMU full run; a refused form stops `unsupported_form` at its RIP); with it not present,
    #PF at 0x101000 with RIP at the instruction (SDM: the fetch fault comes before its decode), whatever the bytes before
    the page end decode to. EVEX (62) is refused before its fetch completes, the next page present or not."""
    out = []
    for head, lead, line, refused, keys in STRADDLE_FETCH:
        k = len(PRELUDE) + len(lead)
        lines = PRELUDE + lead + [line.replace('{next}', f'e{k}')] + AFTER
        code, offs = offsets(lines)
        assert code[offs[k]:].startswith(bytes.fromhex(head)), (head, line)
        name, rip = '-'.join(lead + [head]), 0x101000 - offs[k] - len(head) // 2
        # Annotations gen_i1 cannot derive (QEMU checks the mnemonic; `sse`: no flag written, the first three letters).
        insns = {len(PRELUDE) + i: {'op': l, 'sse': True} for i, l in enumerate(lead)} | ({k: keys['insn']} if 'insn' in keys else {})
        keys = dict({m: v for m, v in keys.items() if m != 'insn'}, rip=rip, insns=insns, straddle=True)
        present = {'refused': True} if refused else {} if 'expect' in keys else {'nofault': True}
        out.append((f'straddle-{name}-present', REGS, lines, k, dict(keys, **present)))
        absent = dict({m: v for m, v in keys.items() if m != 'expect'}, unmap=[0x101000], **({'refused': True} if refused == 2 else {}))
        out.append((f'straddle-{name}-absent', REGS, lines, k if refused == 2 else None, absent))
        if head == '0f' and not lead:       # the next page present but not executable at CPL 3: XD, or supervisor
            for what, edit in (('xd', (0x101000, None, XD, 0)), ('supervisor', (0x101000, None, 0, US))):
                out.append((f'straddle-0f-{what}', REGS, lines, None, dict(keys, entry=[edit])))
    return out + long_probes()

# Over-long instructions (fexc series28): 66 prefixes on `mov qword ptr [rbx + r8*4 + 0x100], 0` (12 bytes), and a 3DNow!
# form whose suffix opcode would be its 17th byte. (name, prefixes, bytes in the first page or None (one page), the next
# page present, the expectation: None runs, else the fault).
LONG_MOV = '4ac7848300010000' + '00000000'
GP0, PF_NEXT = (13, 0, 0), 'fetch'
LONG_FORMS = [('15', '66' * 3 + LONG_MOV, None, True, None), ('16', '66' * 4 + LONG_MOV, None, True, GP0),
              ('17', '66' * 5 + LONG_MOV, None, True, GP0), ('3dnow-17', '66' * 8 + '0f0f8488000000009e', None, True, GP0),
              ('16-split10-present', '66' * 4 + LONG_MOV, 10, True, GP0), ('16-split10-absent', '66' * 4 + LONG_MOV, 10, False, PF_NEXT),
              ('16-split15-present', '66' * 4 + LONG_MOV, 15, True, GP0),
              ('16-split15-absent', '66' * 4 + LONG_MOV, 15, False, GP0),       # natemu policy (Machine.fetch): not #PF
              ('15-prefixes', '66' * 15 + '90', None, True, GP0), ('16-readbyte', '66' * 14 + '0f8500000000', None, True, GP0)]

def long_probes():
    """An instruction longer than 15 bytes is #GP(0) at it (SDM Vol.3 Interrupt 13 (#GP), "Exceeding the instruction
    length limit of 15 bytes"), after a fetch fault within its first 15 bytes (Vol.3 Table 7-2) and, by natemu policy
    (`Machine.fetch`), with no fetch of its 16th: all bytes present (a field past byte 15, 15 prefixes, a 16th opcode byte), split at byte
    10 or 15 of a page end with the next page present or not. The 15-byte form runs (QEMU full run)."""
    out = []
    for name, code, head, present, want in LONG_FORMS:
        k = len(PRELUDE)
        lines = PRELUDE + ['.byte ' + ', '.join(f'{b:#x}' for b in bytes.fromhex(code))] + AFTER
        rip = 0x101000 - offsets(lines)[1][k] - head if head else gen.CODE
        keys = {'rip': rip, 'straddle': True, 'insns': {k: {'op': 'mov', 'width': 64}}} | ({} if present else {'unmap': [0x101000]})
        keys |= {'nofault': True} if want is None else {'expect': want} if want != PF_NEXT else {}
        out.append((f'long-{name}', REGS, lines, k, keys))
    return out

# I7: FS/GS-overridden accesses. For each (segment, base) and form, the pointer register is set so that base + EA (mod 2^64)
# is the target: a not-present page (#PF, CR2 = base + EA), the second page of a crossing access (#PF at that page), a
# non-canonical address and a canonical straddle (#GP(0) even with an RSP/RBP base: the reference is FS/GS, not SS).
SEGMENT_BASES = [('fs', 0x7ffe_0000_1000), ('gs', 0xffff_8000_dead_0000), ('fs', 0xffff_ffff_ffff_f000)]
SEGMENT_FORMS = [   # (name, pointer register, displacement + index part of the EA, line with {s} for the segment), 8-byte unless noted
    ('load', 'rbx', 8, 'mov rax, qword ptr {s}:[rbx + 8]'), ('store', 'rbx', 0, 'mov qword ptr {s}:[rbx], rcx'),
    ('rmw', 'rbx', 0, 'add qword ptr {s}:[rbx], 1'), ('read-index', 'rsi', 6, 'cmp qword ptr {s}:[rsi + r8*2], 5'),
    ('push-mem', 'rbx', 0, 'push qword ptr {s}:[rbx]'), ('pop-mem', 'rbx', 0, 'pop qword ptr {s}:[rbx]'),
    ('rbp-load', 'rbp', 16, 'mov rax, qword ptr {s}:[rbp + 16]'), ('rsp-store', 'rsp', 8, 'mov qword ptr {s}:[rsp + 8], rax'),
    ('rbp-rmw', 'rbp', 0, 'xor qword ptr {s}:[rbp], rax'), ('xchg', 'rbx', 0, 'xchg qword ptr {s}:[rbx], rax'),
    ('cmpxchg', 'rbx', 0, 'cmpxchg qword ptr {s}:[rbx], rcx'), ('xadd', 'rbx', 0, 'xadd qword ptr {s}:[rbx], rcx'),
    ('shld', 'rbx', 0, 'shld qword ptr {s}:[rbx], rcx, 5'), ('movnti', 'rbx', 0, 'movnti qword ptr {s}:[rbx], rcx'),
    ('jmp-mem', 'rbx', 0, 'jmp qword ptr {s}:[rbx]'), ('call-mem', 'rbx', 0, 'call qword ptr {s}:[rbx]'),
    ('moffs', None, 0, 'movabs rax, qword ptr {s}:[{off:#x}]')]
SEGMENT_TARGETS = [('pf', D + 0x1010, {'unmap': [D + 0x1000]}), ('pf-crossing', D + 0xffc, {'unmap': [D + 0x1000]}),
                   ('noncanonical', 0x0000_8000_0000_0010, {}), ('straddle', 0x7fff_ffff_fffc, {}), ('window', PRIVATE + 0x1_0800, {})]

def segment_probes():
    out = []
    for seg, base in SEGMENT_BASES:
        for name, reg, ea, line in SEGMENT_FORMS:
            for tname, target, mut in SEGMENT_TARGETS:
                if tname == 'pf-crossing' and name in ('xchg', 'jmp-mem', 'call-mem'): continue    # split lock / no data write
                text = line.format(s=seg, off=(target - base) & M64)
                regs = dict(REGS, **({reg: (target - base - ea) & M64} if reg else {}))
                if name == 'cmpxchg': regs['rax'] = 0         # either outcome writes the destination
                out.append((f'segment-{seg}-{base:#x}-{name}-{tname}', regs, PRELUDE + [text] + AFTER, len(PRELUDE),
                            dict(mut, **{f'{seg}base': base})))
        # An indirect branch through an FS/GS operand to a non-canonical target: #GP(0) at the branch (before a call's push).
        for name in ('jmp', 'call'):
            regs = dict(REGS, rbx=(D + 0x40 - base) & M64, rax=0x8000_0000_0000_0000)
            lines = PRELUDE + [f'mov qword ptr {seg}:[rbx], rax', f'{name} qword ptr {seg}:[rbx]'] + AFTER
            out.append((f'segment-{seg}-{base:#x}-{name}-target', regs, lines, len(PRELUDE) + 1, {f'{seg}base': base}))
    return out

# Intel's class for a CS/DS/ES/SS prefix (64-bit mode adds no base; SDM Vol. 3A 4.5.1): CS/DS/ES make an RSP/RBP-based
# operand DS-class (#GP(0) when non-canonical), SS makes a DS-default operand SS-class (#SS(0)). Every target of
# SEGMENT_TARGETS but `pf-crossing` for the locked forms (a split lock) and BT* (its one byte does not cross).
NULL_SEGMENT_FORMS = [(s, *f) for s in ('ds', 'es', 'cs') for f in [
    ('rbp-load', 'rbp', 16, 'mov rax, qword ptr {s}:[rbp + 16]'), ('rsp-store', 'rsp', 8, 'mov qword ptr {s}:[rsp + 8], rax'),
    ('rbp-rmw', 'rbp', 0, 'xor qword ptr {s}:[rbp], rax'), ('rbp-lock', 'rbp', 0, 'lock add qword ptr {s}:[rbp], rax'),
    ('rbp-bts', 'rbp', 0, 'bts qword ptr {s}:[rbp], 5'), ('rsp-xchg', 'rsp', 0, 'xchg qword ptr {s}:[rsp], rax')]] + \
    [('ss', *f) for f in [
    ('load', 'rbx', 8, 'mov rax, qword ptr {s}:[rbx + 8]'), ('store', 'rbx', 0, 'mov qword ptr {s}:[rbx], rcx'),
    ('rmw', 'rbx', 0, 'add qword ptr {s}:[rbx], 1'), ('lock', 'rbx', 0, 'lock add qword ptr {s}:[rbx], rcx'),
    ('btr', 'rsi', 6, 'btr qword ptr {s}:[rsi + r8*2], 5'), ('xchg', 'rbx', 0, 'xchg qword ptr {s}:[rbx], rax')]]

def null_segment_probes():
    out = []
    for seg, name, reg, ea, line in NULL_SEGMENT_FORMS:
        for tname, target, mut in SEGMENT_TARGETS:
            if tname == 'pf-crossing' and name.endswith(('lock', 'xchg', 'bts', 'btr')): continue    # split lock; BT*'s byte
            regs = dict(REGS, **{reg: (target - ea) & M64})
            out.append((f'null-segment-{seg}-{name}-{tname}', regs, PRELUDE + [line.format(s=seg)] + AFTER, len(PRELUDE), mut))
    return out

DIVISOR = {1: 'r9b', 2: 'r9w', 4: 'r9d', 8: 'r9'}
SIZE = {1: 'byte', 2: 'word', 4: 'dword', 8: 'qword'}
def division_probes():
    """#DE cases: (rax, rdx, divisor) per width and signedness, register and memory divisors. Values outside an operand's
    width are garbage the division must ignore. A `cmp` right before the division defines all six flags (CF set, so FEX
    holds it inverted), which the #DE state must keep."""
    out = []
    def probe(name, setup, line, mut=None):
        lines = PRELUDE + setup + ['cmp r13, r14', line] + AFTER
        out.append((name, REGS, lines, len(lines) - len(AFTER) - 1, mut or {}))
    M = 2**64 - 1
    cases = {   # name -> {width: (rax, rdx, divisor) for div, idiv}
        'zero': {w: ((0x1234_5678_9abc_def0, 0x77, 0x5500_0000_0000_0000 if w < 8 else 0),) * 2 for w in (1, 2, 4, 8)},
        'overflow': {1: ((0x0505, 0, 5), (0xff80, 0, M)), 2: ((0x1111, 5, 5), (0x8000, 0xffff, M)),
                     4: ((0, 0xffff_ffff_0000_0007, 7), (0x8000_0000, 0xffff_ffff, M)), 8: ((0, 5, 5), (2**63, M, M))},
        'overflow-positive': {1: ((0x0100, 0, 0x3301), (0x0100, 0, 1)), 2: ((0, 0xffff, 0xffff), (0, 1, 1)),
                              4: ((1, 0xffff_ffff, 0xffff_ffff), (0, 1, 1)), 8: ((0, M, 7), (0, 1, 1))},
        'overflow-negative': {1: ((0xfe7f, 0, 0x7f), (0xfe7f, 0, 2)), 2: ((0x7fff, 0xffff, 0x7fff), (0, 0xfffe, 2)),
                              4: ((0x7fff_ffff, 0x8000_0000, 0x7fff_ffff), (0, 0xffff_fffe, 2)), 8: ((1, 2**63, 2**63), (0, M - 1, 2))},
    }
    for case, by_width in cases.items():
        for w, pair in by_width.items():
            for m, (rax, rdx, d) in zip(('div', 'idiv'), pair):
                assert divide_error(m == 'idiv', w, d, rax, rdx), (case, m, w)
                setup = [f'movabs rax, {rax:#x}', f'movabs rdx, {rdx:#x}', f'movabs r9, {d:#x}']
                probe(f'divide-{case}-{m}{8 * w}', setup, f'{m} {DIVISOR[w]}')
                probe(f'divide-{case}-{m}{8 * w}-mem', setup + ['mov qword ptr [rsi + 8], r9'], f'{m} {SIZE[w]} ptr [rsi + 8]')
    # A not-present divisor page faults before #DE (the byte at D + 0x1000 is zero); mapped, it is a zero divisor.
    for m in ('div', 'idiv'):
        setup = [f'movabs rbx, {D + 0x1000:#x}', 'movabs rax, 0x1234']
        probe(f'divide-{m}-unmapped-zero-divisor', setup, f'{m} byte ptr [rbx]', {'unmap': [D + 0x1000]})
        probe(f'divide-{m}-mapped-zero-divisor', setup, f'{m} byte ptr [rbx]')
        probe(f'divide-{m}-crossing-unmapped', [f'movabs rbx, {D + 0xffe:#x}'], f'{m} dword ptr [rbx]', {'unmap': [D + 0x1000]})
        probe(f'divide-{m}-rsp-unmapped', [f'movabs rsp, {S + 0x1000:#x}'], f'{m} qword ptr [rsp]', {'unmap': [S + 0x1000]})
    # 64-bit divisions whose dividend needs 128 bits (FEX's helper call), each stored, then one that faults through the
    # helper: inputs from a fixed seed (no expected values here: QEMU's prefix run is the reference).
    import random
    rng = random.Random(1027)
    for m, signed in (('div', False), ('idiv', True)):
        edges = [(0, 1, 2), (M, 2**63 - 1, 2**63), (0, M - 1, M)] if not signed else \
                [(0, M, 2), (2**63, 0, M), (0, 2**62, 2**63), (5, M, 3), (1, M, 3)]
        assert not any(divide_error(signed, 8, d, rax, rdx) for rax, rdx, d in edges)
        while len(edges) < 24:
            rax, d = rng.getrandbits(64), rng.getrandbits(rng.choice([8, 33, 63, 64])) | 1
            rdx = rng.getrandbits(64) if signed else rng.randrange(1, max(2, d))
            if signed and rng.random() < 0.5: rdx = M if rax >> 63 else 0     # FEX's inline path, mixed in
            if (rdx != 0 or signed) and not divide_error(signed, 8, d, rax, rdx): edges.append((rax, rdx, d))
        last = (3, 7, 7) if not signed else (0, 1, 1)     # high >= divisor; 2^64 / 1
        assert divide_error(signed, 8, last[2], last[0], last[1])
        for n in range(0, len(edges), 8):    # short programs: QEMU's in_asm log of a long translation block is unreliable
            setup = []
            for k, (rax, rdx, d) in enumerate(edges[n:n + 8]):
                setup += [f'movabs rax, {rax:#x}', f'movabs rdx, {rdx:#x}', f'movabs rcx, {d:#x}', f'{m} rcx',
                          f'mov qword ptr [rdi + {0x10 + 16 * k:#x}], rax', f'mov qword ptr [rdi + {0x18 + 16 * k:#x}], rdx']
            setup += [f'movabs rax, {last[0]:#x}', f'movabs rdx, {last[1]:#x}', f'movabs rcx, {last[2]:#x}']
            probe(f'divide-128-{m}-{n // 8}', setup, f'{m} rcx')
    return out

def probe_program(name, regs, lines, rip):
    gpr = [regs.get(r, 0x0101010101010101 * (i + 1)) for i, r in enumerate(ref.REGS)]
    code, _ = offsets(lines)
    mem = [{'addr': hex(D), 'data': bytes((i * 37) & 0xff for i in range(0x2000)).hex()},
           {'addr': hex(S), 'data': bytes((i * 11) & 0xff for i in range(0x2000)).hex()}]
    return {'name': name, 'code': code.hex(), 'rip': hex(rip), 'gpr': [hex(g) for g in gpr], 'rflags': '0x1', 'memory': mem,
            'insns': [gen_i1.annotate(l) for l in lines], 'asm': lines} | ({'cpu': gen_i1.cpu(lines)} if gen_i1.cpu(lines) else {})

# Paging cells (generated on every program, see `variants`): controls, then (mutation, entry level, 2 MiB leaf) on a
# data page an access touches or on the code page. `supervisor` in the controls: the code pages' leaves are supervisor.
CONTROLS = [('cpl3', {}), ('cpl0', {'cpl': 0}), ('cpl0-wp0', {'cpl': 0, 'wp': 0}), ('cpl0-smep', {'cpl': 0, 'smep': 1}),
            ('cpl0-smep-sup', {'cpl': 0, 'smep': 1, 'supervisor': 1}), ('cpl3-nxe0', {'nxe': 0})]
DATA_KINDS, CODE_KINDS = ('unmap', 'readonly', 'supervisor', 'reserved', 'np-garbage'), ('noexec', 'supervisor', 'unmap', 'reserved')

def mutation(kind, level, large):
    """(set, clear) bits of a cell's mutation of the entry at `level` (2 with `large`: the 2 MiB leaf)."""
    reserved = PS if level >= 3 else 1 << 13 if large and level == 2 else 1 << 51
    return {'unmap': (0, P), 'readonly': (0, RW), 'supervisor': (0, US), 'noexec': (XD, 0), 'reserved': (reserved, 0),
            'np-garbage': (M64 & ~P, P)}[kind]

def variants(kinds):
    return [(c, k, level, large) for c in CONTROLS for k in kinds for large in (False, True)
            for level in ((2, 3, 4) if large else (1, 2, 3, 4))]
DATA_VARIANTS, CODE_VARIANTS = variants(DATA_KINDS), variants(CODE_KINDS)
LEAF_VARIANTS = [v for v in DATA_VARIANTS if v[2] == 1 and not v[3]]
UPPER_VARIANTS = [v for v in DATA_VARIANTS if v not in LEAF_VARIANTS]

def cell(variant, page, code_pages):
    """(label, mutations) of a paging cell on `page`."""
    (cname, controls), kind, level, large = variant
    mut = {k: v for k, v in controls.items() if k != 'supervisor'}
    region = page & ~(LARGE - 1)
    if large: mut['large'] = [region]
    sup = [(pg, None, 0, US) for pg in sorted(code_pages)] if controls.get('supervisor') else []
    mut['entry'] = sup + [(page, level, *mutation(kind, level, large))]
    return f"{cname}-{kind}@{level}{'L' if large else ''}-{page:#x}", mut

# Base programs for the paging cells besides the corpus: the data access forms (no fault unmutated), and accesses
# crossing the data page boundary. Position independent (no rip-relative operand): they also run in the upper half.
CROSSING = [('crossing-store', 'mov qword ptr [rbx + 0xefc], rax'), ('crossing-load', 'mov rax, qword ptr [rbp + 0xbfd]'),
            ('crossing-rmw', 'add dword ptr [rsi + 0xdfe], ecx')]
POINTERS = ('rbx', 'rsi', 'rdi', 'rbp', 'rsp')
PLACEMENTS = [('low', (SLOT, 0, 2 * SLOT)), ('high', (HIGH, HIGH + SLOT, HIGH + 2 * SLOT)), ('high-code', (HIGH, 0, 2 * SLOT))]
def paging_programs():
    """The access-form programs. `rdi` (the prelude's and the tail's stores) points into `SCR`, a page of its own, so
    the page under test is never the prelude's; `ret` returns to the instruction after it through a code pointer in the
    stack (`codeptr`: moved with the code); `leave` loads a data pointer and `enter` pushes one (`dataptr`: moved with
    the data). `access`: the index of the instruction under test."""
    out = []
    for name, line in [(n, l) for n, _, l in DATA_FORMS if n not in NOFAULT] + CROSSING:
        lines = PRELUDE + [line] + AFTER
        p = probe_program(f'paging-{name}', dict(REGS, rdi=SCR + 0x300), lines, gen.CODE)
        p['memory'].append({'addr': hex(SCR), 'data': bytes(0x1000).hex()})
        p.update(relocatable=True, scratch=SCR, access=len(PRELUDE))
        if line == 'ret':
            target = gen.CODE + offsets(lines)[1][len(PRELUDE) + 1]
            data = bytearray(bytes.fromhex(p['memory'][1]['data'])); data[0x1000:0x1008] = target.to_bytes(8, 'little')
            p['memory'][1]['data'] = data.hex(); p['codeptr'] = [S + 0x1000]
        if line == 'leave':     # leave loads RBP from [RBP]: a data pointer there (`dataptr`: moved with the data)
            data = bytearray(bytes.fromhex(p['memory'][0]['data'])); data[0x400:0x408] = (D + 0x400).to_bytes(8, 'little')
            p['memory'][0]['data'] = data.hex(); p['dataptr'] = [D + 0x400]
        if line.startswith('enter'):    # enter pushes RBP (a data pointer) to RSP - 8: that slot moves with the data
            p['dataptr'] = [REGS['rsp'] - 8]
        out.append(p)
    return out

def relocate(p, results, mine, code, data, scr):
    """The program and its QEMU results moved: the code and the code pointers in memory by `code`, the pointer registers
    and the data/stack regions by `data`, the `scratch` region (and `rdi`) by `scr`. Pointer registers must stay in
    their regions."""
    regions = [(D, S + 0x2000, data), (SCR, SCR + 0x1000, scr)]
    def delta(v):
        for lo, hi, d in regions:
            if lo <= v <= hi: return d
        raise Unsupported(f'{v:#x} left the moved regions')
    def moved(gpr):
        out = list(gpr)
        for i, r in enumerate(ref.REGS):
            if r in POINTERS: out[i] = ref.hx((ref.num(out[i]) + delta(ref.num(out[i]))) & M64)
        return out
    def mem(ms):
        out = []
        for m in ms:
            a, bytes_ = ref.num(m['addr']), bytearray.fromhex(m['data'])
            for at, by in [(at, code) for at in p.get('codeptr', [])] + [(at, delta(at)) for at in p.get('dataptr', [])]:
                if a <= at < a + len(bytes_):
                    bytes_[at - a:at - a + 8] = ((int.from_bytes(bytes_[at - a:at - a + 8], 'little') + by) & M64).to_bytes(8, 'little')
            out.append(dict(m, addr=hex(a + delta(a)), data=bytes_.hex()))
        return out
    q = dict(p, rip=hex(ref.num(p['rip']) + code), gpr=moved(p['gpr']), memory=mem(p['memory']), scratch=p['scratch'] + scr)
    rs = list(results)       # only this program's results (`mine`) move
    for i in mine:
        r = results[i]
        rs[i] = dict(r, qemu=dict(r['qemu'], rip=ref.hx(ref.num(r['qemu']['rip']) + code), gpr=moved(r['qemu']['gpr']),
                                  memory=mem(r['qemu']['memory'])))
    return q, rs

def run_case(p, offs, evs, read, mut):
    """Runs the program's instructions in order on the case's `Machine`: (fault or None, instruction index, fetch fault?,
    expected A/D per page: {va: (accessed or None (either), dirty)})."""
    m, rip = layout(p, mut), ref.num(p['rip'])
    fault, i, fetched = None, len(offs) - 1, False
    for k in range(len(offs) - 1):
        fault = m.fetch(rip + offs[k], offs[k + 1] - offs[k])
        if fault: i, fetched = k, True; break
        fault = first_fault(evs.get(k, []), m, read(k)) if k in evs else None
        if fault: i = k; break
    code = set(pages(rip, offs[-1]))
    data = {pg for x in p['memory'] for pg in pages(ref.num(x['addr']), len(x['data']) // 2)}
    code_leaves = {m.leaf(pg) for pg in code}
    def bit(leaf, walked, mask): return leaf in walked or bool(m.e.get(leaf, 0) & mask)    # or set by an edit
    # Code leaves the fetches did not reach may be prefetched (A either way); data leaves are exact.
    ad = {pg: ((True if bit(leaf, m.a, 32) else None) if leaf in code_leaves else bit(leaf, m.a, 32), bit(leaf, m.d, 64))
          for pg in code | data for leaf in [m.leaf(pg)]}
    if fetched and fault[0] == 14: ad[fault[1] & ~(PAGE - 1)] = (False, False)
    return fault, i, fetched, ad

def expectations(programs, probe_specs, work, paging=True):
    """Every case: (name, program, mutations, expected (vector, cr2, error) or None (runs to its end), offset of the
    faulting instruction (or the end), QEMU result there, kind, expected A/D). One QEMU batch: the prefix state at every
    instruction with a data access, the full run of every program, and the prefix at each probe."""
    jobs, plan = [], []     # plan: (program, offsets, {instruction: job index}, full-run job, corpus?)
    for p in programs + (paging_programs() if paging else []):
        if not p.get('asm'): continue
        code, offs = offsets(p['asm'])
        if code.hex() != p['code']: print(f"skip {p['name']}: asm does not assemble to the code"); continue
        at = {}
        for i, line in enumerate(p['asm']):
            if '[' in line or line.split()[0] in IMPLICIT_ACCESS: at[i] = len(jobs); jobs.append(dict(p, prefix=offs[i]))
        plan.append((p, offs, at, len(jobs), not p.get('relocatable')))
        jobs.append(dict(p))
    probing = []
    for name, regs, lines, k, mut in probe_specs:
        rip = mut.get('rip', gen.CODE)
        p, (_, offs) = probe_program(name, regs, lines, rip), offsets(lines)
        p.update({b: hex(mut[b]) for b in ('fsbase', 'gsbase') if b in mut})      # I7: the segment bases
        p.update({b: mut[b] for b in ('cpu', 'sse') if b in mut})                  # QEMU's CPU model, CR4.OSFXSR
        for i, ann in mut.get('insns', {}).items(): p['insns'][i] = ann           # annotations gen_i1 cannot derive
        mut = {m: v for m, v in mut.items() if m not in ('fsbase', 'gsbase', 'cpu', 'sse', 'insns')}
        if k is None:       # fetch probe: the first instruction with a byte in a not-present page
            k = next(i for i in range(len(lines)) if layout(p, mut).fetch(rip + offs[i], offs[i + 1] - offs[i]))
        probing.append((p, offs, k, {m: v for m, v in mut.items() if m != 'rip'}, len(jobs)))
        # Outside QEMU's program window the (position-independent) code runs at the same page offset in it.
        here = rip if rip + offs[-1] <= 0x3_0000_0000 else gen.CODE + (rip & (PAGE - 1))
        jobs.append(dict(p, rip=hex(here)) if mut.get('nofault') else dict(p, prefix=offs[k], rip=hex(here)))
    results, _ = ref.run_programs(jobs, work, ref_qemu())
    cases, counters, skipped = [], {}, []
    def take(key, items, n):
        """The next `n` of `items`, cycling (one rotation per program kind and target, so every cell recurs)."""
        c = counters.get(key, 0); counters[key] = c + n
        return [items[(c + j) % len(items)] for j in range(n)]
    for p, offs, at, full, corpus in plan:
        mine = [*at.values(), full]
        placements = [('low', (p, results))] if corpus else [(w, relocate(p, results, mine, *d)) for w, d in PLACEMENTS]
        for where, (q, rs) in placements:
            rip = ref.num(q['rip'])
            try: evs = {i: events(q['asm'][i], dict(state(rs[j]), **bases(q)), rip + offs[i + 1]) for i, j in at.items()}
            except Unsupported as e: print(f"skip {q['name']} ({where}): {e}"); continue
            def case(name, mut):
                f, i, fetched, ad = run_case(q, offs, evs, lambda k: reader(rs[at[k]]), mut)
                if not f: r = rs[full]
                elif i in at: r = rs[at[i]]
                elif i == 0: r = initial_result(q)
                else: skipped.append(f"{q['name']}@{where}/{name}"); return     # no QEMU prefix there (a fetch fault mid-program)
                kind = describe(q['asm'][i], [] if fetched else evs.get(i, []), f, fetched) if f else 'runs to its end'
                cases.append((f"{q['name']}@{where}/{name}", q, mut, f, offs[i], r, kind, ad))
            code_pages = set(pages(rip, offs[-1]))
            touched = sorted({pg for es in evs.values() for e in es if e[0] == 'access'
                              for pg in (e[1] & ~(PAGE - 1), (e[1] + e[2] - 1) & ~(PAGE - 1))} - code_pages - {q.get('scratch')})
            if corpus:          # the slice-1 page mutations: leaf P or R/W of each touched page, user mode
                for page in touched:
                    for mode in ('unmap', 'readonly'):
                        mut = {mode: [page]}
                        f, i, fetched, ad = run_case(q, offs, evs, lambda k: reader(rs[at[k]]), mut)
                        if f: cases.append((f"{q['name']}@{where}/{mode}-{page:#x}", q, mut, f, offs[i], rs[at[i]], describe(q['asm'][i], evs[i], f), ad))
            if not paging: continue
            n = 2 if corpus else 6
            for page in touched:
                if page in data_pages(q):     # the access forms: every control x mutation of the leaf, then a rotation of the rest
                    for v in ([] if corpus else LEAF_VARIANTS) + take(('data', corpus, where), DATA_VARIANTS if corpus else UPPER_VARIANTS, n):
                        case(*cell(v, page, code_pages))
            for v in take(('code', corpus, where), CODE_VARIANTS, n + 1): case(*cell(v, min(code_pages), code_pages))
    for p, offs, k, mut, j in probing:
        rip, r = ref.num(p['rip']), results[j]
        r['qemu']['rip'] = ref.hx(rip + offs[k])
        m = layout(p, mut)
        fetched = m.fetch(rip + offs[k], offs[k + 1] - offs[k])
        try: evs = events(p['asm'][k], dict(state(r), **bases(p)), rip + offs[k + 1])
        except Unsupported:     # a straddling branch: its fetch fault or the QEMU full run is the reference
            if not mut.get('straddle'): raise
            evs = []
        if mut.get('atomic') or mut.get('refused'):
            cases.append((p['name'], p, mut, None, offs[k], r, ATOMIC if mut.get('atomic') else REFUSED, None)); continue
        if mut.get('deviation'):    # the guest-state page: runs to its end, the QEMU prefix plus the loads' values
            r = json.loads(json.dumps(r))
            for reg, v in mut['deviation'].items(): r['qemu']['gpr'][ref.REGS.index(reg)] = ref.hx(v)
            r['qemu']['rip'] = ref.hx(rip + offs[-1])
            cases.append((p['name'], p, {}, None, offs[-1], r, 'guest-state deviation', None)); continue
        if mut.get('nofault'):      # the full run: the program reaches its end
            if first_fault(evs, m, reader(r)): raise ValueError(f"{p['name']}: a no-fault probe faults in the model")
            r['qemu']['rip'] = ref.hx(rip + offs[-1])
            cases.append((p['name'], p, {}, None, offs[-1], r, 'runs to its end', None)); continue
        f = fetched or mut.get('expect') or first_fault(evs, m, reader(r))
        if not f: raise ValueError(f"{p['name']}: the probe does not fault")
        cases.append((p['name'], p, mut, f, offs[k], r, describe(p['asm'][k], [] if fetched else evs, f, bool(fetched)), None))
    if skipped: print(f'{len(skipped)} paging cells skipped (fetch fault after the first instruction, no QEMU prefix): {skipped[:5]}')
    return cases

def access_cells(cases):
    """{access-form program: number of cells faulting at its own access instruction (not at a fetch)}; every program
    needs at least one."""
    out = {p['name']: 0 for p in paging_programs()}
    access = {p['name']: offsets(p['asm'])[1][p['access']] for p in paging_programs()}
    for c in cases:
        n = c[1]['name']
        if n in out and c[3] and c[4] == access[n] and 'fetch' not in c[6]: out[n] += 1
    missing = [n for n, k in out.items() if not k]
    if missing: raise SystemExit(f'access-form programs that never fault at their own access: {missing}')
    return out

def data_pages(p): return {pg for m in p['memory'] for pg in pages(ref.num(m['addr']), len(m['data']) // 2)}

def initial_result(p):
    """The program's initial state as a reference result (a fetch fault at its first instruction)."""
    return {'name': p['name'], 'qemu': {'gpr': p['gpr'], 'rip': p['rip'], 'rflags': p['rflags'], 'memory': p['memory']},
            'rflags_compare_mask': ref.hx(ref.flags.ARITH)}

def ref_qemu():
    import os
    raw = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))
    return raw / 'reference-slice1-20261026-01' / 'pin' / 'qemu-x86_64-static'

def case_description(case):
    """The driver description of a fault case (`expectations`): its program, controls and table mutations (an
    `entry` edit's level resolved: a leaf is the PTE, or the PDE of a 2 MiB page)."""
    _, p, mut = case[:3]
    m = layout(p, mut)
    out = vm.description(p) + ''.join(f'{k} {page:#x}\n' for k in ('unmap', 'readonly') for page in mut.get(k, []))
    out += ''.join(f'{k} {mut[k]}\n' for k in ('cpl', 'wp', 'smep', 'nxe') if k in mut)
    out += ''.join(f'large {r:#x}\n' for r in mut.get('large', []))
    for va, level, set_, clear in mut.get('entry', []):
        level = level or m.leaf(va)[0]
        out += (f'entry {va:#x} {level} set {set_:#x}\n' if set_ else '') + (f'entry {va:#x} {level} clear {clear:#x}\n' if clear else '')
    return out

ATOMIC = 'unsupported_atomic'      # a probe's kind: the VM stops so at the instruction, before any effect
REFUSED = 'unsupported_form'       # the same for a form fexc refuses (straddle probes), an `unsupported` outcome

def evaluate(case, out):
    """{'expected_fault', 'vm', 'outcome', 'diffs'} of the VM's final state `out` (driver JSON) for a fault case (or
    one expected to run to its end), with its leaf A/D bits."""
    _, p, _, f, off, prefix, _, ad = case
    rip = ref.num(p['rip']) + off
    want = [*f, rip] if f else [0, 0, 0, 0]
    entry = {'expected_fault': [hex(x) for x in want], 'vm': {k: out[k] for k in ('stop', 'reason', 'rip', 'fault', 'native', 'stats', 'frame_intact')}}
    if case[6] in (ATOMIC, REFUSED):
        # Data and registers are those of the QEMU prefix. The leaf A/D bits are NOT checked here: the swap's write-intent
        # walk may already have set A (and D) before the alignment fault stopped it.
        stop = 'resident' if case[6] == ATOMIC else 'no_code'
        diffs = [] if (out['stop'], out['reason'].split(' ')[0]) == (stop, case[6]) else [f"stopped {out['stop']} {out['reason']} at {out['rip']}, not {case[6]}"]
        diffs += ref.compare(prefix, [ref.num(v) for v in out['gpr']], ref.num(out['rflags']), ref.num(out['rip']), out['memory'])
        if not out['frame_intact']: diffs.append('FEX frame changed')
        return {**entry, 'outcome': 'fail' if diffs else 'match' if case[6] == ATOMIC else 'unsupported', 'diffs': diffs}
    limit_hit = vm.declared_limit(out)
    if limit_hit and ref.num(out['rip']) < rip:
        return {**entry, 'outcome': 'unsupported', 'diffs': [f"stopped at {out['rip']}: {limit_hit}"]}
    stop = 'guest_fault' if f else 'reached'
    diffs = [] if out['stop'] == stop else [f"stopped {out['stop']} {out['reason']} at {out['rip']}"]
    got = [ref.num(x) for x in out['fault']]
    if got != want: diffs.append(f'fault {[hex(x) for x in got]} != expected {[hex(x) for x in want]}')
    # X1a: the fault is delivered through the driver's logging IDT; its frame, handler RSP and CPL are delivery_model's.
    if f and out['stop'] == 'guest_fault':
        diffs += delivery_check.problems(out, f[0], f[2], f[1], int(case[2].get('cpl', p.get('cpl', 3))))
    diffs += ref.compare(prefix, [ref.num(v) for v in out['gpr']], ref.num(out['rflags']), ref.num(out['rip']), out['memory'])
    for x in out['pages']:
        a, d = (ad or {}).get(ref.num(x['va']), (None, None))
        if (a is not None and x['accessed'] != a) or (d is not None and x['dirty'] != d):
            diffs.append(f"{x['va']}: A/D {int(x['accessed'])}{int(x['dirty'])}, expected {'?' if a is None else int(a)}{int(d)}")
    if not out['frame_intact']: diffs.append('FEX frame changed')
    if f and off == 0 and case[6].split()[1:2] == ['fetch'] and out['stats']['compiles']:     # fetch checked before compiling
        diffs.append(f"{out['stats']['compiles']} compiles before the first instruction's fetch fault")
    return {**entry, 'outcome': 'fail' if diffs else 'match', 'diffs': diffs}

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('programs', type=pathlib.Path, nargs='*', default=[ROOT / 'fixtures/slice1/programs.json'])
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int)
    ap.add_argument('--no-probes', action='store_true')
    ap.add_argument('--no-window', action='store_true', help='leave out the E3 window and straddle probes')
    ap.add_argument('--only', help='run only the cases whose name contains this')
    ap.add_argument('--no-paging', action='store_true', help='only the slice-1 page mutations and probes')
    a = ap.parse_args()
    host, runtime = vm.build()
    programs = [p for f in a.programs for p in json.loads(f.read_text())['programs']]
    a.out.mkdir(parents=True, exist_ok=True)
    cases = expectations(programs, [] if a.no_probes else probes(not a.no_window), a.out / 'ref', paging=not a.no_paging)
    if a.only: cases = [c for c in cases if a.only in c[0]]
    at_access = {} if a.no_paging else access_cells(cases)
    counts, results = {'match': 0, 'unsupported': 0, 'fail': 0}, []
    for n, case in enumerate(cases):
        name, kind = case[0], case[6]
        work = a.out / 'work' / f'c{n:04d}'; work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(case_description(case))
        limit = [str(a.max_block)] if a.max_block else []
        run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + limit + vm.CODE_SHAPE, capture_output=True, text=True)
        entry = {'case': name, 'kind': kind, 'returncode': run.returncode, 'stderr': run.stderr[-1000:]}
        if run.returncode:
            entry['outcome'], diffs = 'fail', ['host error']
        else:
            entry.update(evaluate(case, json.loads(run.stdout)))
            diffs = entry['diffs']
        entry['diffs'] = diffs
        counts[entry['outcome']] += 1; results.append(entry)
        print(f"{name} [{kind}]: {entry['outcome']} {'; '.join(diffs)[:300]} {run.stderr.strip()[-200:]}")
    kinds = {}
    for e in results: kinds.setdefault(e['kind'], {}).setdefault(e['outcome'], 0); kinds[e['kind']][e['outcome']] += 1
    cells = {}      # paging cells by control and mutation (`cell` labels)
    for e in results:
        label = e['case'].split('/')[-1]
        for cname, _ in sorted(CONTROLS, key=lambda c: -len(c[0])):
            if label.startswith(cname + '-'):
                key = f"{cname} {label[len(cname) + 1:].split('-0x')[0]}"
                cells.setdefault(key, {}).setdefault(e['outcome'], 0); cells[key][e['outcome']] += 1
                break
    (a.out / 'faults.json').write_text(json.dumps({'counts': counts, 'kinds': kinds, 'cells': cells, 'at_access': at_access, 'results': results}, indent=1) + '\n')
    print(json.dumps(kinds, indent=1)); print(f'{len(cells)} paging cells:', json.dumps(cells)); print('cells faulting at the access under test:', json.dumps(at_access)); print(counts)
    sys.exit(1 if counts['fail'] else 0)

if __name__ == '__main__': main()
