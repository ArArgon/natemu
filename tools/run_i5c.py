#!/usr/bin/env python3
"""Unmasked floating-point exceptions (boot-features plan, chunk I5c), judged by the SDM rules below.

    tools/with-env python3 tools/run_i5c.py --fexc FEXC --out DIR [--max-block N] [--only SUBSTR] [--host H --runtime R]

SIMD (SDM Vol.1 11.5, Vol.3 events 19 and 6): for each SSE/AVX/FMA/F16C form and the flags it raises, the run with every
exception masked (no fault, MXCSR's flags), with each raised flag unmasked alone (#XM at the instruction, the destination
unchanged, MXCSR with the flags the precedence leaves: an unmasked pre-computation exception (IE, DE, ZE) sets only the
pre-computation flags, else every flag of every element), and with every flag it does not raise unmasked (no fault).
Plus: exact tiny results (UE unmasked: reported though exact; masked: no flag), FTZ ignored under an unmasked UE, DAZ (no DE
ever), DPPS's multiply and add steps, a YMM form, #UD without CR4.OSXMMEXCPT (the flags still set), LDMXCSR/FXRSTOR
re-keying (a flag already set raises nothing until an instruction raises it).
x87 (SDM Vol.1 8.5-8.7, Vol.3 event 16): an unmasked IE (stack fault, invalid operand), DE or ZE leaves TOP and the operands
unchanged and sets the flags (C1 for a stack fault); an unmasked OE/UE of FST/FSTP m32/m64 stores nothing, sets no PE and
clears C1; PE (and OE/UE to a register) stores the result; then ES and B are set and #MF (CR0.NE = 1) is delivered at the
next waiting x87 instruction, WAIT/FWAIT or MMX instruction (RIP there), not at FNSTSW/FNSTCW/FNCLEX/FNINIT/FNSTENV/FNSAVE
(FNCLEX/FNINIT clear it, FNSTENV masks every exception, FNSTSW reads ES/B), nor at FXSAVE/FXRSTOR; with CR0.NE = 0 the
stop `unsupported_ferr` (the board wires no FERR#/IGNNE#); the resident's waiting forms (FLDCW, FLDENV, FRSTOR, FLD/FSTP
m80) and helper forms (FYL2X...) follow the same rules.
QEMU 7.2 (Linux-user) raises no #XM at all and delivers #MF only at FWAIT, without leaving operands unaltered, so it cannot
judge these cases: the expectations are the SDM's.
"""
import argparse, json, pathlib, struct, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import run_slice1_vm as vm1, ref, fp_deviations as dev

D = 0x200000
CODE = 0x10000
AFTER = ['mov ecx, 1', 'hlt']
IE, DE, ZE, OE, UE, PE = 1, 2, 4, 8, 16, 32
PRE = IE | DE | ZE
MX = 0x1f80                                            # every SIMD exception masked
f32 = lambda x: struct.unpack('<I', struct.pack('<f', x))[0]
f64 = lambda x: struct.unpack('<Q', struct.pack('<d', x))[0]
ONE, TWO, ZERO, MAXF, INF, NINF, QN, SN, DEN = 0x3f800000, 0x40000000, 0, 0x7f7fffff, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001, 1
E30, E100 = 0x30800000, 0x0d800000                    # 2^-30, 2^-100
lanes = lambda *v: (v[0] | v[1] << 32, v[2] | v[3] << 32) if len(v) == 4 else (v[0] | (v[1] if len(v) > 1 else 0) << 32, 0)


def case(name, lines, setup=(), want=None, at=None, check=None, cpl=3, ne=True, mx=None):
    """`want`: None (reach the hlt), ('fault', vector) at line `at` (default the last of `lines`), ('stop', reason).
    `mx`: the structured expected MXCSR of a SIMD case (`mxc`): judged by `judge` through tools/fp_deviations.py, which tags a case
    with a documented deviation of fast mode on a host without AFP; `check` keeps everything that is not MXCSR."""
    return {'name': name, 'lines': list(lines) + AFTER, 'setup': list(setup) + (['ne 1'] if ne else []), 'want': want,
            'at': len(lines) - 1 if at is None else at, 'check': check, 'cpl': cpl, 'mx': mx}


def mxc(want, start=MX):
    """A SIMD case's expected MXCSR at the end (an int, or a tuple of acceptable values) and its starting one (the driver's reset value
    unless the setup says `mxcsr`)."""
    return dev.make_mx(start, want)


def xmm_of(vm, n):
    return (int(vm['xmm'][2 * n], 0), int(vm['xmm'][2 * n + 1], 0))


# (name, instruction, {xmm: (lo, hi)}, flags raised with every exception masked, flags raised with UE unmasked (exact tiny),
#  destination register)
FORMS = [
    ('addss-pe', 'addss xmm1, xmm2', {1: lanes(ONE), 2: lanes(E30)}, PE, None, 1),
    ('addss-de', 'addss xmm1, xmm2', {1: lanes(ONE), 2: lanes(DEN)}, DE | PE, None, 1),
    ('addss-snan', 'addss xmm1, xmm2', {1: lanes(ONE), 2: lanes(SN)}, IE, None, 1),
    ('subps-inf', 'subps xmm1, xmm2', {1: lanes(INF, INF, INF, INF), 2: lanes(INF, INF, INF, INF)}, IE, None, 1),
    ('mulss-oe', 'mulss xmm1, xmm2', {1: lanes(MAXF), 2: lanes(TWO)}, OE | PE, None, 1),
    ('mulss-ue', 'mulss xmm1, xmm2', {1: lanes(E100 | 1), 2: lanes(E30)}, UE | PE, None, 1),
    ('mulss-tiny-exact', 'mulss xmm1, xmm2', {1: lanes(E100), 2: lanes(E30)}, 0, UE, 1),
    ('divss-ze', 'divss xmm1, xmm2', {1: lanes(ONE), 2: lanes(ZERO)}, ZE, None, 1),
    ('divss-ie', 'divss xmm1, xmm2', {1: lanes(ZERO), 2: lanes(ZERO)}, IE, None, 1),
    ('divps-de-ze', 'divps xmm1, xmm2', {1: lanes(DEN, ONE, ONE, ONE), 2: lanes(ONE, ZERO, ONE, ONE)}, DE | ZE, UE, 1),
    ('sqrtss-ie', 'sqrtss xmm1, xmm2', {1: lanes(ONE), 2: lanes(0xbf800000)}, IE, None, 1),
    ('sqrtsd-pe', 'sqrtsd xmm1, xmm2', {1: (0, 0), 2: (f64(2.0), 0)}, PE, None, 1),
    ('maxss-qnan', 'maxss xmm1, xmm2', {1: lanes(QN), 2: lanes(ONE)}, IE, None, 1),
    ('minpd-de', 'minpd xmm1, xmm2', {1: (1, 1), 2: (f64(1.0), f64(1.0))}, DE, None, 1),
    ('cmpltps-qnan', 'cmpltps xmm1, xmm2', {1: lanes(QN, ONE, ONE, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, IE, None, 1),
    ('cmpeqps-snan', 'cmpeqps xmm1, xmm2', {1: lanes(SN, ONE, ONE, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, IE, None, 1),
    ('comiss-qnan', 'comiss xmm1, xmm2', {1: lanes(QN), 2: lanes(ONE)}, IE, None, None),
    ('ucomisd-snan', 'ucomisd xmm1, xmm2', {1: (0x7ff0000000000001, 0), 2: (f64(1.0), 0)}, IE, None, None),
    ('cvttss2si-ie', 'cvttss2si eax, xmm1', {1: lanes(f32(3e9))}, IE, None, None),
    ('cvtss2si-pe', 'cvtss2si eax, xmm1', {1: lanes(f32(1.5))}, PE, None, None),
    ('cvtsi2ss-pe', 'cvtsi2ss xmm1, ebx', {1: lanes(ONE)}, PE, None, 1),
    ('cvtsd2ss-oe', 'cvtsd2ss xmm1, xmm2', {1: lanes(ONE), 2: (f64(1e300), 0)}, OE | PE, None, 1),
    ('cvtsd2ss-tiny-exact', 'cvtsd2ss xmm1, xmm2', {1: lanes(ONE), 2: (f64(2.0 ** -140), 0)}, 0, UE, 1),
    ('cvtpd2ps-pe', 'cvtpd2ps xmm1, xmm2', {1: lanes(ONE), 2: (f64(0.1), f64(1.0))}, PE, None, 1),
    ('cvtps2pd-de', 'cvtps2pd xmm1, xmm2', {1: (0, 0), 2: lanes(DEN, ONE)}, DE, None, 1),
    ('cvtdq2ps-pe', 'cvtdq2ps xmm1, xmm2', {1: (0, 0), 2: lanes(0x7fffffff, 1, 2, 3)}, PE, None, 1),
    ('cvtps2dq-ie', 'cvtps2dq xmm1, xmm2', {1: (0, 0), 2: lanes(f32(3e9), ONE, ONE, ONE)}, IE, None, 1),
    ('haddps-pe', 'haddps xmm1, xmm2', {1: lanes(ONE, E30, ONE, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, PE, None, 1),
    ('addsubps-pe', 'addsubps xmm1, xmm2', {1: lanes(ONE, ONE, ONE, ONE), 2: lanes(ONE, E30, ONE, ONE)}, PE, None, 1),
    ('dpps-mul-oe', 'dpps xmm1, xmm2, 0xf1', {1: lanes(MAXF, MAXF, MAXF, MAXF), 2: lanes(TWO, TWO, TWO, TWO)}, OE | PE, None, 1),
    ('dpps-add-pe', 'dpps xmm1, xmm2, 0x31', {1: lanes(ONE, E30, 0, 0), 2: lanes(ONE, ONE, 0, 0)}, PE, None, 1),
    ('dpps-mul-ie', 'dpps xmm1, xmm2, 0x11', {1: lanes(INF, 0, 0, 0), 2: lanes(ZERO, 0, 0, 0)}, IE, None, 1),
    ('dppd-add-pe', 'dppd xmm1, xmm2, 0x31', {1: (f64(1.0), f64(2.0 ** -60)), 2: (f64(1.0), f64(1.0))}, PE, None, 1),
    ('roundss-pe', 'roundss xmm1, xmm2, 4', {1: lanes(ONE), 2: lanes(f32(1.5))}, PE, None, 1),
    ('roundss-quiet', 'roundss xmm1, xmm2, 12', {1: lanes(ONE), 2: lanes(f32(1.5))}, 0, None, 1),
    ('rcpps-none', 'rcpps xmm1, xmm2', {1: (0, 0), 2: lanes(ZERO, DEN, f32(3.0), ONE)}, 0, None, 1),
    ('vaddss-pe', 'vaddss xmm1, xmm3, xmm2', {1: lanes(ONE), 2: lanes(E30), 3: lanes(ONE)}, PE, None, 1),
    ('vfmadd213ss-pe', 'vfmadd213ss xmm1, xmm2, xmm3', {1: lanes(ONE), 2: lanes(ONE), 3: lanes(E30)}, PE, None, 1),
    ('vfmadd231ps-oe', 'vfmadd231ps xmm1, xmm2, xmm3', {1: lanes(ONE, ONE, ONE, ONE), 2: lanes(MAXF, ONE, ONE, ONE), 3: lanes(TWO, ONE, ONE, ONE)}, OE | PE, None, 1),
    ('vcvtps2ph-oe', 'vcvtps2ph xmm1, xmm2, 4', {1: lanes(ONE), 2: lanes(f32(1e6), ONE, ONE, ONE)}, OE | PE, None, 1),
    ('vcvtps2ph-tiny-exact', 'vcvtps2ph xmm1, xmm2, 4', {1: lanes(ONE), 2: lanes(f32(2.0 ** -20), ONE, ONE, ONE)}, 0, UE, 1),
    ('vcvtph2ps-none', 'vcvtph2ps xmm1, xmm2', {1: lanes(ONE), 2: lanes(0x3c003c00)}, 0, None, 1),
    ('vdivpd-ze', 'vdivpd xmm1, xmm2, xmm3', {1: (0, 0), 2: (f64(1.0), f64(1.0)), 3: (0, f64(1.0))}, ZE, None, 1),
    # Several elements (SDM Vol.1 11.5.3.2, Figure 11-9): every element's flags are ORed; an unmasked pre-computation one in
    # any element leaves only the pre-computation flags.
    ('addps-ie-oe', 'addps xmm1, xmm2', {1: lanes(INF, MAXF, ONE, ONE), 2: lanes(NINF, MAXF, ZERO, ZERO)}, IE | OE | PE, None, 1),
    # AFP fallback chunk 3b (rows 7 and 8): the DE precedence (none beside a NaN operand, ZE or IE), and lanes an instruction does not examine.
    # Each runs masked, with every other exception unmasked (DE among them: no fault) and, where it raises one, with it unmasked.
    # (The cases that do raise DE are in `simd_cases`: with DE unmasked alone, as the masked flag is decision B of the AFP fallback.)
    ('addss-qnan-de', 'addss xmm1, xmm2', {1: lanes(QN), 2: lanes(DEN)}, 0, None, 1),
    ('addss-snan-de', 'addss xmm1, xmm2', {1: lanes(SN), 2: lanes(DEN)}, IE, None, 1),
    ('divss-de-by-zero', 'divss xmm1, xmm2', {1: lanes(DEN), 2: lanes(ZERO)}, ZE, None, 1),
    ('sqrtss-negative-denormal', 'sqrtss xmm1, xmm2', {1: lanes(ONE), 2: lanes(0x80000001)}, IE, None, 1),
    ('addss-upper-lanes-denormal', 'addss xmm1, xmm2', {1: lanes(ONE, DEN, DEN, DEN), 2: lanes(ONE, DEN, DEN, DEN)}, 0, None, 1),
    ('cvtps2pd-upper-lanes-denormal', 'cvtps2pd xmm1, xmm2', {1: (0, 0), 2: lanes(ONE, ONE, DEN, DEN)}, 0, None, 1),
    ('addps-de-oe', 'addps xmm1, xmm2', {1: lanes(DEN, MAXF, ONE, ONE), 2: lanes(ONE, MAXF, ZERO, ZERO)}, DE | OE | PE, None, 1),
    ('mulps-figure-11-9', 'mulps xmm1, xmm2', {1: lanes(DEN, ONE, MAXF, f32(1.5)), 2: lanes(ONE, ONE, TWO, DEN)}, DE | OE | UE | PE, None, 1),
    ('addpd-ze-free-pe', 'addpd xmm1, xmm2', {1: (f64(1.0), 0x7ff0000000000001), 2: (f64(2.0 ** -60), f64(1.0))}, IE | PE, None, 1),
]


def simd_cases():
    out = []
    regs = lambda x: [f'xmm {n} {lo:#x} {hi:#x}' for n, (lo, hi) in x.items()]
    def one(name, insn, x, mxcsr, flags, fault, dest, prelude=(), extra=(), ymm=False):
        def check(vm, x=x, dest=dest, fault=fault):
            p = []
            if fault and dest is not None and xmm_of(vm, dest) != x[dest]: p.append(f'xmm{dest} written: {xmm_of(vm, dest)}')
            if fault and int(vm['rflags'], 0) & 0x8d5: p.append(f"rflags {vm['rflags']}: the instruction's flags written")
            if fault and ymm and [int(v, 0) for v in vm['ymmh'][2 * dest:2 * dest + 2]] != list(lanes(ONE, ONE, ONE, ONE)): p.append(f"ymm upper {vm['ymmh'][2 * dest:2 * dest + 2]}")
            return p
        vex = insn.startswith('v') or any(l.startswith('v') for l in prelude)
        return case(name, list(prelude) + [insn], ['sse 1', f'mxcsr {mxcsr:#x}', f'gpr-rbx 0x7fffffff'] + (['xcr0 7'] if vex and 'xcr0 7' not in extra else []) + regs(x) + list(extra),
                    ('fault', fault) if fault else None, check=check, mx=mxc(mxcsr | flags, mxcsr))
    for name, insn, x, raised, tiny, dest in FORMS:
        out.append(one(f'{name}-masked', insn, x, MX, raised, None, dest))
        triggers = [f for f in (IE, DE, ZE, OE, UE, PE) if raised & f] + ([UE] if tiny else [])
        for f in triggers:
            got = raised | (tiny or 0) if f == UE else raised
            flags = got & PRE if f & PRE else got
            out.append(one(f'{name}-unmasked-{f:02x}', insn, x, MX & ~(f << 7), flags, 19, dest))
        quiet = 0x3f & ~(raised | (tiny or 0))
        if quiet:
            out.append(one(f'{name}-others-unmasked', insn, x, MX & ~(quiet << 7), raised, None, dest))
    # FTZ is ignored under an unmasked UE (SDM Vol.1 10.2.3.3); masked, the tiny result flushes with UE and PE.
    tiny = {1: lanes(E100 | 1), 2: lanes(E30)}
    out.append(one('ftz-ue-unmasked', 'mulss xmm1, xmm2', tiny, (MX | 0x8000) & ~(UE << 7), UE | PE, 19, 1))
    out.append(one('ftz-ue-masked', 'mulss xmm1, xmm2', tiny, MX | 0x8000, UE | PE, None, 1))
    out.append(one('ftz-exact-tiny-ue-unmasked', 'mulss xmm1, xmm2', {1: lanes(E100), 2: lanes(E30)}, (MX | 0x8000) & ~(UE << 7), UE, 19, 1))
    # AFP accurate chunk 3 (FTZ alone, DAZ clear): 4095 * 2^-75 times 4097 * 2^-75 is 2^-126 - 2^-150: x86 rounds it to a value below MIN_NORMAL
    # (tiny, unchanged) and FTZ flushes it to +0 with UE and PE; Arm's denormal grid rounds it up to MIN_NORMAL. Nearest and round up.
    for rc in (0, 2):
        c1 = one(f'ftz-band-flush-rc{rc}', 'mulss xmm1, xmm2', {1: lanes(0x1ffff000, ONE, ONE, ONE), 2: lanes(0x20000800, ONE, ONE, ONE)}, MX | 0x8000 | rc << 13, UE | PE, None, 1)
        # A registered value deviation (fp_deviations `ftz-band`): fast mode without AFP leaves +MIN_NORMAL in lane 0, every other setting +0.
        c1['value'] = {'tag': 'ftz-band', 'read': lambda vm: xmm_of(vm, 1), 'exact': (ONE << 32, ONE | ONE << 32), 'deviated': (0x00800000 | ONE << 32, ONE | ONE << 32)}
        out.append(c1)
    # DAZ: a denormal source is zero, no DE whatever DM (10.2.3.4).
    out.append(one('daz-de-unmasked', 'addss xmm1, xmm2', {1: lanes(ONE), 2: lanes(DEN)}, (MX | 0x40) & ~(DE << 7), 0, None, 1))
    out.append(one('daz-ze-unmasked', 'divss xmm1, xmm2', {1: lanes(ONE), 2: lanes(DEN)}, (MX | 0x40) & ~(ZE << 7), ZE, 19, 1))
    # The in-block skip of the DE screen (AFP accurate plan, accurate mode without AFP): an operand that is the result of an earlier FP op of the
    # block is not screened, but with DE unmasked every operand is tested as ever: the exact difference of two doubles is a denormal (a tiny
    # result: the resident's trap handler writes it), and the next instruction that computes on it faults (#XM, DE alone), after a
    # producer in between too.
    min64 = 0x0010000000000000
    tiny = {1: (min64 + 1, 0), 2: (min64, 0)}
    out.append(one('de-unmasked-inblock-tiny-consumer', 'addsd xmm1, xmm1', tiny, MX & ~(DE << 7), DE, 19, None, prelude=['subsd xmm1, xmm2']))
    out.append(one('de-unmasked-inblock-tiny-producer-between', 'mulsd xmm1, xmm1', tiny, MX & ~(DE << 7), DE, 19, None,
                   prelude=['subsd xmm1, xmm2', 'addsd xmm5, xmm5']))
    # A YMM form: both halves checked before either is written.
    out.append(one('vaddps-ymm-pe', 'vaddps ymm1, ymm1, ymm2', {1: lanes(ONE, ONE, ONE, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, MX & ~(PE << 7), PE, 19, 1,
                   prelude=['vbroadcastss ymm2, dword ptr [rdi + 0x80]', 'vbroadcastss ymm1, dword ptr [rdi + 0x84]'],
                   extra=[f'memory {D + 0x280:#x} {struct.pack("<II", E30, ONE).hex()}', 'xcr0 7'], ymm=True))
    # The flags raised before the unmasked instruction stay; a flag set and then unmasked raises nothing by itself (Vol.3 19).
    ld = lambda v: [f'memory {D + 0x280:#x} {struct.pack("<I", v).hex()}']
    out.append(case('ldmxcsr-rekey', ['divss xmm1, xmm2', 'ldmxcsr dword ptr [rdi + 0x80]', 'addss xmm3, xmm3', 'movaps xmm1, xmm4', 'divss xmm1, xmm2'],
                    ['sse 1', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0', f'xmm 3 {ONE:#x} 0', f'xmm 4 {ONE:#x} 0'] + ld(0x1d84), ('fault', 19),
                    mx=mxc(0x1d84)))
    out.append(case('ldmxcsr-flag-set-no-fault', ['ldmxcsr dword ptr [rdi + 0x80]', 'addss xmm3, xmm3'],
                    ['sse 1', f'xmm 3 {ONE:#x} 0'] + ld(0x1d84), None, mx=mxc(0x1d84)))
    out.append(case('ldmxcsr-remask', ['ldmxcsr dword ptr [rdi + 0x80]', 'divss xmm1, xmm2', 'ldmxcsr dword ptr [rdi + 0x84]', 'movaps xmm1, xmm4', 'divss xmm1, xmm2'],
                    ['sse 1', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0', f'xmm 4 {ONE:#x} 0', f'memory {D + 0x280:#x} {struct.pack("<II", 0x1f80, 0x1d80).hex()}'], ('fault', 19),
                    mx=mxc(0x1d84)))
    # AFP fallback plan section 5 (mixed modes): LDMXCSR switches DAZ, and FTZ, on and off within one program; each switch re-keys
    # the code, so the same multiply flushes or not as the current mode says (host without AFP: software flushes or FZ; with
    # AFP: the hardware's). Results are saved in xmm6, xmm7 and xmm3.
    def mixed(name, modes, x, finals, mxcsr):
        lines = []
        for i, dest in enumerate((6, 7, 3)):
            lines += [f'ldmxcsr dword ptr [rdi + {0x80 + 4 * i}]', 'movaps xmm1, xmm5', 'mulss xmm1, xmm2', f'movaps xmm{dest}, xmm1']
        def check(vm):
            p = []
            for dest, flushed in zip((6, 7, 3), finals):
                lo = xmm_of(vm, dest)[0] & 0xffffffff
                if (lo == 0) != flushed: p.append(f'xmm{dest} {lo:#x}: {"not " if flushed else ""}flushed, want the opposite')
            return p
        out.append(case(name, lines, ['sse 1', f'xmm 5 {x[0]:#x} 0', f'xmm 2 {x[1]:#x} 0', f'memory {D + 0x280:#x} {struct.pack("<III", *modes).hex()}'], None, check=check, mx=mxc(mxcsr)))
    mixed('mixed-daz-on-off-on', (MX | 0x40, MX, MX | 0x40), (DEN, TWO), (True, False, True), MX | 0x40)        # den * 2: zero under DAZ, else 2 * den
    mixed('mixed-ftz-on-off-with-daz', (MX | 0x8000, MX, MX | 0x8000 | 0x40), (E100 | 1, E30), (True, False, True), 0x8000 | MX | 0x40 | UE | PE)   # the last flags are from the last mode only
    img = bytearray(512); img[0:2] = (0x37f).to_bytes(2, 'little'); img[24:28] = (0x1d80).to_bytes(4, 'little'); img[28:32] = (0xffff).to_bytes(4, 'little')
    out.append(case('fxrstor-unmasked-mxcsr', ['fxrstor [rsi]', f'mov eax, {ONE}', 'movd xmm1, eax', 'xorps xmm2, xmm2', 'divss xmm1, xmm2'],
                    ['sse 1', f'memory {D + 0x1000:#x} {img.hex()}'], ('fault', 19), mx=mxc(0x1d84)))
    out.append(case('vldmxcsr-unmasked', ['vldmxcsr dword ptr [rdi + 0x80]', 'vdivss xmm1, xmm1, xmm2'],
                    ['sse 1', 'xcr0 7', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0'] + ld(0x1d80), ('fault', 19), mx=mxc(0x1d84)))
    # Without CR4.OSXMMEXCPT: #UD, the flags set (11.5.3.2 step 2). At CPL 0, OSXMMEXCPT cleared first.
    cr4 = ['mov rax, cr4', 'btr rax, 10', 'mov cr4, rax']
    out.append(case('no-osxmmexcpt-ud', cr4 + ['divss xmm1, xmm2'], ['sse 1', f'mxcsr {MX & ~(ZE << 7):#x}', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0'],
                    ('fault', 6), cpl=0, check=lambda vm: [] if xmm_of(vm, 1) == (ONE, 0) else ['xmm1 written'],
                    mx=mxc((MX & ~(ZE << 7)) | ZE, MX & ~(ZE << 7))))
    out.append(case('no-osxmmexcpt-masked-runs', cr4 + ['divss xmm1, xmm2'], ['sse 1', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0'], None, cpl=0,
                    mx=mxc(MX | ZE)))
    # FPSR does not leak from an instruction that raises nothing (RCPPS's exact division of 1/0) into the next one's check.
    out.append(one('rcpps-then-checked', 'addss xmm1, xmm2', {1: lanes(ONE), 2: lanes(ONE), 4: (0, 0)}, MX & ~0x1f80, 0, None, 1,
                   prelude=['rcpps xmm3, xmm4']))
    # VCVTPS2PH from YMM: the upper four results (FEX's VFCVTN2) are tested for an exact tiny result too.
    up = struct.pack('<IIII', ONE, ONE, ONE, f32(2.0 ** -20)).hex()
    for tag, mx, want, fl in (('masked', MX, None, 0), ('ue', MX & ~(UE << 7), 19, UE)):
        out.append(one(f'vcvtps2ph-ymm-upper-tiny-{tag}', 'vcvtps2ph xmm1, ymm2, 4', {1: lanes(ONE), 2: lanes(ONE, ONE, ONE, ONE)}, mx, fl, want, 1,
                       prelude=['vinsertf128 ymm2, ymm2, xmmword ptr [rdi + 0x80], 1'], extra=[f'memory {D + 0x280:#x} {up}', 'xcr0 7']))
    # VDPPS ymm: the SDM orders the steps of one dot product, not the two halves (unspecified): the low multiply's masked OE/PE
    # are set or not when the high multiply's unmasked IE is delivered, either order accepted (natemu's is the low half first).
    hi = struct.pack('<IIIIIIII', INF, 0, 0, 0, ZERO, 0, 0, 0).hex()
    c = one('vdpps-ymm-steps', 'vdpps ymm1, ymm1, ymm2, 0x11', {1: lanes(MAXF, 0, 0, 0), 2: lanes(TWO, 0, 0, 0)}, MX & ~(IE << 7), IE, 19, 1,
            prelude=['vinsertf128 ymm1, ymm1, xmmword ptr [rdi + 0x80], 1', 'vinsertf128 ymm2, ymm2, xmmword ptr [rdi + 0x90], 1'],
            extra=[f'memory {D + 0x280:#x} {hi}', 'xcr0 7'])
    c['mx'] = mxc(((MX & ~(IE << 7)) | IE, (MX & ~(IE << 7)) | IE | OE | PE), MX & ~(IE << 7))
    out.append(c)
    # XRSTOR re-keys as FXRSTOR does (the SSE component loads MXCSR).
    x = bytearray(832); x[24:28] = (0x1d80).to_bytes(4, 'little'); x[28:32] = (0xffff).to_bytes(4, 'little'); x[512:520] = (2).to_bytes(8, 'little')
    out.append(case('xrstor-unmasked-mxcsr', ['mov eax, 2', 'xor edx, edx', 'xrstor [rsi]', f'mov eax, {ONE}', 'movd xmm1, eax', 'xorps xmm2, xmm2', 'divss xmm1, xmm2'],
                    ['sse 1', 'xcr0 7', f'memory {D + 0x1000:#x} {x.hex()}'], ('fault', 19), mx=mxc(0x1d84)))
    # Review cases (independent I5c review): VCVTPS2PH's upper lanes one by one; FMA inf * 0 + QNaN (no IE: SDM Vol.1 Table
    # 14-17); VCVTPH2PS of a half denormal (no DE); VCVTPS2PH of a single denormal (DE, UE, PE); CVT(T)PS2PI mm, xmm read only
    # xmm's low two floats (SDM CVTPS2PI: the upper two raise nothing, masked or not).
    T20 = f32(2.0 ** -20)
    for tag, up, mx, fl, want in (('elem4', (T20, ONE, ONE, ONE), MX & ~(UE << 7), UE, 19), ('zero-elem5', (ONE, 0, ONE, ONE), MX & ~(UE << 7), 0, None),
                                  ('elem7', (ONE, ONE, ONE, T20), MX & ~(UE << 7), UE, 19)):
        out.append(one(f'rv-ph-upper-{tag}', 'vcvtps2ph xmm1, ymm2, 4', {1: lanes(ONE), 2: lanes(ONE, ONE, ONE, ONE)}, mx, fl, want, 1,
                       prelude=['vinsertf128 ymm2, ymm2, xmmword ptr [rdi + 0x80], 1'], extra=[f'memory {D + 0x280:#x} {struct.pack("<IIII", *up).hex()}', 'xcr0 7']))
    out.append(one('rv-fma-inf0-qnan-ie', 'vfmadd231ss xmm1, xmm2, xmm3', {1: lanes(QN), 2: lanes(INF), 3: lanes(0)}, MX & ~(IE << 7), 0, None, 1))
    # AFP fallback chunk 3b (row 7): DE unmasked alone: a denormal in one HADD pair beside a QNaN pair, in the upper lane of a vector, as an
    # FMA's addend; none for the QNaN and denormal operands of one FMA.
    NODE = MX & ~(DE << 7)
    QN64, INF64 = 0x7ff8000000000000, 0x7ff0000000000000
    out.append(one('rv-haddps-de-pair', 'haddps xmm1, xmm2', {1: lanes(ONE, ONE, DEN, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, NODE, DE, 19, 1))
    out.append(one('rv-haddps-qnan-pair-beside-de-pair', 'haddps xmm1, xmm2', {1: lanes(QN, ONE, DEN, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, NODE, DE, 19, 1))
    out.append(one('rv-haddps-qnan-and-denormal-in-a-pair', 'haddps xmm1, xmm2', {1: lanes(QN, DEN, ONE, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, NODE, 0, None, 1))
    out.append(one('rv-vaddps-de-high-lane', 'vaddps xmm1, xmm3, xmm2', {1: lanes(ONE), 2: lanes(ONE, ONE, ONE, DEN), 3: lanes(ONE, ONE, ONE, ONE)}, NODE, DE, 19, 1))
    out.append(one('rv-vfmadd231ss-de-addend', 'vfmadd231ss xmm1, xmm2, xmm3', {1: lanes(DEN), 2: lanes(ONE), 3: lanes(ONE)}, NODE, DE, 19, 1))
    out.append(one('rv-vfmadd231ss-qnan-and-denormal', 'vfmadd231ss xmm1, xmm2, xmm3', {1: lanes(DEN), 2: lanes(QN), 3: lanes(ONE)}, NODE, 0, None, 1))
    out.append(one('rv-vfmsub231ps-inf-minus-inf-and-denormal-ie-not-de', 'vfmsub231ps xmm1, xmm2, xmm3',
                   {1: lanes(INF, ONE, ONE, ONE), 2: lanes(INF, ONE, ONE, ONE), 3: lanes(DEN, ONE, ONE, ONE)}, MX & ~(DE << 7), IE, None, 1))   # IE (masked) beats DE
    out.append(one('rv-sqrtps-de-lane2', 'sqrtps xmm1, xmm2', {1: (0, 0), 2: lanes(ONE, ONE, DEN, ONE)}, NODE, DE, 19, 1))
    out.append(one('rv-cvtpd2ps-de-high-lane', 'cvtpd2ps xmm1, xmm2', {1: (0, 0), 2: (f64(1.0), 1)}, NODE, DE, 19, 1))
    out.append(one('rv-cmpps-de-lane1', 'cmpltps xmm1, xmm2', {1: lanes(ONE, DEN, ONE, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, NODE, DE, 19, 1))
    out.append(one('rv-minpd-qnan-and-denormal-ie-not-de', 'minpd xmm1, xmm2', {1: (QN64, 1), 2: (1, QN64)}, NODE, IE, None, 1))   # a QNaN is IE (masked) in MIN/MAX: no DE
    # AFP fallback chunk 3b (row 10): with IE unmasked, inf * 0 + QNaN raises nothing (vector, negated forms and double: a lane each), the
    # real invalid cases still do: inf * 0 + a number, an SNaN multiplicand beside a QNaN addend, inf - inf (flags IE).
    NOIE = MX & ~(IE << 7)
    out.append(one('rv-fma-inf0-qnan-ie-ps', 'vfmadd231ps xmm1, xmm2, xmm3', {1: lanes(QN, ONE, QN, ONE), 2: lanes(INF, ONE, ZERO, ONE), 3: lanes(ZERO, ONE, INF, ONE)}, NOIE, 0, None, 1))
    out.append(one('rv-fma-inf0-qnan-ie-pd-nmsub', 'vfnmsub231pd xmm1, xmm2, xmm3', {1: (QN64, QN64), 2: (INF64, 0), 3: (0, INF64)}, NOIE, 0, None, 1))
    out.append(one('rv-fma-inf0-qnan-ie-sd-nmadd', 'vfnmadd231sd xmm1, xmm2, xmm3', {1: (QN64, 0), 2: (0, 0), 3: (INF64, 0)}, NOIE, 0, None, 1))
    out.append(one('rv-fma-inf0-number-ie', 'vfmadd231ss xmm1, xmm2, xmm3', {1: lanes(ONE), 2: lanes(INF), 3: lanes(0)}, NOIE, IE, 19, 1))
    out.append(one('rv-fma-inf0-number-ie-ps-lane3', 'vfmadd231ps xmm1, xmm2, xmm3', {1: lanes(QN, QN, QN, ONE), 2: lanes(INF, INF, INF, INF), 3: lanes(0, 0, 0, 0)}, NOIE, IE, 19, 1))
    out.append(one('rv-fma-snan-multiplicand-qnan-addend-ie', 'vfmadd231ss xmm1, xmm2, xmm3', {1: lanes(QN), 2: lanes(SN), 3: lanes(ONE)}, NOIE, IE, 19, 1))
    out.append(one('rv-fma-inf-minus-inf-ie', 'vfmsub231ss xmm1, xmm2, xmm3', {1: lanes(INF), 2: lanes(INF), 3: lanes(ONE)}, NOIE, IE, 19, 1))
    out.append(one('rv-fma-qnan-addend-normal-multiplicands-ie', 'vfmadd231ps xmm1, xmm2, xmm3', {1: lanes(QN, QN, QN, QN), 2: lanes(TWO, TWO, TWO, TWO), 3: lanes(TWO, TWO, TWO, TWO)}, NOIE, 0, None, 1))
    out.append(one('rv-ph2ps-denormal-dm', 'vcvtph2ps xmm1, xmm2', {1: lanes(ONE), 2: lanes(0x3c000001)}, MX & ~(DE << 7), 0, None, 1))
    out.append(one('rv-ps2ph-denormal-pm', 'vcvtps2ph xmm1, xmm2, 4', {1: lanes(ONE), 2: lanes(1, ONE, ONE, ONE)}, MX & ~(PE << 7), DE | UE | PE, 19, 1))
    out.append(one('rv-ps2ph-denormal-dm', 'vcvtps2ph xmm1, xmm2, 4', {1: lanes(ONE), 2: lanes(1, ONE, ONE, ONE)}, MX & ~(DE << 7), DE, 19, 1))
    for tag, ins, v, mx in (('cvtps2pi-upper-pe', 'cvtps2pi mm0, xmm1', f32(1.5), MX & ~(PE << 7)), ('cvttps2pi-upper-ie', 'cvttps2pi mm0, xmm1', f32(3e9), MX & ~(IE << 7)),
                            ('cvtps2pi-upper-masked', 'cvtps2pi mm0, xmm1', f32(1.5), MX), ('cvttps2pi-upper-masked', 'cvttps2pi mm0, xmm1', f32(3e9), MX)):
        out.append(one(f'rv-{tag}', ins, {1: lanes(ONE, ONE, v, ONE)}, mx, 0, None, None))
    # AFP fallback, decision C (the unmasked case): x86 judges tininess AFTER rounding to the destination precision as if the exponent range were
    # unbounded (SDM Vol.1 4.9.1.5), a host without FEAT_AFP BEFORE it. A result whose exact value lies just below +-MIN_NORMAL and that rounds to it
    # is not tiny on x86: no UE, no #XM (PE only; the result is +-MIN_NORMAL). Just below it, between half an ulp of the destination format below MIN_NORMAL and
    # half the denormal spacing, the exact value is tiny on x86 although the denormal-grid rounding also gives MIN_NORMAL (the hardware result alone does not
    # tell): UE, #XM. Rows from tools/models/gen_tiny_vectors.py's reference (A: not tiny after rounding, B: tiny): (rc, tag, a, b, c, sign of the exact value).
    UM = MX & ~(UE << 7)
    MIN32, MIN64 = 0x00800000, 0x0010000000000000
    BAND = {
        'mulss': [(0, 'A', 0x2339d5a4, 0x9cb05433, 0, -1), (0, 'B', 0x243ce5cf, 0x1bad7835, 0, 1), (1, 'A', 0x1ecfc2ac, 0xa11db859, 0, -1),
                  (1, 'B', 0x1c54cb25, 0xa399fd58, 0, -1), (2, 'A', 0x20341153, 0x1fb5f9dd, 0, 1), (2, 'B', 0x20285b00, 0x1fc2a2c2, 0, 1),
                  (3, 'B', 0x2339d5a4, 0x9cb05433, 0, -1), (3, 'B', 0x20341153, 0x1fb5f9dd, 0, 1)],
        'mulsd': [(0, 'A', 0x1f8af557d8e94b15, 0xa072fe03c4afc4e7, 0, -1), (0, 'B', 0x1fee9079ed52a241, 0x2010c064b2495dd7, 0, 1),
                  (1, 'A', 0x1faa123f90f5380e, 0xa053a37285f2af8b, 0, -1), (1, 'B', 0x1f868f91f6cdb2f8, 0xa076b1b7b845c507, 0, -1),
                  (2, 'A', 0x1ff19de2b4ff00ae, 0x200d103224303c4f, 0, 1), (2, 'B', 0x2067ca07cc099a1e, 0x1f9585bb3ca3298e, 0, 1),
                  (3, 'B', 0x1f8af557d8e94b15, 0xa072fe03c4afc4e7, 0, -1)],
        # (a quotient of two p-bit numbers is never within half a spacing of MIN_NORMAL without being tiny after rounding: 2^-150 at least away
        # for binary32, so every divide row is B; 0x00ffffff / 2 is MIN_NORMAL - 2^-150 exactly, a tie that the denormal grid rounds up)
        'divss': [(0, 'B', 0x00ffffff, 0x40000000, 0, 1), (1, 'B', 0x85eeea5c, 0x44eeea5d, 0, -1), (2, 'B', 0x0861e8ae, 0x4761e8af, 0, 1)],
        'divsd': [(1, 'B', 0x81383e87ea252462, 0x41183e87ea252463, 0, -1), (2, 'B', 0x010c74bbb4f79d8c, 0x40ec74bbb4f79d8d, 0, 1)],
        'vfmadd231ss': [(0, 'A', 0x1d8ffbf9, 0x2376c6dd, 0x815597f5, 1),
                        (0, 'B', 0x1d293af6, 0x21208ae6, 0x808d4415, -1),
                        (1, 'A', 0x20d9a6f6, 0x209ebfc4, 0x8216f816, -1),
                        (1, 'B', 0x1d2781a5, 0x1eb564c3, 0x808076b0, -1),
                        (2, 'A', 0x201ae62d, 0x20f7e850, 0x816c014d, 1),
                        (2, 'B', 0x1d41579c, 0x2346f1e1, 0x80ac80e1, 1)],
        'vfmadd231sd': [(0, 'A', 0x206d9c90d56a3d0b, 0x1fd7c272eaf79df3, 0x8054fc779ea42dfc, 1),
                        (0, 'B', 0x1fae501a8a10ab6b, 0x1fcf7fe7086c6ee0, 0x80100eeb613ca735, -1),
                        (1, 'A', 0x1fc0ab67b82977de, 0x1fc98f67f5879cd9, 0x80101aa13a4df85d, -1),
                        (1, 'B', 0x202ca53039a2f210, 0x1fa15375fa7639c1, 0x8011f04ef5fc17f1, -1),
                        (2, 'A', 0x2027ae2d03daba3f, 0x1feeaec8f09b736f, 0x801d6943f6116d74, 1),
                        (2, 'B', 0x203912751b647aec, 0x1fdef4cc8871ccb9, 0x8020411ac7313b50, 1)],
        'cvtsd2ss': [(0, 'A', 0x380ffffff7490213, 0, 0, 1), (0, 'B', 0x380fffffe06cfe0a, 0, 0, 1), (1, 'A', 0xb80ffffff0000000, 0, 0, -1),
                     (1, 'B', 0xb80fffffcd8af040, 0, 0, -1), (2, 'A', 0x380fffffffb2d2c8, 0, 0, 1), (2, 'B', 0x380fffffd519fa45, 0, 0, 1),
                     (3, 'B', 0x380ffffff7490213, 0, 0, 1)],
    }
    SPEC = {   # insn, element bits, registers (xmm1 = destination or the addend, the sources), the destination's format and element bits
        'mulss': ('mulss xmm1, xmm2', 32, (1, 2), 32), 'mulsd': ('mulsd xmm1, xmm2', 64, (1, 2), 64), 'divss': ('divss xmm1, xmm2', 32, (1, 2), 32),
        'divsd': ('divsd xmm1, xmm2', 64, (1, 2), 64), 'vfmadd231ss': ('vfmadd231ss xmm1, xmm2, xmm3', 32, (2, 3, 1), 32),
        'vfmadd231sd': ('vfmadd231sd xmm1, xmm2, xmm3', 64, (2, 3, 1), 64), 'cvtsd2ss': ('cvtsd2ss xmm1, xmm2', 64, (2,), 32),
    }
    def band_result(vm, dest, lane0, bits, rest=()):
        got = xmm_of(vm, dest)
        want = (lane0 | (rest[0] if rest else 0) << bits, rest[1] if len(rest) > 1 else 0) if bits == 32 else (lane0, rest[0] if rest else 0)
        return [] if got == want else [f'xmm{dest} {got[0]:#x} {got[1]:#x}, want {want[0]:#x} {want[1]:#x}']
    for op, rows in BAND.items():
        insn, ebits, srcs, dbits = SPEC[op]
        for rc, tag, a, b, c, sign in rows:
            x = {srcs[k]: ((v, 0) if ebits == 64 else (v, 0)) for k, v in enumerate((a, b, c)[:len(srcs)])}
            if op == 'cvtsd2ss': x[1] = lanes(ONE)
            elif op == 'vfmadd231ss' or op == 'vfmadd231sd': pass
            mx = UM | rc << 13
            name = f'band-{op}-rc{rc}-{"not-tiny" if tag == "A" else "tiny"}{"-neg" if sign < 0 else ""}'
            if tag == 'A':   # no #XM: the result is +-MIN_NORMAL, PE (inexact) is the only flag
                c1 = one(name, insn, x, mx, PE, None, 1)
                old, res = c1['check'], (MIN32 if dbits == 32 else MIN64) | (sign < 0) << (dbits - 1)
                c1['check'] = lambda vm, old=old, res=res, dbits=dbits: old(vm) + band_result(vm, 1, res, dbits)
                out.append(c1)
            else:            # #XM (UE and PE): the destination unchanged
                out.append(one(name, insn, x, mx, UE | PE, 19, 1))
    # A band lane beside other lanes (packed, SDM Vol.1 11.5.3.2: the flags are those of the elements together, UE for any tiny one).
    A32, B32 = (0x2339d5a4, 0x9cb05433), (0x243ce5cf, 0x1bad7835)      # (a, b): A not tiny, B tiny (round to nearest, mulps)
    A64, B64 = (0x1f8af557d8e94b15, 0xa072fe03c4afc4e7), (0x1fee9079ed52a241, 0x2010c064b2495dd7)
    def packed(name, insn, x, mx, flags, fault, res=None, ymm=None, prelude=(), extra=()):
        c1 = one(name, insn, x, mx, flags, fault, 1, prelude=prelude, extra=extra, ymm=ymm is not None)
        if res is not None:
            old = c1['check']
            c1['check'] = lambda vm, old=old, res=res: old(vm) + ([] if xmm_of(vm, 1) == res else [f'xmm1 {xmm_of(vm, 1)[0]:#x} {xmm_of(vm, 1)[1]:#x}, want {res[0]:#x} {res[1]:#x}'])
        out.append(c1)
    m = lambda s: MIN32 | (s << 31)
    packed('band-mulps-lane0-not-tiny', 'mulps xmm1, xmm2', {1: lanes(A32[0], ONE, ONE, ONE), 2: lanes(A32[1], ONE, ONE, ONE)}, UM, PE, None, res=(m(1) | ONE << 32, ONE | ONE << 32))
    # The destination is the addend (FMLA): the handler's re-run overwrites it, so the band decision must come from the operands saved at the trap.
    packed('band-vfmadd231ps-dest-is-addend-lane0-not-tiny', 'vfmadd231ps xmm1, xmm2, xmm3', {1: lanes(ZERO, ZERO, ZERO, ZERO), 2: lanes(A32[0], ONE, ONE, ONE), 3: lanes(A32[1], ONE, ONE, ONE)}, UM, PE, None, res=(m(1) | ONE << 32, ONE | ONE << 32))
    packed('band-mulps-lane0-not-tiny-lane1-tiny-sub-band', 'mulps xmm1, xmm2', {1: lanes(A32[0], B32[0], ONE, ONE), 2: lanes(A32[1], B32[1], ONE, ONE)}, UM, UE | PE, 19)
    packed('band-mulps-lane0-not-tiny-lane1-exact-tiny', 'mulps xmm1, xmm2', {1: lanes(A32[0], E100, ONE, ONE), 2: lanes(A32[1], E30, ONE, ONE)}, UM, UE | PE, 19)
    packed('band-mulps-lane0-not-tiny-lane1-tiny-first', 'mulps xmm1, xmm2', {1: lanes(B32[0], A32[0], ONE, ONE), 2: lanes(B32[1], A32[1], ONE, ONE)}, UM, UE | PE, 19)
    packed('band-mulps-both-lanes-not-tiny', 'mulps xmm1, xmm2', {1: lanes(A32[0], A32[0], ONE, ONE), 2: lanes(A32[1], A32[1], ONE, ONE)}, UM, PE, None, res=(m(1) | m(1) << 32, ONE | ONE << 32))
    packed('band-mulps-lane0-not-tiny-lane1-overflow-oe-masked', 'mulps xmm1, xmm2', {1: lanes(A32[0], MAXF, ONE, ONE), 2: lanes(A32[1], TWO, ONE, ONE)}, UM, OE | PE, None,
           res=(m(1) | INF << 32, ONE | ONE << 32))
    packed('band-mulps-lane0-not-tiny-pe-unmasked', 'mulps xmm1, xmm2', {1: lanes(A32[0], ONE, ONE, ONE), 2: lanes(A32[1], ONE, ONE, ONE)}, UM & ~(PE << 7), PE, 19)
    packed('band-mulps-lane1-tiny-pe-unmasked', 'mulps xmm1, xmm2', {1: lanes(A32[0], B32[0], ONE, ONE), 2: lanes(A32[1], B32[1], ONE, ONE)}, UM & ~(PE << 7), UE | PE, 19)
    packed('band-mulpd-lane0-not-tiny-lane1-tiny', 'mulpd xmm1, xmm2', {1: (A64[0], B64[0]), 2: (A64[1], B64[1])}, UM, UE | PE, 19)
    packed('band-mulpd-lane0-tiny-lane1-not-tiny', 'mulpd xmm1, xmm2', {1: (B64[0], A64[0]), 2: (B64[1], A64[1])}, UM, UE | PE, 19)
    packed('band-mulpd-both-not-tiny', 'mulpd xmm1, xmm2', {1: (A64[0], A64[0]), 2: (A64[1], A64[1])}, UM, PE, None, res=(MIN64 | 1 << 63, MIN64 | 1 << 63))
    packed('band-cvtpd2ps-lane0-not-tiny-lane1-tiny', 'cvtpd2ps xmm1, xmm2', {1: (0, 0), 2: (0x380ffffff7490213, 0x380fffffe06cfe0a)}, UM, UE | PE, 19)
    packed('band-cvtpd2ps-lane0-tiny-lane1-not-tiny', 'cvtpd2ps xmm1, xmm2', {1: (0, 0), 2: (0x380fffffe06cfe0a, 0x380ffffff7490213)}, UM, UE | PE, 19)
    packed('band-cvtpd2ps-both-not-tiny', 'cvtpd2ps xmm1, xmm2', {1: (0, 0), 2: (0x380ffffff7490213, 0x380ffffff7490213)}, UM, PE, None, res=(MIN32 | MIN32 << 32, 0))
    # A 256-bit multiply: each half's lanes count (both halves are checked before either is written).
    ymm = lambda name, a_hi, b_hi, flags, fault: out.append(one(name, 'vmulps ymm1, ymm1, ymm2', {1: lanes(A32[0], ONE, ONE, ONE), 2: lanes(A32[1], ONE, ONE, ONE)}, UM, flags, fault, 1,
                   prelude=['vinsertf128 ymm1, ymm1, xmmword ptr [rdi + 0x80], 1', 'vinsertf128 ymm2, ymm2, xmmword ptr [rdi + 0x90], 1'],
                   extra=[f'memory {D + 0x280:#x} {a_hi.hex()}', f'memory {D + 0x290:#x} {b_hi.hex()}', 'xcr0 7'], ymm=False))
    ymm('band-vmulps-ymm-upper-not-tiny', struct.pack('<IIII', A32[0], ONE, ONE, ONE), struct.pack('<IIII', A32[1], ONE, ONE, ONE), PE, None)
    ymm('band-vmulps-ymm-upper-tiny', struct.pack('<IIII', B32[0], ONE, ONE, ONE), struct.pack('<IIII', B32[1], ONE, ONE, ONE), UE | PE, 19)
    # Controls: an add and a square root never produce a tiny inexact result (a tiny sum is exact; a root is never tiny): MIN_NORMAL from 2^-125 - 2^-126 is
    # exact, and raises nothing (normal operands, so no DE either).
    out.append(one('band-addss-exact-min-normal', 'addss xmm1, xmm2', {1: lanes(0x01000000), 2: lanes(0x80800000)}, UM, 0, None, 1))
    # AFP accurate chunk 5: DAZ + FTZ together, every exception masked (FZ = 0 with UFE in accurate mode without AFP: row 4 flushes the sources, the
    # trap handler gives x86's flushed result and UE + PE; the M4 and fast mode flush in hardware). A denormal source is a zero (no DE, no PE), a tiny
    # result (exact ones too) is +-0 with UE and PE, a result that x86 rounds up to MIN_NORMAL is not tiny: +-MIN_NORMAL with PE (fast mode without AFP
    # flushes it to 0 with UE and PE: the registered deviation `daz-ftz-band`, value and flags). Results are the whole xmm1.
    DF = MX | 0x8000 | 0x40
    zz, dz = (0, 0), lambda *v: (v[0] | v[1] << 32, v[2] | v[3] << 32)
    packed('daz-ftz-flush-mulss', 'mulss xmm1, xmm2', {1: lanes(E100 | 1), 2: lanes(E30)}, DF, UE | PE, None, res=zz)
    packed('daz-ftz-exact-tiny-mulss', 'mulss xmm1, xmm2', {1: lanes(E100), 2: lanes(E30)}, DF, UE | PE, None, res=zz)
    packed('daz-ftz-denormal-input-mulss', 'mulss xmm1, xmm2', {1: lanes(DEN), 2: lanes(TWO)}, DF, 0, None, res=zz)
    packed('daz-ftz-denormal-input-addss', 'addss xmm1, xmm2', {1: lanes(ONE), 2: lanes(DEN)}, DF, 0, None, res=(ONE, 0))
    packed('daz-ftz-exact-tiny-addss', 'addss xmm1, xmm2', {1: lanes(0x00c00000), 2: lanes(0x80800000)}, DF, UE | PE, None, res=zz)
    packed('daz-ftz-exact-tiny-subsd', 'subsd xmm1, xmm2', {1: (0x0018000000000000, 0), 2: (0x0010000000000000, 0)}, DF, UE | PE, None, res=zz)
    packed('daz-ftz-exact-tiny-addps-lanes', 'addps xmm1, xmm2', {1: lanes(0x00c00000, ONE, ONE, ONE), 2: lanes(0x80800000, ONE, ONE, ONE)}, DF, UE | PE, None,
           res=dz(0, TWO, TWO, TWO))
    packed('daz-ftz-mulps-mixed-lanes', 'mulps xmm1, xmm2', {1: lanes(E100 | 1, ONE, DEN, TWO), 2: lanes(E30, TWO, ONE, ONE)}, DF, UE | PE, None, res=dz(0, TWO, 0, TWO))
    # the negating and sign-mask multiplies of ADDSUB, FMSUB, FNMADD and FMADDSUB see no denormal (row 4 runs before them): no flag, the exact value
    packed('daz-ftz-addsubps-denormal', 'addsubps xmm1, xmm2', {1: lanes(ONE, ONE, ONE, ONE), 2: lanes(DEN, DEN, DEN, DEN)}, DF, 0, None, res=dz(ONE, ONE, ONE, ONE))
    for ins in ('vfmsub231ps', 'vfnmadd231ps', 'vfmaddsub231ps', 'vfmsubadd231ps', 'vfnmsub231ps'):
        sign = -1 if ins in ('vfnmadd231ps', 'vfnmsub231ps') else 1
        packed(f'daz-ftz-{ins}-denormal-addend', f'{ins} xmm1, xmm2, xmm3', {1: lanes(DEN, DEN, DEN, DEN), 2: lanes(ONE, ONE, ONE, ONE), 3: lanes(TWO, TWO, TWO, TWO)}, DF, 0, None,
               res=dz(*([0xc0000000] * 4 if sign < 0 else [TWO] * 4)))
    packed('daz-ftz-vfmadd231ps-flush', 'vfmadd231ps xmm1, xmm2, xmm3', {1: lanes(0, 0, 0, 0), 2: lanes(E100 | 1, ONE, ONE, ONE), 3: lanes(E30, ONE, ONE, ONE)}, DF, UE | PE, None,
           res=dz(0, ONE, ONE, ONE))
    # DPPS's steps: a denormal product is flushed (UE + PE), a denormal input is zero, a denormal sum of the first add level is flushed too
    f70 = f32(2.0 ** -70)
    packed('daz-ftz-dpps-denormal-product', 'dpps xmm1, xmm2, 0xf1', {1: lanes(f70, ONE, ONE, ONE), 2: lanes(f70, ONE, ONE, ONE)}, DF, UE | PE, None, res=(0x40400000, 0))
    packed('daz-ftz-dpps-denormal-input', 'dpps xmm1, xmm2, 0xf1', {1: lanes(DEN, ONE, ONE, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, DF, 0, None, res=(0x40400000, 0))
    packed('daz-ftz-dpps-denormal-sum', 'dpps xmm1, xmm2, 0x31', {1: lanes(0x00c00000, 0x80800000, 0, 0), 2: lanes(ONE, ONE, 0, 0)}, DF, UE | PE, None, res=zz)
    # DAZ dedupe (accurate mode without AFP: one input flush per distinct value, the flushed copy feeds FP consumers only): a denormal is a zero
    # to every multiply, and the guest's own register or memory keeps its bits. Run under DAZ + FTZ and under DAZ alone; every product is by 1.0, so a
    # source that was not flushed would show (the denormal itself under DAZ alone, UE and PE under DAZ + FTZ). 1 is the smallest denormal of
    # a double (and of a float); `d1` and `f1` are 1.0.
    d1, d1f = f64(1.0), f32(1.0)
    DEN2 = 0x40000000   # a double denormal (2^-1043) whose low float is 2.0 and whose high float is zero: a double flush changes it, a float flush does not
    def dedupe(name, lines, x, res, mem=None, finals=(), rax=None, flags=0, mxs=(MX | 0x40,)):
        for tag, start in (('daz-ftz', DF), ('daz', MX | 0x40)):
            def check(vm, res=res, rax=rax):
                p = [f'xmm{n} {xmm_of(vm, n)[0]:#x} {xmm_of(vm, n)[1]:#x}, want {w[0]:#x} {w[1]:#x}' for n, w in res.items() if xmm_of(vm, n) != w]
                if rax is not None and int(vm['gpr'][0], 0) != rax: p.append(f"rax {vm['gpr'][0]}, want {rax:#x}")
                return p
            out.append(case(f'dedupe-{tag}-{name}', lines, ['sse 1', f'mxcsr {start:#x}', f'gpr-rbx 0x7fffffff'] + regs(x) + ([f'memory {D + 0x280:#x} {mem.hex()}'] if mem else []),
                            None, check=check, mx=mxc(flags or start, start)))
    Z = (0, 0)
    # the reviewer's two cases: a scalar screen does not cover a later packed read; the flushed copy is never written back
    dedupe('scalar-then-packed', ['mulsd xmm1, xmm0', 'mulpd xmm2, xmm0'], {0: (1, 1), 1: (d1, 5), 2: (d1, d1)}, {1: (0, 5), 2: Z, 0: (1, 1)})
    dedupe('packed-then-scalar', ['mulpd xmm2, xmm0', 'mulsd xmm1, xmm0'], {0: (1, 1), 1: (d1, 5), 2: (d1, d1)}, {1: (0, 5), 2: Z, 0: (1, 1)})
    dedupe('flushed-copy-not-written-back', ['mulsd xmm1, xmm0', 'movq rax, xmm0', 'movq xmm3, xmm0'], {0: (1, 7), 1: (d1, 0)}, {1: Z, 0: (1, 7), 3: (1, 0)}, rax=1)
    # a value read twice in one instruction, and by later instructions
    dedupe('same-instruction', ['mulsd xmm0, xmm0'], {0: (1, 7)}, {0: (0, 7)})
    dedupe('across-instructions', ['mulsd xmm1, xmm0', 'mulsd xmm2, xmm0', 'addsd xmm3, xmm0'], {0: (1, 7), 1: (d1, 0), 2: (d1, 0), 3: (d1, 0)}, {1: Z, 2: Z, 3: (d1, 0), 0: (1, 7)})
    # a copy, then reads of both: the same value
    dedupe('copy', ['movaps xmm4, xmm0', 'mulsd xmm1, xmm0', 'mulsd xmm2, xmm4', 'mulsd xmm3, xmm0'], {0: (1, 7), 1: (d1, 0), 2: (d1, 0), 3: (d1, 0)},
           {1: Z, 2: Z, 3: Z, 0: (1, 7), 4: (1, 7)})
    # an intervening write of the register: the later read is another value (a full store, a merge, an arithmetic result)
    dedupe('write-between-movaps', ['mulsd xmm1, xmm0', 'movaps xmm0, xmm5', 'mulsd xmm2, xmm0'], {0: (1, 7), 1: (d1, 0), 2: (d1, 0), 5: (d1, 9)}, {1: Z, 2: (d1, 0), 0: (d1, 9)})
    dedupe('write-between-movsd', ['mulsd xmm1, xmm0', 'movsd xmm0, xmm5', 'mulsd xmm2, xmm0'], {0: (1, 7), 1: (d1, 0), 2: (d1, 0), 5: (d1, 9)}, {1: Z, 2: (d1, 0), 0: (d1, 7)})
    dedupe('write-between-arith', ['mulsd xmm1, xmm0', 'addsd xmm0, xmm5', 'mulsd xmm2, xmm0'], {0: (1, 7), 1: (d1, 0), 2: (d1, 0), 5: (d1, 9)}, {1: Z, 2: (d1, 0), 0: (d1, 7)})
    dedupe('write-between-movq', ['mulsd xmm1, xmm0', 'movq xmm0, xmm5', 'mulsd xmm2, xmm0'], {0: (1, 7), 1: (d1, 0), 2: (d1, 0), 5: (d1, 9)}, {1: Z, 2: (d1, 0), 0: (d1, 0)})
    # packed against scalar, and against another element size (the same bits are a different set of values)
    dedupe('float-then-double', ['mulss xmm1, xmm0', 'mulsd xmm2, xmm0'], {0: (DEN2, 0), 1: (d1f, 0), 2: (d1, 0)}, {1: (DEN2, 0), 2: Z, 0: (DEN2, 0)})
    dedupe('double-then-float', ['mulsd xmm2, xmm0', 'mulss xmm1, xmm0'], {0: (DEN2, 0), 1: (d1f, 0), 2: (d1, 0)}, {1: (DEN2, 0), 2: Z, 0: (DEN2, 0)})
    dedupe('packed-float-then-double', ['mulps xmm1, xmm0', 'mulpd xmm2, xmm0'], {0: (DEN2, DEN2), 1: (d1f | d1f << 32, d1f | d1f << 32), 2: (d1, d1)},
           {1: (DEN2, DEN2), 2: Z, 0: (DEN2, DEN2)})
    # the same address loaded twice: memory can change between the loads, each is screened
    dedupe('load-twice', ['mulsd xmm1, qword ptr [rdi + 0x80]', 'mulsd xmm2, qword ptr [rdi + 0x80]'], {1: (d1, 0), 2: (d1, 0)}, {1: Z, 2: Z}, mem=struct.pack('<Q', 1))
    dedupe('load-changed-between', ['mulsd xmm1, qword ptr [rdi + 0x80]', 'mov rax, 1', 'mov qword ptr [rdi + 0x80], rax', 'mulsd xmm2, qword ptr [rdi + 0x80]'],
           {1: (d1, 0), 2: (d1, 0)}, {1: (d1, 0), 2: Z}, mem=struct.pack('<Q', d1), rax=1)
    # LDMXCSR switches DAZ between two reads of one value: each read follows the mode in force (the DE of the read with DAZ clear)
    for tag, first, second, finals in (('off-on', 0x1f80, DF, ((1, 0), Z)), ('on-off', DF, 0x1f80, (Z, (1, 0)))):
        out.append(case(f'dedupe-ldmxcsr-{tag}', ['ldmxcsr dword ptr [rdi + 0x80]', 'mulsd xmm1, xmm0', 'ldmxcsr dword ptr [rdi + 0x84]', 'mulsd xmm2, xmm0', 'mulsd xmm3, xmm0'],
                        ['sse 1', f'mxcsr {first:#x}', f'xmm 0 0x1 0x7', f'xmm 1 {d1:#x} 0', f'xmm 2 {d1:#x} 0', f'xmm 3 {d1:#x} 0',
                         f'memory {D + 0x280:#x} {struct.pack("<II", first, second).hex()}'], None,
                        check=lambda vm, finals=finals: [f'xmm{n} {xmm_of(vm, n)[0]:#x}, want {w[0]:#x}' for n, w in zip((1, 2, 3), (finals[0], finals[1], finals[1])) if xmm_of(vm, n) != w],
                        mx=mxc(second | DE if second == 0x1f80 else second, first)))
    # VCVTPS2PH ignores FTZ: an exact half denormal keeps its value and raises nothing
    packed('daz-ftz-vcvtps2ph-ignores-ftz', 'vcvtps2ph xmm1, xmm2, 4', {1: lanes(ONE), 2: lanes(f32(2.0 ** -20), ONE, ONE, ONE)}, DF, 0, None, res=(0x3c003c003c000010, 0),
           extra=['xcr0 7'])
    for op, rows in BAND.items():
        insn, ebits, srcs, dbits = SPEC[op]
        for rc, tag, a, b, c, sign in rows:
            x = {srcs[k]: (v, 0) for k, v in enumerate((a, b, c)[:len(srcs)])}
            if op == 'cvtsd2ss': x[1] = lanes(ONE)
            start, neg = DF | rc << 13, (1 << (dbits - 1)) if sign < 0 else 0
            name = f'daz-ftz-band-{op}-rc{rc}-{"not-tiny" if tag == "A" else "tiny"}{"-neg" if sign < 0 else ""}'
            if tag == 'A':   # +-MIN_NORMAL and PE; fast mode without AFP: +-0 with UE and PE
                c1 = one(name, insn, x, start, PE, None, 1)
                c1['mx'] = None
                res = ((MIN32 if dbits == 32 else MIN64) | neg, start | PE)
                c1['value'] = {'tag': 'daz-ftz-band', 'read': lambda vm: (xmm_of(vm, 1)[0], int(vm['mxcsr'], 0)), 'exact': res, 'deviated': (neg, start | UE | PE)}
                out.append(c1)
            else:            # tiny after rounding: +-0, UE and PE, in every mode
                c1 = one(name, insn, x, start, UE | PE, None, 1)
                old = c1['check']
                c1['check'] = lambda vm, old=old, neg=neg, dbits=dbits: old(vm) + band_result(vm, 1, neg, dbits)
                out.append(c1)
    # x87 arithmetic leaves MXCSR alone and does not trip a SIMD check (its own FPSR wrapper).
    out.append(case('x87-between-simd', ['fninit', 'fld1', 'fldz', 'fdivp st(1), st(0)', 'addss xmm3, xmm3', 'fstp st(0)'],
                    ['sse 1', f'mxcsr {MX & ~(ZE << 7):#x}', f'xmm 3 {ONE:#x} 0'], None,
                    mx=mxc(MX & ~(ZE << 7), MX & ~(ZE << 7))))
    return out


# x87 ------------------------------------------------------------------------------------------------------------------
def fsw_of(vm):
    x = vm['x87']
    e = int(x['exceptions'], 0)
    flags = x['ie'] & 1 | (x['pe'] & 1) << 5 | (x['sf'] & 1) << 6 | e & 1 | (e >> 7 & 1) << 1 | (e >> 1 & 0xf) << 2 | e & 0x40
    fcw = int(x['fcw'], 0)
    es = 0x8080 if flags & ~fcw & 0x3f else 0
    c = x['c']
    return flags | es | x['top'] << 11 | c[0] << 8 | c[1] << 9 | c[2] << 10 | c[3] << 14


def mm(vm, r): return int(vm['x87']['mm'][r], 0)


def x87_cases():
    out = []
    cw = lambda v: [f'memory {D + 0x2f0:#x} {v.to_bytes(2, "little").hex()}']
    FLDCW = 'fldcw word ptr [rdi + 0xf0]'
    data = lambda *vals: [f'memory {D + 0x300:#x} ' + b''.join(struct.pack('<Q', v) for v in vals).hex()]
    def expect(fsw=None, mask=0xffff, st=None, top=None, ftw=None, mem=None, mxcsr=None, rax=None, fcw=None):
        def check(vm):
            p = []
            if fsw is not None and fsw_of(vm) & mask != fsw: p.append(f'fsw {fsw_of(vm):#06x}, want {fsw:#06x} (mask {mask:#x})')
            if top is not None and vm['x87']['top'] != top: p.append(f"top {vm['x87']['top']}, want {top}")
            if ftw is not None and int(vm['x87']['ftw'], 0) != ftw: p.append(f"ftw {vm['x87']['ftw']}, want {ftw:#x}")
            for r, v in (st or {}).items():
                if mm(vm, r) != v: p.append(f'mm[{r}] {mm(vm, r):#x}, want {v:#x}')
            if mem is not None:
                at, want = mem
                got = bytes.fromhex(vm['memory'][0]['data'])[at - D:at - D + len(want)]
                if got != want: p.append(f'memory at {at:#x} {got.hex()}, want {want.hex()}')
            if rax is not None and int(vm['gpr'][0], 0) & 0xffff != rax: p.append(f"ax {int(vm['gpr'][0], 0) & 0xffff:#06x}, want {rax:#06x}")
            if fcw is not None and int(vm['x87']['fcw'], 0) != fcw: p.append(f"fcw {vm['x87']['fcw']}, want {fcw:#x}")
            return p
        return check
    ZM = 0x37b                                       # ZE unmasked
    zdiv = ['fninit', FLDCW, 'fld1', 'fldz', 'fdivp st(1), st(0)']   # 1/0: ZE, TOP 6, ST0 = 0 (reg 6), ST1 = 1 (reg 7)
    after = {6: 0, 7: f64(1.0)}
    ZFSW = 0x8084 | 6 << 11                          # ZE, ES, B, TOP 6
    # The unmasked ZE: nothing written, no pop; #MF at the next waiting instruction (RIP there), not before.
    for nxt in ('fwait', 'fld1', 'fadd st(0), st(1)', 'fxch st(1)', 'fnop', 'fst qword ptr [rdi + 0x100]', 'emms', 'paddb mm0, mm1',
                'fldcw word ptr [rdi + 0xf0]', 'fld tbyte ptr [rdi + 0x100]', 'fstp tbyte ptr [rdi + 0x100]', 'fldenv [rdi + 0x100]', 'frstor [rdi + 0x100]',
                'fsin', 'fyl2x', 'fxam', 'ffree st(3)', 'fincstp'):
        out.append(case(f'mf-at-{nxt.split()[0]}', zdiv + ['fnstsw ax', 'mov ebx, 7', nxt], cw(ZM), ('fault', 16),
                        check=expect(fsw=ZFSW, st=after, top=6, ftw=0xc0, rax=ZFSW)))
    # Non-waiting instructions pass (FNCLEX/FNINIT clear the exception; FNSTSW/FNSTCW keep it; FNSTENV masks everything).
    out.append(case('no-mf-fnclex', zdiv + ['fnclex', 'fld1'], cw(ZM), None, check=expect(fsw=5 << 11, top=5)))
    out.append(case('no-mf-fninit', zdiv + ['fninit', 'fld1'], cw(ZM), None, check=expect(fsw=7 << 11, top=7, fcw=0x37f)))
    out.append(case('no-mf-fnstcw', zdiv + ['fnstcw word ptr [rdi + 0x100]', 'fnstsw word ptr [rdi + 0x102]', 'fwait'], cw(ZM), ('fault', 16),
                    check=expect(fsw=ZFSW, mem=(D + 0x300, struct.pack('<HH', ZM, ZFSW)))))
    out.append(case('fnstenv-masks', zdiv + ['fnstenv [rdi + 0x100]', 'fld1'], cw(ZM), None,
                    check=expect(fcw=ZM | 0x3f, mem=(D + 0x300, struct.pack('<IHH', ZM, ZFSW, 0)[:6]))))
    out.append(case('fnsave-inits', zdiv + ['fnsave [rdi + 0x100]', 'fld1'], cw(ZM), None,
                    check=expect(fcw=0x37f, fsw=7 << 11, mem=(D + 0x300, struct.pack('<IHH', ZM, ZFSW, 0)[:6]))))
    out.append(case('no-mf-fxsave', zdiv + ['fxsave [rsi]', 'mov eax, 1'], ['sse 1'] + cw(ZM), None,
                    check=expect(fsw=ZFSW, mem=(D + 0x1000, struct.pack('<HH', ZM, ZFSW)))))
    out.append(case('integer-between-then-fwait', zdiv + ['mov ebx, 5', 'add ebx, ebx', 'mov dword ptr [rdi + 0x100], ebx', 'fwait'], cw(ZM), ('fault', 16),
                    check=lambda vm: [] if bytes.fromhex(vm['memory'][0]['data'])[0x300:0x304] == struct.pack('<I', 10) else ['integer store missing']))
    out.append(case('cr0-ne-clear-ferr', zdiv + ['fwait'], cw(ZM), ('stop', 'unsupported_ferr'), ne=False, check=expect(fsw=ZFSW)))
    out.append(case('cr0-ne-clear-resident-fldcw', zdiv + [FLDCW], cw(ZM), ('stop', 'unsupported_ferr'), ne=False))
    out.append(case('cr0-ne-clear-no-error-runs', ['fninit', FLDCW, 'fld1', 'fwait'], cw(ZM), None, ne=False))
    # PE unmasked: the rounded result is stored, then #MF at the next waiting instruction.
    third = f64(1 / 3)
    out.append(case('pe-stores-then-mf', ['fninit', FLDCW, 'fld1', 'fld qword ptr [rdi + 0x100]', 'fdivp st(1), st(0)', 'fwait'],
                    cw(0x35f) + data(f64(3.0)), ('fault', 16), check=expect(fsw=0x80a0 | 7 << 11, st={7: third}, top=7)))
    # Register-destination OE: the double's masked response is stored (reduced precision: no biased exponent), OE and PE, #MF next.
    out.append(case('oe-register-stores', ['fninit', FLDCW, 'fld qword ptr [rdi + 0x100]', 'fmul st(0), st(0)', 'fwait'],
                    cw(0x377) + data(f64(1e300)), ('fault', 16), check=expect(fsw=0x80a8 | 7 << 11, mask=0xfdff, st={7: f64(float('inf'))})))
    # FST/FSTP m32/m64 with an unmasked OE/UE: nothing stored, no PE, C1 = 0, TOP unchanged (FSTP does not pop).
    for tag, ins, fcw_, val, fl in (('fst-m32-oe', 'fst dword ptr [rdi + 0x108]', 0x377, f64(1e300), 0x08),
                                    ('fstp-m32-oe', 'fstp dword ptr [rdi + 0x108]', 0x377, f64(1e300), 0x08),
                                    ('fst-m32-ue', 'fst dword ptr [rdi + 0x108]', 0x36f, f64(1e-40), 0x10),
                                    ('fstp-m64-ue-exact', 'fstp qword ptr [rdi + 0x108]', 0x36f | 2, 0x8000000000000, 0x12)):
        out.append(case(tag, ['fninit', FLDCW, 'fld qword ptr [rdi + 0x100]', ins, 'fwait'], cw(fcw_) + data(val, 0x1111111111111111), ('fault', 16),
                        check=expect(fsw=0x8080 | fl | 7 << 11, top=7, st={7: val}, mem=(D + 0x308, struct.pack('<Q', 0x1111111111111111)))))
        out.append(case(tag + '-masked', ['fninit', 'fld qword ptr [rdi + 0x100]', ins, 'fwait'], data(val, 0x1111111111111111), None,
                        check=expect(fsw=fl | 0x20 if 'exact' not in tag else 0x2, mask=0x7f)))
    # Invalid operands with IE unmasked: FIST out of range, FST m64 of an SNaN, FSQRT(-1), FCOM QNaN, FCOMI QNaN: no write,
    # condition codes and EFLAGS unchanged, no pop.
    IM = 0x37e
    out.append(case('fist-range-ie', ['fninit', FLDCW, 'fld qword ptr [rdi + 0x100]', 'fistp word ptr [rdi + 0x108]', 'fwait'],
                    cw(IM) + data(f64(40000.5), 0x1111111111111111), ('fault', 16),
                    check=expect(fsw=0x8081 | 7 << 11, top=7, mem=(D + 0x308, b'\x11\x11'))))
    out.append(case('fsqrt-ie', ['fninit', FLDCW, 'fld qword ptr [rdi + 0x100]', 'fsqrt', 'fwait'], cw(IM) + data(f64(-1.0)), ('fault', 16),
                    check=expect(fsw=0x8081 | 7 << 11, st={7: f64(-1.0)})))
    out.append(case('fcom-qnan-ie', ['fninit', FLDCW, 'fld1', 'fld1', 'fcom st(1)', 'fld qword ptr [rdi + 0x100]', 'fcomp st(1)', 'fwait'],
                    cw(IM) + data(0x7ff8000000000000), ('fault', 16), check=expect(fsw=0xc081 | 5 << 11, top=5)))
    out.append(case('fcomi-qnan-ie', ['fninit', FLDCW, 'fld1', 'fld qword ptr [rdi + 0x100]', 'mov eax, 0', 'add eax, 0', 'fcomi st(0), st(1)', 'setz bl', 'setp cl', 'fwait'],
                    cw(IM) + data(0x7ff8000000000000), ('fault', 16),
                    check=lambda vm: [] if int(vm['gpr'][3], 0) & 0xff == 1 and int(vm['gpr'][1], 0) & 0xff == 1 else [f"zf/pf {vm['gpr'][3]} {vm['gpr'][1]}: EFLAGS from add 0 (ZF PF) expected"]))
    out.append(case('fld-m64-snan-ie', ['fninit', FLDCW, 'fld qword ptr [rdi + 0x100]', 'fwait'], cw(IM) + data(0x7ff0000000000001), ('fault', 16),
                    check=expect(fsw=0x8081 | 0 << 11, top=0, ftw=0)))
    out.append(case('fld-m64-denormal-de', ['fninit', FLDCW, 'fld qword ptr [rdi + 0x100]', 'fwait'], cw(0x37d) + data(1), ('fault', 16),
                    check=expect(fsw=0x8082, top=0, ftw=0)))
    # Stack faults with IE unmasked: IE, SF, C1 (1 overflow, 0 underflow), TOP and the registers unchanged.
    out.append(case('stack-underflow-ie', ['fninit', FLDCW, 'fadd st(0), st(1)', 'fwait'], cw(IM), ('fault', 16),
                    check=expect(fsw=0x80c1, top=0, ftw=0)))
    out.append(case('stack-overflow-ie', ['fninit', FLDCW] + ['fld1'] * 8 + ['fldz', 'fwait'], cw(IM), ('fault', 16),
                    check=expect(fsw=0x82c1, top=0, ftw=0xff, st={0: f64(1.0)})))
    out.append(case('stack-overflow-masked-denormal', ['fninit', FLDCW] + ['fld1'] * 8 + ['fld qword ptr [rdi + 0x100]', 'fwait'], cw(0x37d) + data(1), None,
                    check=expect(fsw=0x0241 | 7 << 11, st={7: 0xfff8000000000000})))
    out.append(case('fxch-empty-ie', ['fninit', FLDCW, 'fld1', 'fxch st(1)', 'fwait'], cw(IM), ('fault', 16),
                    check=expect(fsw=0x80c1 | 7 << 11, top=7, ftw=0x80)))
    # FLDCW unmasking a flag already set: #MF at the next waiting instruction (not at the FLDCW itself).
    out.append(case('fldcw-unmasks-set-flag', ['fninit', 'fld1', 'fldz', 'fdivp st(1), st(0)', FLDCW, 'fstp st(0)'], cw(ZM), ('fault', 16),
                    check=expect(fsw=0x8084 | 7 << 11, top=7)))
    env = struct.pack('<HHHHHHHHHHHHHH', ZM, 0, 0x0004, 0, 0xffff, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    out.append(case('fldenv-pending', ['fninit', 'fldenv [rdi + 0x100]', 'fld1'], [f'memory {D + 0x300:#x} ' + env.hex()], ('fault', 16),
                    check=expect(fsw=0x8084, top=0)))
    img = bytearray(512); img[0:2] = ZM.to_bytes(2, 'little'); img[2:4] = (4).to_bytes(2, 'little'); img[24:28] = (0x1f80).to_bytes(4, 'little'); img[28:32] = (0xffff).to_bytes(4, 'little')
    out.append(case('fxrstor-pending-then-fld1', ['fninit', 'fxrstor [rsi]', 'fld1'], ['sse 1', f'memory {D + 0x1000:#x} {img.hex()}'], ('fault', 16),
                    check=expect(fsw=0x8084, top=0)))
    # Helper forms (svc to the resident): FYL2X log2(0) with ZE unmasked leaves both operands; FPTAN's PE stores, #MF next.
    out.append(case('fyl2x-ze', ['fninit', FLDCW, 'fld1', 'fldz', 'fyl2x', 'fwait'], cw(ZM), ('fault', 16),
                    check=expect(fsw=0x8084 | 6 << 11, top=6, st={6: 0, 7: f64(1.0)})))
    out.append(case('fptan-pe', ['fninit', FLDCW, 'fld1', 'fptan', 'fwait'], cw(0x35f), ('fault', 16),
                    check=expect(fsw=0x80a0 | 6 << 11, mask=0xfdff, top=6)))
    out.append(case('fsin-empty-ie', ['fninit', FLDCW, 'fsin', 'fwait'], cw(IM), ('fault', 16), check=expect(fsw=0x80c1, top=0, ftw=0)))
    # The resident's FLD/FSTP m80 with IE unmasked: TOP, the registers and memory unchanged, IE SF C1 (SDM Vol.1 8.5.1.1).
    out.append(case('fld-m80-overflow-ie', ['fninit', FLDCW] + ['fld1'] * 8 + ['fld tbyte ptr [rdi + 0x100]', 'fwait'], cw(IM) + data(0, 0x3fff), ('fault', 16),
                    check=expect(fsw=0x82c1, top=0, ftw=0xff, st={7: f64(1.0)})))
    out.append(case('fstp-m80-underflow-ie', ['fninit', FLDCW, 'fstp tbyte ptr [rdi + 0x100]', 'fwait'], cw(IM) + data(0x1111111111111111, 0x1111), ('fault', 16),
                    check=expect(fsw=0x80c1, top=0, ftw=0, mem=(D + 0x300, struct.pack('<QH', 0x1111111111111111, 0x1111)))))
    # FLD m32 of a denormal with DM unmasked: no push, DE.
    out.append(case('fld-m32-denormal-de', ['fninit', FLDCW, 'fld dword ptr [rdi + 0x100]', 'fwait'], cw(0x37d) + data(1), ('fault', 16),
                    check=expect(fsw=0x8082, top=0, ftw=0)))
    # AFP fallback chunk 3b (row 8): DE of a memory operand in arithmetic and FCOM, with x87's precedence (none beside a NaN in ST0, or a
    # memory dividend over a zero ST0, which is ZE). The m32 forms are not here: the M4 raises DE for them beside a NaN (its conversion
    # of a single to a double is an op of its own), x86 does not. A denormal in a register is no DE (not here: the M4 raises it).
    DM, DENBIT, QN64 = 0x37d, 1, 0x7ff8000000000000
    for name, ins, setup in (('fadd', 'fadd qword ptr [rdi + 0x100]', ['fld1']), ('fsub', 'fsub qword ptr [rdi + 0x100]', ['fld1']),
                             ('fsubr', 'fsubr qword ptr [rdi + 0x100]', ['fld1']), ('fmul', 'fmul qword ptr [rdi + 0x100]', ['fld1']),
                             ('fdiv', 'fdiv qword ptr [rdi + 0x100]', ['fldz']), ('fdivr', 'fdivr qword ptr [rdi + 0x100]', ['fld1']),
                             ('fcom', 'fcom qword ptr [rdi + 0x100]', ['fld1']), ('fcomp', 'fcomp qword ptr [rdi + 0x100]', ['fld1'])):
        st0 = f64(0.0) if setup == ['fldz'] else f64(1.0)
        out.append(case(f'{name}-m64-denormal-de', ['fninit', FLDCW] + setup + [ins, 'fwait'], cw(DM) + data(DENBIT), ('fault', 16),
                        check=expect(fsw=0x8082 | 7 << 11, top=7, st={7: st0})))   # nothing written, no pop: TOP and ST0 as before
    for name, ins in (('fadd', 'fadd qword ptr [rdi + 0x100]'), ('fdiv', 'fdiv qword ptr [rdi + 0x100]'), ('fmul', 'fmul qword ptr [rdi + 0x100]')):
        out.append(case(f'{name}-m64-denormal-st0-qnan-no-de', ['fninit', FLDCW, 'fld qword ptr [rdi + 0x108]', ins, 'fwait'], cw(DM) + data(DENBIT, QN64), None,
                        check=expect(fsw=7 << 11, top=7, st={7: QN64})))   # the QNaN comes back, nothing raised
    out.append(case('fcom-m64-denormal-st0-qnan-ie-not-de', ['fninit', FLDCW, 'fld qword ptr [rdi + 0x108]', 'fcom qword ptr [rdi + 0x100]', 'fwait'], cw(DM) + data(DENBIT, QN64), None,
                    check=expect(fsw=0x0001 | 0x4500 | 7 << 11, top=7)))   # IE (masked) for the QNaN, C3 C2 C0; no DE
    out.append(case('fdivr-m64-denormal-st0-zero-ze-not-de', ['fninit', FLDCW, 'fldz', 'fdivr qword ptr [rdi + 0x100]', 'fwait'], cw(DM) + data(DENBIT), None,
                    check=expect(fsw=0x0004 | 7 << 11, top=7, st={7: f64(float('inf'))})))   # den / 0: ZE (masked), the infinity stored
    out.append(case('fld-m64-denormal-stack-overflow-ie-not-de', ['fninit', FLDCW] + ['fld1'] * 8 + ['fld qword ptr [rdi + 0x100]', 'fwait'], cw(0x37c) + data(DENBIT), ('fault', 16),
                    check=expect(fsw=0x82c1, top=0, ftw=0xff)))   # a stack fault (IE unmasked) drops the DE
    # AFP fallback, x87 in default mode (no AFP: M2, or M2 emulation NATEMU_FEXC_FEATURE_OFF=afp): FSW.DE of an m32/m64 operand with every exception
    # masked (set, nothing raised), with the same precedence; and the NaN results of arithmetic (x86's negative "real indefinite" for an
    # invalid operation; a NaN operand's quieted self). Must pass on the M4 too (its m32 convert's IDC beside a NaN in ST0,
    # or FDIVR m32 over a zero ST0, is dropped by the AFP code: FexcAfterOp).
    MK = 0x384f                                       # IE, DE, ZE, OE, SF and TOP (PE, UE, the condition codes are not judged)
    DEN32, SN32 = 1, 0x7f800001
    QN64, SN64, INF64, INDEF = 0x7ff8000000000000, 0x7ff0000000000001, 0x7ff0000000000000, 0xfff8000000000000
    SN64Q = SN64 | 1 << 51                            # the SNaN quieted
    SN32D = 0x7ff8000020000000                        # SNaN32 as a double (reduced-precision x87 registers hold doubles), quieted
    for w, ptr in ((64, 'qword'), (32, 'dword')):
        den = DEN32 if w == 32 else DENBIT
        dtop = lambda pops: 7 + pops & 7              # TOP after fld (7) and the pops
        for name, ins, setup, pops in (('fadd', 'fadd', ['fld1'], 0), ('fsub', 'fsub', ['fld1'], 0), ('fsubr', 'fsubr', ['fld1'], 0),
                                       ('fmul', 'fmul', ['fld1'], 0), ('fdiv', 'fdiv', ['fldz'], 0), ('fdivr', 'fdivr', ['fld1'], 0),
                                       ('fcom', 'fcom', ['fld1'], 0), ('fcomp', 'fcomp', ['fld1'], 1)):
            out.append(case(f'{name}-m{w}-denormal-masked-de', ['fninit'] + setup + [f'{ins} {ptr} ptr [rdi + 0x100]', 'fwait'], data(den), None,
                            check=expect(fsw=DE | dtop(pops) << 11, mask=MK)))
        # DE-free: a NaN in ST0 (IE for an SNaN, nothing for a QNaN); a memory dividend over a zero ST0 is ZE.
        for name, ins in (('fadd', 'fadd'), ('fmul', 'fmul'), ('fdiv', 'fdiv'), ('fsub', 'fsub')):
            out.append(case(f'{name}-m{w}-denormal-st0-qnan-masked-no-de', ['fninit', 'fld qword ptr [rdi + 0x108]', f'{ins} {ptr} ptr [rdi + 0x100]', 'fwait'],
                            data(den, QN64), None, check=expect(fsw=7 << 11, mask=MK, st={7: QN64})))
            out.append(case(f'{name}-m{w}-snan-masked-ie-quieted', ['fninit', 'fld1', f'{ins} {ptr} ptr [rdi + 0x100]', 'fwait'],
                            data(SN32 if w == 32 else SN64), None, check=expect(fsw=IE | 7 << 11, mask=MK, st={7: SN32D if w == 32 else SN64Q})))
        out.append(case(f'fcom-m{w}-denormal-st0-qnan-masked-ie-not-de', ['fninit', 'fld qword ptr [rdi + 0x108]', f'fcom {ptr} ptr [rdi + 0x100]', 'fwait'],
                        data(den, QN64), None, check=expect(fsw=IE | 7 << 11, mask=MK)))
        out.append(case(f'fdivr-m{w}-denormal-st0-zero-masked-ze-not-de', ['fninit', 'fldz', f'fdivr {ptr} ptr [rdi + 0x100]', 'fwait'], data(den), None,
                        check=expect(fsw=ZE | 7 << 11, mask=MK, st={7: INF64})))
        out.append(case(f'fld-m{w}-denormal-masked-de', ['fninit', f'fld {ptr} ptr [rdi + 0x100]', 'fwait'], data(den), None, check=expect(fsw=DE | 7 << 11, mask=MK)))
    out.append(case('fld-m64-snan-masked-ie-quieted', ['fninit', 'fld qword ptr [rdi + 0x100]', 'fwait'], data(SN64), None, check=expect(fsw=IE | 7 << 11, mask=MK, st={7: SN64Q})))
    out.append(case('fld-m32-snan-masked-ie-quieted', ['fninit', 'fld dword ptr [rdi + 0x100]', 'fwait'], data(SN32), None, check=expect(fsw=IE | 7 << 11, mask=MK, st={7: SN32D})))
    out.append(case('fld-m64-qnan-masked-clean', ['fninit', 'fld qword ptr [rdi + 0x100]', 'fwait'], data(QN64), None, check=expect(fsw=7 << 11, mask=MK, st={7: QN64})))
    # NaN results, every exception masked: x86's real indefinite (negative QNaN) for an invalid operation (TOP 7, the result in reg 7).
    NINF64 = 0xfff0000000000000
    invalid = (('0-over-0', ['fldz', 'fldz'], 'fdivp st(1), st(0)'), ('inf-minus-inf', ['fld qword ptr [rdi + 0x100]'] * 2, 'fsubp st(1), st(0)'),
               ('inf-times-0', ['fldz', 'fld qword ptr [rdi + 0x100]'], 'fmulp st(1), st(0)'),
               ('inf-plus-minus-inf', ['fld qword ptr [rdi + 0x100]', 'fld qword ptr [rdi + 0x108]'], 'faddp st(1), st(0)'),
               ('0-over-0-fdivrp', ['fldz', 'fldz'], 'fdivrp st(1), st(0)'), ('sqrt-minus-1', ['fld1', 'fchs'], 'fsqrt'),
               ('sqrt-minus-inf', ['fld qword ptr [rdi + 0x108]'], 'fsqrt'), ('0-over-0-m64', ['fldz'], 'fdiv qword ptr [rdi + 0x110]'),
               ('0-over-0-m32', ['fldz'], 'fdiv dword ptr [rdi + 0x110]'), ('inf-times-0-m64', ['fld qword ptr [rdi + 0x100]'], 'fmul qword ptr [rdi + 0x110]'))
    for name, setup, ins in invalid:
        out.append(case(f'nan-{name}-indefinite', ['fninit'] + setup + [ins, 'fwait'], data(INF64, NINF64, 0), None,
                        check=expect(fsw=IE | 7 << 11, mask=MK, st={7: INDEF})))
    # NaN operands (an SNaN can only reach an operation from memory: FLD quiets it): the SNaN is quieted (IE); beside a QNaN in ST0 the QNaN
    # (also the M4's first-operand rule); of two QNaNs the one with the larger significand when it is the first operand (natemu keeps the M4's
    # first-operand rule for x87 where x86 takes the larger one in either order: COR-09, not judged here). Memory is ST0's second operand
    # except FDIVR/FSUBR's first.
    nan = lambda name, lines, mem, fsw, st: out.append(case(f'nan-{name}', ['fninit'] + lines + ['fwait'], data(*mem), None, check=expect(fsw=fsw | 7 << 11, mask=MK, st={7: st})))
    nan('qnan-st0-snan-mem-fadd', ['fld qword ptr [rdi + 0x108]', 'fadd qword ptr [rdi + 0x100]'], (SN64, QN64), IE, QN64)
    nan('snan-mem-fdivr-one', ['fld1', 'fdivr qword ptr [rdi + 0x100]'], (SN64,), IE, SN64Q)
    nan('two-qnans-larger-mem-first-fsubr', ['fld qword ptr [rdi + 0x108]', 'fsubr qword ptr [rdi + 0x100]'], (QN64 | 0x1234, QN64), 0, QN64 | 0x1234)
    nan('two-qnans-larger-st0-first-fmul', ['fld qword ptr [rdi + 0x108]', 'fmul qword ptr [rdi + 0x100]'], (QN64, QN64 | 0x1234), 0, QN64 | 0x1234)
    nan('two-qnans-larger-first-faddp', ['fld qword ptr [rdi + 0x100]', 'fld qword ptr [rdi + 0x108]', 'faddp st(1), st(0)'], (QN64 | 0x1234, QN64), 0, QN64 | 0x1234)
    # CVTPI2PD xmm, m64 takes no x87 exception (SDM CVTPI2PD); its mm form does.
    out.append(case('cvtpi2pd-m64-no-mf', zdiv + ['cvtpi2pd xmm1, qword ptr [rdi + 0x100]', 'fwait'], ['sse 1'] + cw(ZM), ('fault', 16)))
    out.append(case('cvtpi2pd-mm-mf', zdiv + ['cvtpi2pd xmm1, mm0'], ['sse 1'] + cw(ZM), ('fault', 16)))
    # A #PF at an x87 store after its check passed: FSW (its PE) and TOP as before the instruction.
    out.append(case('fst-m32-pf-after-check', ['fninit', FLDCW, 'fld qword ptr [rdi + 0x100]', f'fstp dword ptr [{D + 0x1800:#x}]'],
                    cw(ZM) + data(f64(0.1)) + [f'unmap {D + 0x1000:#x}'], ('fault', 14), check=expect(fsw=7 << 11, top=7)))
    # TF over an aborted instruction: it completes (no #MF yet); the single-step trap follows it, RIP the next instruction.
    out.append(case('aborted-under-tf', zdiv[:-1] + ['pushfq', 'or qword ptr [rsp], 0x100', 'popfq', 'fdivp st(1), st(0)', 'nop'], cw(ZM), ('fault', 1),
                    at=len(zdiv) + 3, check=expect(fsw=ZFSW, top=6)))
    # Review cases: FST/FSTP m64 of +-DBL_MIN (a normal double, exact) with UE unmasked is stored, nothing raised; FST m32 of
    # an exact float denormal aborts, of FLT_MIN stores; FISTTP m32/FISTP m16/m64 invalid with IE unmasked store nothing.
    DMIN, keep = 0x0010000000000000, struct.pack('<Q', 0x1111111111111111)
    for name, ins, fcw_, val, fault, mem in (
            ('rv-fstp-m64-dblmin-ue', 'fstp qword ptr [rdi + 0x108]', 0x36f, DMIN, None, struct.pack('<Q', DMIN)),
            ('rv-fstp-m64-dblmin-neg-ue', 'fstp qword ptr [rdi + 0x108]', 0x36f, DMIN | 1 << 63, None, struct.pack('<Q', DMIN | 1 << 63)),
            ('rv-fst-m64-denormal-neg-ue', 'fst qword ptr [rdi + 0x108]', 0x36f, (1 << 63) | 0xfffffffffffff, ('fault', 16), keep),
            ('rv-fst-m32-exact-tiny-ue', 'fst dword ptr [rdi + 0x108]', 0x36f, f64(2.0 ** -140), ('fault', 16), keep),
            ('rv-fst-m32-fltmin-ue', 'fst dword ptr [rdi + 0x108]', 0x36f, f64(2.0 ** -126), None, struct.pack('<I', 0x00800000)),
            ('rv-fisttp-m32-range-ie', 'fisttp dword ptr [rdi + 0x108]', IM, f64(3e9), ('fault', 16), keep),
            ('rv-fistp-m16-nan-ie', 'fistp word ptr [rdi + 0x108]', IM, 0x7ff8000000000000, ('fault', 16), keep),
            ('rv-fistp-m64-range-ie', 'fistp qword ptr [rdi + 0x108]', IM, f64(1e19), ('fault', 16), keep)):
        out.append(case(name, ['fninit', FLDCW, 'fld qword ptr [rdi + 0x100]', ins, 'fwait'], cw(fcw_) + data(val, 0x1111111111111111), fault,
                        check=expect(mem=(D + 0x308, mem))))
    # Every exception masked (as Linux runs): no ES/B, nothing stops.
    out.append(case('masked-no-es', ['fninit', 'fld1', 'fldz', 'fdivp st(1), st(0)', 'fnstsw ax', 'fwait'], [], None,
                    check=expect(rax=0x0004 | 7 << 11)))
    return out


def cases():
    return simd_cases() + x87_cases()


def description(c, code):
    regs = {'rdi': D + 0x200, 'rsi': D + 0x1000, 'rsp': D + 0x1f00}
    setup = []
    for s in c['setup']:
        if s.startswith('gpr-'):
            r, v = s[4:].split(); regs[r] = int(v, 0)
        else: setup.append(s)
    lines = [f'rip {CODE:#x}', 'rflags 0x202', 'gpr ' + ' '.join(f'{regs.get(r, 0):#x}' for r in ref.REGS), f'code {code.hex()}',
             f'region {D:#x} 0x2000', f'cpl {c["cpl"]}'] + [f'memory {D:#x} ' + '00' * 0x2000]
    return '\n'.join(lines + setup) + '\n'


def judge(c, vm, offsets):
    problems = []
    rip = CODE + offsets[c['at']]
    want = c['want']
    if want and want[0] == 'stop':
        if vm['stop'] != 'resident' or not vm['reason'].startswith(want[1]) or int(vm['rip'], 0) != rip:
            problems.append(f"stop {vm['stop']} {vm['reason']} rip {vm['rip']}, want {want[1]} at {rip:#x}")
    elif want:
        got = [int(v, 0) for v in vm['fault']]
        if vm['stop'] != 'guest_fault' or got[0] != want[1] or got[3] != rip:
            problems.append(f"fault {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}, want vector {want[1]} at {rip:#x}")
    elif not vm1.at_hlt(vm, CODE + offsets[-1]):
        problems.append(f"did not reach hlt: {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}")
    kind = 'ok'
    if not problems:
        if c['check']: problems += c['check'](vm)
        if c['mx']:
            kind, why = dev.mxcsr_verdict(c['mx'], vm, int(vm['mxcsr'], 0))
            if why: problems.append(why)
        v = c.get('value')
        if v:   # a case with a registered value deviation: judged in one place, `dev.verdict`
            got = v['read'](vm)
            vk, why = dev.verdict(v['tag'], vm, got, v['exact'], v['deviated'])
            if vk == 'fail': problems.append(why or f"{v['tag']} case: value {got[0]:#x} {got[1]:#x}, want {v['exact'][0]:#x} {v['exact'][1]:#x}")
            elif vk == 'deviated' and kind == 'ok': kind = 'deviated'
    return problems, ('fail' if problems else kind)


TAGGED = 22      # the cases tagged `masked-sse-de`: 22 of 512 (update with the case list)


def self_check():
    cs = cases()
    simd = [c['mx'] for c in cs if c['mx']]
    tagged = [m for m in simd if m['tag']]
    if len(tagged) != TAGGED: sys.exit(f'run_i5c self-check FAILED: {len(tagged)} tagged cases, expected {TAGGED}')
    dev.self_check(tagged[0], next(m for m in simd if not m['tag'] and not isinstance(m['want'], tuple)), 'run_i5c')
    # `judge` itself: a case tagged masked-sse-de reaches its hlt with the deviated MXCSR, as a report of a host without AFP.
    c = next(c for c in cs if c['mx'] and c['mx']['tag'] and c['want'] is None)
    code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' for i, l in enumerate(c['lines'])), labels=True)
    offsets = [0] + [labels[f'l{i}'] for i in range(len(c['lines']) - 1)]
    vm = lambda mx, **r: {'stop': 'resident', 'reason': 'halted', 'rip': hex(CODE + offsets[-1] + 1), 'mxcsr': hex(mx), 'fault': [], **r}
    w, d = c['mx']['want'], dev.deviated_mxcsr(c['mx']['want'])
    for report, mx, kind in (({'afp': False, 'fp': 'fast'}, d, 'deviated'), ({'afp': False, 'fp': 'fast'}, w, 'fail'),
                             ({'afp': False, 'fp': 'fast'}, d ^ 1, 'fail'), ({'afp': True, 'fp': 'fast'}, w, 'ok'),
                             ({'afp': True, 'fp': 'fast'}, d, 'fail'), ({'afp': False, 'fp': 'accurate'}, d, 'fail')):
        got = judge(c, vm(mx, **report), offsets)[1]
        if got != kind: sys.exit(f"run_i5c self-check FAILED: judge({c['name']}, {report}, mxcsr {mx:#x}) is {got}, want {kind}")
    # a case with a registered value deviation (`ftz-band`): the deviated value in fast mode without AFP, exact everywhere else
    c = next(c for c in cs if c.get('value'))
    code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' for i, l in enumerate(c['lines'])), labels=True)
    offsets = [0] + [labels[f'l{i}'] for i in range(len(c['lines']) - 1)]
    v = c['value']
    def vmv(value, **r):
        x = [hex(0)] * 32; x[2], x[3] = hex(value[0]), hex(value[1])
        return {'stop': 'resident', 'reason': 'halted', 'rip': hex(CODE + offsets[-1] + 1), 'mxcsr': hex(c['mx']['want']), 'fault': [], 'xmm': x, **r}
    m2, m4, acc = {'afp': False, 'fp': 'fast'}, {'afp': True, 'fp': 'fast'}, {'afp': False, 'fp': 'accurate'}
    other = (v['deviated'][0] ^ 1, v['deviated'][1])
    for report, value, kind in ((m2, v['deviated'], 'deviated'), (m2, v['exact'], 'fail'), (m2, other, 'fail'), (m4, v['exact'], 'ok'),
                                (m4, v['deviated'], 'fail'), (acc, v['exact'], 'ok'), (acc, v['deviated'], 'fail'), ({}, v['deviated'], 'fail')):
        got = judge(c, vmv(value, **report), offsets)[1]
        if got != kind: sys.exit(f"run_i5c self-check FAILED: judge({c['name']}, {report}, lane {value[0]:#x}) is {got}, want {kind}")
    # a DAZ + FTZ band case (`daz-ftz-band`: value and MXCSR flags deviate together): fast mode without AFP gives +-0 with UE and PE, the M4 and
    # accurate mode must give +-MIN_NORMAL with PE and nothing else
    c = next(c for c in cs if c.get('value') and c['value']['tag'] == 'daz-ftz-band')
    code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' for i, l in enumerate(c['lines'])), labels=True)
    offsets = [0] + [labels[f'l{i}'] for i in range(len(c['lines']) - 1)]
    v = c['value']
    def vmd(value, **r):
        x = [hex(0)] * 32; x[2] = hex(value[0])
        return {'stop': 'resident', 'reason': 'halted', 'rip': hex(CODE + offsets[-1] + 1), 'mxcsr': hex(value[1]), 'fault': [], 'xmm': x, **r}
    for report, value, kind in ((m2, v['deviated'], 'deviated'), (m2, v['exact'], 'fail'), (m2, (v['deviated'][0], v['exact'][1]), 'fail'),
                                (m2, (v['exact'][0], v['deviated'][1]), 'fail'), (m4, v['exact'], 'ok'), (m4, v['deviated'], 'fail'),
                                (acc, v['exact'], 'ok'), (acc, v['deviated'], 'fail'), ({}, v['deviated'], 'fail')):
        got = judge(c, vmd(value, **report), offsets)[1]
        if got != kind: sys.exit(f"run_i5c self-check FAILED: judge({c['name']}, {report}, {value[0]:#x} {value[1]:#x}) is {got}, want {kind}")
    print(f'{len(cs)} cases ({len(simd)} SIMD), {len(tagged)} tagged masked-sse-de, {sum(1 for c in cs if c.get("value"))} with a value deviation (ftz-band, daz-ftz-band)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path); ap.add_argument('--out', type=pathlib.Path)
    ap.add_argument('--max-block', type=int); ap.add_argument('--only'); ap.add_argument('--host'); ap.add_argument('--runtime')
    ap.add_argument('--self-check', action='store_true', help='no VM: the case tags and the judging rule (tools/fp_deviations.py)')
    a = ap.parse_args()
    if a.self_check: return self_check()
    if not (a.fexc and a.out): ap.error('--fexc and --out are required')
    host, runtime = (a.host, a.runtime) if a.host else vm1.build()
    results, counts, reports = [], dev.Counts(), set()
    for c in cases():
        if a.only and a.only not in c['name']: continue
        code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' for i, l in enumerate(c['lines'])), labels=True)
        offsets = [0] + [labels[f'l{i}'] for i in range(len(c['lines']) - 1)]
        work = a.out / 'work' / c['name']; work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(description(c, code))
        run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([str(a.max_block)] if a.max_block else []) +
                             vm1.CODE_SHAPE, capture_output=True, text=True)
        if run.returncode: problems, kind = [f'driver: {run.stderr.strip()[-300:]}'], 'fail'
        else:
            vm = json.loads(run.stdout)
            reports.add((vm.get('afp'), vm.get('fp')))
            problems, kind = judge(c, vm, offsets)
        tag = (c['mx'] and c['mx']['tag']) or (c.get('value') or {}).get('tag')
        counts.add(kind, tag)
        results.append({'name': c['name'], 'ok': not problems, 'outcome': kind, 'tag': tag, 'problems': problems})
        if problems: print('FAIL', c['name'], '; '.join(problems)[:400])
    dev.write_results(a.out / 'results.json', results)
    print(f'afp/fp as reported: {sorted(reports, key=str)}: ' + counts.line())
    sys.exit(1 if counts.bad else 0)

if __name__ == '__main__': main()
