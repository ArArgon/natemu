#!/usr/bin/env python3
"""Precise guest faults in the VM against QEMU prefix runs (slice 1, S1e).

    tools/with-env python3 tools/run_slice1_faults.py --fexc FEXC --out DIR [--max-block N] [PROGRAMS.json ...]

Cases are derived from program descriptions, never written per program:

* Page mutations (default inputs: fixtures/slice1/programs.json; any corpus with `asm` works). For every program,
  every data page an access touches, and each mutation (`unmap`: clear P; `readonly`: clear R/W), the expected
  fault is the first access in program order the mutation affects (readonly: the first write-intent access).
  Effective addresses come from the generator's `asm` text evaluated in QEMU's prefix state at that instruction
  (tools/reference/ref.py `prefix`), never from fexc.
* Built-in probes (PROBES below): non-canonical data and stack addresses (#GP(0)/#SS(0)), branches to
  non-canonical targets (#GP(0) at the branch), instruction fetch from a not-present code page, and divide errors
  (#DE: zero divisor and quotient overflow per width and signedness, register and memory divisors, a not-present
  divisor page before #DE, 64-bit divisions whose dividend needs 128 bits after a run of such divisions that do not
  fault).

The expected exception, from the instruction text and the prefix state alone, follows x86 and the contract's natemu
policies: per access in order, a non-canonical access (any byte) is #SS(0) for an RSP/RBP base or stack operation,
else #GP(0); then each page it touches in address order, #PF with CR2 = the first byte of the access in that page,
error code U | W (write intent; a read-modify-write's load is write intent) | P (protection). A branch target is
checked after the accesses that produce it and before a call's push. div/idiv: after its memory divisor is read, #DE
(no error code) when the divisor is zero or the quotient does not fit the destination (SDM DIV/IDIV), computed from the
prefix state. Fetch #PF: CR2 = the first instruction byte in the not-present page, error U | I. bt-family memory forms
access one byte (natemu policy, tools/fexc/README.md).

The VM must stop with exactly that exception record (vector, CR2, error code, RIP = start of the faulting
instruction), its GPRs, masked RFLAGS and memory equal to the QEMU prefix run (nothing later ran, no partial store),
and the FEX frame intact. A stop at a declared slice limit before the expected fault (unsupported_form,
unsupported_smc) is "unsupported". Writes DIR/faults.json; exit status 1 on any failure."""
import argparse, json, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
import ref, gen_slice1 as gen, run_slice1_vm as vm  # noqa: E402

M64, PAGE = 2**64 - 1, 4096
SIZES = {'byte': 1, 'word': 2, 'dword': 4, 'qword': 8}
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

MEM = re.compile(r'(?:(byte|word|dword|qword) ptr )?\[(.*)\]')

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

def events(text, regs, next_rip):
    """One instruction's guest data accesses and branch-target check, in architectural order:
    ('access', address, bytes, write intent, stack segment), ('target', register value | ('mem', address)) and
    ('divide', signed, bytes, divisor value | ('mem', address), rax, rdx)."""
    m, ops = text.split()[0], split_operands(text)
    def mem(op):
        x = MEM.fullmatch(op)
        if not x: return None
        a, base = address(x[2], regs, next_rip)
        return a, SIZES[x[1]] if x[1] else None, base in STACK_BASES
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
            a, base = address(MEM.fullmatch(ops[0])[2], dict(regs, rsp=(rsp + w) & M64), next_rip)
            out.append(('access', a, w, True, base in STACK_BASES))
        return out
    if m == 'ret' and (not ops or len(ops) == 1 and re.fullmatch(r'\d+|0x[0-9a-f]+', ops[0])):    # ret / ret imm16
        return [('access', rsp, 8, False, True), ('target', ('mem', rsp))]
    if m in ('jmp', 'call') and len(ops) == 1 and (mem(ops[0]) or ops[0] in ref.R64):
        x = mem(ops[0])
        if x and x[1] not in (None, 8): raise Unsupported(text)
        out = [('access', x[0], 8, False, x[2]), ('target', ('mem', x[0]))] if x else [('target', regs[ops[0]])]
        return out + ([('access', (rsp - 8) & M64, 8, True, True)] if m == 'call' else [])     # the push follows the check
    # Anything else that transfers control (jcc, direct jmp/call, loop, ...) is not straight-line: never guessed.
    if m in ('jmp', 'call', 'ret', 'push', 'pop') or m.startswith('j') or m.startswith('loop') or m in ('iret', 'iretq', 'int', 'syscall'):
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
    # so does the implicit-destination one-operand group (mul/imul/div/idiv).
    write = i == 0 and m not in ('cmp', 'test', 'mul', 'imul', 'div', 'idiv')
    return [('access', a, w, write, ss)]

def first_fault(evs, layout, read):
    """The x86 exception of one instruction's events in the page layout (page -> writable; absent: not present), or None."""
    for e in evs:
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
            if page not in layout: return (14, max(a, page), 4 | 2 * write)
            if write and not layout[page]: return (14, max(a, page), 7)
    return None

def fetch_fault(start, length, layout):
    for page in sorted({start & ~(PAGE - 1), (start + length - 1) & ~(PAGE - 1)}):
        if not canonical(max(start, page)): return (13, 0, 0)
        if page not in layout: return (14, max(start, page), 4 | 16)
    return None

def pages(a, n): return range(a & ~(PAGE - 1), a + n, PAGE)

def layout(p, mutations):
    """The test driver's guest mappings (tools/run_slice1_vm.py descriptions, natemu-host run-program): code pages
    present and read-only, region pages present and writable; then the mutations."""
    rip = ref.num(p['rip'])
    out = {pg: False for pg in pages(rip, len(p['code']) // 2)}
    for m in p['memory']: out.update({pg: True for pg in pages(ref.num(m['addr']), len(m['data']) // 2)})
    for pg in mutations.get('unmap', []): out.pop(pg, None)
    for pg in mutations.get('readonly', []): out[pg] = False
    return out

def describe(line, evs, f, fetched=False):
    m = line.split()[0]
    acc = [e for e in evs if e[0] == 'access']
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

# Built-in probes: (name, registers, lines, index of the faulting line, mutations).
D, S = gen.DATA, gen.STACK
PRIVATE = 0x0000_8000_4000_0000
NONCANONICAL = [PRIVATE, PRIVATE + 0x23000 + 2920, 0x0000_8000_0000_0000, 0x8000_0000_0000_0000, 0xffff_7fff_ffff_f000,
                0x1234_5678_9abc_def0]
PRELUDE = ['add rcx, rdx', 'mov qword ptr [rdi], rcx', 'sub rdx, 3']                       # flags and a store that stand
AFTER = ['mov qword ptr [rdi + 8], rax', 'inc rdx']                                        # must not run
DATA_FORMS = [('load', 'rbx', 'mov rax, qword ptr [rbx + 8]'), ('store', 'rbx', 'mov dword ptr [rbx], ecx'),
              ('rmw', 'rbx', 'add qword ptr [rbx], 1'), ('read-index', 'rsi', 'cmp word ptr [rsi + r8*2], 5'),
              ('push-mem', 'rbx', 'push qword ptr [rbx]'), ('rbp-load', 'rbp', 'mov rax, qword ptr [rbp + 16]'),
              ('rsp-store', 'rsp', 'mov qword ptr [rsp + 8], rax'), ('push', 'rsp', 'push rax'), ('pop', 'rsp', 'pop rcx'),
              ('rmw-byte', 'rbp', 'not byte ptr [rbp]')]
REGS = {'rax': 0x1122334455667788, 'rcx': 0x7fffffffffffffff, 'rdx': 5, 'rbx': D + 0x100, 'rsi': D + 0x200, 'rdi': D + 0x300,
        'rbp': D + 0x400, 'rsp': S + 0x1000, 'r8': 3}
def probes():
    out = []
    for name, base, line in DATA_FORMS:
        for a in NONCANONICAL:
            out.append((f'noncanonical-{name}-{a:#x}', REGS, PRELUDE + [f'movabs {base}, {a}', line] + AFTER, len(PRELUDE) + 1, {}))
    # Canonical start, non-canonical end: a page-crossing store (split stub) and load (abort in the gap).
    for name, line in [('store', 'mov qword ptr [rbx], rax'), ('load', 'mov rax, qword ptr [rbx]'), ('rmw', 'add dword ptr [rbx + 2], ecx')]:
        out.append((f'noncanonical-end-{name}', REGS, PRELUDE + ['movabs rbx, 0x7ffffffffffc', line] + AFTER, len(PRELUDE) + 1, {}))
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
    return out + division_probes()

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
            'insns': [gen.annotate(l) for l in lines], 'asm': lines}

def expectations(programs, probe_specs, work):
    """Every case: (name, program, mutations, expected (vector, cr2, error), faulting instruction offset, QEMU prefix
    result at it, kind). One QEMU batch: the prefix state at every instruction with a data access, and at each probe."""
    jobs, plan = [], []     # plan: (program, offsets, {instruction: job index})
    for p in programs:
        if not p.get('asm'): continue
        code, offs = offsets(p['asm'])
        if code.hex() != p['code']: print(f"skip {p['name']}: asm does not assemble to the code"); continue
        at = {}
        for i, line in enumerate(p['asm']):
            if '[' in line or line.split()[0] in ('push', 'pop'): at[i] = len(jobs); jobs.append(dict(p, prefix=offs[i]))
        plan.append((p, offs, at))
    probing = []
    for name, regs, lines, k, mut in probe_specs:
        rip = mut.get('rip', gen.CODE)
        p, (_, offs) = probe_program(name, regs, lines, rip), offsets(lines)
        if k is None:       # fetch probe: the first instruction with a byte in a not-present page
            k = next(i for i in range(len(lines)) if fetch_fault(rip + offs[i], offs[i + 1] - offs[i], layout(p, mut)))
        probing.append((p, offs, k, {m: v for m, v in mut.items() if m != 'rip'}, len(jobs)))
        # Outside QEMU's program window the (position-independent) code runs at the same page offset in it.
        here = rip if rip + offs[-1] <= 0x3_0000_0000 else gen.CODE + (rip & (PAGE - 1))
        jobs.append(dict(p, prefix=offs[k], rip=hex(here)))
    results, _ = ref.run_programs(jobs, work, ref_qemu())
    cases = []
    for p, offs, at in plan:
        rip = ref.num(p['rip'])
        code_pages = set(pages(rip, offs[-1]))
        try: evs = {i: events(p['asm'][i], state(results[j]), rip + offs[i + 1]) for i, j in at.items()}
        except Unsupported as e: print(f"skip {p['name']}: {e}"); continue
        touched = {pg for es in evs.values() for e in es if e[0] == 'access' for pg in (e[1] & ~(PAGE - 1), (e[1] + e[2] - 1) & ~(PAGE - 1))}
        for page in sorted(touched - code_pages):
            for mode in ('unmap', 'readonly'):
                pl = layout(p, {mode: [page]})
                for i in sorted(evs):
                    f = first_fault(evs[i], pl, reader(results[at[i]]))
                    if f:
                        cases.append((f"{p['name']}/{mode}-{page:#x}", p, {mode: [page]}, f, offs[i], results[at[i]], describe(p['asm'][i], evs[i], f)))
                        break
    for p, offs, k, mut, j in probing:
        rip, r = ref.num(p['rip']), results[j]
        r['qemu']['rip'] = ref.hx(rip + offs[k])
        evs = events(p['asm'][k], state(r), rip + offs[k + 1])
        pl = layout(p, mut)
        f = fetch_fault(rip + offs[k], offs[k + 1] - offs[k], pl) or first_fault(evs, pl, reader(r))
        if not f: raise ValueError(f"{p['name']}: the probe does not fault")
        fetched = fetch_fault(rip + offs[k], offs[k + 1] - offs[k], pl) is not None
        cases.append((p['name'], p, mut, f, offs[k], r, describe(p['asm'][k], [] if fetched else evs, f, fetched)))
    return cases

def ref_qemu():
    import os
    raw = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))
    return raw / 'reference-slice1-20261026-01' / 'pin' / 'qemu-x86_64-static'

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('programs', type=pathlib.Path, nargs='*', default=[ROOT / 'fixtures/slice1/programs.json'])
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int)
    ap.add_argument('--no-probes', action='store_true')
    a = ap.parse_args()
    host, runtime = vm.build()
    programs = [p for f in a.programs for p in json.loads(f.read_text())['programs']]
    a.out.mkdir(parents=True, exist_ok=True)
    cases = expectations(programs, [] if a.no_probes else probes(), a.out / 'ref')
    counts, results = {'match': 0, 'unsupported': 0, 'fail': 0}, []
    for n, (name, p, mut, (vector, cr2, error), off, prefix, kind) in enumerate(cases):
        work = a.out / 'work' / f'c{n:04d}'; work.mkdir(parents=True, exist_ok=True)
        text = vm.description(p) + ''.join(f'{m} {page:#x}\n' for m, pages in mut.items() for page in pages)
        (work / 'program.txt').write_text(text)
        limit = [str(a.max_block)] if a.max_block else []
        run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + limit, capture_output=True, text=True)
        rip = ref.num(p['rip']) + off
        want = [vector, cr2, error, rip]
        entry = {'case': name, 'kind': kind, 'expected_fault': [hex(x) for x in want], 'returncode': run.returncode, 'stderr': run.stderr[-1000:]}
        if run.returncode:
            entry['outcome'], diffs = 'fail', ['host error']
        else:
            out = json.loads(run.stdout); entry['vm'] = {k: out[k] for k in ('stop', 'reason', 'rip', 'fault', 'native', 'stats', 'frame_intact')}
            limit_hit = vm.declared_limit(out)
            if limit_hit and ref.num(out['rip']) < rip:
                entry['outcome'], diffs = 'unsupported', [f"stopped at {out['rip']}: {limit_hit}"]
            else:
                diffs = [] if out['stop'] == 'guest_fault' else [f"stopped {out['stop']} {out['reason']} at {out['rip']}"]
                got = [ref.num(x) for x in out['fault']]
                if got != want: diffs.append(f'fault {[hex(x) for x in got]} != expected {[hex(x) for x in want]}')
                diffs += ref.compare(prefix, [ref.num(v) for v in out['gpr']], ref.num(out['rflags']), ref.num(out['rip']), out['memory'])
                if not out['frame_intact']: diffs.append('FEX frame changed')
                entry['outcome'] = 'fail' if diffs else 'match'
        entry['diffs'] = diffs
        counts[entry['outcome']] += 1; results.append(entry)
        print(f"{name} [{kind}]: {entry['outcome']} {'; '.join(diffs)[:300]} {run.stderr.strip()[-200:]}")
    kinds = {}
    for e in results: kinds.setdefault(e['kind'], {}).setdefault(e['outcome'], 0); kinds[e['kind']][e['outcome']] += 1
    (a.out / 'faults.json').write_text(json.dumps({'counts': counts, 'kinds': kinds, 'results': results}, indent=1) + '\n')
    print(json.dumps(kinds, indent=1)); print(counts)
    sys.exit(1 if counts['fail'] else 0)

if __name__ == '__main__': main()
