#!/usr/bin/env python3
"""Seeded generator for the I5a corpus: SSE, SSE2, SSE3, SSSE3, SSE4.1, SSE4.2 forms (boot-features plan, chunk I5a).

    python3 tools/reference/gen_i5a.py -o OUT.json [--seed N] [--random 80]

Every admitted SSE form (FORMS: integer and floating-point vector forms, scalar float, conversions, moves, shuffles,
compares, PTEST, CRC32) in its register and memory forms, with random data drawn from float patterns (signed zeros,
infinities, quiet and signalling NaNs, denormals, the largest and smallest normals, ones) and random bits, under every
rounding mode and with FTZ/DAZ on and off (MXCSR from LDMXCSR at the start and between groups; every exception stays
masked). Memory operands are 16-byte aligned for the forms that require it, else anywhere, including across the page
boundary at DATA + 0x1000. The result: every XMM register stored to RESULT, MXCSR (its exception flags) by STMXCSR, the
general registers, RFLAGS (COMISS/PTEST define flags) and the data region. Inputs only: expected results come from ref.py
under QEMU. Programs carry `sse: true` (the runner sets CR4.OSFXSR).
"""
import argparse, json, pathlib, random, struct, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, gen_slice1 as g
from gen_slice1 import DATA, REGION, NAMES, SCRATCH

PATTERNS = DATA            # 0x200 bytes of float patterns the registers start from
MXCSR_SLOT = DATA + 0x200  # LDMXCSR source values
RESULT = DATA + 0x1e00     # XMM0..15, then MXCSR at RESULT + 0x100

F32 = [0x00000000, 0x80000000, 0x3f800000, 0xbf800000, 0x7f800000, 0xff800000, 0x7fc00000, 0xffc00000, 0x7fa00000,
       0xffa00001, 0x00000001, 0x807fffff, 0x00400000, 0x7f7fffff, 0xff7fffff, 0x00800000, 0x4f000000, 0xcf000000,
       0x5f000000, 0x3effffff, 0x3f000000, 0x40490fdb, 0x4b000001, 0x34000000]
F64 = [0, 1 << 63, 0x3ff0000000000000, 0xbff0000000000000, 0x7ff0000000000000, 0xfff0000000000000, 0x7ff8000000000000,
       0xfff8000000000000, 0x7ff4000000000000, 0xfff0000000000001, 1, 0x800fffffffffffff, 0x0008000000000000,
       0x7fefffffffffffff, 0x0010000000000000, 0x43e0000000000000, 0xc3e0000000000000, 0x41e0000000000000,
       0x3fe0000000000000, 0x400921fb54442d18, 0x4330000000000001]

# (template, kind): {x} an XMM register; {xm} XMM or m128 (aligned); {xu} XMM or m128 (unaligned allowed); {xm64}, {xm32},
# {xm16} XMM or an m64/m32/m16; {m128}/{m64}/{m32}/{m16}/{m8} memory only (aligned only for {m128}); {mu128} unaligned m128;
# {r32}/{r64} a writable general register; {rm32}/{rm64}/{rm8}/{rm16} register or memory; {i8} imm8; {cnt} a shift count.
F_ARITH = ['addps', 'subps', 'mulps', 'divps', 'minps', 'maxps', 'andps', 'andnps', 'orps', 'xorps', 'unpcklps', 'unpckhps',
           'addpd', 'subpd', 'mulpd', 'divpd', 'minpd', 'maxpd', 'andpd', 'andnpd', 'orpd', 'xorpd', 'unpcklpd', 'unpckhpd',
           'haddps', 'hsubps', 'haddpd', 'hsubpd', 'addsubps', 'addsubpd', 'sqrtps', 'sqrtpd', 'rcpps', 'rsqrtps',
           'cvtps2dq', 'cvttps2dq', 'cvtdq2ps', 'cvtpd2ps', 'cvtpd2dq', 'cvttpd2dq', 'movsldup', 'movshdup']
F_SCALAR = [('addss', 32), ('subss', 32), ('mulss', 32), ('divss', 32), ('minss', 32), ('maxss', 32), ('sqrtss', 32),
            ('rcpss', 32), ('rsqrtss', 32), ('addsd', 64), ('subsd', 64), ('mulsd', 64), ('divsd', 64), ('minsd', 64),
            ('maxsd', 64), ('sqrtsd', 64), ('cvtss2sd', 32), ('cvtsd2ss', 64), ('cvtps2pd', 64), ('cvtdq2pd', 64),
            ('movddup', 64)]
I_ARITH = ['paddb', 'paddw', 'paddd', 'paddq', 'psubb', 'psubw', 'psubd', 'psubq', 'paddsb', 'paddsw', 'paddusb', 'paddusw',
           'psubsb', 'psubsw', 'psubusb', 'psubusw', 'pmullw', 'pmulhw', 'pmulhuw', 'pmuludq', 'pmaddwd', 'pavgb', 'pavgw',
           'pminub', 'pmaxub', 'pminsw', 'pmaxsw', 'psadbw', 'pand', 'pandn', 'por', 'pxor', 'pcmpeqb', 'pcmpeqw', 'pcmpeqd',
           'pcmpgtb', 'pcmpgtw', 'pcmpgtd', 'packsswb', 'packssdw', 'packuswb', 'punpcklbw', 'punpcklwd', 'punpckldq',
           'punpcklqdq', 'punpckhbw', 'punpckhwd', 'punpckhdq', 'punpckhqdq', 'psllw', 'pslld', 'psllq', 'psrlw', 'psrld',
           'psrlq', 'psraw', 'psrad',
           # SSSE3
           'pshufb', 'phaddw', 'phaddd', 'phaddsw', 'phsubw', 'phsubd', 'phsubsw', 'pmaddubsw', 'pmulhrsw', 'psignb', 'psignw',
           'psignd', 'pabsb', 'pabsw', 'pabsd',
           # SSE4.1, SSE4.2
           'pmulld', 'pmuldq', 'pminsb', 'pminsd', 'pminuw', 'pminud', 'pmaxsb', 'pmaxsd', 'pmaxuw', 'pmaxud', 'pcmpeqq',
           'packusdw', 'phminposuw', 'pcmpgtq']
PMOV = [('pmovsxbw', 64), ('pmovsxbd', 32), ('pmovsxbq', 16), ('pmovsxwd', 64), ('pmovsxwq', 32), ('pmovsxdq', 64),
        ('pmovzxbw', 64), ('pmovzxbd', 32), ('pmovzxbq', 16), ('pmovzxwd', 64), ('pmovzxwq', 32), ('pmovzxdq', 64)]
FORMS = (
    [f'{m} {{x}}, {{xm}}' for m in F_ARITH + I_ARITH] +
    [f'{m} {{x}}, {{xm{w}}}' for m, w in F_SCALAR + PMOV] +
    ['cmpps {x}, {xm}, {c8}', 'cmppd {x}, {xm}, {c8}', 'cmpss {x}, {xm32}, {c8}', 'cmpsd {x}, {xm64}, {c8}',
     'shufps {x}, {xm}, {i8}', 'shufpd {x}, {xm}, {i8}', 'pshufd {x}, {xm}, {i8}', 'pshuflw {x}, {xm}, {i8}',
     'pshufhw {x}, {xm}, {i8}', 'palignr {x}, {xm}, {i8}', 'blendps {x}, {xm}, {i8}', 'blendpd {x}, {xm}, {i8}',
     'pblendw {x}, {xm}, {i8}', 'blendvps {x}, {xm}, xmm0', 'blendvpd {x}, {xm}, xmm0', 'pblendvb {x}, {xm}, xmm0',
     'dpps {x}, {xm}, {i8}', 'dppd {x}, {xm}, {dp8}', 'mpsadbw {x}, {xm}, {i8}', 'insertps {x}, {xm32}, {ins8}',
     'roundps {x}, {xm}, {r8}', 'roundpd {x}, {xm}, {r8}', 'roundss {x}, {xm32}, {r8}', 'roundsd {x}, {xm64}, {r8}',
     'psllw {x}, {cnt}', 'pslld {x}, {cnt}', 'psllq {x}, {cnt}', 'psrlw {x}, {cnt}', 'psrld {x}, {cnt}', 'psrlq {x}, {cnt}',
     'psraw {x}, {cnt}', 'psrad {x}, {cnt}', 'pslldq {x}, {cnt}', 'psrldq {x}, {cnt}',
     'comiss {x}, {xm32}', 'ucomiss {x}, {xm32}', 'comisd {x}, {xm64}', 'ucomisd {x}, {xm64}', 'ptest {x}, {xm}',
     # moves
     'movaps {x}, {xm}', 'movapd {x}, {xm}', 'movdqa {x}, {xm}', 'movups {x}, {xu}', 'movupd {x}, {xu}', 'movdqu {x}, {xu}',
     'lddqu {x}, {mu128}', 'movaps {m128}, {x}', 'movapd {m128}, {x}', 'movdqa {m128}, {x}', 'movups {mu128}, {x}',
     'movupd {mu128}, {x}', 'movdqu {mu128}, {x}', 'movntps {m128}, {x}', 'movntpd {m128}, {x}', 'movntdq {m128}, {x}',
     'movntdqa {x}, {m128}', 'movss {x}, {xm32}', 'movsd {x}, {xm64}', 'movss {m32}, {x}', 'movsd {m64}, {x}',
     'movlps {x}, {m64}', 'movhps {x}, {m64}', 'movlpd {x}, {m64}', 'movhpd {x}, {m64}', 'movlps {m64}, {x}',
     'movhps {m64}, {x}', 'movlpd {m64}, {x}', 'movhpd {m64}, {x}', 'movhlps {x}, {x}', 'movlhps {x}, {x}',
     'movd {x}, {rm32}', 'movq {x}, {rm64}', 'movd {rm32}, {x}', 'movq {rm64}, {x}', 'movq {x}, {xm64}', 'movq {m64}, {x}',
     'movmskps {r32}, {x}', 'movmskpd {r32}, {x}', 'pmovmskb {r32}, {x}',
     'pinsrw {x}, {rm16i}, {i8}', 'pextrw {r32}, {x}, {i8}', 'pextrw {m16}, {x}, {i8}', 'pinsrb {x}, {rm8i}, {i8}',
     'pinsrd {x}, {rm32}, {i8}', 'pinsrq {x}, {rm64}, {i8}', 'pextrb {rm8o}, {x}, {i8}', 'pextrd {rm32}, {x}, {i8}',
     'pextrq {rm64}, {x}, {i8}', 'extractps {rm32}, {x}, {i8}',
     # conversions with general registers
     'cvtsi2ss {x}, {rm32}', 'cvtsi2ss {x}, {rm64}', 'cvtsi2sd {x}, {rm32}', 'cvtsi2sd {x}, {rm64}',
     'cvtss2si {r32}, {xm32}', 'cvtss2si {r64}, {xm32}', 'cvttss2si {r32}, {xm32}', 'cvttss2si {r64}, {xm32}',
     'cvtsd2si {r32}, {xm64}', 'cvtsd2si {r64}, {xm64}', 'cvttsd2si {r32}, {xm64}', 'cvttsd2si {r64}, {xm64}',
     'crc32 {r32}, {rm8}', 'crc32 {r32}, {rm16}', 'crc32 {r32}, {rm32}', 'crc32 {r64}, {rm64}'])
PTR = {128: 'xmmword', 64: 'qword', 32: 'dword', 16: 'word', 8: 'byte'}


class Prog(g.Prog):
    def __init__(self, name, rng, gpr):
        super().__init__(name, rng)
        self.gpr = gpr

    def sse(self, text):
        m = text.split()[0]
        a = {'op': m, 'sse': True}
        self.undef = flags.step(a, self.undef); self.cl = None
        self.lines.append(text); self.insns.append(a)

    def mem(self, w, aligned):
        """An m`w` operand in the data region, below RESULT (absolute, or rbx/rbp-relative: rbx and rbp are never written)."""
        r, n = self.rng, w // 8
        for _ in range(1000):
            a = DATA + 0x300 + r.randrange(0, RESULT - DATA - 0x300 - n)
            if r.random() < 0.15: a = DATA + 0x1000 - r.randrange(1, n + 1)       # across the page boundary
            if aligned or r.random() < 0.4: a &= ~15 if aligned else ~(n - 1)
            if a < DATA + 0x300 or a + n > RESULT or (aligned and a & 15): continue
            if r.random() < 0.5:
                b = r.choice([3, 5]); d = a - self.gpr[b]
                return f'{PTR[w]} ptr [{g.R64[b]} + {d}]'
            return f'{PTR[w]} ptr [{a}]'
        raise ValueError('no operand')

    def fill(self, text):
        r = self.rng
        x = lambda: f'xmm{r.randrange(16)}'
        reg = lambda w: NAMES[w][r.choice(SCRATCH)]
        def either(w, aligned):
            return x() if r.random() < 0.5 else self.mem(w, aligned)
        sub = {
            'x': x, 'xm': lambda: either(128, True), 'xu': lambda: either(128, False), 'xm64': lambda: either(64, False),
            'xm32': lambda: either(32, False), 'xm16': lambda: either(16, False), 'm128': lambda: self.mem(128, True),
            'mu128': lambda: self.mem(128, False), 'm64': lambda: self.mem(64, False), 'm32': lambda: self.mem(32, False),
            'm16': lambda: self.mem(16, False), 'r32': lambda: reg(32), 'r64': lambda: reg(64),
            'rm32': lambda: reg(32) if r.random() < 0.5 else self.mem(32, False),
            'rm64': lambda: reg(64) if r.random() < 0.5 else self.mem(64, False),
            'rm16': lambda: reg(16) if r.random() < 0.5 else self.mem(16, False),
            'rm8': lambda: reg(8) if r.random() < 0.5 else self.mem(8, False),
            'rm16i': lambda: reg(32) if r.random() < 0.5 else self.mem(16, False),   # pinsrw r32/m16
            'rm8i': lambda: reg(32) if r.random() < 0.5 else self.mem(8, False),     # pinsrb r32/m8
            'rm8o': lambda: reg(32) if r.random() < 0.5 else self.mem(8, False),     # pextrb r32/m8
            'i8': lambda: str(r.randrange(256)), 'c8': lambda: str(r.randrange(8)), 'r8': lambda: str(r.randrange(16)),
            'dp8': lambda: str(r.randrange(256) & 0x33),
            # insertps: a zero mask clearing the inserted element is refused with a memory source (FEX drops the load)
            'ins8': lambda: str((lambda v: v & ~(1 << (v >> 4 & 3)))(r.randrange(256))), 'cnt': lambda: str(r.choice([0, 1, 7, 8, 15, 16, 31, 32, 63, 64, r.randrange(256)])),
        }
        out = text
        while '{' in out:
            i = out.index('{'); j = out.index('}', i)
            out = out[:i] + sub[out[i + 1:j]]() + out[j + 1:]
        return out

    def mxcsr(self, rng):
        """LDMXCSR of a random rounding mode, FTZ, DAZ and initial exception flags (every exception masked); the DAZ bit."""
        v = 0x1f80 | rng.randrange(4) << 13 | (rng.random() < 0.3) << 15 | (rng.random() < 0.3) << 6 | rng.getrandbits(6) * (rng.random() < 0.3)
        slot = MXCSR_SLOT + 4 * len(self.slots); self.slots.append(v)
        self.sse(f'ldmxcsr dword ptr [{slot}]')
        return v & 0x40 != 0


# The pinned QEMU (7.2) deviates from the SDM in two places the corpus meets (each failure is classified by
# tools/reference/i5a_differs.py, which re-runs the program's prefixes to find the diverging instruction):
#  - two NaN operands: QEMU keeps the x87 rule (the NaN with the larger significand) for SSE too; the SDM's SSE rule
#    (Vol.1 Table 4-7) returns the first source operand's (quieted). natemu (FPCR.AH = 1) follows the SDM.
#  - MIN/MAX under DAZ: QEMU returns the original denormal operand; the SDM converts denormal sources to a zero of the same
#    sign before the operation (Vol.1 10.2.3.4), which natemu (FPCR.FIZ) returns. The generator keeps MIN/MAX out of DAZ
#    groups; the hand cases are in i5a_differs.py.
MINMAX = ('min', 'max')


def program(name, rng, forms, groups=1):
    for attempt in range(100):
        r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r)
        p = Prog(name, r, gpr); p.slots = []
        try:
            for i in range(16): p.sse(f'movdqu xmm{i}, xmmword ptr [{PATTERNS + 16 * r.randrange(32)}]')
            for _ in range(groups):
                daz = p.mxcsr(r)
                for f in forms(r):
                    if daz and f.startswith(MINMAX): continue
                    p.sse(p.fill(f))
            for i in range(16): p.sse(f'movdqu xmmword ptr [{RESULT + 16 * i}], xmm{i}')
            p.sse(f'stmxcsr dword ptr [{RESULT + 0x100}]')
            # DE is not compared: QEMU 7.2 never sets it (tools/run_i5a_de.py judges it by the SDM).
            p.emit(f'and dword ptr [{RESULT + 0x100}], -3')
            out = g.finish(p, gpr, rf, r)
        except ValueError as e:
            last = e; continue
        data = bytearray.fromhex(out['memory'][0]['data'])
        pats = b''.join(struct.pack('<I', r.choice(F32) if r.random() < 0.6 else r.getrandbits(32)) for _ in range(64)) \
            if r.random() < 0.5 else b''.join(struct.pack('<Q', r.choice(F64) if r.random() < 0.6 else r.getrandbits(64)) for _ in range(32))
        data[0:0x200] = pats
        # The data region past the patterns: float patterns too, so memory operands meet them.
        for off in range(0x300, RESULT - DATA, 8):
            if r.random() < 0.5: data[off:off + 8] = struct.pack('<Q', r.choice(F64)) if r.random() < 0.5 else struct.pack('<II', r.choice(F32), r.choice(F32))
        data[0x200:0x200 + 4 * len(p.slots)] = b''.join(struct.pack('<I', v) for v in p.slots)
        out['memory'][0]['data'] = data.hex()
        return out | {'sse': True, 'cpu': 'qemu64,+ssse3,+sse4_1,+sse4_2'}     # qemu64 has SSE, SSE2 and SSE3 only
    raise last


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261029); ap.add_argument('--random', type=int, default=80)
    a = ap.parse_args(); rng = random.Random(a.seed)
    # Every form, eight at a time, under four MXCSR settings each; then random mixes of forms and slice-1 integer forms.
    progs = []
    for i in range(0, len(FORMS), 8):
        chunk = FORMS[i:i + 8]
        progs.append(program(f'i5a-forms-{i // 8:02d}', rng, lambda r, c=chunk: [r.choice(c) for _ in range(6)], groups=4))
    progs += [program(f'i5a-random-{n:02d}', rng, lambda r: [r.choice(FORMS) for _ in range(r.randrange(8, 24))], groups=2)
              for n in range(a.random)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i5a.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA), 'result': hex(RESULT)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs ({len(FORMS)} forms) -> {a.output}')

if __name__ == '__main__': main()
