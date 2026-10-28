#!/usr/bin/env python3
"""Assemble the benchmark bodies and write benchmarks.json (the reusable description); helpers shared with run_qemu.py.

    python3 tools/bench/build_bench.py            # rewrite benchmarks.json (needs nasm)

benchmarks.json is the authority for the bytes. A benchmark is a program in the tools/reference schema
({name, code, rip, gpr[16], rflags, memory, insns}) with two compact extensions: memory regions may carry
{"addr","size","fill":"zero"|"lcg","seed"} instead of {"addr","data"} (expanded by `program()`), and rcx is the iteration
count N (gpr[1] is a placeholder 0). `program(b, n)` returns the exact tools/reference program for N."""
import json, pathlib, re, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
CODE, ARRAY, STACK, BUF = 0x100000, 0x200000, 0x300000, 0x110000
REGS = ('rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15')
M64 = (1 << 64) - 1
STACK_RSP = STACK + 0x1800
# name: (source, nasm -D defines, insns per iteration or None (data dependent), fixed extra insns, initial GPRs, regions, default N)
SPEC = {
    'int':     ('int.asm', [], 10, 0, dict(rax=0x0123456789abcdef, rbx=0x9e3779b97f4a7c15, rsi=0xff51afd7ed558ccd), ['stack'], 240_000_000),
    'mem64':   ('mem.asm', ['STRIDE=64'], 8, 0, dict(rbx=ARRAY), ['stack', 'array'], 120_000_000),
    'mem4096': ('mem.asm', ['STRIDE=4096'], 8, 0, dict(rbx=ARRAY), ['stack', 'array'], 120_000_000),
    'call':    ('call.asm', [], 11, 1, dict(rbx=0xdeadbeefcafef00d, rsi=0x1234567), ['stack'], 60_000_000),
    'branchy': ('branchy.asm', [], None, 0, dict(rax=0x9e3779b97f4a7c15, rsi=6364136223846793005, rdi=1442695040888963407), ['stack'], 60_000_000),
    'mixed':   ('mixed.asm', [], 13, 0, dict(rsi=BUF, r9=1), ['stack', 'buffer'], 120_000_000),
}
REGION = {'stack': dict(addr=STACK, size=0x2000, fill='zero'), 'array': dict(addr=ARRAY, size=0x100000, fill='zero'),
          'buffer': dict(addr=BUF, size=0x10000, fill='lcg', seed=1)}


def fill_bytes(r):
    if r['fill'] == 'zero': return bytes(r['size'])
    assert r['fill'] == 'lcg'
    x, out = r['seed'], bytearray()
    for _ in range(r['size']):
        x = (x * 6364136223846793005 + 1442695040888963407) & M64
        out.append(x >> 56)
    return bytes(out)


def assemble(src, defines):
    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d, 'b.bin')
        subprocess.run(['nasm', '-f', 'bin', *[f'-D{x}' for x in defines], str(HERE / 'bodies' / src), '-o', str(out)], check=True)
        return out.read_bytes()


def mnemonics(src):
    """Static instruction list (informational annotations) from the source: one {"op"} per instruction line."""
    ops = []
    for line in (HERE / 'bodies' / src).read_text().splitlines():
        line = re.sub(r';.*', '', line).strip()
        line = re.sub(r'^\w+:\s*', '', line)
        if line and not line.startswith(('bits', '%')): ops.append({'op': line.split()[0]})
    return ops


def build():
    nasm = subprocess.run(['nasm', '-v'], capture_output=True, text=True).stdout.strip()
    out = {'format': 'natemu-bench-v1', 'nasm': nasm, 'n_reg': 'rcx', 'rip': CODE,
           'note': 'code is position independent; a program falls through at its end; rcx=N; see README.md', 'benchmarks': []}
    for name, (src, defs, per, fixed, init, regions, n) in SPEC.items():
        gpr = [init.get(r, 0) for r in REGS]; gpr[REGS.index('rsp')] = STACK_RSP
        out['benchmarks'].append({
            'name': name, 'source': 'bodies/' + src, 'defines': defs, 'code': assemble(src, defs).hex(), 'rip': CODE,
            'gpr': [f'0x{g:016x}' for g in gpr], 'rflags': 0, 'memory': [REGION[r] for r in regions], 'insns': mnemonics(src),
            'insns_per_iter': per, 'insns_fixed': fixed, 'n_default': n})
    (HERE / 'benchmarks.json').write_text(json.dumps(out, indent=1) + '\n')


def load():
    return json.loads((HERE / 'benchmarks.json').read_text())


def gprs(b, n):
    g = [int(x, 0) for x in b['gpr']]; g[1] = n
    return g


def program(b, n):
    """Exact tools/reference program for iteration count n."""
    mem = [{'addr': r['addr'], 'data': fill_bytes(r).hex()} if 'fill' in r else r for r in b['memory']]
    return {'name': f"{b['name']}-n{n}", 'code': b['code'], 'rip': b['rip'], 'gpr': [f'0x{g:x}' for g in gprs(b, n)],
            'rflags': b['rflags'], 'memory': mem, 'insns': b['insns']}


def build_model(path):
    subprocess.run(['cc', '-O2', '-o', str(path), str(HERE / 'model.c')], check=True)


def model(b, n, exe):
    """(final GPRs [16 ints], exact dynamic instruction count) from the native C model (independent of every emulator)."""
    args = [str(exe), b['name'], str(n)] + ['%x' % g for g in gprs(b, n)]
    with tempfile.TemporaryDirectory() as d:
        if b['name'] == 'mixed': pathlib.Path(d, 'buf').write_bytes(fill_bytes(REGION['buffer'])); args.append(str(pathlib.Path(d, 'buf')))
        lines = subprocess.run(args, capture_output=True, text=True, check=True).stdout.split()
    return [int(x, 16) for x in lines[:16]], int(lines[16])


if __name__ == '__main__':
    build()
