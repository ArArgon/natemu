#!/usr/bin/env python3
"""Loop probes for the slice-2 code cache (S2b): looping programs run in the VM against pinned QEMU, with the cache's counters.

    tools/with-env python3 tools/run_slice2_probes.py --fexc FEXC --out DIR

Each probe is a structured program: plain instructions, counted loops (`mov r32, N; L: body; dec r32; jnz L`, nestable) and
leaf calls (`call F` ... `F: body; ret`). Two forms are generated mechanically from the same structure:

* the looping form, assembled by clang and run in the VM (`natemu-host RUNTIME run-program`);
* its unrolling, a straight-line program with the same effects on GPRs, flags and memory: each loop becomes its trip count
  of copies of `body; dec r32` (`jnz` writes no state), each `call F` becomes `push RETURN` (the looping form's return
  address), F's body, and `lea rsp, [rsp + 8]` (`ret` writes no flags). Pinned QEMU (tools/reference/ref.py) runs it.

The VM's final GPRs, masked RFLAGS and memory must equal QEMU's, and it must stop at the looping form's end. Counters:
without a flush (only the one requested when the driver writes guest RAM), host translation requests must equal the
distinct block RIPs (every block is compiled once and then found in the VM); a forced small code cache (`--code-cache`,
at least the largest block) must flush repeatedly and still match; a cache smaller than that is refused. Linking (S2c): a
hot loop at N and 2N iterations makes the same resident entries, and a forged literal exit (its RIP move no longer
builds its link point's target) is refused at the reply (`bad_reply`, E2; the linker's own check stays behind it); every run self-checks its link points (the driver). A non-terminating loop must be stopped by the watchdog. Run-time tamper checks
(driver `--tamper`, `--tamper-after`): a link point reported outside its block is refused (`bad_reply`); a corrupted linked
branch or link record makes the next flush halt (`bad_link`) and is refused by the host's self-check
(`links_check`). Self-modifying code (S2e): stores into the running block (next instruction, an already executed one in a
loop, page-crossing), into another block's page, data on a loop's own code page, and FEX's upstream SelfModifyingCode tests
must reach their end with QEMU's state (QEMU runs the self-modifying program itself). Nothing is keyed by probe name;
expected values come only from QEMU. Writes DIR/probes.json; exit status 1 on any failure."""
import argparse, json, pathlib, random, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
import ref, gen_slice1 as gen, run_slice1_vm as vm  # noqa: E402

CODE, DATA, STACK = 0x100000, 0x200000, 0x300000


class Form:
    """Emits the looping (`loop=True`) or unrolled form of a structured program."""
    def __init__(self, functions, loop, returns=None):
        self.functions, self.loop, self.returns, self.lines, self.labels = functions, loop, returns or {}, [], 0

    def label(self):
        self.labels += 1
        return f'l{self.labels}'

    def emit(self, items):
        for item in items:
            if isinstance(item, str):
                self.lines.append(item)
            elif item[0] == 'loop':
                _, reg, trips, body = item
                self.lines.append(f'mov {reg}, {trips}')
                top = self.label()      # labels are numbered alike in both forms
                if self.loop:
                    self.lines.append(f'{top}:'); self.emit(body); self.lines += [f'dec {reg}', f'jnz {top}']
                else:
                    start = self.labels
                    for _ in range(trips):     # each copy numbers its labels as the looping form's one body
                        self.labels = start
                        self.emit(body); self.lines.append(f'dec {reg}')
            elif item[0] == 'call':
                back = self.label()     # the looping form's return address
                if self.loop:
                    self.lines += [f'call {item[1]}', f'{back}:']
                else:
                    self.lines.append(f'push {self.returns[back]:#x}')
                    self.emit(self.functions[item[1]]); self.lines.append('lea rsp, [rsp + 8]')
            elif item[0] == 'gap':      # ends the block with a jump over `item[1]` bytes that never execute
                after = self.label()
                if self.loop:
                    self.lines += [f'jmp {after}'] + ([f'.fill {item[1]}, 1, 0xcc'] if item[1] else []) + [f'{after}:']

    def program(self, main):
        self.emit(main)
        if self.loop and self.functions:
            self.lines.append('jmp end')
            for name, body in self.functions.items():
                self.lines.append(f'{name}:'); self.emit(body); self.lines.append('ret')
            self.lines.append('end:')
        return self.lines


def build(probe):
    """(looping program description, unrolled reference program, end RIP of the looping form)."""
    functions, main = probe.get('functions', {}), probe['main']
    assert all(isinstance(i, str) for body in functions.values() for i in body), 'leaf functions of plain instructions only'
    looping, labels = ref.assemble('\n'.join(Form(functions, True).program(main)), labels=True)
    returns = {name: CODE + offset for name, offset in labels.items()}
    unrolled = Form(functions, False, returns).program(main)
    code = ref.assemble('\n'.join(unrolled))
    common = {'rip': CODE, 'gpr': probe['gpr'], 'rflags': 0, 'memory': probe['memory']}
    reference = {'name': probe['name'], 'code': code.hex(), 'insns': [gen.annotate(t) for t in unrolled], **common}
    return {'name': probe['name'], 'code': looping.hex(), **common}, reference, CODE + len(looping)


def alu_body(seed, n):
    """`n` straight-line ALU instructions on scratch registers (never rcx/rsp/rsi), deterministic per seed."""
    rng, regs, out = random.Random(seed), ['rax', 'rdx', 'rbx', 'rdi', 'r8', 'r9', 'r10', 'r11'], []
    for _ in range(n):
        a, b = rng.choice(regs), rng.choice(regs)
        out.append(rng.choice([f'add {a}, {b}', f'xor {a}, {b}', f'sub {a}, {rng.randrange(1, 1 << 20)}',
                               f'imul {a}, {b}', f'rol {a}, {rng.randrange(1, 63)}', f'lea {a}, [{a} + {b}*2 + 7]']))
    return out


def add_chain(count, trips, rax=0):
    """`count` distinct `add rax, imm32` in a counted loop of `trips` (straight-line for `trips=None`), and the state
    they leave, derived from x86 semantics (ADD, DEC; SDM vol. 2) because the QEMU reference cannot run programs
    this long: (looping program description, expected result in ref.py's format, end RIP)."""
    imms = [0x10000 + 977 * i for i in range(count)]
    lines = [f'add rax, {v:#x}' for v in imms]
    lines = [f'mov ecx, {trips}', '1:'] + lines + ['dec ecx', 'jnz 1b'] if trips else lines
    code = ref.assemble('\n'.join(lines))
    gpr, mask = gprs(rax=rax), 2**64 - 1
    for _ in range(trips or 1):
        for v in imms:
            a = gpr[0]; r = (a + v) & mask; gpr[0] = r
            cf, af, of = a + v > mask, ((a ^ v ^ r) >> 4) & 1, ((~(a ^ v) & (a ^ r)) >> 63) & 1
    zf, sf, pf = r == 0, r >> 63, bin(r & 0xff).count('1') % 2 == 0
    if trips:     # the last `dec ecx` (1 -> 0) sets ZF PF, clears SF OF AF; CF stays the last add's
        gpr[1], zf, sf, pf, af, of = 0, 1, 0, 1, 0, 0
    rflags = 0x202 | cf | pf << 2 | af << 4 | zf << 6 | sf << 7 | of << 11
    program = {'name': f'adds-{count}x{trips}', 'code': code.hex(), 'rip': CODE, 'gpr': gprs(rax=rax), 'rflags': 0, 'memory': []}
    result = {'qemu': {'gpr': [hex(v) for v in gpr], 'rflags': hex(rflags), 'rip': hex(CODE + len(code)), 'memory': []},
              'rflags_compare_mask': hex(0x8d5)}
    return program, result, CODE + len(code)


def gprs(**values):
    g = [0x1111 * (i + 1) for i in range(16)]
    for name, v in values.items(): g[ref.REGS.index(name)] = v
    return g


ZERO = '00' * 64
PROBES = [
    # A counted loop whose body is one block at the default size (its back edge targets its own entry).
    {'name': 'counted', 'gpr': gprs(), 'memory': [],
     'main': [('loop', 'ecx', 40, ['add rax, rcx', 'xor rdx, rax', 'imul rbx, rdx'])]},
    # Two-block loop body (an unconditional jump splits it).
    {'name': 'split-body', 'gpr': gprs(), 'memory': [],
     'main': [('loop', 'ecx', 25, ['add rax, rcx', ('gap', 0), 'xor rdx, rax', 'rol rdx, 5'])]},
    # Nested loops storing to memory (load-add-store).
    {'name': 'nested-memory', 'gpr': gprs(rsi=DATA), 'memory': [{'addr': DATA, 'data': ZERO}],
     'main': [('loop', 'ecx', 6, [('loop', 'edx', 8, ['add qword ptr [rsi + rdx*8 - 8], rcx', 'add rax, rdx'])])]},
    # call/ret in a loop: a leaf function, then a second call site of it.
    {'name': 'call-loop', 'gpr': gprs(rsp=STACK + 0x800), 'memory': [{'addr': STACK, 'data': '00' * 0x1000}],
     'functions': {'f': ['add rax, rcx', 'xor rbx, rax'], 'g': ['imul rdx, rbx', 'sub rdx, 3']},
     'main': [('loop', 'ecx', 30, [('call', 'f'), 'add rdi, 1', ('call', 'g'), ('call', 'f')])]},
    # Two blocks 4 KiB apart share an L1 slot: every dispatch between them is an L1 miss found in L2
    # (`add` 3 bytes and a 5-byte `jmp` precede the gap).
    {'name': 'l1-conflict', 'gpr': gprs(), 'memory': [],
     'main': [('loop', 'ecx', 20, ['add rax, rcx', ('gap', 4096 - 8), 'xor rdx, rax'])]},
    # A long loop body: at one instruction per block it overflows a small code cache every iteration. (The unrolled
    # reference stays within 1 KiB: QEMU's `in_asm` log shows an instruction crossing a 1 KiB boundary as bytes,
    # which ref.py's disassembly cross-check refuses.)
    {'name': 'long-body', 'gpr': gprs(), 'memory': [], 'main': [('loop', 'ecx', 3, alu_body(7, 70))]},
]
FEX_SMC = ROOT / 'third_party' / 'FEX' / 'unittests' / 'ASM' / 'SelfModifyingCode'
# Self-modifying programs (assembly, extra memory regions). Expected values come only from QEMU; the comments say what
# the SDM rule ("Handling Self- and Cross-Modifying Code") makes them compute.
SMC = [
    # A data store to the running block's own page (no translated byte changes).
    ('smc-own-page', 'mov dword ptr [rip + 0x7fa], eax', [{'addr': CODE + 0x800, 'data': '00' * 0x10}]),
    # A store into the next instruction of the running block: its new immediate (5) is executed.
    ('smc-next-insn', 'mov byte ptr [rip + 1f + 3], 5\n1: add rax, 1\nadd rbx, rax', []),
    # A page-crossing store (8 bytes over CODE + 0x1000) into the running block's next two instructions.
    ('smc-split-next', 'movabs rdx, 0x20c0834810c08348\njmp 1f\n.org 0xff5, 0xcc\n1: mov qword ptr [rip + 2f], rdx\n'
                       '2: add rax, 1\nadd rax, 2\nadd rbx, rax', []),
    # A store into another block (a function on the next page), which then runs retranslated: adds 0x11, 3, 2.
    ('smc-other-page', 'mov ecx, 3\n1: call f\nmov byte ptr [rip + f + 3], cl\ndec ecx\njnz 1b\njmp 2f\n'
                       '.org 0x1000, 0xcc\nf: add rax, 0x11\nret\n2:', [{'addr': STACK, 'data': '00' * 0x1000}]),
    # A page-crossing store from a block on a third page into a function spanning CODE + 0x1000: adds 3, then 0x30.
    ('smc-split-other', 'mov ecx, 2\nmovabs rdx, 0x20c0834810c08348\n1: call f\njmp far\nback: dec ecx\njnz 1b\njmp 2f\n'
                        '.org 0xffc, 0xcc\nf: add rax, 1\nadd rax, 2\nret\n.org 0x2100, 0xcc\nfar: mov qword ptr [rip + f], rdx\n'
                        'jmp back\n2:', [{'addr': STACK, 'data': '00' * 0x1000}]),
    # A store into an already executed instruction of the running loop body: the next iteration adds 4, 3, 2 (after 1).
    ('smc-earlier-loop', 'mov ecx, 4\n1: add rax, 1\nmov byte ptr [rip + 1b + 3], cl\ndec ecx\njnz 1b', []),
    # Code rewritten every iteration, each time just before it runs: rax = (rax + ecx) * 3 for ecx = 5..1.
    ('smc-patch-loop', 'mov ecx, 5\n1: mov byte ptr [rip + 2f + 3], cl\n2: add rax, 0x7f\nimul rax, rax, 3\ndec ecx\njnz 1b', []),
    # Data on the loop's own code page, written every iteration (the known cliff: a step and a recompile each time).
    ('smc-data-loop', f'mov esi, {CODE + 0x800:#x}\nmov ecx, 50\n1: add qword ptr [rsi], rcx\nadd rax, rcx\ndec ecx\njnz 1b',
     [{'addr': CODE + 0x800, 'data': '00' * 8}]),
    # A byte of the running loop body incremented by itself four times, then read back as data (movzx).
    ('smc-self-inc', 'mov ecx, 4\n1: add byte ptr [rip + 1b + 6], 1\ndec ecx\njnz 1b\nmovzx eax, byte ptr [rip + 1b + 6]', []),
    # A function straddling CODE + 0x1000 (its `add rax, 1` on the second page), patched from a third page on each call.
    ('smc-span-other-p2', 'mov ecx, 3\n1: call g\njmp far\nback: dec ecx\njnz 1b\njmp 2f\n.org 0xff8, 0xcc\n'
                          'g: nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nadd rax, 1\nret\n.org 0x2100, 0xcc\n'
                          'far: mov byte ptr [rip + g + 11], cl\njmp back\n2:', [{'addr': STACK, 'data': '00' * 0x1000}]),
    # A memory XCHG (S3e, `swpal`) into the next instruction (its immediate becomes 5, al gets the old 1), and one rewriting
    # the loop's next instruction every iteration, as smc-patch-loop.
    ('smc-xchg-next', 'mov eax, 5\nxchg byte ptr [rip + 1f + 3], al\n1: add rax, 1\nadd rbx, rax', []),
    ('smc-xchg-loop', 'mov ecx, 5\n1: mov edx, ecx\nxchg byte ptr [rip + 2f + 3], dl\n2: add rax, 0x7f\nimul rax, rax, 3\n'
                      'add rbx, rdx\ndec ecx\njnz 1b', []),
]
# Every SMC probe ends by copying its own final bytes (the code as it stands then) into this region, which is compared.
CODE_COPY = 0x180000


def writable_code(program):
    """Memory regions holding the program's own code bytes on each code page no region of the program touches: they make
    those pages writable in the VM (the driver maps a page with a region writable, and executable if code is on it)."""
    rip, code = ref.num(program['rip']), bytes.fromhex(program['code'])
    touched = {a >> 12 for m in program['memory'] for a in range(ref.num(m['addr']), ref.num(m['addr']) + len(m['data']) // 2)}
    pages = sorted({a >> 12 for a in range(rip, rip + len(code))} - touched)
    spans = [(max(rip, p << 12), min(rip + len(code), (p + 1) << 12)) for p in pages]
    return [{'addr': s, 'data': code[s - rip:e - rip].hex()} for s, e in spans]


def smc_programs():
    """[(VM program, QEMU reference program, FEX's expected GPRs)]: the SMC probes above and FEX's upstream
    SelfModifyingCode tests (assembled with nasm at 0x10000; their final `hlt` dropped, so they end where their bytes do;
    stack at 0xe0001000 as FEX's DifferentBlock sets it)."""
    out = []
    def add(name, code, rip, memory, gpr, expected):
        program = {'name': name, 'code': code.hex(), 'rip': rip, 'gpr': gpr, 'rflags': 0, 'memory': memory}
        out.append((program, {**program, 'insns': [{'op': 'mov', 'width': 64}]}, expected))
    for name, text, memory in SMC:
        # r8..r12 are scratch (equal in both runs); `loop` and `lea` leave the flags alone.
        size = (len(ref.assemble(text)) + 7) // 8 * 8
        tail = (f'\nmov r12, rcx\nlea r8, [rip + Lcode]\nmov r9, {CODE_COPY:#x}\nmov ecx, {size // 8}\n'
                'Lcopy: mov r10, qword ptr [r8]\nmov qword ptr [r9], r10\nlea r8, [r8 + 8]\nlea r9, [r9 + 8]\nloop Lcopy\nmov rcx, r12')
        add(name, ref.assemble('Lcode:\n' + text + tail), CODE, memory + [{'addr': CODE_COPY, 'data': '00' * size}],
            gprs(rsp=STACK + 0x800), {})
    for test in sorted(FEX_SMC.glob('*.asm')):
        source = test.read_text()
        config = json.loads(source.split('%ifdef CONFIG', 1)[1].split('%endif', 1)[0])
        with __import__('tempfile').TemporaryDirectory() as d:
            src, binary = pathlib.Path(d, 'a.asm'), pathlib.Path(d, 'a.bin')
            src.write_text('BITS 64\norg 0x10000\n' + source)
            subprocess.run(['nasm', '-f', 'bin', '-o', binary, src], check=True)
            code = binary.read_bytes()
        assert code[-1] == 0xf4, f'{test.name} does not end with hlt'
        expected = {r.lower(): int(v, 0) for r, v in config['RegData'].items()}
        add('fex-smc-' + test.stem, code[:-1], 0x10000, [{'addr': 0xe0000f00, 'data': '00' * 0x100}], [0] * 16, expected)
    return out


# (probe, extra run-program arguments, expect repeated flushes: True, none: False, either: None)
RUNS = [(p['name'], args, False) for p in PROBES for args in ([], ['1'], ['2'])]
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--qemu', type=pathlib.Path)
    a = ap.parse_args()
    host, runtime = vm.build()
    a.out.mkdir(parents=True, exist_ok=True)
    built = {p['name']: build(p) for p in PROBES}
    raw = pathlib.Path(__import__('os').environ['NATEMU_RAW_ROOT'])
    qemu = a.qemu or raw / 'reference-slice1-20261026-01' / 'pin' / 'qemu-x86_64-static'
    results, _ = ref.run_programs([r for _, r, _ in built.values()], a.out / 'ref', qemu)
    expected = {r['name']: r for r in results}
    out, failures = [], 0

    def run(name, program, args):
        work = a.out / 'work' / name
        work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(vm.description(program))
        r = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + args + vm.CODE_SHAPE, capture_output=True, text=True)
        return (json.loads(r.stdout) if r.returncode == 0 else None), r.stderr.strip()[-300:]

    for name, args, flushes in RUNS:
        program, _, end = built[name]
        state, err = run(name, program, args)
        entry = {'name': name, 'args': args, 'stderr': err}
        if state is None:
            diffs = [f'host error: {err}']
        else:
            s = state['stats']
            diffs = ref.compare(expected[name], [ref.num(v) for v in state['gpr']], ref.num(state['rflags']),
                                ref.num(expected[name]['qemu']['rip']), state['memory'])
            if state['stop'] != 'reached' or ref.num(state['rip']) != end:
                diffs.insert(0, f"stopped {state['stop']} {state['reason']} at {state['rip']}, end {end:#x}")
            if flushes and s['flushes'] < 3:
                diffs.append(f"expected repeated flushes, got {s['flushes']}")
            if flushes is False and (s['flushes'] != 1 or s['host_requests'] != s['distinct'] or s['requests'] != s['host_requests']):
                diffs.append(f"requests {s['requests']}/{s['host_requests']} vs distinct blocks {s['distinct']}, flushes {s['flushes']}")
            entry['stats'] = s
        entry['diffs'] = diffs
        failures += bool(diffs)
        out.append(entry)
        print(f"{name} {' '.join(args)}: {'fail' if diffs else 'match'} {entry.get('stats', {})} {'; '.join(diffs)[:300]}")

    # Derived from semantics (see add_chain): a forced code cache of the smallest size flushes repeatedly and still
    # matches; a straight-line program of 33,000 blocks reaches its end although its host work (one compile per block)
    # far exceeds the watchdog, which bounds guest time between host exits only.
    for (count, trips), args, flushes in [((600, 3), ['1', '--code-cache', '32768'], True), ((600, 3), ['2', '--code-cache', '32768'], True),
                                         ((33000, None), ['1', '--watchdog-ms', '2000'], False)]:
        program, result, end = add_chain(count, trips)
        state, err = run(program['name'], program, args)
        diffs = [f'host error: {err}'] if state is None else ref.compare(result, [ref.num(v) for v in state['gpr']],
                 ref.num(state['rflags']), ref.num(state['rip']), state['memory'])
        if state is not None:
            if state['stop'] != 'reached' or ref.num(state['rip']) != end: diffs.insert(0, f"stopped {state['stop']} at {state['rip']}")
            if flushes and state['stats']['flushes'] < 3: diffs.append(f"expected repeated flushes, got {state['stats']['flushes']}")
            if flushes and not state['stats']['stale_links']: diffs.append('expected a flush during a link target request (stale_links)')
        failures += bool(diffs)
        out.append({'name': program['name'], 'args': args, 'stats': state and state['stats'], 'diffs': diffs, 'stderr': err})
        print(f"{program['name']} {' '.join(args)}: {'fail' if diffs else 'match'} {state and state['stats']} {'; '.join(diffs)[:300]}")

    # A code cache smaller than the largest block is refused at setup.
    program, _, _ = add_chain(1, None)
    state, err = run('tiny-cache', program, ['--code-cache', '1'])
    ok = state is None and 'must hold the largest block' in err
    failures += not ok
    out.append({'name': 'tiny-cache', 'stderr': err})
    print(f"tiny-cache --code-cache 1: {'match' if ok else 'fail'} {err}")

    # Linking (S2c): once a hot loop's edges are linked it runs without entering the resident, so N and 2N iterations
    # (N = 10^6) make the same number of resident entries (`blocks`) and linker entries. Dispatcher hits are not counted
    # (generated code writes no emulator data), so the loop's blocks must have no computed exit (`dispatch_exits` 0): with
    # equal resident and linker entries at N and 2N, every edge the loop repeats is then a direct branch.
    for args in ([], ['1'], ['2']):
        counts = []
        for trips in (10**6, 2 * 10**6):
            program, result, end = add_chain(3, trips)
            state, err = run(f'hot-{trips}', program, args)
            diffs = [f'host error: {err}'] if state is None else ref.compare(result, [ref.num(v) for v in state['gpr']],
                     ref.num(state['rflags']), ref.num(state['rip']), state['memory'])
            if state is not None:
                if state['stop'] != 'reached': diffs.insert(0, f"stopped {state['stop']} at {state['rip']}")
                st = state['stats']
                counts.append((st['blocks'], st['linker'], st['linked']))
                if st['dispatch_exits']:
                    diffs.append(f"{st['dispatch_exits']} computed exits: the loop could run through the dispatcher uncounted")
            failures += bool(diffs)
            out.append({'name': program['name'], 'args': args, 'stats': state and state['stats'], 'diffs': diffs, 'stderr': err})
            print(f"{program['name']} {' '.join(args)}: {'fail' if diffs else 'match'} {state and state['stats']} {'; '.join(diffs)[:300]}")
        ok = len(counts) == 2 and counts[0] == counts[1] and counts[0][2] > 0
        failures += not ok
        out.append({'name': 'hot-loop-entries', 'args': args, 'counts': counts})
        print(f"hot loop {' '.join(args)}: {'match' if ok else 'fail'} (resident entries, linker entries, linked) at N, 2N: {counts}")

    # A literal exit whose RIP is none of its block's link targets (admission refuses this; the driver forges it after
    # admission) is refused at the reply, before the block is placed: its move must build its link point's target.
    program, _, _ = built['split-body']
    state, err = run('forged-exit', program, ['--forge-exit'])
    ok = state is not None and state['stop'] == 'resident' and state['reason'].startswith('bad_reply') and state['stats']['links'] == 0
    failures += not ok
    out.append({'name': 'forged-exit', 'stop': state and state['stop'], 'reason': state and state['reason'], 'stderr': err})
    print(f"forged-exit: {'match' if ok else 'fail'} {state and state['stop']} {state and state['reason']}")

    # Run-time tampering (driver options): what admission and the resident never produce must be caught. A host-supplied
    # link point outside its block is refused at placement; a corrupted linked branch or link record
    # (corrupted just before a flush) halts the unlink, and the host's self-check refuses the same corruption after a run.
    flush_args = ['1', '--code-cache', '32768']
    program = add_chain(600, 3)[0]
    state, err = run('tamper-offset', program, flush_args + ['--tamper', 'offset'])
    ok = state is not None and state['tampered'] and state['stop'] == 'resident' and state['reason'].startswith('bad_reply')
    failures += not ok
    out.append({'name': 'tamper-offset', 'stop': state and state['stop'], 'reason': state and state['reason'], 'stderr': err})
    print(f"tamper offset: {'match' if ok else 'fail'} {state and state['stop']} {state and state['reason']} {err}")
    for kind in ('word', 'record'):
        state, err = run(f'tamper-{kind}', program, flush_args + ['--tamper', kind])
        ok = state is not None and state['tampered'] and state['stop'] == 'resident' and state['reason'].startswith('bad_link')
        failures += not ok
        out.append({'name': f'tamper-{kind}', 'stop': state and state['stop'], 'reason': state and state['reason'], 'stderr': err})
        print(f"tamper {kind} before a flush: {'match' if ok else 'fail'} {state and state['stop']} {state and state['reason']} {err}")
        small, _, _ = add_chain(3, 40)
        state, err = run(f'tamper-after-{kind}', small, ['--tamper-after', kind])
        ok = state is not None and state['tampered'] and state['stop'] == 'reached' and state['links_check'] != 'ok'
        failures += not ok
        out.append({'name': f'tamper-after-{kind}', 'links_check': state and state['links_check'], 'stderr': err})
        print(f"tamper {kind} after the run: {'match' if ok else 'fail'} host self-check: {state and state['links_check']} {err}")
    state, err = run('untampered', add_chain(3, 40)[0], [])
    ok = state is not None and state['links_check'] == 'ok' and not state['tampered']
    failures += not ok
    print(f"untampered: {'match' if ok else 'fail'} {state and state['links_check']}")

    # The watchdog stops a loop that never ends (no host exit after its first block; linked, it never leaves the code).
    spin = {'name': 'spin', 'code': ref.assemble('1: inc rax\njmp 1b').hex(), 'rip': CODE, 'gpr': gprs(), 'rflags': 0, 'memory': []}
    state, err = run('spin', spin, ['--watchdog-ms', '300'])
    ok = state is not None and state['stop'] == 'watchdog' and state['stats']['linked'] == 1
    failures += not ok
    out.append({'name': 'spin', 'stop': state and state['stop'], 'stats': state and state['stats'], 'stderr': err})
    print(f"spin --watchdog-ms 300: {'match' if ok else 'fail'} {state and state['stop']} {state and state['stats']}")

    # Self-modifying code (S2e). Each program runs in the VM with its code pages writable (regions over the code holding
    # the same bytes, not compared: `writable_code`) and under pinned QEMU as is (its code is writable there): QEMU translates the
    # rewritten bytes, so ref.py's check that it translated exactly the supplied bytes cannot hold and is skipped for
    # these programs. Their last flag writers define all six arithmetic flags (or none is written), so all are compared.
    smc = smc_programs()
    saved = ref.check_annotations
    ref.check_annotations = lambda p, log: []
    try:
        smc_results, _ = ref.run_programs([r for _, r, _ in smc], a.out / 'ref-smc', qemu)
    finally:
        ref.check_annotations = saved
    for (program, reference, expected_gpr), result in zip(smc, smc_results):
        end = ref.num(program['rip']) + len(program['code']) // 2
        for args in ([], ['1'], ['2']):
            cover = writable_code(program)
            state, err = run(program['name'], {**program, 'memory': cover + program['memory']}, args)
            diffs = [f'host error: {err}'] if state is None else ref.compare(result, [ref.num(v) for v in state['gpr']],
                     ref.num(state['rflags']), ref.num(state['rip']), state['memory'][len(cover):])
            if state is not None and (state['stop'] != 'reached' or ref.num(state['rip']) != end):
                diffs.insert(0, f"stopped {state['stop']} {state['reason']} at {state['rip']}, end {end:#x}")
            # FEX's expected registers (RegData): a second reference.
            diffs += [f'{r}: FEX expects {v:#x}, qemu {result["qemu"]["gpr"][ref.REGS.index(r)]}'
                      for r, v in expected_gpr.items() if ref.num(result['qemu']['gpr'][ref.REGS.index(r)]) != v]
            failures += bool(diffs)
            out.append({'name': program['name'], 'args': args, 'stats': state and state['stats'], 'diffs': diffs, 'stderr': err})
            print(f"{program['name']} {' '.join(args)}: {'fail' if diffs else 'match'} {state and state['stats']} {'; '.join(diffs)[:300]}")
    (a.out / 'probes.json').write_text(json.dumps(out, indent=1) + '\n')
    print(f'{len(out) - failures} match, {failures} fail')
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
