#!/usr/bin/env python3
"""Seeded generator for the I8 corpus: AVX, AVX2, FMA and F16C (VEX-encoded) forms (boot-features plan, chunk I8).

    python3 tools/reference/gen_i8.py -o OUT.json [--seed N] [--random 150] [--k 2]

(The first corpus, natemu-raw/i8-corpus-20261030-01, came from the version before the review: seed 20261030, six random draws
per eight-template group, 60 random mixes without legacy SSE.)

Every admitted VEX form (FORMS) at VEX.128 and VEX.256 where it has both, register and memory operands, with random data
from float patterns (gen_i5a's F32/F64: signed zeros, infinities, quiet and signalling NaNs, denormals, extreme normals)
and random bits, under every rounding mode and FTZ/DAZ (LDMXCSR, every exception masked). Memory operands are aligned to
their size for VMOVAPS/VMOVAPD/VMOVDQA/VMOVNT*, else anywhere, including across the page boundary at DATA + 0x1000. The
result: YMM0..15 stored to RESULT (32 bytes each), MXCSR (STMXCSR, DE masked out: QEMU 7.2 never sets it), the general
registers, RFLAGS (VCOMISS/VPTEST/VTESTPS define flags) and the data region. Programs carry `sse: true` and `avx: true`
(the runner sets CR4.OSFXSR, CR4.OSXSAVE and XCR0 = 7) and the QEMU model `qemu64` plus the profile's features.
"""
import argparse, json, pathlib, random, struct, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, gen_slice1 as g
from gen_slice1 import DATA, NAMES, SCRATCH
from gen_i5a import F32, F64, I_ARITH

PATTERNS = DATA            # 0x200 bytes of float patterns the registers start from
MXCSR_SLOT = DATA + 0x200  # LDMXCSR source values
RESULT = DATA + 0x1c00     # YMM0..15 (32 bytes each), then MXCSR at RESULT + 0x200
CPU = 'qemu64,+ssse3,+sse4_1,+sse4_2,+popcnt,+xsave,+avx,+avx2,+fma,+f16c,+bmi1,+bmi2'
PTR = {256: 'ymmword', 128: 'xmmword', 64: 'qword', 32: 'dword', 16: 'word', 8: 'byte'}

FP2 = ['addps', 'subps', 'mulps', 'divps', 'minps', 'maxps', 'andps', 'andnps', 'orps', 'xorps', 'unpcklps', 'unpckhps',
       'addpd', 'subpd', 'mulpd', 'divpd', 'minpd', 'maxpd', 'andpd', 'andnpd', 'orpd', 'xorpd', 'unpcklpd', 'unpckhpd',
       'haddps', 'hsubps', 'haddpd', 'hsubpd', 'addsubps', 'addsubpd', 'permilps', 'permilpd']
FP1 = ['sqrtps', 'sqrtpd', 'rcpps', 'rsqrtps', 'cvtps2dq', 'cvttps2dq', 'cvtdq2ps', 'movsldup', 'movshdup']
SCALAR = [('addss', 32), ('subss', 32), ('mulss', 32), ('divss', 32), ('minss', 32), ('maxss', 32), ('sqrtss', 32),
          ('rcpss', 32), ('rsqrtss', 32), ('addsd', 64), ('subsd', 64), ('mulsd', 64), ('divsd', 64), ('minsd', 64),
          ('maxsd', 64), ('sqrtsd', 64), ('cvtss2sd', 32), ('cvtsd2ss', 64)]
INT2 = [m for m in I_ARITH if m not in ('phminposuw', 'pabsb', 'pabsw', 'pabsd') and not m.startswith(('psll', 'psrl', 'psra'))] + \
       ['psllvd', 'psllvq', 'psrlvd', 'psrlvq', 'psravd']
SHIFTS = ['psllw', 'pslld', 'psllq', 'psrlw', 'psrld', 'psrlq', 'psraw', 'psrad']
PMOV = [('pmovsxbw', 2), ('pmovsxbd', 4), ('pmovsxbq', 8), ('pmovsxwd', 2), ('pmovsxwq', 4), ('pmovsxdq', 2),
        ('pmovzxbw', 2), ('pmovzxbd', 4), ('pmovzxbq', 8), ('pmovzxwd', 2), ('pmovzxwq', 4), ('pmovzxdq', 2)]
FMA = [f'{k}{o}{t}' for k in ('vfmadd', 'vfmsub', 'vfnmadd', 'vfnmsub') for o in ('132', '213', '231') for t in ('ps', 'pd')] + \
      [f'{k}{o}{t}' for k in ('vfmaddsub', 'vfmsubadd') for o in ('132', '213', '231') for t in ('ps', 'pd')]
FMA_S = [(f'{k}{o}{t}', 32 if t == 'ss' else 64) for k in ('vfmadd', 'vfmsub', 'vfnmadd', 'vfnmsub') for o in ('132', '213', '231')
         for t in ('ss', 'sd')]

# Templates: {V} the form's vector register (xmm at VEX.128, ymm at VEX.256: each template is instantiated at both widths
# unless marked), {Vm} V or its memory operand (unaligned allowed), {Va} V or aligned memory, {X} xmm, {Y} ymm, {Xm64}
# xmm or m64, {Xh} xmm or a memory operand half the vector width (conversions), {Xq} a quarter, {Xe} an eighth;
# {ma} aligned memory of the vector width, {mu} unaligned; {r32}/{r64} writable general registers, {rm32}/{rm64}
# register or memory, {i8} imm8, {c32} a compare predicate 0..31, {cnt} a shift count.
BOTH = ([f'v{m} {{V}}, {{V}}, {{Vm}}' for m in FP2 + INT2] + [f'v{m} {{V}}, {{Vm}}' for m in FP1 + ['pabsb', 'pabsw', 'pabsd', 'ptest', 'testps', 'testpd']] +
        [f'v{m} {{V}}, {{V}}, {{X}}' for m in SHIFTS] + [f'v{m} {{V}}, {{V}}, {{cnt}}' for m in SHIFTS + ['pslldq', 'psrldq']] +
        [f'v{m} {{V}}, {{Xd{n}}}' for m, n in PMOV] + [f'{m} {{V}}, {{V}}, {{Vm}}' for m in FMA] +
        ['vcmpps {V}, {V}, {Vm}, {c32}', 'vcmppd {V}, {V}, {Vm}, {c32}', 'vshufps {V}, {V}, {Vm}, {i8}', 'vshufpd {V}, {V}, {Vm}, {i8}',
         'vpshufd {V}, {Vm}, {i8}', 'vpshuflw {V}, {Vm}, {i8}', 'vpshufhw {V}, {Vm}, {i8}', 'vpalignr {V}, {V}, {Vm}, {i8}',
         'vblendps {V}, {V}, {Vm}, {i8}', 'vblendpd {V}, {V}, {Vm}, {i8}', 'vpblendw {V}, {V}, {Vm}, {i8}', 'vpblendd {V}, {V}, {Vm}, {i8}',
         'vblendvps {V}, {V}, {Vm}, {V}', 'vblendvpd {V}, {V}, {Vm}, {V}', 'vpblendvb {V}, {V}, {Vm}, {V}', 'vdpps {V}, {V}, {Vm}, {i8}',
         'vmpsadbw {V}, {V}, {Vm}, {i8}', 'vroundps {V}, {Vm}, {r8}', 'vroundpd {V}, {Vm}, {r8}', 'vpermilps {V}, {Vm}, {i8}',
         'vpermilpd {V}, {Vm}, {i8}', 'vpbroadcastb {V}, {Xr8}', 'vpbroadcastw {V}, {Xr16}', 'vpbroadcastd {V}, {Xr32}',
         'vpbroadcastq {V}, {Xr64}', 'vbroadcastss {V}, {Xr32}', 'vcvtph2ps {V}, {Xh}', 'vcvtps2ph {X}, {V}, {r8}',
         'vcvtdq2pd {V}, {Xh}', 'vcvtps2pd {V}, {Xh}',
         'vmovaps {V}, {Va}', 'vmovapd {V}, {Va}', 'vmovdqa {V}, {Va}', 'vmovups {V}, {Vm}', 'vmovupd {V}, {Vm}', 'vmovdqu {V}, {Vm}',
         'vmovaps {ma}, {V}', 'vmovapd {ma}, {V}', 'vmovdqa {ma}, {V}', 'vmovups {mu}, {V}', 'vmovupd {mu}, {V}', 'vmovdqu {mu}, {V}',
         'vmovntps {ma}, {V}', 'vmovntpd {ma}, {V}', 'vmovntdq {ma}, {V}', 'vmovntdqa {V}, {ma}', 'vlddqu {V}, {mu}',
         'vpmovmskb {r32}, {V}', 'vmovmskps {r32}, {V}', 'vmovmskpd {r32}, {V}'])
# VEX.128 only (scalar, element and 128-bit forms) and VEX.256 only.
ONLY128 = ([f'v{m} {{X}}, {{X}}, {{Xm{w}}}' for m, w in SCALAR] + [f'{m} {{X}}, {{X}}, {{Xm{w}}}' for m, w in FMA_S] +
           ['vcmpss {X}, {X}, {Xm32}, {c32}', 'vcmpsd {X}, {X}, {Xm64}, {c32}', 'vroundss {X}, {X}, {Xm32}, {r8}', 'vroundsd {X}, {X}, {Xm64}, {r8}',
            'vcomiss {X}, {Xm32}', 'vucomiss {X}, {Xm32}', 'vcomisd {X}, {Xm64}', 'vucomisd {X}, {Xm64}', 'vmovddup {X}, {Xm64}',
            'vphminposuw {X}, {Xm}', 'vdppd {X}, {X}, {Xm}, {dp8}', 'vinsertps {X}, {X}, {Xm32}, {ins8}',
            'vmovss {X}, {X}, {X}', 'vmovsd {X}, {X}, {X}', 'vmovss {X}, {m32}', 'vmovsd {X}, {m64}', 'vmovss {m32}, {X}', 'vmovsd {m64}, {X}',
            'vmovlps {X}, {X}, {m64}', 'vmovhps {X}, {X}, {m64}', 'vmovlpd {X}, {X}, {m64}', 'vmovhpd {X}, {X}, {m64}',
            'vmovlps {m64}, {X}', 'vmovhps {m64}, {X}', 'vmovlpd {m64}, {X}', 'vmovhpd {m64}, {X}', 'vmovhlps {X}, {X}, {X}', 'vmovlhps {X}, {X}, {X}',
            'vmovd {X}, {rm32}', 'vmovq {X}, {rm64}', 'vmovd {rm32}, {X}', 'vmovq {rm64}, {X}', 'vmovq {X}, {Xm64}', 'vmovq {m64}, {X}',
            'vpinsrw {X}, {X}, {rm16i}, {i8}', 'vpextrw {r32}, {X}, {i8}', 'vpextrw {m16}, {X}, {i8}', 'vpinsrb {X}, {X}, {rm8i}, {i8}',
            'vpinsrd {X}, {X}, {rm32}, {i8}', 'vpinsrq {X}, {X}, {rm64}, {i8}', 'vpextrb {rm8o}, {X}, {i8}', 'vpextrd {rm32}, {X}, {i8}',
            'vpextrq {rm64}, {X}, {i8}', 'vextractps {rm32}, {X}, {i8}',
            'vcvtsi2ss {X}, {X}, {rm32}', 'vcvtsi2ss {X}, {X}, {rm64}', 'vcvtsi2sd {X}, {X}, {rm32}', 'vcvtsi2sd {X}, {X}, {rm64}',
            'vcvtss2si {r32}, {Xm32}', 'vcvtss2si {r64}, {Xm32}', 'vcvttss2si {r32}, {Xm32}', 'vcvttss2si {r64}, {Xm32}',
            'vcvtsd2si {r32}, {Xm64}', 'vcvtsd2si {r64}, {Xm64}', 'vcvttsd2si {r32}, {Xm64}', 'vcvttsd2si {r64}, {Xm64}',
            'vcvtpd2ps {X}, {Xm}', 'vcvtpd2dq {X}, {Xm}', 'vcvttpd2dq {X}, {Xm}'])
ONLY256 = ['vmovddup {Y}, {Ym}', 'vpermd {Y}, {Y}, {Ym}', 'vpermps {Y}, {Y}, {Ym}', 'vpermq {Y}, {Ym}, {i8}', 'vpermpd {Y}, {Ym}, {i8}',
           'vperm2i128 {Y}, {Y}, {Ym}, {i8}', 'vperm2f128 {Y}, {Y}, {Ym}, {i8}', 'vinserti128 {Y}, {Y}, {Xm}, {i8}',
           'vinsertf128 {Y}, {Y}, {Xm}, {i8}', 'vextracti128 {Xmw}, {Y}, {i8}', 'vextractf128 {Xmw}, {Y}, {i8}',
           'vbroadcastsd {Y}, {Xr64}', 'vbroadcastf128 {Y}, {m128}',
           'vcvtpd2ps {X}, {Ym}', 'vcvtpd2dq {X}, {Ym}', 'vcvttpd2dq {X}, {Ym}']
FORMS = [(f, 128) for f in BOTH + ONLY128] + [(f, 256) for f in BOTH + ONLY256]
ZERO = ['vzeroupper', 'vzeroall']
# Not in the QEMU corpus: VBROADCASTI128 (QEMU 7.2's disassembler prints it as `.byte`, which the annotation check cannot
# follow); the fault matrix and the FEX ASM tests (VEX/vbroadcasti128.asm) cover it.


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
            if aligned: a &= ~(n - 1)
            elif r.random() < 0.4: a &= ~(min(n, 16) - 1)
            if a < DATA + 0x300 or a + n > RESULT or (aligned and a & (n - 1)): continue
            if r.random() < 0.5:
                b = r.choice([3, 5]); d = a - self.gpr[b]
                return f'{PTR[w]} ptr [{g.R64[b]} + {d}]'
            return f'{PTR[w]} ptr [{a}]'
        raise ValueError('no operand')

    def fill(self, text, width):
        r = self.rng
        x = lambda: f'xmm{r.randrange(16)}'
        y = lambda: f'ymm{r.randrange(16)}'
        v = y if width == 256 else x
        reg = lambda w: NAMES[w][r.choice(SCRATCH)]
        either = lambda f, w, aligned: f() if r.random() < 0.5 else self.mem(w, aligned)
        sub = {
            'V': v, 'X': x, 'Y': y, 'Vm': lambda: either(v, width, False), 'Va': lambda: either(v, width, True),
            'Ym': lambda: either(y, 256, False), 'Xm': lambda: either(x, 128, False), 'Xmw': lambda: either(x, 128, False),
            'Xm64': lambda: either(x, 64, False), 'Xm32': lambda: either(x, 32, False),
            'Xh': lambda: either(x, width // 2, False), 'Xhw': lambda: either(x, width // 2, False),
            'Xd2': lambda: either(x, width // 2, False), 'Xd4': lambda: either(x, width // 4, False), 'Xd8': lambda: either(x, width // 8, False),
            'Xr8': lambda: either(x, 8, False), 'Xr16': lambda: either(x, 16, False), 'Xr32': lambda: either(x, 32, False), 'Xr64': lambda: either(x, 64, False),
            'ma': lambda: self.mem(width, True), 'mu': lambda: self.mem(width, False), 'm128': lambda: self.mem(128, False),
            'm64': lambda: self.mem(64, False), 'm32': lambda: self.mem(32, False), 'm16': lambda: self.mem(16, False),
            'r32': lambda: reg(32), 'r64': lambda: reg(64),
            'rm32': lambda: reg(32) if r.random() < 0.5 else self.mem(32, False),
            'rm64': lambda: reg(64) if r.random() < 0.5 else self.mem(64, False),
            'rm16i': lambda: reg(32) if r.random() < 0.5 else self.mem(16, False),
            'rm8i': lambda: reg(32) if r.random() < 0.5 else self.mem(8, False),
            'rm8o': lambda: reg(32) if r.random() < 0.5 else self.mem(8, False),
            'i8': lambda: str(r.randrange(256)), 'c32': lambda: str(r.randrange(32)), 'r8': lambda: str(r.randrange(16)),
            'dp8': lambda: str(r.randrange(256) & 0x33),
            # vinsertps: a zero mask clearing the inserted element is refused with a memory source (FEX drops the load)
            'ins8': lambda: str((lambda v: v & ~(1 << (v >> 4 & 3)))(r.randrange(256))),
            'cnt': lambda: str(r.choice([0, 1, 7, 8, 15, 16, 31, 32, 63, 64, r.randrange(256)])),
        }
        out = text
        while '{' in out:
            i = out.index('{'); j = out.index('}', i)
            out = out[:i] + sub[out[i + 1:j]]() + out[j + 1:]
        return out

    def mxcsr(self, rng):
        """LDMXCSR of a random rounding mode, FTZ, DAZ and initial exception flags (every exception masked); DAZ or FTZ set."""
        v = 0x1f80 | rng.randrange(4) << 13 | (rng.random() < 0.3) << 15 | (rng.random() < 0.3) << 6 | rng.getrandbits(6) * (rng.random() < 0.3)
        slot = MXCSR_SLOT + 4 * len(self.slots); self.slots.append(v)
        self.sse(f'ldmxcsr dword ptr [{slot}]')
        return v & 0x8040 != 0


# QEMU 7.2's departures from the SDM that I5a met (two-NaN rule, MIN/MAX under DAZ) apply to the VEX forms as well; the
# generator keeps MIN/MAX out of DAZ groups as gen_i5a does. Found by this corpus (prefix runs): QEMU 7.2 applies FTZ to
# VCVTPS2PH's results and DAZ to VCVTPH2PS's half-precision inputs, where the SDM says "MXCSR.FTZ is ignored" and "MXCSR.DAZ
# is ignored and is treated as if it 0" (natemu follows the SDM: FPCR.FZ16 stays clear): F16C stays out of FTZ/DAZ groups too.
MINMAX = ('vmin', 'vmax', 'vcvtph2ps', 'vcvtps2ph')


# Legacy SSE forms interleaved with the VEX ones (I8 review, the reviewer's gen_rev.py): a legacy form keeps YMM's upper half,
# a VEX.128 form zeroes it, and both share XMM state and MXCSR.
LEGACY = ['addps {X}, {Va}', 'mulsd {X}, {Xm64}', 'movaps {X}, {X}', 'movups {X}, {Xm}', 'movss {X}, {m32}', 'movss {X}, {X}',
          'movsd {X}, {m64}', 'movsd {X}, {X}', 'movq {X}, {X}', 'movq {X}, {m64}', 'movd {X}, {r32}', 'movq {X}, {r64}',
          'pinsrb {X}, {r32}, {i8}', 'pinsrq {X}, {r64}, {i8}', 'pshufb {X}, {Va}', 'cvtsi2ss {X}, {r64}', 'cvtss2sd {X}, {X}',
          'cvtsd2ss {X}, {Xm64}', 'sqrtsd {X}, {X}', 'roundss {X}, {X}, {r8}', 'movhlps {X}, {X}', 'movlhps {X}, {X}',
          'shufps {X}, {X}, {i8}', 'pxor {X}, {X}', 'blendvps {X}, {X}, xmm0', 'pblendvb {X}, {X}, xmm0', 'pmovzxbw {X}, {X}',
          'cvtps2pd {X}, {X}', 'unpcklps {X}, {Va}', 'palignr {X}, {Va}, {i8}', 'pmuludq {X}, {X}', 'psrldq {X}, {i8}',
          'movlps {X}, {m64}', 'movhps {X}, {m64}', 'movddup {X}, {X}', 'lddqu {X}, {Xm}', 'movshdup {X}, {X}', 'pextrd {r32}, {X}, {i8}',
          'cmpps {X}, {X}, {r8}', 'cmpss {X}, {X}, {r8}', 'dpps {X}, {X}, {i8}', 'insertps {X}, {X}, {i8}', 'pmaxsd {X}, {X}',
          'packusdw {X}, {X}', 'haddps {X}, {X}', 'haddpd {X}, {Va}', 'hsubps {X}, {X}', 'pcmpeqq {X}, {X}', 'pcmpgtq {X}, {X}',
          'movmskps {r32}, {X}', 'pmovmskb {r32}, {X}', 'movntdqa {X}, {Va}', 'phminposuw {X}, {X}']
TESTS = ['vptest {V}, {Vm}', 'vtestps {V}, {Vm}', 'vtestpd {V}, {Vm}', 'ptest {X}, {X}', 'vcomiss {X}, {Xm32}', 'vucomisd {X}, {Xm64}']
# Variable shifts with in-range counts (the counts a random register holds are almost always out of range: all-zero or
# all-sign results): ymm15 := a register shifted right so each count element is below its width, then the shift by it.
VSHIFTS = [('vpsllvd', 'vpsrld', 27), ('vpsrlvd', 'vpsrld', 27), ('vpsravd', 'vpsrld', 27), ('vpsllvq', 'vpsrlq', 58), ('vpsrlvq', 'vpsrlq', 58)]


def flagcap(p):
    """SF ZF AF PF CF (LAHF) and OF (SETO) folded into r12 (never a template register), so every test's flags are compared."""
    p.emit('lahf'); p.emit('seto al'); p.emit('rol r12, 9'); p.emit('xor r12, rax')


def special(p, r, kind):
    """The review's program kinds: `shifts` (in-range VPSLLV/VPSRLV/VPSRAV), `flags` (VPTEST/VTESTPS/VTESTPD/VCOMISS after
    each other's results), `addr` (RIP-relative, SIB and FS-based 32-byte operands)."""
    if kind == 'shifts':
        for _ in range(14):
            m, sh, n = r.choice(VSHIFTS)
            V = 'ymm' if r.random() < 0.6 else 'xmm'
            p.sse(f'{sh} ymm15, ymm{r.randrange(15)}, {n}')
            p.sse(f'{m} {V}{r.randrange(15)}, {V}{r.randrange(15)}, {V}15')
    elif kind == 'flags':
        for _ in range(10):
            if r.random() < 0.3: p.sse(f'vpand ymm{r.randrange(16)}, ymm{r.randrange(16)}, ymm{r.randrange(16)}')
            p.sse(p.fill(r.choice(TESTS), r.choice([128, 256]))); flagcap(p)
    else:
        for _ in range(10):
            a = DATA + 0x300 + r.randrange(0, 0x1800); a32 = a & ~31
            c = r.choice(['rip', 'sib', 'fs'])
            if c == 'rip': op, opa = f'[rip + {{{a}}}]', f'[rip + {{{a32}}}]'
            elif c == 'sib':
                base = p.gpr[3] + 4 * p.gpr[8]
                op, opa = f'[rbx + r8*4 + {a - base}]', f'[rbx + r8*4 + {a32 - base}]'
            else: op, opa = f'fs:[{a - 0x40}]', f'fs:[{a32 - 0x40}]'
            y, x = r.randrange(16), r.randrange(16)
            p.sse(r.choice([f'vmovdqu ymm{y}, ymmword ptr {op}', f'vpaddd ymm{y}, ymm{x}, ymmword ptr {op}', f'vmovaps ymm{y}, ymmword ptr {opa}',
                            f'vmovdqu ymmword ptr {op}, ymm{y}', f'vmovaps ymmword ptr {opa}, ymm{y}', f'vpbroadcastd ymm{y}, dword ptr {op}',
                            f'vbroadcastf128 ymm{y}, xmmword ptr {op}', f'vfmadd231ps ymm{y}, ymm{x}, ymmword ptr {op}',
                            f'vmovdqa xmmword ptr {opa}, xmm{y}']))


def program(name, rng, forms, groups=1, kind=None):
    for attempt in range(100):
        r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r)
        p = Prog(name, r, gpr); p.slots = []
        try:
            for i in range(16): p.sse(f'vmovdqu ymm{i}, ymmword ptr [{PATTERNS + 16 * r.randrange(31)}]')
            for _ in range(groups):
                daz = p.mxcsr(r)   # DAZ or FTZ
                if kind: special(p, r, kind); continue
                for f, w in forms(r):
                    if daz and (f.startswith(MINMAX) or f.startswith(('min', 'max'))): continue
                    p.sse(p.fill(f, w))
            for i in range(16): p.sse(f'vmovdqu ymmword ptr [{RESULT + 32 * i}], ymm{i}')
            p.sse(f'stmxcsr dword ptr [{RESULT + 0x200}]')
            # DE is not compared: QEMU 7.2 never sets it (I5a; the SDM's tables judge it there).
            p.emit(f'and dword ptr [{RESULT + 0x200}], -3')
            out = g.finish(p, gpr, rf, r)
        except ValueError as e:
            last = e; continue
        data = bytearray.fromhex(out['memory'][0]['data'])
        pats = b''.join(struct.pack('<I', r.choice(F32) if r.random() < 0.6 else r.getrandbits(32)) for _ in range(128)) \
            if r.random() < 0.5 else b''.join(struct.pack('<Q', r.choice(F64) if r.random() < 0.6 else r.getrandbits(64)) for _ in range(64))
        data[0:0x200] = pats
        for off in range(0x300, RESULT - DATA, 8):
            if r.random() < 0.5: data[off:off + 8] = struct.pack('<Q', r.choice(F64)) if r.random() < 0.5 else struct.pack('<II', r.choice(F32), r.choice(F32))
        data[0x200:0x200 + 4 * len(p.slots)] = b''.join(struct.pack('<I', v) for v in p.slots)
        out['memory'][0]['data'] = data.hex()
        return out | {'sse': True, 'avx': True, 'cpu': CPU} | ({'fsbase': hex(0x40)} if kind == 'addr' else {})
    raise last


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261031); ap.add_argument('--random', type=int, default=150)
    ap.add_argument('--k', type=int, default=2, help='instances of every template per MXCSR group (four groups)')
    a = ap.parse_args(); rng = random.Random(a.seed)
    # Every template (each width its own) k times under each of four MXCSR settings, eight templates per program; then random
    # mixes of VEX and legacy SSE forms with VZEROUPPER/VZEROALL among them; then the review's shifts, flags and addressing kinds.
    progs = []
    for i in range(0, len(FORMS), 8):
        chunk = FORMS[i:i + 8]
        progs.append(program(f'i8-forms-{i // 8:03d}', rng, lambda r, c=chunk: r.sample(c * a.k, len(c) * a.k), groups=4))
    pick = lambda r: (r.choice(ZERO), 128) if r.random() < 0.04 else (r.choice(LEGACY), 128) if r.random() < 0.4 else r.choice(FORMS)
    progs += [program(f'i8-random-{n:03d}', rng, lambda r: [pick(r) for _ in range(r.randrange(12, 32))], groups=2) for n in range(a.random)]
    for kind, n in (('shifts', 30), ('flags', 40), ('addr', 40)):
        progs += [program(f'i8-{kind}-{j:02d}', rng, None, groups=2, kind=kind) for j in range(n)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i8.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA), 'result': hex(RESULT)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs ({len(FORMS)} forms) -> {a.output}')

if __name__ == '__main__': main()
