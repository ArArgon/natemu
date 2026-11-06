#!/usr/bin/env python3
"""Seeded generator for the I10 corpus: 64-bit code with the 0x67 address-size prefix, and XLAT (boot-features plan, chunk I10).

    python3 tools/reference/gen_i10.py -o OUT.json [--seed N]

With 0x67 the effective address is computed, truncated to 32 bits and zero-extended (then the FS/GS base is added). Each
access below gets its base and index registers loaded with random upper 32 bits (which must be ignored) and low halves chosen
so that base + index * scale + displacement wraps around 4 GiB in 32-bit arithmetic and lands in the data region. Families:
  - `i10-ea-*`: loads, stores, read-modify-writes, LOCK forms, XCHG, PUSH/POP m, MOVZX/MOVSX, CMP/BT, LEA (64-, 32- and 16-bit
    destinations: the 32-bit address zero-extended), SSE/AVX/x87 loads and stores, at every width, FS-based ones with FS.base
    near and below 0 (the base added after the truncation);
  - `i10-rip-*`: EIP-relative operands (RIP + displacement truncated), 0x67 moffs (A0..A3 with a 4-byte offset), XLAT with and
    without 0x67 (EBX + AL) and with FS, `addr32 call`/`addr32 jmp` (linker relaxation: the prefix has no effect);
  - `i10-str-*`: MOVS/STOS/LODS/SCAS/CMPS with 0x67 (ESI/EDI/ECX, written zero-extended), alone and with REP/REPE/REPNE, both DF
    directions, RSI/RDI/RCX holding random upper halves;
  - `i10-loop-*`: LOOP/LOOPE/LOOPNE with 0x67 (ECX, written zero-extended) and a not-taken JECXZ.
Expected results come from ref.py under QEMU 7.2 (unannotated: QEMU's disassembly prints the 0x67 forms in its own way; the
bytes are still checked); every program ends with an ADD that defines all six flags.
"""
import argparse, json, pathlib, random, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_slice1 as g
from gen_slice1 import CODE, DATA, REGION

CPU = 'qemu64,+ssse3,+sse4_1,+sse4_2,+popcnt,+xsave,+avx,+avx2,+bmi1,+bmi2'
RESULT = DATA + 0x1f00                # vector registers stored here (64-bit addressing)
M32 = 2**32 - 1
B64 = {'rbx': 'ebx', 'rsi': 'esi', 'rdi': 'edi', 'rbp': 'ebp', 'r12': 'r12d', 'r13': 'r13d', 'rdx': 'edx'}
I64 = {'r8': 'r8d', 'r9': 'r9d', 'r10': 'r10d', 'rcx': 'ecx'}
DST = {64: ['rax', 'r11', 'r14', 'r15'], 32: ['eax', 'r11d', 'r14d', 'r15d'], 16: ['ax', 'r11w', 'r14w', 'r15w'], 8: ['al', 'r11b', 'r14b', 'r15b']}
PTR = {8: 'byte', 16: 'word', 32: 'dword', 64: 'qword', 128: 'xmmword', 256: 'ymmword'}


class Prog(g.Prog):
    def x(self, text):              # a vector or x87 form: no arithmetic flag (flags.step's `sse` rule)
        self.lines.append(text); self.insns.append({'op': text.split()[0], 'sse': True}); self.cl = None

    def raw(self, line, op='mov', width=64):
        self.lines.append(line); self.insns.append({'op': op, 'width': width}); self.cl = None

    def ea(self, target, fs=0):
        """A 32-bit address expression for linear `target` (FS-relative when fs): random upper halves in its registers."""
        r = self.rng
        kind = r.choice(['base', 'base-disp', 'sib', 'index-only', 'sib'])
        base, index = r.choice(list(B64)), r.choice(list(I64))
        scale = r.choice([1, 2, 4, 8])
        disp = r.choice([0, r.randrange(-128, 128), r.randrange(-2**31, 2**31)]) if kind != 'base' else 0
        want = (target - fs) & M32
        if kind == 'index-only':
            iv = r.getrandbits(32)
            disp = (want - iv * scale) & M32
            disp = disp - 2**32 if disp >= 2**31 else disp
            self.emit(f'mov {index}, {r.getrandbits(32) << 32 | iv:#x}')
            return f'{I64[index]}*{scale} + {disp}'
        iv = r.getrandbits(32) if kind == 'sib' else 0
        bv = (want - (iv * scale if kind == 'sib' else 0) - disp) & M32
        self.emit(f'mov {base}, {r.getrandbits(32) << 32 | bv:#x}')
        if kind == 'sib':
            self.emit(f'mov {index}, {r.getrandbits(32) << 32 | iv:#x}')
            return f'{B64[base]} + {I64[index]}*{scale} + {disp}'
        return f'{B64[base]} + {disp}' if disp else B64[base]

    def done(self):
        self.emit('add rax, r11')     # defines all six arithmetic flags


def target(r, size, align=1):
    a = DATA + r.randrange(0x100, 0x1e00 - size)
    if align == 1 and r.random() < 0.3: a = DATA + 0x1000 - r.randrange(1, size + 1)    # across the page boundary
    return a - a % align


def ea_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    fs = r.choice([0, 0, DATA - 0x100000, (DATA - 0x8000_0000) & (2**64 - 1)])
    for _ in range(14):
        w = r.choice([8, 16, 32, 64]); d = r.choice(DST[w]); t = target(r, w // 8)
        seg = 'fs:' if fs and r.random() < 0.6 else ''
        f = fs if seg else 0
        kind = r.choice(['load', 'store', 'rmw', 'rmw2', 'lock', 'xchg', 'unary', 'movx', 'cmp', 'bt', 'lea', 'pushpop', 'sse', 'avx', 'x87', 'imm'])
        if kind in ('lock', 'xchg'): t = target(r, w // 8, w // 8)   # natural alignment (an atomic crossing 16 bytes stops: I3)
        if kind == 'load': p.emit(f'mov {d}, {PTR[w]} ptr {seg}[{p.ea(t, f)}]')
        elif kind == 'store': p.emit(f'mov {PTR[w]} ptr {seg}[{p.ea(t, f)}], {d}')
        elif kind == 'imm': p.emit(f'mov {PTR[w]} ptr {seg}[{p.ea(t, f)}], {r.randrange(-128, 128)}')
        elif kind == 'rmw': p.emit(f'{r.choice(["add", "sub", "xor", "and", "or"])} {PTR[w]} ptr {seg}[{p.ea(t, f)}], {d}')
        elif kind == 'rmw2': p.emit(f'{r.choice(["add", "sub", "xor", "or"])} {d}, {PTR[w]} ptr {seg}[{p.ea(t, f)}]')
        elif kind == 'lock': p.emit(f'lock {r.choice(["add", "xadd", "or", "sub"])} {PTR[w]} ptr {seg}[{p.ea(t, f)}], {d}')
        elif kind == 'xchg': p.emit(f'xchg {PTR[w]} ptr {seg}[{p.ea(t, f)}], {d}')
        elif kind == 'unary': p.emit(f'{r.choice(["inc", "dec", "neg", "not"])} {PTR[w]} ptr {seg}[{p.ea(t, f)}]')
        elif kind == 'movx':
            sw = r.choice([8, 16]); t = target(r, sw // 8)
            p.emit(f'{r.choice(["movzx", "movsx"])} {r.choice(DST[r.choice([32, 64])])}, {PTR[sw]} ptr {seg}[{p.ea(t, f)}]')
        elif kind == 'cmp': p.emit(f'cmp {PTR[w]} ptr {seg}[{p.ea(t, f)}], {r.randrange(-128, 128)}')
        elif kind == 'bt':
            bw = r.choice([16, 32, 64]); t = target(r, bw // 8)
            p.emit(f'{r.choice(["bt", "bts", "btr", "btc"])} {PTR[bw]} ptr {seg}[{p.ea(t, f)}], {r.randrange(bw)}')
        elif kind == 'lea':
            lw = r.choice([16, 32, 64]); x = r.getrandbits(32)            # any address: LEA does not access memory
            p.emit(f'lea {r.choice(DST[lw])}, [{p.ea(x)}]')
        elif kind == 'pushpop':
            t2 = target(r, 8)
            p.emit(f'push qword ptr {seg}[{p.ea(t, f)}]'); p.emit(f'pop qword ptr [{p.ea(t2)}]')
        elif kind == 'sse':
            x = r.randrange(16); t = target(r, 16, r.choice([1, 16]))
            op = 'movdqa' if t % 16 == 0 and r.random() < 0.5 else 'movdqu'
            if r.random() < 0.5: p.x(f'{op} xmm{x}, xmmword ptr {seg}[{p.ea(t, f)}]')
            else: p.x(f'{op} xmmword ptr {seg}[{p.ea(t, f)}], xmm{x}')
            if t % 16 == 0: p.x(f'paddd xmm{x}, xmmword ptr {seg}[{p.ea(t, f)}]')
        elif kind == 'avx':
            x = r.randrange(16); t = target(r, 32)
            if r.random() < 0.5: p.x(f'vmovdqu ymm{x}, ymmword ptr {seg}[{p.ea(t, f)}]')
            else: p.x(f'vpaddq ymm{x}, ymm{(x + 1) % 16}, ymmword ptr {seg}[{p.ea(t, f)}]')
            p.x(f'vmovdqu ymmword ptr {seg}[{p.ea(target(r, 32), f)}], ymm{x}')
        else:
            t2 = target(r, 8)
            p.x(f'fild dword ptr {seg}[{p.ea(t, f)}]'); p.x(f'fistp dword ptr [{p.ea(t2)}]')   # 32-bit: exact as a double (I6)
    p.x('vzeroupper')
    for x in range(16): p.x(f'movdqu xmmword ptr [{RESULT - 0x100 + 16 * x}], xmm{x}')
    p.done()
    out = g.finish(p, gpr, rf, r) | {'cpu': CPU, 'sse': True, 'avx': True, 'unannotated': True}
    return out | ({'fsbase': hex(fs)} if fs else {})


def rip_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    fs = DATA - 0x8000
    for _ in range(12):
        kind = r.choice(['rip-load', 'rip-store', 'rip-rmw', 'rip-lea', 'moffs-load', 'moffs-store', 'xlat', 'xlat32', 'xlat-fs', 'call', 'jmp'])
        w = r.choice([8, 16, 32, 64]); t = target(r, w // 8)
        if kind == 'rip-load': p.emit(f'mov {r.choice(DST[w])}, {PTR[w]} ptr [eip + {{{t}}}]')
        elif kind == 'rip-store': p.emit(f'mov {PTR[w]} ptr [eip + {{{t}}}], {r.choice(DST[w])}')
        elif kind == 'rip-rmw': p.emit(f'add {PTR[w]} ptr [eip + {{{t}}}], {r.choice(DST[w])}')
        elif kind == 'rip-lea': p.emit(f'lea {r.choice(DST[r.choice([32, 64])])}, [eip + {{{t}}}]')
        elif kind in ('moffs-load', 'moffs-store'):     # 67 [66] [REX.W] A0..A3 moffs32
            w = r.choice([8, 16, 32, 64])
            op = (0xa0 if w == 8 else 0xa1) + (2 if kind == 'moffs-store' else 0)
            pre = [0x67] + ([0x66] if w == 16 else []) + ([0x48] if w == 64 else [])
            p.raw('.byte ' + ', '.join(f'{b:#x}' for b in pre + [op] + list(t.to_bytes(4, 'little'))))
        elif kind == 'xlat':
            p.emit(f'mov rbx, {target(r, 256):#x}'); p.emit(f'mov al, {r.getrandbits(8)}'); p.raw('xlatb')
        elif kind == 'xlat32':
            base = target(r, 256)
            p.emit(f'mov rbx, {r.getrandbits(32) << 32 | base:#x}'); p.emit(f'mov al, {r.getrandbits(8)}'); p.raw('.byte 0x67, 0xd7')
        elif kind == 'xlat-fs':
            base = (target(r, 256) - fs) & M32
            p.emit(f'mov rbx, {r.getrandbits(32) << 32 | base:#x}'); p.emit(f'mov al, {r.getrandbits(8)}'); p.raw('.byte 0x64, 0x67, 0xd7')
        elif kind == 'call':    # addr32 call next; pop: the return address is the next instruction's
            n = len(p.lines)
            p.raw('.byte 0x67'); p.raw(f'call l{n}', 'call'); p.lines.append(f'l{n}:'); p.emit('pop r14')
        else:
            n = len(p.lines)
            p.raw('.byte 0x67'); p.raw(f'jmp l{n}', 'jmp'); p.lines.append(f'l{n}:')
    p.done()
    return g.finish(p, gpr, rf, r) | {'unannotated': True, 'fsbase': hex(fs)}


STR = {'movs': 0xa4, 'cmps': 0xa6, 'stos': 0xaa, 'lods': 0xac, 'scas': 0xae}


def str_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    for _ in range(8):
        op = r.choice(list(STR)); w = r.choice([8, 16, 32, 64]); k = w // 8
        rep = r.choice([None, 'f3' if op not in ('cmps', 'scas') else r.choice(['f3', 'f2'])])
        count = r.choice([0, 1, 2, r.randrange(3, 30)]) if rep else 1
        df = r.random() < 0.4
        span = max(count, 1) * k
        def place():
            lo, hi = DATA + span + 16, DATA + REGION - span - 16
            return r.randrange(lo, hi)
        if rep: p.emit(f'mov rcx, {r.getrandbits(32) << 32 | count:#x}')
        p.emit(f'mov rsi, {r.getrandbits(32) << 32 | place():#x}')
        p.emit(f'mov rdi, {r.getrandbits(32) << 32 | place():#x}')
        if op in ('cmps', 'scas') and r.random() < 0.5: p.emit('mov rdi, rsi')
        if df: p.emit('std')
        pre = [0x67] + ([int(rep, 16)] if rep else []) + ([0x66] if w == 16 else []) + ([0x48] if w == 64 else [])
        p.raw('.byte ' + ', '.join(f'{b:#x}' for b in pre + [STR[op] + (w > 8)]), op, w)
        if df: p.emit('cld')
        p.emit('add rax, rsi'); p.emit('xor rax, rdi'); p.emit('add rax, rcx')
    p.done()
    return g.finish(p, gpr, rf, r) | {'unannotated': True}


def loop_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    for _ in range(4):
        n = len(p.lines)
        op = r.choice([0xe2, 0xe1, 0xe0])
        p.emit(f'mov rcx, {r.getrandbits(32) << 32 | r.randrange(1, 9):#x}')
        p.emit(f'mov r9, {r.randrange(1, 9)}')
        p.lines.append(f'l{n}:')
        p.emit('add rax, 3'); p.emit('dec r9'); p.emit('sete dl'); p.emit('cmp dl, 0')    # ZF = (r9 != 0) for LOOPE/LOOPNE
        p.raw('.byte 0x67'); p.raw(f'{ {0xe2: "loop", 0xe1: "loope", 0xe0: "loopne"}[op]} l{n}', 'loop')
        p.emit('add rax, rcx')
        # JECXZ not taken: RCX's low half nonzero, upper half random; then taken would skip the next ADD (not generated: QEMU
        # translates only what runs; the taken path is a run_i10 case).
        p.emit(f'mov rcx, {r.getrandbits(32) << 32 | r.randrange(1, 2**32):#x}')
        p.raw('.byte 0x67'); p.raw(f'jrcxz l{n}e', 'jrcxz'); p.lines.append(f'l{n}e:')
        p.emit('xor rax, rcx')
    p.done()
    return g.finish(p, gpr, rf, r) | {'unannotated': True}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261030)
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = [f(f'i10-{k}-{i:03d}', rng) for k, f, n in [('ea', ea_program, 40), ('rip', rip_program, 16), ('str', str_program, 24),
                                                       ('loop', loop_program, 8)] for i in range(n)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i10.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
