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
at least the largest block) must flush repeatedly and still match; a cache smaller than that is refused. A non-terminating loop must be stopped by the watchdog. A block refused for a
native backward branch (see SELF_LOOP) is reported as unsupported. Nothing is keyed by probe
name; expected values come only from QEMU. Writes DIR/probes.json; exit status 1 on any failure."""
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
# (probe, extra run-program arguments, expect repeated flushes: True, none: False, either: None)
RUNS = [(p['name'], args, False) for p in PROBES for args in ([], ['1'], ['2'])]
# A declared limit, not a failure: FEX compiles a jcc back to its own block's entry as a native backward branch (the
# taken edge of `CondJump` uses the compile's own entry as a jump target even without multiblock), which admission
# refuses. A loop whose body is one block therefore runs only with a block limit until the producer exits there.
SELF_LOOP = 'not forward to an instruction of the code region'


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
    out, failures, unsupported = [], 0, 0

    def run(name, program, args):
        work = a.out / 'work' / name
        work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(vm.description(program))
        r = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + args, capture_output=True, text=True)
        return (json.loads(r.stdout) if r.returncode == 0 else None), r.stderr.strip()[-300:]

    for name, args, flushes in RUNS:
        program, _, end = built[name]
        state, err = run(name, program, args)
        entry = {'name': name, 'args': args, 'stderr': err}
        if state is None and SELF_LOOP in err:
            unsupported += 1
            out.append(entry | {'outcome': 'unsupported'})
            print(f"{name} {' '.join(args)}: unsupported (self-loop branch refused by admission)")
            continue
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

    # The watchdog stops a loop that never ends (no host exit after its first block).
    spin = {'name': 'spin', 'code': ref.assemble('1: inc rax\njmp 1b').hex(), 'rip': CODE, 'gpr': gprs(), 'rflags': 0, 'memory': []}
    state, err = run('spin', spin, ['--watchdog-ms', '300'])
    ok = state is not None and state['stop'] == 'watchdog' and state['stats']['fast'] > 1000
    failures += not ok
    out.append({'name': 'spin', 'stop': state and state['stop'], 'stats': state and state['stats'], 'stderr': err})
    print(f"spin --watchdog-ms 300: {'match' if ok else 'fail'} {state and state['stop']} {state and state['stats']}")

    # Stores to a page code was translated from stop (`unsupported_smc`) until SMC invalidation exists, whether the
    # page holds the running block or only an earlier one. A memory region at CODE + 0x800 (past the first block's
    # code) makes the first code page writable; the store writes there.
    for name, text in [('smc-own-page', 'mov dword ptr [rip + 0x7fa], eax'),
                       ('smc-earlier-page', 'add rax, 1\njmp 1f\n.fill 4096, 1, 0xcc\n1: mov dword ptr [rip - 0x80f], eax')]:
        code = ref.assemble(text)
        program = {'name': name, 'code': code.hex(), 'rip': CODE, 'gpr': gprs(), 'rflags': 0,
                   'memory': [{'addr': CODE + 0x800, 'data': '00' * 0x10}]}
        state, err = run(name, program, [])
        ok = state is not None and state['stop'] == 'resident' and state['reason'].startswith('unsupported_smc')
        failures += not ok
        out.append({'name': name, 'stop': state and state['stop'], 'reason': state and state['reason'], 'stderr': err})
        print(f"{name}: {'match' if ok else 'fail'} {state and state['stop']} {state and state['reason']}")
    (a.out / 'probes.json').write_text(json.dumps(out, indent=1) + '\n')
    print(f'{len(out) - failures - unsupported} match, {unsupported} unsupported, {failures} fail')
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
