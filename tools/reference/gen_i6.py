#!/usr/bin/env python3
"""Seeded generator for the I6 corpus: x87 forms natemu admits, on values whose results are the same in FEX's
reduced-precision mode (doubles) and on an 80-bit x87 (boot-features plan, chunk I6).

    python3 tools/reference/gen_i6.py -o OUT.json [--seed N] [--random 60]

Two kinds of groups, each starting with FNINIT and FLDCW (every exception masked, a random rounding control):
  - `double`: FCW.PC = 10 (53-bit significands). An 80-bit x87 then rounds every FADD/FSUB/FMUL/FDIV/FSQRT result to a
    double's precision once, which is what a double operation does while the result stays in the double's normal range: the
    values are drawn so it does (magnitudes 2^-20..2^20, at most a few dozen operations, plus zeros, infinities and NaNs).
  - `exact`: FCW.PC = 11 (64-bit significands) with operations that are exact in both: loads and stores of values a double
    holds, small-integer and binary-fraction add/sub/mul, division by powers of two and by zero, FSQRT of perfect squares and
    negatives, integer conversions in and out of range under every rounding control (FIST/FISTP/FISTTP), FRNDINT, compares.
Every form also appears under the stack discipline both keep (no stack overflow or underflow: FEX models neither). Results:
FNSTSW after compares and FXAM, SETcc after FCOMI (the pinned QEMU keeps OF, SF and AF there, which the SDM clears), FNSTCW, FNSTENV (its instruction and data pointer fields zeroed by the program: not modelled),
the stack popped into memory with FSTP m64, FNSAVE/FRSTOR and FLD/FSTP m80 round trips. Avoided by construction, because they
are recorded deviations: 80-bit precision, constants other than 1 and 0 (FEX loads FLDPI & co. as nearest doubles whatever
FCW.RC), denormal operands (x87 DE on loads), two NaN operands (x87's larger-significand rule), results outside the double range.
"""
import argparse, json, pathlib, random, struct, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_slice1 as g
from gen_slice1 import DATA

F64P = DATA            # 32 doubles
F32P = DATA + 0x100    # 32 floats
I16P = DATA + 0x180    # 16 words
I32P = DATA + 0x1a0    # 16 dwords
I64P = DATA + 0x1e0    # 16 qwords
M80P = DATA + 0x260    # 12 80-bit images, 16 bytes apart
FCWP = DATA + 0x320    # FCW values
SCRATCH = DATA + 0x400 # stores land in [SCRATCH, RESULT), across the page boundary at DATA + 0x1000 too
RESULT = DATA + 0x1800

INF, NINF, QNAN = 0x7ff0000000000000, 0xfff0000000000000, 0x7ff8000000000000


def d2b(x): return struct.unpack('<Q', struct.pack('<d', x))[0]
def to_f80(d):
    sign, exp, frac = d >> 63, d >> 52 & 0x7ff, d & ((1 << 52) - 1)
    if exp == 0 and frac == 0: return 0, sign << 15
    if exp == 0x7ff: return 1 << 63 | frac << 11, sign << 15 | 0x7fff
    assert exp != 0, 'no denormals in the corpus'
    return 1 << 63 | frac << 11, sign << 15 | (exp + 15360)


def pools(rng, kind):
    """Doubles, floats (exact in both), int16/32/64 and the 80-bit images for one program."""
    if kind == 'double':
        v = [rng.choice([-1, 1]) * rng.uniform(1, 2) * 2.0 ** rng.randrange(-20, 21) for _ in range(26)]
        f = [struct.unpack('<f', struct.pack('<f', rng.choice([-1, 1]) * rng.uniform(1, 2) * 2.0 ** rng.randrange(-20, 21)))[0] for _ in range(28)]
    else:
        v = [rng.choice([-1, 1]) * rng.randrange(0, 65) / rng.choice([1, 2, 4]) for _ in range(26)]
        f = [rng.choice([-1, 1]) * rng.randrange(0, 257) / rng.choice([1, 2, 4, 8]) for _ in range(28)]
    d = [d2b(x) for x in v] + [0, 1 << 63, INF, NINF, QNAN, d2b(rng.choice([2.5, -2.5, 3.5, 0.5, -0.5, 1.5]))]
    fl = [struct.unpack('<I', struct.pack('<f', x))[0] for x in f] + [0, 0x80000000, 0x7f800000, 0x7fc00000]
    rng.shuffle(d); rng.shuffle(fl)
    i16 = [rng.choice([0, 1, -1, 7, -300, 32767, -32768, rng.randrange(-32768, 32768)]) for _ in range(16)]
    i32 = [rng.choice([0, 1, -1, 2 ** 31 - 1, -2 ** 31, rng.randrange(-2 ** 31, 2 ** 31)]) for _ in range(16)]
    # exact groups: sums of a few dozen operands stay below 2^53 (FMUL there is by a power of two only)
    i64 = [rng.choice([0, -1, 2 ** 24, -2 ** 24, rng.randrange(-2 ** 24, 2 ** 24)] if kind == 'exact' else
                      [0, -1, 2 ** 53, -2 ** 53, rng.randrange(-2 ** 53, 2 ** 53)]) for _ in range(16)]
    m80 = [to_f80(rng.choice(d)) for _ in range(12)]
    return d, fl, i16, i32, i64, m80


class Prog(g.Prog):
    def x(self, text):
        m = text.split()[0]
        a = {'op': m, 'sse': True}   # checked by its first three letters against QEMU's disassembly (ref.check_one)
        import flags
        self.undef = flags.step(a, self.undef); self.cl = None
        self.lines.append(text); self.insns.append(a)

    def pointers(self, a):
        """Zero the instruction/data pointer fields (FIP, FCS:FOP, FDP, FDS) of a 28-byte environment image: not modelled."""
        for o in (12, 16, 20, 24): self.emit(f'mov dword ptr [{a + o}], 0')

    def scratch(self, n):
        r = self.rng
        a = SCRATCH + r.randrange(0, RESULT - SCRATCH - n)
        if r.random() < 0.15: a = DATA + 0x1000 - r.randrange(1, n)
        if r.random() < 0.5: a &= ~(n - 1) if n in (2, 4, 8) else ~7
        return a


def group(p, kind, r, n):
    """One group: FNINIT, FLDCW, then `n` x87 operations keeping 0 <= depth <= 8 and the group's exactness rules."""
    rc = r.randrange(4)
    fcw = 0x7f | (2 if kind == 'double' else 3) << 8 | rc << 10
    slot = FCWP + 2 * len(p.fcws); p.fcws.append(fcw)
    p.x('fninit'); p.x(f'fldcw word ptr [{slot}]')
    depth = 0
    exact = kind == 'exact'
    d64 = lambda: f'qword ptr [{F64P + 8 * r.randrange(32)}]'
    d32 = lambda: f'dword ptr [{F32P + 4 * r.randrange(32)}]'
    for step in range(n):
        # End QEMU's translation block now and then: its in_asm log disassembles at most 1 KiB of a block (an instruction
        # crossing that shows as `.byte`, which ref.py's annotation check would reject).
        if step % 10 == 9:
            p.jumps = getattr(p, 'jumps', 0) + 1
            p.emit(f'jmp tb{p.jumps}'); p.lines.append(f'tb{p.jumps}:')   # the label is no instruction (no annotation)
        c = r.random()
        if depth < 2 or (depth < 7 and c < 0.25):               # push
            k = r.randrange(8)
            if k == 0: p.x(f'fld {d64()}')
            elif k == 1: p.x(f'fld {d32()}')
            elif k == 2: p.x(f'fild word ptr [{I16P + 2 * r.randrange(16)}]')
            elif k == 3: p.x(f'fild dword ptr [{I32P + 4 * r.randrange(16)}]')
            elif k == 4: p.x(f'fild qword ptr [{I64P + 8 * r.randrange(16)}]')
            elif k == 5: p.x(r.choice(['fld1', 'fldz']))
            elif k == 6 and depth: p.x(f'fld st({r.randrange(depth)})')
            else: p.x(f'fld tbyte ptr [{M80P + 16 * r.randrange(12)}]')
            depth += 1
            continue
        k = r.randrange(16)
        i = r.randrange(1, depth)
        if k <= 3:                                                 # arithmetic
            op = r.choice(['fadd', 'fsub', 'fsubr', 'fmul', 'fdiv', 'fdivr'] if not exact else ['fadd', 'fsub', 'fsubr'])
            form = r.randrange(5)
            if form == 0: p.x(f'{op} st(0), st({i})')
            elif form == 1: p.x(f'{op} st({i}), st(0)')
            elif form == 2: p.x(f'{op}p st({i}), st(0)'); depth -= 1
            elif form == 3: p.x(f'{op} {r.choice([d64(), d32()])}')
            else: p.x(f'fi{op[1:]} {r.choice([f"word ptr [{I16P + 2 * r.randrange(16)}]", f"dword ptr [{I32P + 4 * r.randrange(16)}]"])}')
        elif k == 4 and exact:                                     # exact: by a power of two (or zero)
            p.x(f'fdiv qword ptr [{p.divisor}]')
        elif k == 5:
            op = r.choice(['fchs', 'fabs', 'frndint', 'fxch st(%d)' % i, 'fsqrt' if not exact else 'frndint', 'fnop', 'wait', 'fxam'])
            p.x(op)
            if op == 'fxam':
                p.x('fnstsw ax'); p.emit(f'mov word ptr [{p.scratch(2)}], ax')
                # FXAM leaves C1 = the sign, which the pinned QEMU then keeps through FCOM/FTST (the SDM clears it there):
                # clear it through the environment, which both load as given.
                a = p.scratch(32) & ~3
                p.x(f'fnstenv [{a}]'); p.pointers(a); p.emit(f'and byte ptr [{a + 5}], 0xfd'); p.x(f'fldenv [{a}]')
            if op == 'fsqrt': p.x('ftst')   # C2 is undefined after FSQRT (the pinned QEMU sets it for a negative operand)
        elif k == 6:                                               # compares: x87 flags into AX
            op = r.choice(['fcom', 'fucom', 'fcomp', 'fucomp', 'ftst', 'fcompp', 'fucompp', 'ficom'])
            if op in ('fcompp', 'fucompp') and depth < 2: op = 'fcom'
            p.x({'ftst': 'ftst', 'fcompp': 'fcompp', 'fucompp': 'fucompp', 'ficom': f'ficom dword ptr [{I32P + 4 * r.randrange(16)}]'}.get(op, f'{op} st({i})'))
            depth -= {'fcomp': 1, 'fucomp': 1, 'fcompp': 2, 'fucompp': 2}.get(op, 0)
            p.x('fnstsw ax')
            p.emit('and ah, 0xfd')   # C1: the SDM clears it in compares (natemu does); the pinned QEMU leaves it (helper_fcom)
            p.emit(f'mov word ptr [{p.scratch(2)}], ax')
        elif k == 7:
            op = r.choice(['fcomi', 'fucomi', 'fcomip', 'fucomip'])
            p.x(f'{op} st(0), st({i})'); depth -= op.endswith('p')
            for cc in ('b', 'e', 'p'): p.emit(f'set{cc} byte ptr [{p.scratch(1)}]')
            if depth >= 2: p.x(f'{r.choice(["fcmovb", "fcmove", "fcmovbe", "fcmovu", "fcmovnb", "fcmovne", "fcmovnbe", "fcmovnu"])} st(0), st({r.randrange(1, depth)})')
        elif k == 8:                                               # stores
            form = r.randrange(4)
            if form == 0: p.x(f'fst qword ptr [{p.scratch(8)}]')
            elif form == 1: p.x(f'fstp qword ptr [{p.scratch(8)}]'); depth -= 1
            elif form == 2: p.x(f'fst dword ptr [{p.scratch(4)}]' if not exact else f'fst st({i})')
            else: p.x(f'fstp st({i})'); depth -= 1
        elif k == 9:                                               # integer stores, in range or not, under every RC
            w = r.choice(['word', 'dword', 'qword'])
            op = r.choice(['fist', 'fistp', 'fisttp'] if w != 'qword' else ['fistp', 'fisttp'])
            p.x(f'{op} {w} ptr [{p.scratch({"word": 2, "dword": 4, "qword": 8}[w])}]'); depth -= op != 'fist'
        elif k == 10:
            p.x('ffree st(7)' if depth < 8 else 'fnop')   # an already empty register (FEX models no stack fault)
        elif k == 11:
            p.x('fnstcw word ptr [%d]' % p.scratch(2))
        elif k == 12:                                              # environment round trip
            a = p.scratch(32) & ~3
            p.x(f'fnstenv [{a}]'); p.pointers(a)
            p.x(f'fldenv [{a}]')
        elif k == 13:                                              # 80-bit store and reload
            a = p.scratch(10)
            p.x(f'fstp tbyte ptr [{a}]'); p.x(f'fld tbyte ptr [{a}]')
        elif k == 14 and r.random() < 0.3:                         # FNSAVE/FRSTOR round trip (FNSAVE reinitialises)
            a = (p.scratch(112) & ~3)
            p.x(f'fnsave [{a}]'); p.pointers(a)
            p.x(f'frstor [{a}]')
        elif k == 15:
            p.x('fnclex')
        else:
            p.x(f'fst st({i})')
    return depth


def program(name, rng, kinds, n):
    for attempt in range(100):
        r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r)
        p = Prog(name, r); p.fcws = []
        p.divisor = F64P + 8 * 31
        try:
            res = RESULT
            for kind in kinds:
                depth = group(p, kind, r, n)
                # C0, C2 and C3 are undefined after most x87 operations (SDM: arithmetic, loads, FSQRT; the pinned QEMU sets C2
                # after FSQRT of a negative): FTST defines them, and C1 (= 0), before the status word is stored.
                if depth: p.x('ftst')
                else: p.x('fldz'); p.x('ftst'); p.x('fstp st(0)')
                p.x(f'fnstsw word ptr [{res}]'); p.x(f'fnstcw word ptr [{res + 2}]')
                p.x(f'fnstenv [{res + 4}]'); p.pointers(res + 4)
                for k in range(depth): p.x(f'fstp qword ptr [{res + 40 + 8 * k}]')
                res += 0x70
            out = g.finish(p, gpr, rf, r)
        except ValueError as e:
            last = e; continue
        data = bytearray.fromhex(out['memory'][0]['data'])
        dd, fl, i16, i32, i64, m80 = pools(r, kinds[-1])
        # The exact groups' divisor (by 2, the significand keeps its width; by 0, an infinity): the values then stay below 53
        # significant bits in both, so every result is exact.
        dd[31] = d2b(r.choice([2.0, -2.0, 0.0]))
        data[0:0x100] = b''.join(struct.pack('<Q', v) for v in dd)
        data[0x100:0x180] = b''.join(struct.pack('<I', v) for v in fl)
        data[0x180:0x1a0] = b''.join(struct.pack('<h', v) for v in i16)
        data[0x1a0:0x1e0] = b''.join(struct.pack('<i', v) for v in i32)
        data[0x1e0:0x260] = b''.join(struct.pack('<q', v) for v in i64)
        for k, (m, se) in enumerate(m80): data[0x260 + 16 * k:0x26a + 16 * k] = struct.pack('<QH', m, se)
        data[0x320:0x320 + 2 * len(p.fcws)] = b''.join(struct.pack('<H', v) for v in p.fcws)
        out['memory'][0]['data'] = data.hex()
        return out
    raise last


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261029); ap.add_argument('--random', type=int, default=60)
    ap.add_argument('--vp', type=pathlib.Path, help='also write the bodies for verify_precision: the resident forms (FNINIT, FLDCW, '
                    'FNCLEX, environment and save images, m80) and the block-ending jumps removed, so each program compiles as one block')
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = [program(f'i6-{"double" if n % 2 else "exact"}-{n:02d}', rng, ['double' if n % 2 else 'exact'] * 2, rng.randrange(12, 40))
             for n in range(a.random)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i6.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA), 'result': hex(RESULT)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')
    if a.vp:
        import ref
        resident = ('fninit', 'fldcw', 'fnstenv', 'fldenv', 'fnsave', 'frstor', 'fnclex')
        bodies = []
        for p in progs:
            lines = [l for l in p['asm'] if not l.endswith(':')]
            keep = [(l, n) for l, n in zip(lines, p['insns']) if not (l.split()[0] in resident or 'tbyte' in l or l.startswith('jmp'))]
            bodies.append(dict(p, asm=[l for l, _ in keep], insns=[n for _, n in keep], code=ref.assemble('\n'.join(l for l, _ in keep)).hex()))
        a.vp.write_text(json.dumps({'programs': bodies}) + '\n')
        print(f'{len(bodies)} bodies -> {a.vp}')

if __name__ == '__main__': main()
