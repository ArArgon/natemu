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
import run_slice1_vm as vm1, ref

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


def case(name, lines, setup=(), want=None, at=None, check=None, cpl=3, ne=True):
    """`want`: None (reach the hlt), ('fault', vector) at line `at` (default the last of `lines`), ('stop', reason)."""
    return {'name': name, 'lines': list(lines) + AFTER, 'setup': list(setup) + (['ne 1'] if ne else []), 'want': want,
            'at': len(lines) - 1 if at is None else at, 'check': check, 'cpl': cpl}


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
    ('addps-de-oe', 'addps xmm1, xmm2', {1: lanes(DEN, MAXF, ONE, ONE), 2: lanes(ONE, MAXF, ZERO, ZERO)}, DE | OE | PE, None, 1),
    ('mulps-figure-11-9', 'mulps xmm1, xmm2', {1: lanes(DEN, ONE, MAXF, f32(1.5)), 2: lanes(ONE, ONE, TWO, DEN)}, DE | OE | UE | PE, None, 1),
    ('addpd-ze-free-pe', 'addpd xmm1, xmm2', {1: (f64(1.0), 0x7ff0000000000001), 2: (f64(2.0 ** -60), f64(1.0))}, IE | PE, None, 1),
]


def simd_cases():
    out = []
    regs = lambda x: [f'xmm {n} {lo:#x} {hi:#x}' for n, (lo, hi) in x.items()]
    def one(name, insn, x, mxcsr, flags, fault, dest, prelude=(), extra=(), ymm=False):
        def check(vm, x=x, mxcsr=mxcsr, flags=flags, dest=dest, fault=fault):
            p = []
            if int(vm['mxcsr'], 0) != mxcsr | flags: p.append(f"mxcsr {vm['mxcsr']}, want {mxcsr | flags:#x}")
            if fault and dest is not None and xmm_of(vm, dest) != x[dest]: p.append(f'xmm{dest} written: {xmm_of(vm, dest)}')
            if fault and int(vm['rflags'], 0) & 0x8d5: p.append(f"rflags {vm['rflags']}: the instruction's flags written")
            if fault and ymm and [int(v, 0) for v in vm['ymmh'][2 * dest:2 * dest + 2]] != list(lanes(ONE, ONE, ONE, ONE)): p.append(f"ymm upper {vm['ymmh'][2 * dest:2 * dest + 2]}")
            return p
        vex = insn.startswith('v') or any(l.startswith('v') for l in prelude)
        return case(name, list(prelude) + [insn], ['sse 1', f'mxcsr {mxcsr:#x}', f'gpr-rbx 0x7fffffff'] + (['xcr0 7'] if vex and 'xcr0 7' not in extra else []) + regs(x) + list(extra),
                    ('fault', fault) if fault else None, check=check)
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
    # DAZ: a denormal source is zero, no DE whatever DM (10.2.3.4).
    out.append(one('daz-de-unmasked', 'addss xmm1, xmm2', {1: lanes(ONE), 2: lanes(DEN)}, (MX | 0x40) & ~(DE << 7), 0, None, 1))
    out.append(one('daz-ze-unmasked', 'divss xmm1, xmm2', {1: lanes(ONE), 2: lanes(DEN)}, (MX | 0x40) & ~(ZE << 7), ZE, 19, 1))
    # A YMM form: both halves checked before either is written.
    out.append(one('vaddps-ymm-pe', 'vaddps ymm1, ymm1, ymm2', {1: lanes(ONE, ONE, ONE, ONE), 2: lanes(ONE, ONE, ONE, ONE)}, MX & ~(PE << 7), PE, 19, 1,
                   prelude=['vbroadcastss ymm2, dword ptr [rdi + 0x80]', 'vbroadcastss ymm1, dword ptr [rdi + 0x84]'],
                   extra=[f'memory {D + 0x280:#x} {struct.pack("<II", E30, ONE).hex()}', 'xcr0 7'], ymm=True))
    # The flags raised before the unmasked instruction stay; a flag set and then unmasked raises nothing by itself (Vol.3 19).
    ld = lambda v: [f'memory {D + 0x280:#x} {struct.pack("<I", v).hex()}']
    out.append(case('ldmxcsr-rekey', ['divss xmm1, xmm2', 'ldmxcsr dword ptr [rdi + 0x80]', 'addss xmm3, xmm3', 'movaps xmm1, xmm4', 'divss xmm1, xmm2'],
                    ['sse 1', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0', f'xmm 3 {ONE:#x} 0', f'xmm 4 {ONE:#x} 0'] + ld(0x1d84), ('fault', 19),
                    check=lambda vm: [] if int(vm['mxcsr'], 0) == 0x1d84 else [f"mxcsr {vm['mxcsr']}"]))
    out.append(case('ldmxcsr-flag-set-no-fault', ['ldmxcsr dword ptr [rdi + 0x80]', 'addss xmm3, xmm3'],
                    ['sse 1', f'xmm 3 {ONE:#x} 0'] + ld(0x1d84), None, check=lambda vm: [] if int(vm['mxcsr'], 0) == 0x1d84 else [f"mxcsr {vm['mxcsr']}"]))
    out.append(case('ldmxcsr-remask', ['ldmxcsr dword ptr [rdi + 0x80]', 'divss xmm1, xmm2', 'ldmxcsr dword ptr [rdi + 0x84]', 'movaps xmm1, xmm4', 'divss xmm1, xmm2'],
                    ['sse 1', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0', f'xmm 4 {ONE:#x} 0', f'memory {D + 0x280:#x} {struct.pack("<II", 0x1f80, 0x1d80).hex()}'], ('fault', 19),
                    check=lambda vm: [] if int(vm['mxcsr'], 0) == 0x1d84 else [f"mxcsr {vm['mxcsr']}"]))
    img = bytearray(512); img[0:2] = (0x37f).to_bytes(2, 'little'); img[24:28] = (0x1d80).to_bytes(4, 'little'); img[28:32] = (0xffff).to_bytes(4, 'little')
    out.append(case('fxrstor-unmasked-mxcsr', ['fxrstor [rsi]', f'mov eax, {ONE}', 'movd xmm1, eax', 'xorps xmm2, xmm2', 'divss xmm1, xmm2'],
                    ['sse 1', f'memory {D + 0x1000:#x} {img.hex()}'], ('fault', 19),
                    check=lambda vm: [] if int(vm['mxcsr'], 0) == 0x1d84 else [f"mxcsr {vm['mxcsr']}"]))
    out.append(case('vldmxcsr-unmasked', ['vldmxcsr dword ptr [rdi + 0x80]', 'vdivss xmm1, xmm1, xmm2'],
                    ['sse 1', 'xcr0 7', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0'] + ld(0x1d80), ('fault', 19),
                    check=lambda vm: [] if int(vm['mxcsr'], 0) == 0x1d84 else [f"mxcsr {vm['mxcsr']}"]))
    # Without CR4.OSXMMEXCPT: #UD, the flags set (11.5.3.2 step 2). At CPL 0, OSXMMEXCPT cleared first.
    cr4 = ['mov rax, cr4', 'btr rax, 10', 'mov cr4, rax']
    out.append(case('no-osxmmexcpt-ud', cr4 + ['divss xmm1, xmm2'], ['sse 1', f'mxcsr {MX & ~(ZE << 7):#x}', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0'],
                    ('fault', 6), cpl=0, check=lambda vm: ([] if int(vm['mxcsr'], 0) == (MX & ~(ZE << 7)) | ZE else [f"mxcsr {vm['mxcsr']}"]) +
                    ([] if xmm_of(vm, 1) == (ONE, 0) else ['xmm1 written'])))
    out.append(case('no-osxmmexcpt-masked-runs', cr4 + ['divss xmm1, xmm2'], ['sse 1', f'xmm 1 {ONE:#x} 0', 'xmm 2 0 0'], None, cpl=0,
                    check=lambda vm: [] if int(vm['mxcsr'], 0) == MX | ZE else [f"mxcsr {vm['mxcsr']}"]))
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
    one_order = c['check']
    c['check'] = lambda vm, f=one_order: f(vm) if int(vm['mxcsr'], 0) != (MX & ~(IE << 7)) | IE | OE | PE else f({**vm, 'mxcsr': hex((MX & ~(IE << 7)) | IE)})
    out.append(c)
    # XRSTOR re-keys as FXRSTOR does (the SSE component loads MXCSR).
    x = bytearray(832); x[24:28] = (0x1d80).to_bytes(4, 'little'); x[28:32] = (0xffff).to_bytes(4, 'little'); x[512:520] = (2).to_bytes(8, 'little')
    out.append(case('xrstor-unmasked-mxcsr', ['mov eax, 2', 'xor edx, edx', 'xrstor [rsi]', f'mov eax, {ONE}', 'movd xmm1, eax', 'xorps xmm2, xmm2', 'divss xmm1, xmm2'],
                    ['sse 1', 'xcr0 7', f'memory {D + 0x1000:#x} {x.hex()}'], ('fault', 19),
                    check=lambda vm: [] if int(vm['mxcsr'], 0) == 0x1d84 else [f"mxcsr {vm['mxcsr']}"]))
    # Review cases (independent I5c review): VCVTPS2PH's upper lanes one by one; FMA inf * 0 + QNaN (no IE: SDM Vol.1 Table
    # 14-17); VCVTPH2PS of a half denormal (no DE); VCVTPS2PH of a single denormal (DE, UE, PE); CVT(T)PS2PI mm, xmm read only
    # xmm's low two floats (SDM CVTPS2PI: the upper two raise nothing, masked or not).
    T20 = f32(2.0 ** -20)
    for tag, up, mx, fl, want in (('elem4', (T20, ONE, ONE, ONE), MX & ~(UE << 7), UE, 19), ('zero-elem5', (ONE, 0, ONE, ONE), MX & ~(UE << 7), 0, None),
                                  ('elem7', (ONE, ONE, ONE, T20), MX & ~(UE << 7), UE, 19)):
        out.append(one(f'rv-ph-upper-{tag}', 'vcvtps2ph xmm1, ymm2, 4', {1: lanes(ONE), 2: lanes(ONE, ONE, ONE, ONE)}, mx, fl, want, 1,
                       prelude=['vinsertf128 ymm2, ymm2, xmmword ptr [rdi + 0x80], 1'], extra=[f'memory {D + 0x280:#x} {struct.pack("<IIII", *up).hex()}', 'xcr0 7']))
    out.append(one('rv-fma-inf0-qnan-ie', 'vfmadd231ss xmm1, xmm2, xmm3', {1: lanes(QN), 2: lanes(INF), 3: lanes(0)}, MX & ~(IE << 7), 0, None, 1))
    out.append(one('rv-ph2ps-denormal-dm', 'vcvtph2ps xmm1, xmm2', {1: lanes(ONE), 2: lanes(0x3c000001)}, MX & ~(DE << 7), 0, None, 1))
    out.append(one('rv-ps2ph-denormal-pm', 'vcvtps2ph xmm1, xmm2, 4', {1: lanes(ONE), 2: lanes(1, ONE, ONE, ONE)}, MX & ~(PE << 7), DE | UE | PE, 19, 1))
    out.append(one('rv-ps2ph-denormal-dm', 'vcvtps2ph xmm1, xmm2, 4', {1: lanes(ONE), 2: lanes(1, ONE, ONE, ONE)}, MX & ~(DE << 7), DE, 19, 1))
    for tag, ins, v, mx in (('cvtps2pi-upper-pe', 'cvtps2pi mm0, xmm1', f32(1.5), MX & ~(PE << 7)), ('cvttps2pi-upper-ie', 'cvttps2pi mm0, xmm1', f32(3e9), MX & ~(IE << 7)),
                            ('cvtps2pi-upper-masked', 'cvtps2pi mm0, xmm1', f32(1.5), MX), ('cvttps2pi-upper-masked', 'cvttps2pi mm0, xmm1', f32(3e9), MX)):
        out.append(one(f'rv-{tag}', ins, {1: lanes(ONE, ONE, v, ONE)}, mx, 0, None, None))
    # x87 arithmetic leaves MXCSR alone and does not trip a SIMD check (its own FPSR wrapper).
    out.append(case('x87-between-simd', ['fninit', 'fld1', 'fldz', 'fdivp st(1), st(0)', 'addss xmm3, xmm3', 'fstp st(0)'],
                    ['sse 1', f'mxcsr {MX & ~(ZE << 7):#x}', f'xmm 3 {ONE:#x} 0'], None,
                    check=lambda vm: [] if int(vm['mxcsr'], 0) == MX & ~(ZE << 7) else [f"mxcsr {vm['mxcsr']}"]))
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
    if c['check'] and not problems:
        problems += c['check'](vm)
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True); ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int); ap.add_argument('--only'); ap.add_argument('--host'); ap.add_argument('--runtime')
    a = ap.parse_args()
    host, runtime = (a.host, a.runtime) if a.host else vm1.build()
    results, bad = [], 0
    for c in cases():
        if a.only and a.only not in c['name']: continue
        code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' for i, l in enumerate(c['lines'])), labels=True)
        offsets = [0] + [labels[f'l{i}'] for i in range(len(c['lines']) - 1)]
        work = a.out / 'work' / c['name']; work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(description(c, code))
        run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([str(a.max_block)] if a.max_block else []) +
                             vm1.CODE_SHAPE, capture_output=True, text=True)
        problems = [f'driver: {run.stderr.strip()[-300:]}'] if run.returncode else judge(c, json.loads(run.stdout), offsets)
        bad += bool(problems)
        results.append({'name': c['name'], 'ok': not problems, 'problems': problems})
        if problems: print('FAIL', c['name'], '; '.join(problems)[:400])
    (a.out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'{len(results)} cases, {len(results) - bad} as expected, {bad} not')
    sys.exit(1 if bad else 0)

if __name__ == '__main__': main()
