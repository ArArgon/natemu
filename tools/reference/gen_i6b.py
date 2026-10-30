#!/usr/bin/env python3
"""Seeded generator for the I6b corpus: PCMPESTRI/PCMPESTRM/PCMPISTRI/PCMPISTRM (legacy and VEX.128, REX.W/VEX.W lengths)
and the x87 transcendental, remainder, scale and extract forms (boot-features plan, chunk I6b).

    python3 tools/reference/gen_i6b.py -o OUT.json [--seed N]

String programs (`i6b-str-*`): every imm8 (0..255) appears in each of the four forms, legacy and VEX, with register and memory
second operands (unaligned, across the page boundary at DATA + 0x1000 too), implicit lengths from NUL elements and explicit
lengths in EAX/EDX (or RAX/RDX with REX.W/VEX.W: negative, saturating, upper bits set). After each: LAHF and SETO (CF ZF SF
OF, AF = PF = 0), ECX or XMM0 (YMM0 for VEX: its upper half zeroed) stored.

x87 programs (`i6b-x87-*`): each operation starts from an empty stack after FNCLEX, loads its operands with FLD m64, runs, stores
FSW (masked to what the SDM defines and the pinned QEMU computes, below) and pops every result with FSTP m64:
  - exact groups (compared exactly): FPREM and FPREM1 (C0 C1 C2 C3: the quotient's bits and the partial remainder, exception
    flags; also three steps in a row, glibc's fmod loop unrolled), FSCALE (results in the double's normal range: no
    flag), FXTRACT (doubles, denormals included: ZE for zero);
  - value groups (FSIN FCOS FPTAN FSINCOS F2XM1 FYL2X FYL2XP1 FPATAN): QEMU computes FSIN/FCOS/FPTAN/FSINCOS with the host's
    double libm and the others in 80 bits, natemu with compiler_builtins' double libm, so their m64 results are compared within
    `tolerance` ulps (the program lists those addresses); FSW keeps TOP and, for the trigonometric forms, C2 (|x| >= 2^63
    out of range, never exactly 2^63, where QEMU's test is >), their exception flags and C1 being judged by the SDM cases
    (tools/run_i6b.py: QEMU 7.2 raises no flag in FSIN/FCOS/FPTAN/FSINCOS, and C1 is never the round-up bit, I6 deviation 5).
Avoided by construction (recorded differences): FPTAN of a NaN (QEMU pushes 1.0, natemu the NaN, as Bochs), the trigonometric
forms of an infinity (QEMU returns its host's default NaN with no IE), stack faults (QEMU models none).
"""
import argparse, json, math, pathlib, random, struct, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_slice1 as g, ref
from gen_slice1 import DATA

PATTERNS = DATA            # 0x800 bytes of string data (crosses nothing; memory operands also come from DATA + 0xf00..0x1100)
LENGTHS = DATA + 0x800     # unused (lengths are immediates)
F64P = DATA + 0x1200       # x87 operand doubles
RESULT = DATA + 0x1800     # results
CPU = 'qemu64,+ssse3,+sse4_1,+sse4_2,+popcnt,+xsave,+avx'
FORMS = ['pcmpestrm', 'pcmpestri', 'pcmpistrm', 'pcmpistri']


def d2b(x): return struct.unpack('<Q', struct.pack('<d', x))[0]


class Prog(g.Prog):
    def x(self, text, op=None):
        a = {'op': op or text.split()[0], 'sse': True}
        import flags
        self.undef = flags.step(a, self.undef); self.cl = None
        self.lines.append(text); self.insns.append(a)


def string_vector(r, words):
    """16 bytes: elements from a small alphabet (so matches and ranges happen), a few signed-range values, NULs at random."""
    n, w = (8, 2) if words else (16, 1)
    out = b''
    nul = r.choice([n, n, r.randrange(n)])
    for i in range(n):
        if i == nul: e = 0
        elif r.random() < 0.1: e = r.getrandbits(8 * w) or 1
        elif r.random() < 0.1: e = (0x8000 if words else 0x80) | r.randrange(1, 8)
        else: e = 0x41 + r.randrange(6)
        out += e.to_bytes(w, 'little')
    return out


def string_program(name, rng, imms):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r)
    p = Prog(name, r)
    vex = r.random() < 0.5
    res = RESULT
    for imm in imms:
        form = r.choice(FORMS)
        explicit, index = form.startswith('pcmpe'), form.endswith('i')
        a, b = r.sample(range(1, 16), 2)
        p.x(f'movdqu xmm{a}, xmmword ptr [{PATTERNS + 16 * r.randrange(0x80)}]')
        if r.random() < 0.5:
            src = f'xmm{b}'
            p.x(f'movdqu xmm{b}, xmmword ptr [{PATTERNS + 16 * r.randrange(0x80)}]')
        else:
            src = f'xmmword ptr [{r.choice([PATTERNS + r.randrange(0x7f0), DATA + 0x1000 - r.randrange(1, 16), DATA + 0xff0 + r.randrange(32)])}]'
        wide = explicit and r.random() < 0.4
        if explicit:
            pick = lambda: r.choice([0, 1, 2, 3, 5, 7, 8, 9, 15, 16, 17, 100, -1, -3, -8, -9, -16, -17, -2**31, 2**31 - 1, r.randrange(-40, 40)])
            for reg in ('rax', 'rdx'):
                v = pick() & (2**64 - 1)
                # Upper bits are read only with REX.W/VEX.W, which the pinned QEMU 7.2 ignores (its new decoder passes the bare
                # imm8 to helper_pcmpestri/m): its REX.W forms get values whose 32- and 64-bit lengths agree (sign-extended or
                # both saturating); tools/run_i6b.py `cases` checks the 64-bit lengths by the SDM.
                if wide: v = (v if v < 2**31 or v >= 2**64 - 2**31 else r.choice([2**63, 2**64 - 2**33 - 5])) if r.random() < 0.8 else r.choice([2**32 + 2**31, 2**63 + 2**31 + 5])
                elif r.random() < 0.3: v ^= r.getrandbits(32) << 32   # ignored without REX.W
                p.emit(f'mov {reg}, {v:#x}')
        text = f'{"v" if vex else ""}{form} xmm{a}, {src}, {imm:#x}'
        if wide:
            # No mnemonic for the REX.W/VEX.W forms: their bytes (66 REX.W 0F 3A, or VEX.W = 1), the same annotation.
            code = bytearray(ref.assemble(text))
            if vex: code[2] |= 0x80
            else: code[1:1] = b'\x48' if code[1] == 0x0f else b''; code[1] |= 0x08
            p.x('.byte ' + ', '.join(f'{c:#x}' for c in code), op=('v' if vex else '') + form)
        else:
            p.x(text)
        p.emit('lahf'); p.emit('seto al')
        p.emit(f'mov word ptr [{res}], ax')
        if index: p.emit(f'mov dword ptr [{res + 4}], ecx')
        elif vex: p.x(f'vmovdqu ymmword ptr [{res + 8}], ymm0')
        else: p.x(f'movdqu xmmword ptr [{res + 8}], xmm0')
        res += 0x28
    out = g.finish(p, gpr, rf, r)
    data = bytearray.fromhex(out['memory'][0]['data'])
    words = lambda: r.random() < 0.5
    data[0:0x800] = b''.join(string_vector(r, words()) for _ in range(0x80))
    data[0xf00:0x1100] = b''.join(string_vector(r, words()) for _ in range(0x20))
    out['memory'][0]['data'] = data.hex()
    return out | {'sse': True, 'cpu': CPU} | ({'avx': True} if vex else {})


def rnd(r, lo=-30, hi=30):
    return r.choice([-1, 1]) * r.uniform(1, 2) * 2.0 ** r.randrange(lo, hi)


# x87 operations: (instruction, operand count (pushed in order: the last is ST(0)), FSW mask, results left on the stack, exact).
# FPREM1's quotient bits (C0 C1 C3) are not compared: the pinned QEMU 7.2 never computes them (floatx80_modrem passes no quotient
# pointer for the IEEE remainder: they read 0); tools/run_i6b.py `cases` judges them by the SDM.
X87 = {'fprem': (2, 0x7f7f, 2, True), 'fprem1': (2, 0x3c7f, 2, True), 'fscale': (2, 0x383f, 2, True), 'fxtract': (1, 0x383f, 2, True),
       'fsin': (1, 0x3c00, 1, False), 'fcos': (1, 0x3c00, 1, False), 'fptan': (1, 0x3c00, 2, False), 'fsincos': (1, 0x3c00, 2, False),
       'f2xm1': (1, 0x3800, 1, False), 'fyl2x': (2, 0x3800, 1, False), 'fyl2xp1': (2, 0x3800, 1, False), 'fpatan': (2, 0x3800, 1, False)}


def x87_operands(op, r):
    """Operands in push order (ST(1) first, then ST(0))."""
    special = r.random() < 0.15
    inf, nan = float('inf'), float('nan')
    if op in ('fprem', 'fprem1'):
        if special: return [r.choice([0.0, -0.0, inf, -inf, 3.0]), r.choice([0.0, -0.0, inf, -inf, 5.5, nan])]
        y = rnd(r, -40, 40)
        x = r.choice([-1, 1]) * abs(y) * r.uniform(1, 2) * 2.0 ** r.choice([r.randrange(-3, 70), r.randrange(60, 400), r.randrange(0, 8)])
        if r.random() < 0.2: x = float(r.randrange(-100, 100)); y = float(r.choice([2, 3, -3, 4, 7, 0.5, 1.5]))
        return [y, x]
    if op == 'fscale':
        if special: return [r.choice([inf, -inf, 0.0, -0.0, 2.9, -2.9]), r.choice([0.0, -0.0, 1.5, -3.0, inf, -inf])]
        return [r.choice([float(r.randrange(-60, 61)), r.uniform(-60, 60)]), rnd(r, -100, 100)]
    if op == 'fxtract':
        if special: return [r.choice([0.0, -0.0, inf, -inf, 5e-324, -2.5e-310, 1.0])]
        return [r.choice([rnd(r, -1000, 1000), struct.unpack('<d', struct.pack('<Q', r.getrandbits(52) or 1))[0]])]
    if op in ('fsin', 'fcos', 'fptan', 'fsincos'):
        if special: return [r.choice([0.0, -0.0, 2.0 ** 63 * 1.5, -2.0 ** 64, 1e300, 2.0 ** 63 - 1024, math.pi, -math.pi / 2])]
        return [r.choice([r.uniform(-4, 4), rnd(r, -30, 40), r.uniform(-1e6, 1e6)])]
    if op == 'f2xm1':
        if special: return [r.choice([0.0, -0.0, 1.0, -1.0, 0.5, -0.5])]   # beyond [-1, 1] undefined (QEMU: invalid)
        return [r.choice([r.uniform(-1, 1), rnd(r, -60, -1)])]
    if op == 'fyl2x':
        if special: return [r.choice([3.0, -2.0, 0.0, inf]), r.choice([8.0, 1.0, 0.25, 2.0 ** -1074, 2.0 ** 1000])]
        return [rnd(r, -20, 20), r.choice([abs(rnd(r, -300, 300)), float(2 ** r.randrange(-100, 100))])]
    if op == 'fyl2xp1':
        if special: return [r.choice([3.0, -2.0, 0.0]), r.choice([0.0, -0.0])]
        return [rnd(r, -20, 20), r.uniform(-0.29, 0.29)]
    if special: return [r.choice([0.0, -0.0, inf, -inf, 1.0]), r.choice([0.0, -0.0, inf, -inf, -1.0])]   # fpatan: atan2(ST(1), ST(0))
    return [rnd(r, -50, 50), rnd(r, -50, 50)]


def x87_program(name, rng, ops):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r)
    p = Prog(name, r)
    values, res, tolerant, jumps = [], RESULT, [], 0
    p.x('fninit')
    for op in ops:
        n, mask, left, exact = X87[op]
        if len(values) + n > 0x80: break
        operands = x87_operands(op, r)
        if op in ('fptan', 'fsincos') and abs(operands[0]) >= 2.0 ** 63: left = 1   # out of range: nothing pushed
        for v in operands:
            p.x(f'fld qword ptr [{F64P + 8 * len(values)}]'); values.append(v)
        p.x('fnclex')
        # FPREM/FPREM1 sometimes three times in a row (glibc's fmod loop unrolled: partial remainders, then the complete one and
        # its quotient bits, then a quotient of 0), each step's FSW stored.
        for k in range(3 if op in ('fprem', 'fprem1') and r.random() < 0.4 else 1):
            p.x(op)
            p.x('fnstsw ax'); p.emit(f'and ax, {mask:#x}'); p.emit(f'mov word ptr [{res + 2 * k}], ax' if k else f'mov word ptr [{res}], ax')
        for k in range(left):
            p.x(f'fstp qword ptr [{res + 8 + 8 * k}]')
            if not exact: tolerant.append(res + 8 + 8 * k)
        res += 0x28
        if len(p.lines) % 12 == 11:   # end QEMU's translation block now and then (its in_asm log, gen_i6)
            jumps += 1
            p.emit(f'jmp tb{jumps}'); p.lines.append(f'tb{jumps}:')
    out = g.finish(p, gpr, rf, r)
    data = bytearray.fromhex(out['memory'][0]['data'])
    data[F64P - DATA:F64P - DATA + 8 * len(values)] = b''.join(struct.pack('<d', v) for v in values)
    out['memory'][0]['data'] = data.hex()
    return out | {'tolerance': [hex(a) for a in tolerant]}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261030)
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = []
    # Strings: each imm8 four times (legacy and VEX programs draw their forms at random), 16 operations per program.
    imms = list(range(256)) * 4
    rng.shuffle(imms)
    for i in range(0, len(imms), 16):
        progs.append(string_program(f'i6b-str-{i // 16:03d}', rng, imms[i:i + 16]))
    for i in range(48):
        ops = [rng.choice(list(X87)) for _ in range(14)] if i >= 12 else [list(X87)[i]] * 14
        progs.append(x87_program(f'i6b-x87-{i:03d}', rng, ops))
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i6b.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA), 'result': hex(RESULT)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
