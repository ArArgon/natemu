#!/usr/bin/env python3
"""Seeded generator for the I2 corpus: string instructions (boot-features plan, chunk I2).

    python3 tools/reference/gen_i2.py -o OUT.json [--seed N] [--random 40] [--flow]

MOVS/STOS/LODS/SCAS/CMPS in byte/word/dword/qword widths, alone and with REP (movs/stos/lods), REPE and REPNE (cmps/scas),
in both DF directions (`std ... cld`), with RCX counts 0, 1, small random and large (a 64 KiB region), misaligned and
page-crossing elements at DATA + 0x1000, overlapping MOVS (destination just above or below the source, both directions),
REPE/REPNE ended early by the data, FS/GS on the DS:RSI source (`fsbase`/`gsbase`), and the slice-1 forms of
gen_slice1.py between them (a string instruction mid-block, the block going on after it). Each string instruction is
preceded by `mov ecx, N` and pointer moves (`mov esi/edi, imm32`), so counts and addresses are known to the generator;
annotations carry `count` and `rep` (tools/reference/flags.py, ref.py). Inputs only: expected results come from ref.py
under QEMU.

`--flow` writes the same programs as flow programs (instruction `offsets`, `flow: true`) for the signal-based fault
reference of tools/run_slice2_faults.py: QEMU's SIGSEGV context gives RCX/RSI/RDI and the memory at the faulting element.
"""
import argparse, json, pathlib, random, re, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, ref, gen_slice1 as g
from gen_slice1 import CODE, DATA, STACK, REGION

BIG, BIG_SIZE = 0x220000, 0x10000                 # the large-count region (64 KiB)
SUFFIX = {8: 'b', 16: 'w', 32: 'd', 64: 'q'}
SIZE = {8: 'byte', 16: 'word', 32: 'dword', 64: 'qword'}
ACC = {8: 'al', 16: 'ax', 32: 'eax', 64: 'rax'}
OPS = ['movs', 'stos', 'lods', 'scas', 'cmps']
SOURCE = {'movs', 'lods', 'cmps'}                 # DS:RSI (overridable)
DEST = {'movs', 'stos', 'scas', 'cmps'}           # ES:RDI
M64 = 2**64 - 1


def text(op, w, rep=None, seg=None):
    """Intel syntax of one string instruction; with a segment the explicit-operand form (clang encodes the prefix)."""
    pre = {'rep': 'rep ', 'repe': 'repe ', 'repne': 'repne ', None: ''}[rep]
    if not seg: return f'{pre}{op}{SUFFIX[w]}'
    src, dst = f'{SIZE[w]} ptr {seg}:[rsi]', f'{SIZE[w]} ptr es:[rdi]'
    return pre + {'movs': f'movs {dst}, {src}', 'lods': f'lods {ACC[w]}, {src}', 'cmps': f'cmps {src}, {dst}'}[op]


class Prog(g.Prog):
    def string(self, op, w, rep, count, rsi, rdi, df, seg=None):
        """`mov ecx/esi/edi`, then the string instruction under DF (std before, cld after when df)."""
        if rep: self.emit(f'mov ecx, {count}')
        if op in SOURCE: self.emit(f'mov rsi, {rsi:#x}')
        if op in DEST: self.emit(f'mov rdi, {rdi:#x}')
        if df: self.emit('std')
        self.raw(text(op, w, rep, seg), {'op': op, 'width': w} | ({'rep': rep, 'count': count} if rep else {}))
        if df: self.emit('cld')

    def raw(self, line, ann):            # (emit's `count` is a shift count)
        self.undef = flags.step(ann, self.undef); self.cl = None
        self.lines.append(line); self.insns.append(ann)

    def pointers(self):              # back inside the data region for gen_slice1's forms
        for r in ('rsi', 'rdi'): self.emit(f'mov {r}, {DATA + self.rng.randrange(0xc00, 0x1400):#x}')


def place(rng, w, count, region, size):
    """A start address whose elements stay inside [region, region + size) in either direction."""
    n = max(count, 1) * w // 8
    lo, hi = region + n, region + size - n - w // 8
    return rng.randrange(lo, hi) if hi > lo else region + size // 2


def random_program(seed, n):
    for attempt in range(100):
        rng = random.Random(seed + attempt); p = Prog(f'i2-random-{n:02d}', rng); gpr, rflags = g.initial(rng)
        for _ in range(rng.randrange(3, 9)):
            if rng.random() < 0.4: p.one(); continue
            op, w = rng.choice(OPS), rng.choice([8, 16, 32, 64])
            rep = rng.choice([None, 'rep' if op not in ('cmps', 'scas') else rng.choice(['repe', 'repne'])])
            count = rng.choice([0, 1, 2, 3, rng.randrange(4, 40)])
            df = rng.random() < 0.4
            rsi, rdi = (place(rng, w, count, DATA, REGION) for _ in range(2))
            if op in ('cmps', 'scas') and rng.random() < 0.5: rdi = rsi + rng.randrange(-16, 17) * w // 8   # equal runs
            p.string(op, w, rep, count, rsi, rdi, df)
            p.pointers()
        try: return finish(p, gpr, rflags, rng)
        except ValueError as e: last = e
    raise last


def finish(p, gpr, rflags, rng, memory=None, **extra):
    """gen_slice1's program plus the large region when a line names it; `memory` may then rewrite the regions."""
    out = g.finish(p, gpr, rflags, rng) | extra
    if any(BIG <= int(m, 16) < BIG + BIG_SIZE for l in p.lines for m in re.findall(r'0x[0-9a-f]+', l)):
        out['memory'].append({'addr': hex(BIG), 'data': bytes(rng.getrandbits(8) for _ in range(BIG_SIZE)).hex()})
    if memory: out['memory'] = memory(out['memory'])
    return out


def edges(rng):
    """Hand-picked programs: every form at counts 0/1/random/large, both directions; misaligned, page-crossing and overlapping
    elements; REPE/REPNE ended early; FS/GS sources."""
    progs = []
    def new(name):
        p = Prog('i2-edge-' + name, rng); gpr, _ = g.initial(rng)
        gpr[0] = rng.getrandbits(64)
        return p, gpr
    for op in OPS:
        for w in (8, 16, 32, 64):
            for rep in ([None] + (['repe', 'repne'] if op in ('cmps', 'scas') else ['rep'])):
                for df in (False, True):
                    counts = [None] if rep is None else [0, 1, rng.randrange(2, 40)]
                    for count in counts:
                        p, gpr = new(f'{op}{w}-{rep or "once"}-{count}-{"down" if df else "up"}')
                        c = count or 0
                        rsi, rdi = place(rng, w, c, DATA, REGION), place(rng, w, c, DATA, REGION)
                        p.string(op, w, rep, c, rsi, rdi, df)
                        p.emit('add rax, rcx'); p.emit('mov qword ptr [rbx], rsi')
                        progs.append(finish(p, gpr, rng.choice([0, 0x8d5, 0x41]), rng))
    # large counts: 4 KiB pages cleared and copied (the kernel's clear_page/copy_page shape), both directions. The source
    # elements lie at BIG, the destination ones at the region's end; a downward string starts at the highest element.
    for op, w, count, df in (('stos', 64, 512, False), ('stos', 64, 1024, True), ('movs', 64, 1024, False), ('movs', 64, 1024, True),
                             ('stos', 8, 9000, False), ('movs', 8, 6000, True), ('movs', 32, 3000, False), ('lods', 16, 4000, False),
                             ('scas', 64, 2000, False), ('cmps', 32, 3000, True)):
        p, gpr = new(f'large-{op}{w}-{count}-{"down" if df else "up"}')
        k = w // 8; n = count * k; top = (n - k) if df else 0
        src, dst = BIG + top, BIG + BIG_SIZE - n + top
        rep = 'repe' if op == 'cmps' else 'repne' if op == 'scas' else 'rep'
        if op == 'scas': p.emit('mov rax, 0x123456789abcdef1')   # not in the random data: REPNE runs to the end
        p.string(op, w, rep, count, src, dst if op != 'stos' and op != 'scas' else BIG + top, df)
        p.emit('mov qword ptr [rbx], rcx')
        def equal(m):                            # cmps: equal regions, so REPE runs to the end
            m[-1]['data'] = (bytes([0x33]) * BIG_SIZE).hex(); return m
        progs.append(finish(p, gpr, 0, rng, equal if op == 'cmps' else None))
    # misaligned and page-crossing elements (DATA + 0x1000 is a page boundary), both directions
    for op in OPS:
        for w in (16, 32, 64):
            for df in (False, True):
                p, gpr = new(f'crossing-{op}{w}-{"down" if df else "up"}')
                k = w // 8; c = 7
                at = DATA + 0x1000 - 3 * k - 1 if not df else DATA + 0x1000 + 3 * k - k + 1
                rep = 'rep' if op in ('movs', 'stos', 'lods') else 'repne'
                if op == 'scas': p.emit('mov rax, 0x0102030405060708')
                p.string(op, w, rep, c, at if op in SOURCE else DATA + 0x200 + 3, at if op in DEST else DATA + 0x200, df)
                if op == 'cmps': p.string(op, w, 'repne', c, at, at + 0x11, df)
                progs.append(finish(p, gpr, 0, rng))
    # long strings whose source crosses DATA + 0x1000 after 9 elements and whose destination crosses STACK + 0x1000 after
    # 5 (misaligned by 1): a page made absent or read-only faults midway, at an element of either operand
    for op in OPS:
        for w in (8, 16, 32, 64):
            for df in (False, True):
                p, gpr = new(f'midway-{op}{w}-{"down" if df else "up"}')
                k = w // 8
                src = DATA + 0x1000 + (8 * k + 1 if df else -9 * k + 1)
                dst = STACK + 0x1000 + (4 * k + 1 if df else -5 * k + 1)
                rep = 'repe' if op == 'cmps' else 'repne' if op == 'scas' else 'rep'
                if op == 'scas': p.emit('mov rax, 0x0102030405060708')
                def same(m, src=src, dst=dst, k=k):     # cmps: equal bytes, so REPE reaches the crossing
                    for r in m[:2]: r['data'] = (bytes([0x77]) * (len(r['data']) // 2)).hex()
                    return m
                p.string(op, w, rep, 20, src, dst, df)
                progs.append(finish(p, gpr, 0, rng, same if op == 'cmps' else None))
    # overlapping movs: destination a few bytes above/below the source, both directions
    for w in (8, 16, 32, 64):
        for delta in (-9, -1, 1, 3, 8, 17):
            for df in (False, True):
                p, gpr = new(f'overlap-movs{w}-{delta}-{"down" if df else "up"}')
                src = DATA + 0x800 + rng.randrange(0, 8)
                p.string('movs', w, 'rep', 24, src, src + delta, df)
                progs.append(finish(p, gpr, 0, rng))
    # REPE/REPNE ended by the data: REPE meets `run` equal elements, then a different one; REPNE `run` different ones, then
    # an equal one (0x5a bytes equal, 0xa5 against 0x5a different; scas compares with RAX = 0x5a..5a).
    for op in ('cmps', 'scas'):
        for w in (8, 16, 32, 64):
            for rep in ('repe', 'repne'):
                for df in (False, True):
                    for run in (0, 1, 5):
                        p, gpr = new(f'early-{op}{w}-{rep}-{run}-{"down" if df else "up"}')
                        k = w // 8; s_, d_ = DATA + 0x400, DATA + 0x900
                        def mem(m, s_=s_, d_=d_, run=run, k=k, rep=rep, op=op, df=df):
                            b = bytearray.fromhex(m[0]['data'])
                            for j in range(run + 1):
                                same = (j < run) == (rep == 'repe')
                                for at, v in ((d_, 0x5a), (s_, 0x5a if same else 0xa5)) if op == 'cmps' else ((d_, 0x5a if same else 0xa5),):
                                    e = at - DATA + (-j if df else j) * k
                                    b[e:e + k] = bytes([v]) * k
                            m[0]['data'] = b.hex(); return m
                        if op == 'scas': p.emit('mov rax, 0x5a5a5a5a5a5a5a5a')
                        p.string(op, w, rep, run + 6, s_, d_, df)
                        p.emit('sete byte ptr [rbx]')
                        progs.append(finish(p, gpr, 0, rng, mem))
    # FS/GS on the DS:RSI source of movs/lods/cmps (QEMU sets the bases with WRFSBASE/WRGSBASE)
    for op in ('movs', 'lods', 'cmps'):
        for seg, b in (('fs', 0xffff_8000_dead_0000), ('gs', 0x7ffe_0000_1000)):
            for rep in ([None, 'rep'] if op != 'cmps' else [None, 'repe']):
                p, gpr = new(f'segment-{op}-{seg}-{rep or "once"}')
                rsi = (DATA + 0x300 - b) & M64
                if rep: p.emit('mov ecx, 9')
                p.emit(f'mov rsi, {rsi:#x}')
                if op in DEST: p.emit(f'mov rdi, {DATA + 0x700:#x}')
                p.raw(text(op, 64, rep, seg), {'op': op, 'width': 64} | ({'rep': rep, 'count': 9} if rep else {}))
                progs.append(finish(p, gpr, 0, rng, fsbase=hex(b if seg == 'fs' else 0), gsbase=hex(b if seg == 'gs' else 0),
                                    cpu='qemu64,+fsgsbase'))
    return progs


def flow(p):
    """The program as a flow program (instruction offsets) for the signal-based fault reference."""
    _, offs = ref.assemble('\n'.join(f'I{i}:\n{l}' for i, l in enumerate(p['asm'])), labels=True)
    return dict(p, flow=True, offsets=[offs[f'I{i}'] for i in range(len(p['asm']))])


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261029); ap.add_argument('--random', type=int, default=40)
    ap.add_argument('--flow', action='store_true')
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = edges(rng) + [random_program(rng.getrandbits(64), n) for n in range(a.random)]
    if a.flow: progs = [flow(p) for p in progs]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i2.py', 'seed': a.seed, 'assembler': clang, 'flow': a.flow,
        'layout': {'code': hex(CODE), 'data': hex(DATA), 'stack': hex(STACK), 'big': hex(BIG)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')


if __name__ == '__main__': main()
