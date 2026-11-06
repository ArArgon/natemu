#!/usr/bin/env python3
"""Seeded generator for the I7 corpus: FS/GS segment overrides in 64-bit mode (boot-features plan, chunk I7).

    python3 tools/reference/gen_i7.py -o OUT.json [--seed N] [--random 60]

Straight-line programs mixing gen_i1.py's forms with FS-/GS-overridden memory operands: loads, stores, read-modify-writes
(ALU, unary, shifts, bit tests, CMPXCHG/XADD without LOCK, SHLD/SHRD), memory XCHG, PUSH/POP of memory, SETcc, CMOVcc,
MUL/IMUL, MOVZX/MOVSX, POPCNT/BMI loads, MOVNTI, LEA (the override has no effect), absolute (`fs:[disp32]`), 64-bit
`movabs` and RIP-relative operands, plus CS/DS/ES/SS prefixes naming the operand's default segment (no effect in 64-bit
mode). Each program has its own FS.base and GS.base (`fsbase`/`gsbase`, loaded by tools/reference/ref.py with
WRFSBASE/WRGSBASE under QEMU): near the data (absolute and RIP-relative forms reach the data), far below the canonical
limit, in the upper half (negative), or 0. r12 and r13 hold data addresses minus FS.base and GS.base, so `fs:[r12 + d]`
and `gs:[r13 + d]` (and with r8 as index) reach the data, wrapping modulo 2^64 for a large or negative base. Inputs only:
expected results come from ref.py under QEMU.
"""
import argparse, json, pathlib, random, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_slice1 as g, gen_i1
from gen_slice1 import DATA, REGION, SIZE, NAMES

M64 = 2**64 - 1
FS_PTR, GS_PTR = 12, 13                 # r12 = target - FS.base, r13 = target - GS.base
PLAIN = [3, 5, 6, 7]                    # rbx rbp rsi rdi: plain data pointers (gen_slice1 PTRS without r12/r13)


def base(rng, kind):
    """A canonical segment base of `kind`: near (just below the data, so absolute and RIP-relative operands reach it),
    far (below the lower half's end), negative (upper half) or zero."""
    if kind == 'near': return DATA - rng.randrange(0, 0x8000)
    if kind == 'far': return rng.randrange(0x1_0000_0000, 0x7fff_ffff_f000)
    if kind == 'negative': return rng.choice([0xffff_8000_0000_0000 + rng.randrange(0, 1 << 46), M64 + 1 - rng.randrange(1, 1 << 32),
                                              M64 + 1 - 0x1000, 0xffff_ffff_8000_0000])
    return 0


class Prog(gen_i1.Prog):
    def __init__(self, name, rng, fs, gs):
        super().__init__(name, rng); self.bases = {'fs': fs, 'gs': gs}

    def mem(self, w, hi=False):
        """A plain operand (gen_slice1's, with r12/r13 replaced by a plain pointer) or, mostly, an FS-/GS-overridden one."""
        r = self.rng
        if hi or r.random() < 0.3:     # (a high-byte register cannot take r12/r13's REX)
            m = super().mem(w, hi)
            for reg in ('r12', 'r13'): m = m.replace(f'[{reg}', f'[{r.choice(["rbx", "rsi", "rdi"])}')
            if r.random() < 0.15 and '[rip' not in m:      # a null segment prefix naming the default segment (no effect)
                stack = any(f'[{b}' in m for b in ('rsp', 'rbp'))
                m = m.replace('[', ('ss:[' if stack else r.choice(['ds:[', 'cs:[', 'es:['])), 1)
            return m
        seg = r.choice(['fs', 'gs']); b = self.bases[seg]; ptr = 'r12' if seg == 'fs' else 'r13'
        d = r.randrange(-0x200, 0x200); sc = r.choice([1, 2, 4, 8])
        forms = [f'[{ptr}]', f'[{ptr} + {d}]', f'[{ptr} + r8*{sc}]', f'[{ptr} + r8*{sc} + {d}]']
        target = DATA + r.randrange(0, REGION - 8)
        if b <= DATA and DATA + REGION - b < 0x7fff_0000:   # near or zero: absolute and RIP-relative reach the data
            forms += [f'[{target - b}]', f'[rip + {{{target - b}}}]']
        return f'{SIZE[w]} ptr {seg}:{r.choice(forms)}'

    def one(self):
        r = self.rng
        if r.random() < 0.12:       # 64-bit moffs through a segment: any 64-bit offset that wraps to the data
            seg = r.choice(['fs', 'gs']); w = r.choice([8, 16, 32, 64]); acc = NAMES[w][0]
            off = (DATA + r.randrange(0, REGION - 8) - self.bases[seg]) & M64
            ptr = f'{SIZE[w]} ptr {seg}:[{off:#x}]'
            self.emit(f'movabs {acc}, {ptr}' if r.random() < 0.5 else f'movabs {ptr}, {acc}')
        elif r.random() < 0.05:     # lea ignores the override
            self.emit(f'lea {self.reg(64, writable=True)}, {r.choice(["fs", "gs"])}:[{self.reg(64)} + {r.randrange(-0x80, 0x80)}]')
        else:
            super().one()


def initial(rng, fs, gs):
    gpr, rflags = g.initial(rng)
    gpr[FS_PTR] = (DATA + rng.randrange(0xc00, 0x1400) - fs) & M64
    gpr[GS_PTR] = (DATA + rng.randrange(0xc00, 0x1400) - gs) & M64
    return gpr, rflags


def finish(p, gpr, rflags, rng):
    out = g.finish(p, gpr, rflags, rng) | {'fsbase': hex(p.bases['fs']), 'gsbase': hex(p.bases['gs'])}
    return out | ({'cpu': gen_i1.CPU} if gen_i1.cpu(p.lines) else {})


def random_program(seed, n):
    for attempt in range(100):
        rng = random.Random(seed + attempt)
        fs, gs = (base(rng, rng.choice(['near', 'far', 'negative', 'zero'])) for _ in range(2))
        p = Prog(f'i7-random-{n:02d}', rng, fs, gs); gpr, rflags = initial(rng, fs, gs)
        for _ in range(rng.randrange(10, 36)): p.one()
        try: return finish(p, gpr, rflags, rng)
        except ValueError as e: last = e
    raise last


FS0, GS0 = 0xffff_8000_dead_0000, 0x7ffe_0000_1000      # the edge programs' bases (negative and far)
F, G = lambda a: (a - FS0) & M64, lambda a: (a - GS0) & M64
EDGES = [
 # per-CPU style %gs read-modify-writes and loads (Linux: this_cpu_* on %gs:disp(base)), the canary load at %gs:40
 ('percpu-rmw', {'r13': G(DATA + 0x100), 'r12': F(DATA + 0x800), 'rax': 0x1122334455667788, 'rcx': 3}, 0,
  ['add qword ptr gs:[r13 + 8], 1', 'inc dword ptr gs:[r13 + 16]', 'sub qword ptr gs:[r13 + 24], rcx', 'mov rdx, qword ptr gs:[r13 + 40]',
   'xor qword ptr gs:[r13 + r8*8 + 32], rax', 'cmp qword ptr gs:[r13 + 8], rdx', 'setne byte ptr gs:[r13 + 48]', 'dec byte ptr fs:[r12]',
   'or word ptr fs:[r12 + 2], 0x8000', 'neg dword ptr fs:[r12 + 4]', 'not qword ptr fs:[r12 + 8]', 'and qword ptr gs:[r13], rax',
   'adc qword ptr gs:[r13 + 8], 0', 'xadd qword ptr gs:[r13 + 56], rcx', 'cmpxchg qword ptr gs:[r13 + 64], rdx', 'mov rax, qword ptr gs:[r13 + 64]']),
 ('loads-stores-widths', {'r12': F(DATA + 0x200), 'r13': G(DATA + 0x300), 'rax': 0x8877665544332211, 'rcx': 0xf1f2f3f4}, 0,
  ['mov qword ptr fs:[r12], rax', 'mov dword ptr fs:[r12 + 8], ecx', 'mov word ptr gs:[r13], cx', 'mov byte ptr gs:[r13 + 2], al',
   'mov rdx, qword ptr fs:[r12 + 3]', 'movzx r9d, word ptr gs:[r13 + 1]', 'movsx r10, byte ptr fs:[r12 + 7]', 'movsxd r11, dword ptr gs:[r13]',
   'mov r14b, byte ptr fs:[r12 + 1]', 'mov qword ptr gs:[r13 + r8*4 + 16], -1', 'cmovae r15, qword ptr fs:[r12 + 8]', 'imul r9, qword ptr gs:[r13 + 16], 7',
   'mul qword ptr fs:[r12]', 'popcnt r10, qword ptr gs:[r13 + 16]', 'movnti qword ptr fs:[r12 + 32], rcx']),
 ('page-crossing', {'r12': F(DATA + 0xffc), 'r13': G(DATA + 0xff9), 'rax': 0x0102030405060708, 'rcx': 0xaabbccdd}, 0,
  ['mov qword ptr fs:[r12], rax', 'mov r9, qword ptr fs:[r12]', 'add dword ptr gs:[r13 + 4], ecx', 'mov word ptr gs:[r13 + 6], cx',
   'xchg qword ptr fs:[r12 + 4], rax', 'shld qword ptr gs:[r13 + 3], rcx, 13', 'mov r10, qword ptr gs:[r13 + 3]']),
 ('stack-forms', {'r12': F(DATA + 0x400), 'r13': G(DATA + 0x500), 'rax': 0x5555666677778888}, 0,
  ['push qword ptr fs:[r12]', 'push qword ptr gs:[r13 + 8]', 'pop qword ptr fs:[r12 + 16]', 'pop qword ptr gs:[r13]', 'push rax',
   'pop qword ptr fs:[r12 + r8*8]', 'push word ptr gs:[r13 + 2]', 'pop word ptr fs:[r12 + 4]', 'pop rcx']),
 ('bit-shift-forms', {'r12': F(DATA + 0x600), 'r13': G(DATA + 0x700), 'rbp': G(DATA + 0x900), 'rax': 0x8000000000000001, 'rcx': 67}, 0x40,
  ['bts qword ptr fs:[r12], 5', 'btr dword ptr gs:[r13 + 4], 31', 'btc word ptr fs:[r12 + 8], 15', 'bt qword ptr gs:[r13], 63', 'setb byte ptr fs:[r12 + 16]',
   'shl qword ptr gs:[r13 + 8], 3', 'sar dword ptr fs:[r12 + 4], 1', 'mov cl, 9', 'ror qword ptr gs:[r13 + 16], cl', 'shrd dword ptr fs:[r12 + 20], eax, 7',
   'add qword ptr gs:[rbp + 8], 1', 'mov rdx, qword ptr gs:[rbp + r8*2]']),
 ('absolute-moffs-lea', {'rax': 0xfedcba9876543210, 'rbx': 0x40}, 0,
  [f'movabs qword ptr fs:[{F(DATA + 0x40):#x}], rax', f'movabs eax, dword ptr fs:[{F(DATA + 0x44):#x}]', f'movabs al, byte ptr gs:[{G(DATA + 0x41):#x}]',
   f'movabs qword ptr gs:[{G(DATA + 0xffc):#x}], rax', 'lea rdx, fs:[rbx + 8]', 'lea r9, gs:[rbx + rbx*2]', 'mov r10, qword ptr ds:[rbx + 2097152]',
   'mov r11, qword ptr cs:[rbx + 2097160]', 'mov qword ptr es:[rbx + 2097168], r10', 'mov r14, qword ptr ss:[rsp]', 'add qword ptr ss:[rbp + 0], 1']),
]
# Near bases: absolute disp32 and RIP-relative operands with a segment (the edge assembled with gen_slice1's RIP placeholders).
NEAR_FS, NEAR_GS = DATA - 0x1234, DATA - 0x10
NEAR = [('absolute-rip-near', {'rax': 0x1122334455667788, 'rcx': 0xdeadbeef}, 0,
  [f'mov rdx, qword ptr fs:[{0x1234 + 0x10}]', f'mov dword ptr gs:[{0x10 + 0x20}], ecx', 'mov rcx, qword ptr gs:[0x28]', 'add dword ptr fs:[0x1300], 7',
   f'mov r9, qword ptr fs:[rip + {{{0x1234 + 0x80}}}]', f'add qword ptr gs:[rip + {{{0x10 + 0xff8}}}], rax', f'mov byte ptr fs:[rip + {{{0x1234 + 0x2001 - 0x2}}}], cl',
   f'cmp word ptr gs:[rip + {{{0x10 + 0x50}}}], 0x7fff', 'setl byte ptr gs:[0x60]'])]


# Owner decisions (November 3; plan §5): CS/DS/ES/SS prefixes are ignored (the operand's default segment decides #SS/#GP),
# the last of several FS/GS prefixes applies, an FS/GS prefix on an instruction with no memory operand is ignored. Canonical
# forms, each FS/GS prefix list ending in the one that applies (an FS/GS prefix followed by a null one, and the non-canonical
# forms, are tools/run_i5b.py's `segment-*` cases, against QEMU 11.1.2 and the hardware probes). `.byte` lines: unannotated.
B = lambda *x: '.byte ' + ', '.join(f'{v:#x}' for v in x)
Q = lambda a: list(a.to_bytes(8, 'little'))
PREFIX = [
 ('prefix-null', {'rbx': DATA + 0x100, 'rbp': DATA + 0x200, 'rsi': DATA + 0x300, 'rdi': DATA + 0x400, 'rax': 0x1122334455667788, 'rcx': 0x0f0f}, 0,
  ['mov rax, qword ptr ss:[rbx + 8]', 'add qword ptr ds:[rbp + 8], rax', 'mov qword ptr es:[rsp + 8], rax', 'mov rcx, qword ptr cs:[rsp]',
   'xor qword ptr ss:[rsi + r8*2], rcx', 'push qword ptr ds:[rbp + 0x18]', 'pop qword ptr ss:[rbx + 0x20]', 'sub qword ptr es:[rbp + 0x28], rcx',
   B(0x36, 0x3e, 0x48, 0x8b, 0x13), B(0x3e, 0x36, 0x48, 0x01, 0x4d, 0x10), B(0x36, 0x48, 0xa1, *Q(DATA + 0x500)), 'mov al, 3', B(0x36, 0xd7),
   B(0x36, 0x48, 0xad), B(0x26, 0x48, 0xa5), B(0x36, 0x48, 0xa7), B(0x2e, 0x36, 0x48, 0xa5), 'add rax, rcx']),
 ('prefix-fsgs', {'r12': F(DATA + 0x800), 'r13': G(DATA + 0x900), 'rax': 0x8877665544332211, 'rcx': 0x0102030405060708, 'r9': 0x5a5a,
                  'rsi': F(DATA + 0xa00), 'rdi': DATA + 0xb00}, 0,
  [B(0x64, 0x65, 0x49, 0x8b, 0x45, 0x00), B(0x65, 0x64, 0x4d, 0x89, 0x0c, 0x24), B(0x64, 0x65, 0x64, 0x49, 0x01, 0x4c, 0x24, 0x08),
   B(0x3e, 0x65, 0x49, 0x8b, 0x55, 0x10), B(0x36, 0x64, 0x49, 0x8b, 0x5c, 0x24, 0x18), B(0x65, 0x64, 0x48, 0xa5), B(0x3e, 0x65, 0x64, 0x48, 0xad),
   B(0x64, 0x48, 0xab), B(0x65, 0x48, 0xaf), B(0x64, 0x48, 0x01, 0xc8), B(0x65, 0x50), B(0x64, 0x65, 0x41, 0x59), B(0x65, 0x9c), B(0x64, 0x5e),
   B(0x64, 0x48, 0x89, 0xca), 'add rax, rcx'])]


def prefix_program(spec, rng):
    name, regs, rflags, lines = spec; p = Prog('i7-edge-' + name, rng, FS0, GS0); gpr, _ = g.initial(rng)
    gpr = [0x0101010101010101 * (i + 1) if i not in g.PTRS + [g.INDEX, 4] else gpr[i] for i in range(16)]
    for r, v in regs.items(): gpr[g.R64.index(r)] = v
    for l in lines:
        if l.startswith('.byte'): p.lines.append(l); p.insns.append({'op': 'mov', 'width': 64})
        else: p.emit(l)
    return finish(p, gpr, rflags, rng) | {'unannotated': True}


def edge_program(spec, rng, fs=FS0, gs=GS0):
    name, regs, rflags, lines = spec; p = Prog('i7-edge-' + name, rng, fs, gs); gpr, _ = g.initial(rng)
    gpr = [0x0101010101010101 * (i + 1) if i not in g.PTRS + [g.INDEX, 4] else gpr[i] for i in range(16)]
    for r, v in regs.items(): gpr[g.R64.index(r)] = v
    for l in lines:
        extra = {}
        if l.split()[0] in ('shld', 'shrd'): extra['count'] = int(l.rsplit(',', 1)[1], 0)
        p.emit(l, **extra)
    return finish(p, gpr, rflags, rng)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261028); ap.add_argument('--random', type=int, default=60)
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = [edge_program(e, rng) for e in EDGES] + [edge_program(e, rng, NEAR_FS, NEAR_GS) for e in NEAR] + \
        [random_program(rng.getrandbits(64), n) for n in range(a.random)] + [prefix_program(e, rng) for e in PREFIX]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i7.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA), 'stack': hex(g.STACK)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
