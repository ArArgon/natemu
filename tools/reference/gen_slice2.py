#!/usr/bin/env python3
"""Seeded generator of structured control-flow programs (slice 2, S2d): inputs only, like gen_slice1.py.

    python3 tools/reference/gen_slice2.py -o programs.json [--seed N] [--count 64]

Each program wraps random straight-line segments (gen_slice1's instruction mix) in structured templates:
if/else diamonds and one-armed ifs on a fresh `cmp`; counted loops on a reserved counter register (`dec`+`jnz`/`jg`,
`inc`+`cmp`+`jb`/`jl`/`jne`, the while form with a test at the bottom), `loop`/`loope`/`loopne` on rcx, `jrcxz` guards
(including zero-trip loops); nesting to depth 3; calls of local functions (leaf and nested), recursion to depth 8 on a
depth counter; and a jump table (`jmp [table + idx*8]`, the table in its own data page). Programs terminate by
construction: counters change only in the templates (segments never write them), loop trip counts are bounded (a loop's
whole dynamic instruction count is capped), and the last instruction transfers to the end of the code. The program is
bytes plus initial state, annotations and the offset of each instruction; expected results come only from running the
same bytes in pinned QEMU (tools/reference/ref.py, `flow` programs), whose executed path also decides which flags are
undefined at the end. Segments never touch rsp (return addresses stay intact) and never name a register a template
owns, and every conditional branch is preceded by a `cmp` (or the counter's own `dec`) that defines the flags it reads.
"""
import argparse, json, pathlib, random, re, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, ref
import gen_slice1 as gen

CODE, DATA, STACK, REGION = gen.CODE, gen.DATA, gen.STACK, gen.REGION
TABLE = 0x400000                      # jump tables: one data page
COUNTERS = ['r14', 'r15', 'r11']      # loop counters by nesting depth
RECURSION = 'r10'                     # recursion depth counter; rcx counts loop/loope/loopne/jrcxz; rax and r9 index jump tables
LOOP_CAP = 1500                       # most dynamic instructions one loop may execute
CCS = list(flags.CC_READS)
ALIASES = {'rcx': ['rcx', 'ecx', 'cx', 'cl', 'ch'], **{r: [r, r + 'd', r + 'w', r + 'b'] for r in ('r10', 'r11', 'r14', 'r15')}}


def annotation(text):
    """The flag-analysis annotation of one instruction line (control flow included)."""
    m = {'jnz': 'jne', 'jz': 'je'}.get(text.split()[0], text.split()[0])
    if re.fullmatch(r'j(' + '|'.join(CCS) + ')', m): return {'op': 'jcc', 'cc': m[1:]}
    if m in ('jmp', 'call', 'ret', 'loop', 'loope', 'loopne', 'jrcxz'): return {'op': m}
    return gen.annotate(text)


class Segment(gen.Prog):
    """gen_slice1's straight-line generator, restricted: no stack instructions or rsp operands, none of the `forbidden`
    registers (any alias), and flags assumed undefined at the start (anything reading them defines them first)."""
    def __init__(self, rng, forbidden):
        super().__init__('segment', rng)
        self.undef = flags.ALL6
        self.pattern = re.compile(r'\b(?:' + '|'.join(a for r in forbidden for a in ALIASES[r]) + r')\b') if forbidden else None
        self.rejected = False

    def emit(self, text, count=None, **extra):
        if re.match(r'(push|pop)\b', text) or 'rsp' in text or (self.pattern and self.pattern.search(text)):
            raise Reject
        super().emit(text, count, **extra)

class Reject(Exception): pass

def segment(rng, forbidden, n):
    """`n` instructions: (lines, annotations). An instruction group that needs a forbidden register is redrawn."""
    p = Segment(rng, forbidden)
    while len(p.insns) < n:
        snapshot = (len(p.lines), p.undef, p.cl, p.depth)
        try: p.one()
        except Reject:
            del p.lines[snapshot[0]:]; del p.insns[snapshot[0]:]; p.undef, p.cl, p.depth = snapshot[1:]
    return p.lines, p.insns


class Flow:
    def __init__(self, rng):
        self.rng, self.items, self.labels, self.functions, self.tables, self.pending = rng, [], 0, {}, [], []

    # --- emission -----------------------------------------------------------------------------------------------
    def label(self):
        self.labels += 1
        return f'L{self.labels}'
    def put(self, text):
        self.items.append(('insn', text, annotation(text)))
    def at(self, label):
        self.items.append(('label', label))
    def straight(self, forbidden, n):
        lines, anns = segment(self.rng, forbidden, n)
        self.items += [('insn', l, a) for l, a in zip(lines, anns)]
        return n
    def mark(self): return len(self.items)
    def header(self, mark, *texts):
        """Inserts a loop's header (decided after its body: the trip count depends on the body's cost) at `mark`."""
        self.items[mark:mark] = [('label', t[:-1]) if t.endswith(':') else ('insn', t, annotation(t)) for t in texts]

    def compare(self):
        """A `cmp` of a register with an immediate or another register, defining all six flags."""
        r = self.rng
        names = {64: ['rax', 'rdx', 'r9', 'r8', 'r10', 'r11', 'r14', 'r15'], 32: ['eax', 'edx', 'r9d', 'r8d', 'r10d', 'r11d', 'r14d', 'r15d'],
                 8: ['al', 'dl', 'r9b', 'r8b']}
        w = r.choice([64, 32, 32, 8]); a = r.choice(names[w])
        b = r.choice(names[w]) if r.random() < 0.3 else str(r.choice([0, 1, 2, 3, 5, 7, -1, 0x7f, r.randrange(-128, 128)]))
        self.put(f'cmp {a}, {b}')

    # --- templates (each returns the dynamic instruction count of one execution) --------------------------------
    def block(self, depth, forbidden):
        return sum(self.item(depth, forbidden) for _ in range(self.rng.randrange(1, 4)))

    def item(self, depth, forbidden):
        kinds = ['seq'] * 3 + (['if'] * 3 + ['call'] * 2 + ['loop'] * 5 + ['table'] if depth < 3 else [])
        return getattr(self, 'do_' + self.rng.choice(kinds))(depth, forbidden)

    def do_seq(self, depth, forbidden):
        return self.straight(forbidden, self.rng.randrange(2, 9))

    def do_if(self, depth, forbidden):
        r = self.rng; skip, end = self.label(), self.label()
        self.compare(); self.put(f'j{r.choice(CCS)} {skip}')
        cost = 2 + self.block(depth + 1, forbidden)
        if r.random() < 0.6:
            self.put(f'jmp {end}'); self.at(skip)
            cost += 1 + self.block(depth + 1, forbidden)
            self.at(end)
        else:
            self.at(skip)
        return cost

    def trips(self, body_cost, overhead):
        n = self.rng.choice([1, 2, 3, 4, 5, 8, 16, 31, 64, self.rng.randrange(1, 65)])
        return max(1, min(n, LOOP_CAP // (body_cost + overhead)))

    def do_loop(self, depth, forbidden):
        kind = self.rng.choice(['dec', 'dec', 'count', 'while', 'loop', 'loop', 'loope', 'loopne', 'jrcxz', 'zero_trip'])
        return (self.rcx if kind in ('loop', 'loope', 'loopne', 'jrcxz', 'zero_trip') else self.counted)(kind, depth, forbidden)

    def counted(self, kind, depth, forbidden):
        """Counter loops on COUNTERS[depth]: `dec` (do-while, `dec; jnz/jne/jg`), `count` (`inc/add; cmp N; jb/jl/jne`), and
        `while` (jump to the test at the bottom; N may be zero)."""
        r, c, top = self.rng, COUNTERS[depth], self.label()
        mark = self.mark()
        cost = self.block(depth + 1, forbidden + [c])
        if kind == 'dec':
            n = self.trips(cost, 2)
            self.header(mark, f'mov {c}d, {n}', f'{top}:')
            self.put(f'dec {c}d'); self.put(f'{r.choice(["jnz", "jne", "jg"])} {top}')
            return 1 + n * (cost + 2)
        if kind == 'count':
            n = self.trips(cost, 3)
            self.header(mark, f'xor {c}d, {c}d', f'{top}:')
            self.put(r.choice([f'inc {c}d', f'add {c}d, 1'])); self.put(f'cmp {c}d, {n}'); self.put(f'{r.choice(["jb", "jl", "jne"])} {top}')
            return 1 + n * (cost + 3)
        test = self.label()
        n = r.choice([0, 0, 1, 2]) if r.random() < 0.3 else self.trips(cost, 4)
        self.header(mark, f'xor {c}d, {c}d', f'jmp {test}', f'{top}:')
        self.put(f'inc {c}d'); self.at(test); self.put(f'cmp {c}d, {n}'); self.put(f'{r.choice(["jb", "jl"])} {top}')
        return 3 + n * (cost + 3)

    def small(self, forbidden):
        """A body of up to three straight instructions (the rel8 branches reach 128 bytes) with rcx reserved."""
        return self.straight(forbidden + ['rcx'], self.rng.randrange(1, 4))

    def rcx(self, kind, depth, forbidden):
        """Branches on rcx (rel8 only): `loop`/`loope`/`loopne` (a `cmp` before the last two decides; rcx bounds the trips), a
        `jrcxz` over a small body (rcx zero or not), and a zero-trip guard (`mov ecx, N; jrcxz end; top: ...; loop top; end:`)."""
        r, top = self.rng, self.label()
        if kind == 'jrcxz':
            skip, n = self.label(), r.choice([0, 0, 1, 7, 2**32 - 1])
            self.put(f'mov ecx, {n}'); self.put(f'jrcxz {skip}')
            cost = self.small(forbidden); self.at(skip)
            return 2 + (0 if n == 0 else cost)
        mark = self.mark()
        cost = self.small(forbidden)
        if kind == 'zero_trip':
            end = self.label(); n = r.choice([0, 0, 1, 2, 5]); n = min(n, max(1, LOOP_CAP // (cost + 1)))
            self.header(mark, f'mov ecx, {n}', f'jrcxz {end}', f'{top}:')
            self.put(f'loop {top}'); self.at(end)
            return 2 + n * (cost + 1)
        n = self.trips(cost, 3)
        self.header(mark, f'mov ecx, {n}', f'{top}:')
        if kind != 'loop':
            if r.random() < 0.4: self.put('cmp rax, rax')       # ZF set: loope runs all trips, loopne leaves after the first
            else: self.compare()
        self.put(f'{kind} {top}')
        return 1 + n * (cost + 2)

    def do_call(self, depth, forbidden):
        """A call of a leaf function, of a function calling a leaf, or of a recursive one at depth 1 to 8."""
        r = self.rng
        kind = r.choice(['leaf', 'leaf', 'nested', 'recursive'])
        name = self.function(kind)
        if kind == 'recursive':
            d = r.choice([1, 2, 3, 8, r.randrange(1, 9)])
            self.put(f'mov {RECURSION}d, {d}'); self.put(f'call {name}')
            return 2 + self.functions[name][1](d)
        self.put(f'call {name}')
        return 1 + self.functions[name][1]

    def function(self, kind):
        """A function of `kind` (leaf, nested: calls a leaf, recursive: on the depth counter): defined at most four times per
        program, its body emitted after the main part. Returns its label; `self.functions[label]` = (kind, cost)."""
        same = [n for n, (k, _) in self.functions.items() if k == kind]
        if same and (len(self.functions) >= 4 or self.rng.random() < 0.5): return self.rng.choice(same)
        name = f'{kind}{len(self.functions) + len(self.pending)}'
        saved, self.items = self.items, []
        owned = ['rcx', RECURSION] + COUNTERS
        self.at(name)
        if kind == 'leaf':
            cost = self.straight(owned, self.rng.randrange(2, 7)) + 1
        elif kind == 'nested':
            leaf = self.function('leaf')
            cost = self.straight(owned, self.rng.randrange(1, 4)); self.put(f'call {leaf}')
            cost += 1 + self.functions[leaf][1] + self.straight(owned, 1) + 1
        else:    # dec; jz end; A; call self; B; end: ret
            end = self.label()
            self.put(f'dec {RECURSION}d'); self.put(f'jz {end}')
            a = self.straight(owned, self.rng.randrange(1, 4)); self.put(f'call {name}'); b = self.straight(owned, self.rng.randrange(1, 4))
            self.at(end)
            cost = lambda d, a=a, b=b: (d - 1) * (a + b + 4) + 3        # d entries; the last one returns at once
        self.put('ret')
        self.pending.append(self.items)
        self.items = saved
        self.functions[name] = (kind, cost)
        return name

    def do_table(self, depth, forbidden):
        """`and r9d, M; mov eax, TABLE; jmp [rax + r9*8]` to one of four case blocks (M = 1 leaves two unreached), each ending
        in a jump to the join."""
        r = self.rng; cases, end = [self.label() for _ in range(4)], self.label()
        k = len(self.tables); self.tables.append(cases)
        self.put(f'and r9d, {r.choice([1, 3, 3])}'); self.put(f'mov eax, {TABLE + 32 * k}'); self.put('jmp qword ptr [rax + r9*8]')
        costs = []
        for i, c in enumerate(cases):
            self.at(c); costs.append(self.block(depth + 1, forbidden))
            if i < 3: self.put(f'jmp {end}')
        self.at(end)
        return 3 + max(costs) + 1


def finish(f, name, gpr, rflags, rng):
    """Assemble twice: instruction offsets and RIP-relative displacements, then the jump tables' contents (label addresses)."""
    lines, count = [], 0
    for item in f.items:
        if item[0] == 'label': lines.append(f'{item[1]}:')
        else: lines += [f'I{count}:', item[1], f'E{count}:']; count += 1
    _, labels = ref.assemble('\n'.join(re.sub(r'\{\d+\}', '0', l) for l in lines), labels=True)
    n = [-1]
    def resolve(l):
        if re.match(r'I\d+:$', l): n[0] = int(l[1:-1])
        return re.sub(r'\{(\d+)\}', lambda m: str(int(m[1]) - (CODE + labels[f'E{n[0]}'])), l)
    lines = [resolve(l) for l in lines]
    code, labels = ref.assemble('\n'.join(lines), labels=True)
    insns = [item for item in f.items if item[0] == 'insn']
    offsets = [labels[f'I{i}'] for i in range(len(insns))]
    assert offsets == sorted(set(offsets)), 'instruction offsets'
    table = bytearray(0x1000)
    for t, cases in enumerate(f.tables):
        for i, c in enumerate(cases): table[32 * t + 8 * i:32 * t + 8 * i + 8] = (CODE + labels[c]).to_bytes(8, 'little')
    mem = [{'addr': hex(DATA), 'data': bytes(rng.getrandbits(8) for _ in range(REGION)).hex()},
           {'addr': hex(STACK), 'data': bytes(rng.getrandbits(8) for _ in range(REGION)).hex()}]
    if f.tables: mem.append({'addr': hex(TABLE), 'data': bytes(table).hex()})
    return {'name': name, 'flow': True, 'code': code.hex(), 'rip': hex(CODE), 'gpr': [hex(g) for g in gpr], 'rflags': hex(rflags), 'memory': mem,
            'insns': [i[2] for i in insns], 'offsets': offsets, 'asm': [l for l in lines if not re.match(r'[IE]\d+:$', l)]}


FEATURES = ['dec', 'count', 'while', 'loop', 'loope', 'loopne', 'jrcxz', 'zero_trip', 'nested', 'call', 'recursive', 'table', 'if']


def program(seed, name, feature):
    """A program containing `feature` (so the corpus covers every template whatever the draws), redrawn while the assembler
    cannot encode it (a rel8 branch out of range)."""
    for attempt in range(200):
        rng = random.Random(seed + attempt * 7919)
        f = Flow(rng)
        gpr, rflags = gen.initial(rng)
        try:
            f.straight([], rng.randrange(2, 5))
            if feature in ('dec', 'count', 'while'): f.counted(feature, 0, [])
            elif feature in ('loop', 'loope', 'loopne', 'jrcxz', 'zero_trip'): f.rcx(feature, 0, [])
            elif feature == 'nested':    # three counter loops inside each other
                top, mid, inner = f.label(), f.label(), f.label()
                f.put(f'mov r14d, {rng.randrange(2, 6)}'); f.at(top); f.straight(['r14'], 2)
                f.put(f'mov r15d, {rng.randrange(2, 6)}'); f.at(mid); f.straight(['r14', 'r15'], 2)
                f.put(f'mov r11d, {rng.randrange(1, 5)}'); f.at(inner); f.straight(['r14', 'r15', 'r11'], rng.randrange(2, 5))
                f.put('dec r11d'); f.put(f'jnz {inner}'); f.put('dec r15d'); f.put(f'jnz {mid}'); f.put('dec r14d'); f.put(f'jnz {top}')
            elif feature == 'call': f.put(f'call {f.function("leaf")}'); f.put(f'call {f.function("nested")}')
            elif feature == 'recursive': f.put(f'mov {RECURSION}d, {rng.randrange(2, 9)}'); f.put(f'call {f.function("recursive")}')
            elif feature == 'table': f.do_table(0, [])
            else: f.do_if(0, [])
            for _ in range(rng.randrange(0, 3)): f.item(0, [])
            f.straight([], rng.randrange(1, 4))
            if f.pending:
                f.put('jmp Lend')
                for body in f.pending: f.items += body
            f.at('Lend')
            return finish(f, name, gpr, rflags, rng)
        except ValueError:
            continue
    raise ValueError(f'{name}: no encodable draw')


STRIDES = ['store', 'load', 'rmw', 'index', 'split-store', 'split-load', 'push', 'push-below', 'recursion']


def stride_program(kind, k, seed):
    """A loop whose memory access moves across a page boundary at iteration `k` (and runs a few iterations past it), so a
    page made absent or read-only is first touched in that iteration: store/load/rmw with a pointer stepping 0x40, an index
    form `[rbx + i*8]`, qword accesses stepping 3 bytes (a split access reaches the next page mid-loop), `push` crossing from
    one stack page to the one below, pushes running below the stack region (a fault without any mutation), and recursion one
    level deeper per outer iteration."""
    rng = random.Random(seed)
    f = Flow(rng)
    gpr, rflags = gen.initial(rng)
    n, top = k + 6, f.label()
    if kind in ('store', 'load', 'rmw'):
        f.put(f'mov rsi, {DATA + 0x1000 - 0x40 * k}'); f.put(f'mov r14d, {n}'); f.at(top)
        f.put({'store': 'mov qword ptr [rsi], rax', 'load': 'mov rax, qword ptr [rsi]', 'rmw': 'add qword ptr [rsi], rcx'}[kind])
        f.put('add rsi, 64'); f.put('dec r14d'); f.put(f'jnz {top}')
    elif kind == 'index':
        f.put(f'mov rbx, {DATA + 0x1000 - 8 * k}'); f.put('xor r15d, r15d'); f.at(top)
        f.put('mov qword ptr [rbx + r15*8 + 16], rcx'); f.put('inc r15d'); f.put(f'cmp r15d, {n}'); f.put(f'jb {top}')
    elif kind in ('split-store', 'split-load'):
        f.put(f'mov rsi, {DATA + 0x1000 - 8 - 3 * k}'); f.put(f'mov r14d, {n}'); f.at(top)
        f.put('mov qword ptr [rsi], rax' if kind == 'split-store' else 'mov rax, qword ptr [rsi]')
        f.put('add rsi, 3'); f.put('dec r14d'); f.put(f'jnz {top}')
    elif kind in ('push', 'push-below'):
        base = STACK + 0x1000 + 8 * k if kind == 'push' else STACK + 8 * k
        f.put(f'mov rsp, {base}'); f.put(f'mov r14d, {n}'); f.at(top)
        f.put('push rax'); f.put('add rax, 3'); f.put('dec r14d'); f.put(f'jnz {top}')
    else:    # recursion: outer iteration i calls a function recursing i levels
        fn, end = 'rec', f.label()
        f.put(f'mov rsp, {STACK + 0x1000 + 8 * k}'); f.put('mov r14d, 1'); f.at(top)
        f.put('mov r10d, r14d'); f.put(f'call {fn}'); f.put('inc r14d'); f.put(f'cmp r14d, {n}'); f.put(f'jb {top}')
        f.put('jmp Lend'); f.at(fn); f.put('dec r10d'); f.put(f'jz {end}'); f.put('add rax, 5'); f.put(f'call {fn}'); f.at(end); f.put('ret')
    f.at('Lend')
    return finish(f, f'stride-{kind}-{k}', gpr, rflags, rng)


def stride_corpus(seed):
    rng = random.Random(seed)
    return [stride_program(kind, k, rng.getrandbits(48)) for kind in STRIDES for k in (1, 4, 11)]


def unrolled(p, path_ix):
    """The straight-line program with the effects of the executed path `path_ix` (indices of p's annotated instructions): each
    instruction as it is, except control flow: jmp/jcc/jrcxz are dropped (they write nothing), `loop`/`loope`/`loopne` become
    `lea rcx, [rcx - 1]`, `call L` becomes `push RETURN` and `ret` becomes `lea rsp, [rsp + 8]` (none writes flags); RIP-relative
    operands keep their absolute target. QEMU's prefix-style run of it ends in the exact state at the end of the path (the state
    before the instruction that follows it), which a signal delivered mid-translation-block does not always give for the flags."""
    texts = [l for l in p['asm'] if not l.endswith(':')]
    rip = ref.num(p['rip'])
    ends = p['offsets'][1:] + [len(p['code']) // 2]
    f = Flow(random.Random(0))
    for i in path_ix:
        text, ann = texts[i], p['insns'][i]
        if ann['op'] in ('jmp', 'jcc', 'jrcxz'): continue
        if ann['op'] in ('loop', 'loope', 'loopne'): text, ann = 'lea rcx, [rcx - 1]', {'op': 'lea', 'width': 64}
        elif ann['op'] == 'call': text, ann = f'push {rip + ends[i]}', {'op': 'push', 'width': 64}
        elif ann['op'] == 'ret': text, ann = 'lea rsp, [rsp + 8]', {'op': 'lea', 'width': 64}
        else: text = re.sub(r'\[rip \+ (-?\d+)\]', lambda m: '[rip + {%d}]' % (rip + ends[i] + int(m[1])), text)
        f.items.append(('insn', text, ann))
    if not f.items: f.put('mov rax, rax')       # a fault at the first instruction: an empty path still needs a program
    q = finish(f, p['name'], [0] * 16, 0, random.Random(0))
    return dict(q, gpr=p['gpr'], rflags=p['rflags'], memory=p['memory'])


def corpus(seed, count):
    rng = random.Random(seed)
    return [program(rng.getrandbits(48), f'flow-{n:02d}-{FEATURES[n % len(FEATURES)] if n < 2 * len(FEATURES) else (feature := rng.choice(FEATURES))}',
                    FEATURES[n % len(FEATURES)] if n < 2 * len(FEATURES) else feature) for n in range(count)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261027); ap.add_argument('--count', type=int, default=64)
    a = ap.parse_args()
    progs = corpus(a.seed, a.count)
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_slice2.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(CODE), 'data': hex(DATA), 'stack': hex(STACK), 'table': hex(TABLE)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
