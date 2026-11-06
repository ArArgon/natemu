#!/usr/bin/env python3
"""Seeded generator for the I3 corpus: LOCK forms (boot-features plan, chunk I3).

    python3 tools/reference/gen_i3.py -o OUT.json [--seed N] [--random 60]

Every lockable memory form with LOCK at every width: ADD ADC SUB SBB AND OR XOR (register and immediate sources), INC DEC
NOT NEG, BTS BTR BTC (immediate and register bit offsets, negative and beyond the operand), XADD, CMPXCHG (equal and
unequal), XCHG (with and without the prefix), CMPXCHG8B and CMPXCHG16B (locked and unlocked, equal and unequal), with
random operands and flags, boundary values stored first, every address form (base, base + displacement, index, absolute,
RIP-relative) and an FS override, aligned and unaligned inside 16 bytes, mixed with the slice-1 forms of gen_slice1.py.
A LOCKed operand never crosses a 16-byte boundary (that is the `unsupported_atomic` stop, a fault case) and CMPXCHG16B's
is 16-byte aligned (else #GP, a fault case): the generator knows every address because rbx rbp rsi rdi r12 r13 and r8 are
never written. Inputs only: expected results come from ref.py under QEMU (qemu64 has CX8 and CX16).
"""
import argparse, json, pathlib, random, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, gen_slice1 as g
from gen_slice1 import DATA, STACK, REGION, SIZE, NAMES, PTRS, INDEX
from ref import R64

SIZE = {**SIZE, 128: 'xmmword'}
ALU = ['add', 'adc', 'sub', 'sbb', 'and', 'or', 'xor']
UNARY = ['inc', 'dec', 'not', 'neg']
BITS = ['bts', 'btr', 'btc']
BOUNDARY = {8: [0, 1, 0x7f, 0x80, 0xff], 16: [0, 1, 0x7fff, 0x8000, 0xffff], 32: [0, 1, 0x7fffffff, 0x80000000, 0xffffffff],
            64: [0, 1, 2**63 - 1, 2**63, 2**64 - 1]}


class Prog(g.Prog):
    def __init__(self, name, rng, gpr, fsbase=0):
        super().__init__(name, rng)
        self.gpr, self.fsbase = gpr, fsbase

    def locked(self, text, lock=True, **extra):
        """`lock TEXT` (or TEXT), annotated as the unlocked form plus `lock`."""
        a = g.annotate(text) | extra | ({'lock': True} if lock else {})     # (= g.annotate('lock ' + text) | extra)
        self.undef = flags.step(a, self.undef); self.cl = None
        self.lines.append(('lock ' if lock else '') + text); self.insns.append(a)

    def operand(self, w, align=False, fs=False):
        """(operand text, linear address) of a w-bit operand in the data region that does not cross 16 bytes (16-byte aligned
        when `align`); often aligned to its size, else anywhere inside its 16 bytes."""
        r, n = self.rng, w // 8
        for _ in range(1000):
            form = r.randrange(5 if not fs else 3)
            bi = r.choice(PTRS); b, base = R64[bi], self.gpr[bi]
            if form == 0: s, a = f'[{b}]', base
            elif form == 1: d = r.randrange(-0x300, 0x300); s, a = f'[{b} + {d}]', base + d
            elif form == 2:
                sc, d = r.choice([1, 2, 4, 8]), r.randrange(-0x300, 0x300)
                s, a = f'[{b} + r8*{sc} + {d}]', base + self.gpr[INDEX] * sc + d
            else:
                a = DATA + r.randrange(0, REGION - 16)
                if r.random() < 0.6: a &= ~(n - 1) if n <= 16 else ~15
                s = f'[{a}]' if form == 3 else f'[rip + {{{a}}}]'
            if fs: s, a = 'fs:' + s, a + self.fsbase
            ok = DATA <= a and a + n <= DATA + REGION and ((a & 15) == 0 if align else (a & 15) + n <= 16)
            if ok and (form >= 3 or r.random() < 0.5 or a % n == 0): return f'{SIZE[w]} ptr {s}', a
        raise ValueError('no operand')

    def store(self, a, w, v):
        """mov [a], v (absolute address: the generator's own set-up of the operand's value)."""
        if w == 64 and not -2**31 <= (v if v < 2**63 else v - 2**64) < 2**31:
            self.emit(f'mov r15, {v}'); self.emit(f'mov qword ptr [{a}], r15')
        else:
            self.emit(f'mov {SIZE[w]} ptr [{a}], {v if w < 64 or v < 2**31 else v - 2**64}')

    def scratch(self, w):
        return self.reg(w, writable=True)

    def lock_one(self, kind=None, w=None, fs=False):
        r = self.rng
        kind = kind or r.choice(['alu', 'alu', 'alu', 'unary', 'unary', 'bit', 'bit', 'xadd', 'cmpxchg', 'xchg', 'pair'])
        w = w or r.choice([8, 16, 32, 64])
        if kind == 'pair':
            wide = r.random() < 0.5; m, a = self.operand(128 if wide else 64, align=wide, fs=fs)
            n = 8 if wide else 4; rax, rdx = ('rax', 'rdx') if wide else ('eax', 'edx')
            if r.random() < 0.5:          # equal: load the operand into RDX:RAX first
                self.emit(f'mov {rax}, {SIZE[8 * n]} ptr [{a}]'); self.emit(f'mov {rdx}, {SIZE[8 * n]} ptr [{a + n}]')
            if r.random() < 0.3: self.emit(f'mov rcx, {r.getrandbits(64)}')
            # The pinned QEMU (7.2) does not store its lazy flag state before the cmpxchg8b/16b helper, which then keeps
            # CF PF AF SF OF from a stale state (e.g. `mul al; cmpxchg8b m; sbb`: QEMU subtracts no borrow, while Rosetta 2
            # and natemu keep MUL's CF = 1 as the SDM says). `pushfq` right before stores it; its slot is popped after.
            self.need_flags(flags.ARITH); self.emit('pushfq', op='pushf')
            self.locked(f'cmpxchg{16 if wide else 8}b {m}', lock=r.random() < 0.7, width=128 if wide else 64)
            self.emit('pop r15')
            return
        if kind == 'bit' and w == 8: w = r.choice([16, 32, 64])
        m, a = self.operand(w, fs=fs)
        if r.random() < 0.3: self.store(a, w, r.choice(BOUNDARY[w]))
        if kind == 'alu':
            op = r.choice(ALU)
            if op in ('adc', 'sbb'): self.need_flags(flags.CF)
            src = str(self.imm(w)) if r.random() < 0.4 else self.reg(w)
            self.locked(f'{op} {m}, {src}')
        elif kind == 'unary':
            self.locked(f'{r.choice(UNARY)} {m}')
        elif kind == 'bit':
            op = r.choice(BITS)
            if r.random() < 0.5:
                self.locked(f'{op} {m}, {r.choice([0, 1, w - 1, w, w + 1, 255, r.randrange(256)])}')
            else:   # a register bit offset (signed, beyond the operand too) that keeps the addressed byte in the region
                n = w // 8; lo, hi = (DATA + n - a) * 8, (DATA + REGION - 2 * n - a) * 8   # QEMU reads the operand-size word
                off = r.choice([0, 1, w - 1, w, -1, -w, r.randrange(max(lo, -2000), min(hi, 2000))])
                off = max(lo, min(hi - 1, off))
                reg = NAMES[w][9]
                self.emit(f'mov {NAMES[64][9]}, {off}'); self.locked(f'{op} {m}, {reg}')
        elif kind == 'xadd':
            self.locked(f'xadd {m}, {self.scratch(w)}')
        elif kind == 'cmpxchg':
            if r.random() < 0.5: self.emit(f'mov {NAMES[w][0]}, {SIZE[w]} ptr [{a}]')
            src = self.scratch(w)
            self.locked(f'cmpxchg {m}, {src}')
        else:
            self.locked(f'xchg {m}, {self.scratch(w)}', lock=r.random() < 0.5)


def program(name, rng, body, rflags=None, fsbase=0, slice1=0):
    for attempt in range(100):
        r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r)
        p = Prog(name, r, gpr, fsbase)
        try:
            body(p)
            for _ in range(slice1):
                if r.random() < 0.5: p.one()
                else: p.lock_one()
            out = g.finish(p, gpr, rf if rflags is None else rflags, r)
            return out | ({'fsbase': hex(fsbase)} if fsbase else {})
        except ValueError as e: last = e
    raise last


def edges(rng):
    progs = []
    for w in (8, 16, 32, 64):
        for op in ALU:
            def body(p, op=op, w=w):
                for _ in range(4):
                    m, a = p.operand(w)
                    p.store(a, w, p.rng.choice(BOUNDARY[w]))
                    if op in ('adc', 'sbb'): p.emit(p.rng.choice(['stc', 'clc', 'cmc']))
                    p.locked(f'{op} {m}, {p.reg(w) if p.rng.random() < 0.5 else p.imm(w)}')
                    p.emit(f'mov {p.scratch(w)}, {SIZE[w]} ptr [{a}]')
            progs.append(program(f'i3-edge-{op}{w}', rng, body))
        def unary(p, w=w):
            for op in UNARY:
                for v in p.rng.sample(BOUNDARY[w], 2):
                    m, a = p.operand(w); p.store(a, w, v); p.locked(f'{op} {m}')
        progs.append(program(f'i3-edge-unary{w}', rng, unary))
        def exchange(p, w=w):
            for k in ('xadd', 'cmpxchg', 'cmpxchg', 'xchg', 'xchg'):
                p.lock_one(k, w)
        progs.append(program(f'i3-edge-exchange{w}', rng, exchange))
        if w > 8:
            def bits(p, w=w):
                for _ in range(6): p.lock_one('bit', w)
            progs.append(program(f'i3-edge-bits{w}', rng, bits))
    def pairs(p):
        for _ in range(8): p.lock_one('pair')
    progs += [program(f'i3-edge-cmpxchg8b16b-{i}', rng, pairs) for i in range(4)]
    def fs(p):
        for _ in range(6): p.lock_one(w=p.rng.choice([8, 16, 32, 64]), fs=True)
    progs.append(program('i3-edge-fs', rng, fs, fsbase=0x40))
    # The kernel's shapes: an atomic_inc/dec_and_test, a spin lock (cmpxchg on 0 then a store of 0), test_and_set_bit.
    def kernel(p):
        m, a = p.operand(32); p.store(a, 32, 1)
        p.locked(f'dec {m}'); p.emit('sete al')
        p.locked(f'inc {m}'); p.emit('setne cl')
        m, a = p.operand(32); p.store(a, 32, 0)
        p.emit('xor eax, eax'); p.emit('mov edx, 1'); p.locked(f'cmpxchg {m}, edx'); p.emit('setne r9b')
        p.emit(f'mov dword ptr [{a}], 0')
        m, a = p.operand(64)
        p.locked(f'bts {m}, 5'); p.emit('setb r10b'); p.locked(f'btr {m}, 5'); p.emit('setb r11b')
        m, a = p.operand(64); p.emit('mov rcx, -1'); p.locked(f'xadd {m}, rcx')
    progs.append(program('i3-edge-kernel-shapes', rng, kernel))
    return progs


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261029); ap.add_argument('--random', type=int, default=60)
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = edges(rng) + [program(f'i3-random-{n:02d}', rng, lambda p: None, slice1=rng.randrange(10, 36)) for n in range(a.random)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i3.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA), 'stack': hex(STACK)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
