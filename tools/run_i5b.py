#!/usr/bin/env python3
"""I5b: FXSAVE/FXRSTOR, XSAVE/XRSTOR and XGETBV/XSETBV (boot-features plan, chunk I5b).

    tools/with-env python3 tools/run_i5b.py --fexc FEXC --out DIR [--only NAME] [--no-qemu]

`images`: CPL0 programs (natemu-smp-v1, `system`) run on QEMU system mode (the pinned Homebrew qemu-system-x86_64) with
the CPU model QCPU, whose CPUID leaf 0DH is the profile's (EAX = 7, EBX 576/832 by XCR0, ECX 832; leaf 0DH.1 EAX = 0;
leaf 0DH.2 = 256 bytes at 576), and on natemu at block sizes default and 1: final GPRs and the whole data region byte by
byte. x87 values are doubles (exact under I6's reduced precision); one case loads an 80-bit value with more significand
bits, where natemu's slot must be that value rounded to a double by FCW.RC (I6 deviation 2) and only there. QEMU writes
nothing into bytes 10..15 of an FXSAVE ST slot, natemu writes zeros (the SDM's reserved bytes): those 48 bytes are
compared only where the program pre-zeroed them (a declared difference; every other byte is compared).

`faults`: each case is judged by the SDM rules (Vol.2 FXSAVE, FXRSTOR, XSAVE, XRSTOR, XGETBV, XSETBV; Vol.1 13.7-13.8):
the exception (vector, error code, RIP of the instruction, CR2), and the state at it equal to natemu's own run of the
program cut before the instruction (GPRs, XMM, MXCSR, the x87 report, the data region: no partial update). Where the
case needs no paging mutation or CPL change QEMU runs it too and must raise the same exception (QEMU_DIFFERS lists the
cases it does not, with the reason). Choices recorded in `crates/runtime/src/execution/xsave.rs`: misalignment is #GP,
never #AC; #UD (no CR4.OSXSAVE) before #NM (CR0.TS); FXSAVE/FXRSTOR move XMM/MXCSR whatever CR4.OSFXSR; XINUSE all ones.
`mmio-xsave-*`: an XSAVE whose stores reach a device page stops before storing anything. `segment-*` (fexc series27):
CS/DS/ES/SS prefixes by Intel's class for a non-canonical address (`segment_cases`); QEMU never raises #SS(0) there.
"""
import argparse, json, pathlib, struct, subprocess, sys, time
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import run_slice3_vm as s3  # noqa: E402  (also puts tools/reference and tools/bench/smp on the path)
import ref, smp, run_slice1_vm as vm1  # noqa: E402

# QEMU's Haswell (11.1.2 TCG): leaf 0DH sub-leaves 0 and 2 and leaf 1 ECX equal the profile's; sub-leaf 1 also advertises
# XSAVEOPT (EAX bit 0), which no comparison here executes or reads: with `-xsaveopt` this QEMU loops in vmexit(-1) right after a
# MOV to CR4 that sets OSXSAVE (its `-d int` log), so the model keeps it.
QCPU = 'Haswell,-pcid,-hle,-rtm,-invpcid'
CODE, D, SIZE, STACK = 0x110000, 0x200000, 0x4000, 0x3e1800
A, B, C = D + 0x1000, D + 0x1400, D + 0x1800          # image areas (64-byte aligned), C crosses into D + 0x2000 for some
CR4_ON = ['mov rax, cr4', 'or rax, 0x40600', 'mov cr4, rax']            # OSFXSR | OSXMMEXCPT | OSXSAVE
XCR0 = lambda v: ['xor ecx, ecx', f'mov eax, {v}', 'xor edx, edx', 'xsetbv']
DOUBLES = [1.5, -2.25, 3.0e10, 3.141592653589793, 1e-300, -0.0, float('inf'), 2.0 ** -1060]
F, X = 0x000, 0x100                                    # data: doubles, XMM values; FCW at 0x80, MXCSR at 0x84


def data(seed=1):
    """The data region: 8 doubles, FCW 027FH (PC 53 bits) and MXCSR 3F80H|DE flag clear (RC down), 16 XMM values, the rest
    a pattern (so a byte an instruction must not write is visible)."""
    d = bytearray(smp.lcg_bytes(SIZE, seed))
    d[F:F + 64] = b''.join(struct.pack('<d', v) for v in DOUBLES)
    d[0x80:0x88] = struct.pack('<HHI', 0x027f, 0, 0x3f80)
    d[X:X + 256] = smp.lcg_bytes(256, seed + 7)
    return d


def prelude(xcr0=7, n=6):
    """CR4 controls, XCR0, then known x87 (n pushes: TOP 8 - n), MXCSR and XMM state."""
    return CR4_ON + XCR0(xcr0) + ['fninit', 'fldcw word ptr [rdi + 0x80]'] + \
        [f'fld qword ptr [rdi + {8 * k:#x}]' for k in range(n)] + ['ldmxcsr dword ptr [rdi + 0x84]'] + \
        [f'movdqu xmm{r}, xmmword ptr [rdi + {X + 16 * r:#x}]' for r in range(16)]


def f80(mant, se): return struct.pack('<QH', mant, se) + bytes(6)


def legacy_image(fcw=0x0b7f, fsw=0x4000 | 0x200 | 3 << 11 | 0x24, tags=0xb5, mxcsr=0x5fa1, st=None, xmm=b''):
    """A legacy region: FCW (RC up), FSW (TOP 3, C1 C3 and PE/ZE flags), abridged tags, MXCSR (RC down|up, flags), ST0..7,
    XMM0..15 (416 bytes)."""
    st = st or [f80(0xc000000000000000, 0x3fff), f80(0x8000000000000000, 0xc005), f80(0, 0), f80(0x8000000000000000, 0x7fff),
                f80(0xa000000000000000, 0x4010), f80(0xc000000000000000, 0x7fff), f80(0x8000000000000000, 0x3c01),
                f80(0x9000000000000000, 0xbffe)]
    head = struct.pack('<HHBBH', fcw, fsw, tags, 0, 0) + bytes(16) + struct.pack('<II', mxcsr, 0xffff)
    return head + b''.join(st) + (xmm or smp.lcg_bytes(256, 99))


def xsave_image(bv=7, comp=0, extra=b'', avx=None, **kw):
    hdr = struct.pack('<QQ', bv, comp) + (extra or bytes(48))
    return legacy_image(**kw) + bytes(96) + hdr + (avx if avx is not None else smp.lcg_bytes(256, 77))


def program(name, lines, d, cpl=0):
    code = ref.assemble('\n'.join(lines))
    return {'name': name, 'ncpu': 1, 'system': True, 'bodies': [{'addr': f'{CODE:#x}', 'code': code.hex()}],
            'cpus': [{'rip': f'{CODE:#x}', 'rflags': '0x0', 'cpl': cpl, 'gpr': ['0x0'] * 4 + [f'{STACK:#x}', '0x0', '0x0', f'{D:#x}'] + ['0x0'] * 8}],
            'memory': [{'addr': f'{D:#x}', 'data': bytes(d).hex()}, {'addr': f'{STACK - 0x1000:#x}', 'size': 0x1000, 'fill': 'zero', 'capture': False}]}


# XRSTOR with RFBM[2] = 1, RFBM[1] = 0 loads MXCSR (SDM Vol.2 XRSTOR, standard form); QEMU 11.1.2 does not: natemu's MXCSR
# must be the image's (5FA1H), where XSAVE stores it at B and STMXCSR at 1820H.
QEMU_MXCSR = [(0x1418, 4, struct.pack('<I', 0x5fa1)), (0x1820, 4, struct.pack('<I', 0x5fa1))]


def image_cases():
    out = []
    def case(name, lines, d=None, mask=()):
        out.append((program(name, lines, d if d is not None else data()), mask))
    # FXSAVE and FXSAVE64 of known state, into pattern-filled areas (every byte not written keeps its value).
    case('fxsave', prelude() + ['fxsave [rdi + 0x1000]', 'fxsave64 [rdi + 0x1200]'], mask=fxm(0x1000) + fxm(0x1200))
    case('fxsave-full-stack', prelude(n=8) + ['fxsave [rdi + 0x1000]'], mask=fxm(0x1000))
    case('fxsave-empty', CR4_ON + ['fninit', 'fxsave [rdi + 0x1000]'], mask=fxm(0x1000))
    # Linux's MXCSR_MASK probe (fpu__init_system_mxcsr): FXSAVE of a zeroed buffer, read MXCSR_MASK.
    d = data(); d[0x1000:0x1200] = bytes(0x200)
    case('mxcsr-mask-probe', CR4_ON + ['fxsave [rdi + 0x1000]', 'mov eax, dword ptr [rdi + 0x101c]'], d)
    # FXRSTOR of a crafted image, then everything out: FXSAVE, FNSTENV (full tag word), STMXCSR, the XMM registers.
    for tag, kw in (('image', {}), ('rc-near', {'fcw': 0x037f, 'mxcsr': 0x1f80}), ('rex-w', {})):
        d = data(); d[0x1000:0x11a0] = legacy_image(**kw)
        rstor = 'fxrstor64 [rdi + 0x1000]' if tag == 'rex-w' else 'fxrstor [rdi + 0x1000]'
        case(f'fxrstor-{tag}', prelude() + [rstor, 'fxsave [rdi + 0x1400]', 'fnstenv [rdi + 0x1600]', 'stmxcsr dword ptr [rdi + 0x1620]'] +
             [f'movdqu xmmword ptr [rdi + {0x1700 + 16 * r:#x}], xmm{r}' for r in range(16)], d, fxm(0x1400) + envm(0x1600))
    # An 80-bit value with more significand bits than a double (1 + 2^-60 + 2^-63, FCW.RC up): natemu holds it rounded to
    # 1 + 2^-52 (I6 deviation 2); QEMU keeps it. Only that slot's value differs.
    d = data(); st0 = f80(0x8000000000000009, 0x3fff)
    d[0x1000:0x11a0] = legacy_image(st=[st0] + [f80(0, 0)] * 7)
    case('fxrstor-precision', prelude() + ['fxrstor [rdi + 0x1000]', 'fxsave [rdi + 0x1400]'], d,
         fxm(0x1400) + [(0x1400 + 32, 10, f80(0x8000000000000800, 0x3fff)[:10])])
    # Round trip through FXSAVE/FXRSTOR with the state changed in between.
    case('fxsave-fxrstor-roundtrip', prelude() + ['fxsave [rdi + 0x1000]', 'fninit', 'fld1', 'pxor xmm3, xmm3', 'pcmpeqd xmm9, xmm9',
                                                  'fxrstor [rdi + 0x1000]', 'fxsave [rdi + 0x1400]'], mask=fxm(0x1000) + fxm(0x1400))
    # Linux's fpu__init_cpu_xstateregs and the XSETBV/XGETBV/CPUID.0DH sequences.
    seq = list(CR4_ON)
    for k, v in enumerate((1, 3, 7, 3, 1)):
        seq += XCR0(v) + ['xor ecx, ecx', 'xgetbv', f'mov dword ptr [rdi + {0x1000 + 32 * k:#x}], eax', f'mov dword ptr [rdi + {0x1004 + 32 * k:#x}], edx',
                          'mov eax, 0xd', 'xor ecx, ecx', 'cpuid', f'mov dword ptr [rdi + {0x1008 + 32 * k:#x}], eax',
                          f'mov dword ptr [rdi + {0x100c + 32 * k:#x}], ebx', f'mov dword ptr [rdi + {0x1010 + 32 * k:#x}], ecx',
                          'mov eax, 1', 'cpuid', 'and ecx, 0x1c000000', f'mov dword ptr [rdi + {0x1014 + 32 * k:#x}], ecx']
    case('xsetbv-xgetbv-cpuid', seq + ['xor ebx, ebx', 'mov rcx, 0x100000000', 'xgetbv'])   # ECX's upper half is ignored
    case('xgetbv-reset', CR4_ON + ['xor ecx, ecx', 'xgetbv'])               # XCR0 at reset: 1
    # XSAVE under every RFBM (XCR0 = 7), into a pattern-filled area whose header's XSTATE_BV is a pattern too.
    for rfbm in range(8):
        # RFBM = AVX without SSE: the SDM saves MXCSR and MXCSR_MASK (RFBM[1] or RFBM[2]); QEMU 11.1.2 does not (QEMU_MXCSR).
        qemu_mxcsr = [(0x1018, 8, struct.pack('<II', 0x3f80, 0xffff))] if rfbm & 6 == 4 else []
        case(f'xsave-rfbm{rfbm}', prelude() + [f'mov eax, {rfbm}', 'xor edx, edx', 'xsave [rdi + 0x1000]'], mask=fxm(0x1000, rfbm & 1) + qemu_mxcsr)
    case('xsave64-all-xcr0-3', prelude(xcr0=3) + ['mov eax, -1', 'mov edx, -1', 'xsave64 [rdi + 0x1000]'], mask=fxm(0x1000))
    # XRSTOR of crafted images (x87, SSE, AVX all differing from the prelude's state) under (XSTATE_BV, RFBM), then XSAVE of
    # everything and FNSTENV/STMXCSR.
    for bv, rfbm in ((7, 7), (0, 7), (1, 7), (2, 7), (4, 7), (5, 3), (7, 1), (7, 2), (7, 4), (3, 6), (6, 5), (0, 0), (1, 2)):
        d = data(); d[0x1000:0x1000 + 832] = xsave_image(bv); d[0x1400:0x1400 + 832] = bytes(832)
        case(f'xrstor-bv{bv}-rfbm{rfbm}', prelude() + [f'mov eax, {rfbm}', 'xor edx, edx', 'xrstor [rdi + 0x1000]', 'mov eax, 7',
                                                      'xsave [rdi + 0x1400]', 'fnstenv [rdi + 0x1800]', 'stmxcsr dword ptr [rdi + 0x1820]'], d,
             fxm(0x1400) + envm(0x1800) + (QEMU_MXCSR if rfbm & 6 == 4 else []))
    # Header bytes 24..63 are not checked by the standard form; XCR0 = 3 restores no AVX state; YMM_Hi128 survives FXRSTOR.
    d = data(); d[0x1000:0x1000 + 832] = xsave_image(7, extra=bytes(8) + b'\xff' * 40); d[0x1400:0x1400 + 832] = bytes(832)
    case('xrstor-header-tail-ignored', prelude() + ['mov eax, 7', 'xor edx, edx', 'xrstor [rdi + 0x1000]', 'xsave [rdi + 0x1400]'], d, fxm(0x1400))
    d = data(); d[0x1000:0x1000 + 832] = xsave_image(3); d[0x1400:0x1400 + 832] = bytes(832)
    case('xrstor-xcr0-3', prelude(xcr0=3) + ['mov eax, 7', 'xor edx, edx', 'xrstor [rdi + 0x1000]'] + XCR0(7) +
         ['mov eax, 7', 'xsave [rdi + 0x1400]'], d, fxm(0x1400))
    d = data(); d[0x1000:0x1000 + 832] = xsave_image(7); d[0x1400:0x1400 + 832] = bytes(832); d[0x1800:0x1a00] = bytes(0x200)
    case('xrstor-fxsave-fxrstor-xsave', prelude() + ['mov eax, 7', 'xor edx, edx', 'xrstor [rdi + 0x1000]', 'fxsave [rdi + 0x1800]', 'fninit',
                                                     'fxrstor [rdi + 0x1800]', 'xsave [rdi + 0x1400]'], d, fxm(0x1400) + fxm(0x1800))
    # The XSAVE -> XRSTOR round trip with everything changed between (AVX state from an XRSTOR first).
    d = data(); d[0x1800:0x1800 + 832] = xsave_image(7); d[0x1000:0x1000 + 832] = bytes(832); d[0x1400:0x1400 + 832] = bytes(832)
    d[0x1418:0x1420] = struct.pack('<II', 0x1f80, 0xffff)      # the AVX-only XRSTOR below loads this MXCSR (QEMU does not)
    case('xsave-xrstor-roundtrip', prelude() + ['mov eax, 7', 'xor edx, edx', 'xrstor [rdi + 0x1800]', 'xsave [rdi + 0x1000]', 'fninit',
                                                'pxor xmm0, xmm0', 'mov eax, 4', 'xrstor [rdi + 0x1400]', 'mov eax, 7', 'xrstor [rdi + 0x1000]',
                                                'xsave [rdi + 0x1400]'], d, fxm(0x1000) + fxm(0x1400))
    return out


# Fault cases: (name, lines (the last is the instruction under test), expected (vector, error, cr2) or ('stop', reason) or
# None (runs), setup (natemu description items), d (data), cpl3 (switch to CPL 3 before the instruction), qemu).
PF = lambda cr2, write, present=False, user=False: (14, (1 if present else 0) | (2 if write else 0) | (4 if user else 0), cr2)
GP, NM, UD, SS = (13, 0, 0), (7, 0, 0), (6, 0, 0), (12, 0, 0)
NONCANON = 0x0000_8000_0000_0000
GP_AT = lambda rip: (13, 0, rip, 0)
# QEMU 11.1.2 (TCG, system mode) raises #GP(0) for every non-canonical data address, never #SS(0): an RSP/RBP base with no
# override and an SS override alike (its controls are `segment-none-*`). Its #GP for CS/DS/ES on RSP/RBP agrees with Intel
# only because it never raises #SS; those cases are judged by the SDM rule.
QEMU_NO_SS = 'QEMU 11.1.2 raises #GP(0) for every non-canonical data address, never #SS(0) (Intel SDM Vol. 3A 4.5.1: #SS(0) for an SS-class reference)'
QEMU_DIFFERS = {
    'ts-xsave': 'QEMU 11.1.2 never raises #NM for XSAVE/XRSTOR under CR0.TS (SDM Vol.2 XSAVE, XRSTOR: #NM if CR0.TS = 1)',
    'ts-xrstor': 'as ts-xsave',
    'xrstor-mxcsr-reserved-sse': 'QEMU 11.1.2 loads MXCSR without the reserved-bit #GP(0) (SDM Vol.2 XRSTOR, FXRSTOR)',
    'xrstor-mxcsr-reserved-avx': 'as xrstor-mxcsr-reserved-sse (and it does not load MXCSR for RFBM = AVX alone)',
    'fxrstor-mxcsr-reserved': 'as xrstor-mxcsr-reserved-sse',
    'unadvertised-xsaveopt': "QEMU's model advertises XSAVEOPT (see QCPU), so it runs it",
}


def fault_cases():
    out = []
    def case(name, lines, want, setup=(), d=None, cpl3=False, qemu=True, prefix=None):
        out.append({'name': name, 'lines': lines, 'want': want, 'setup': list(setup), 'd': d if d is not None else data(), 'cpl3': cpl3,
                    'qemu': qemu and not setup and not cpl3, 'prefix': prefix if prefix is not None else prelude()})
    full = ['mov eax, 7', 'xor edx, edx']
    # Alignment: #GP(0) for FX* at 16, X* at 64 bytes, before the walk (the misaligned page is not even present).
    for insn, off in (('fxsave', 8), ('fxrstor', 8), ('fxsave64', 4), ('xsave', 0x10), ('xsave', 0x20), ('xrstor', 0x30), ('xrstor', 0x8), ('xsave64', 0x1)):
        case(f'align-{insn}-{off:#x}', full + [f'{insn} [rdi + {0x1000 + off:#x}]'], GP)
        case(f'align-{insn}-{off:#x}-notpresent', full + [f'{insn} [rdi + {0x1000 + off:#x}]'], GP, [f'unmap {A:#x}'])
    # #PF: the image across a page boundary at each offset, the second (or first) page not present or read-only. Nothing of
    # the state or memory changes. FXSAVE writes bytes 0..415, FXRSTOR reads them; XSAVE writes 0..415 (by RFBM), 512..519,
    # 576..831; XRSTOR reads the header, then what it restores.
    for off in (0x10, 0xa0, 0x190):
        at = D + 0x2000 - off
        for insn, write in (('fxsave', True), ('fxrstor', False)):
            d = data(); d[at - D:at - D + 416] = legacy_image()
            case(f'pf-{insn}-cross-{off:#x}', [f'{insn} [rdi + {at - D:#x}]'], PF(D + 0x2000, write), [f'unmap {D + 0x2000:#x}'], d)
            case(f'pf-{insn}-first-{off:#x}', [f'{insn} [rdi + {at - D:#x}]'], PF(at, write), [f'unmap {D + 0x1000:#x}'], d)
        case(f'pf-fxsave-readonly-{off:#x}', [f'fxsave [rdi + {at - D:#x}]'], PF(D + 0x2000, True, True), [f'readonly {D + 0x2000:#x}'])
        case(f'pf-fxrstor-readonly-{off:#x}', [f'fxrstor [rdi + {at - D:#x}]'], None, [f'readonly {D + 0x2000:#x}'],
             d=(lambda d: (d.__setitem__(slice(at - D, at - D + 416), legacy_image()), d)[1])(data()))
    for off in (0x40, 0x200, 0x240, 0x280, 0x300):
        at = D + 0x2000 - off
        img = xsave_image(7)
        for rfbm in (7, 1, 3):
            # XSAVE: the second page is written when the stores reach it (header at 512, AVX from 576 with RFBM[2]).
            reach = {7: 832, 1: 520, 3: 520}[rfbm]
            want = PF(D + 0x2000, True) if off < reach else None
            case(f'pf-xsave-rfbm{rfbm}-cross-{off:#x}', [f'mov eax, {rfbm}', 'xor edx, edx', f'xsave [rdi + {at - D:#x}]'], want,
                 [f'unmap {D + 0x2000:#x}'])
            d = data(); d[at - D:at - D + 832] = img
            # XRSTOR: reads 512..535 first, then the legacy part and AVX it restores.
            need = 832 if rfbm & 4 else 536
            want = PF(max(at + 512, D + 0x2000), False) if off < need else None     # the header is read first
            case(f'pf-xrstor-rfbm{rfbm}-cross-{off:#x}', [f'mov eax, {rfbm}', 'xor edx, edx', f'xrstor [rdi + {at - D:#x}]'], want,
                 [f'unmap {D + 0x2000:#x}'], d)
        case(f'pf-xsave-readonly-{off:#x}', full + [f'xsave [rdi + {at - D:#x}]'], PF(D + 0x2000, True, True), [f'readonly {D + 0x2000:#x}'])
        case(f'pf-xsave-first-{off:#x}', full + [f'xsave [rdi + {at - D:#x}]'], PF(at, True), [f'unmap {D + 0x1000:#x}'])
    # XRSTOR with RFBM = 0 and x87-only: the legacy page is read only for what it restores.
    for rfbm, bv, want in ((0, 1, None), (1, 0, None), (1, 1, PF(D + 0xe00, False)), (2, 0, PF(D + 0xe00 + 24, False))):
        d = data(); d[0x1000:0x1040] = struct.pack('<Q', bv) + bytes(56)
        case(f'pf-xrstor-rfbm{rfbm}-bv{bv}-legacy-absent', [f'mov eax, {rfbm}', 'xor edx, edx', 'xrstor [rdi + 0xe00]'], want,
             [f'unmap {D:#x}'], d, prefix=CR4_ON + XCR0(7))
    # Non-canonical operands: #GP(0), #SS(0) through RSP/RBP.
    for insn in ('fxsave', 'fxrstor', 'xsave', 'xrstor'):
        case(f'noncanonical-{insn}', full + [f'mov rbx, {NONCANON:#x}', f'{insn} [rbx]'], GP)
        # (natemu only. RSP: QEMU's stub cannot take an exception at CPL 0 on that RSP. Straddle: QEMU maps no page below
        # the canonical gap, so its #PF there says nothing of the order; natemu checks each accessed range's canonical form
        # before walking it, as I6's transactions, so the range reaching into the gap is #GP(0).)
        case(f'noncanonical-rsp-{insn}', full + [f'mov rsp, {NONCANON:#x}', f'{insn} [rsp]'], SS, qemu=False)
        case(f'straddle-{insn}', full + [f'mov rbx, {NONCANON - 0x40:#x}', f'{insn} [rbx]'], GP, qemu=False)
    # XRSTOR reads its header first, but a non-canonical base is #GP(0) even with the header at +512 canonical (and absent).
    case('noncanonical-base-xrstor', full + [f'mov rbx, {0xffff_7fff_ffff_fe00:#x}', 'xrstor [rbx]'], GP, qemu=False)
    # Gating: CR0.TS (#NM for all four), CR0.EM (#NM for FX* only: XSAVE runs), no CR4.OSXSAVE (#UD, XGETBV/XSETBV too).
    cr0 = lambda bits: ['mov rax, cr0', f'or rax, {bits}', 'mov cr0, rax']
    no_osxsave = ['mov rax, cr4', 'btr rax, 18', 'mov cr4, rax']
    d = data(); d[0x1000:0x1000 + 832] = xsave_image(7)
    for insn, ts, em in (('fxsave', NM, NM), ('fxrstor', NM, NM), ('xsave', NM, None), ('xrstor', NM, None)):
        case(f'ts-{insn}', full + cr0(8) + [f'{insn} [rdi + 0x1000]'], ts, d=d)
        case(f'em-{insn}', full + cr0(4) + [f'{insn} [rdi + 0x1000]'], em, d=d)
    for insn in ('xsave [rdi + 0x1000]', 'xrstor [rdi + 0x1000]', 'xgetbv', 'xsetbv'):
        tag = insn.split()[0]
        case(f'no-osxsave-{tag}', full + no_osxsave + ['xor ecx, ecx', insn], UD, d=d)
    case('ts-no-osxsave-xsave', full + no_osxsave + cr0(8) + ['xsave [rdi + 0x1000]'], UD, d=d)
    case('ts-no-osxsave-xrstor', full + no_osxsave + cr0(8) + ['xrstor [rdi + 0x1000]'], UD, d=d)
    case('ts-xgetbv-runs', cr0(8) + ['xor ecx, ecx', 'xgetbv'], None)
    case('no-osfxsr-fxsave-runs', ['mov rax, cr4', 'btr rax, 9', 'mov cr4, rax', 'fxsave [rdi + 0x1000]'], None)
    case('lock-fxsave', ['.byte 0xf0, 0x0f, 0xae, 0x07'], UD)
    case('lock-xsetbv', ['xor ecx, ecx', 'mov eax, 7', '.byte 0xf0, 0x0f, 0x01, 0xd1'], UD)
    # Forms the profile does not advertise: #UD.
    for tag, insn in (('xsaveopt', 'xsaveopt [rdi + 0x1000]'), ('xsavec', 'xsavec [rdi + 0x1000]'), ('xsaves', 'xsaves [rdi + 0x1000]'),
                      ('xrstors', 'xrstors [rdi + 0x1000]'), ('clwb', 'clwb [rdi + 0x1000]')):
        case(f'unadvertised-{tag}', full + [insn], UD)
    # XSETBV/XGETBV #GP: ECX != 0 (XGETBV ECX = 1: XGETBV1 not advertised), bit 0 clear, XCR0[2:1] = 10b, unsupported bits,
    # CPL 3 (XSETBV only).
    for tag, ecx, edx, eax in (('ecx1', 1, 0, 7), ('ecx2', 2, 0, 7), ('x87-clear', 0, 0, 6), ('zero', 0, 0, 0), ('avx-no-sse', 0, 0, 5),
                               ('only-avx-sse', 0, 0, 4), ('bit3', 0, 0, 0xf), ('bit9', 0, 0, 0x207), ('edx', 0, 1, 7)):
        case(f'xsetbv-{tag}', [f'mov ecx, {ecx}', f'mov edx, {edx}', f'mov eax, {eax}', 'xsetbv'], GP)
    case('xgetbv-ecx1', ['mov ecx, 1', 'xgetbv'], GP)
    case('xsetbv-cpl3', ['xor ecx, ecx', 'mov eax, 3', 'xor edx, edx', 'xsetbv'], GP, cpl3=True)
    case('xgetbv-cpl3-runs', ['xor ecx, ecx', 'xgetbv'], None, cpl3=True)
    case('xsave-cpl3-runs', full + ['xsave [rdi + 0x1000]'], None, cpl3=True)
    # XRSTOR's header and MXCSR #GP; and what is not checked.
    for tag, bv, comp, extra, want in (('bv-bit3', 0xf, 0, b'', GP), ('bv-not-in-xcr0', 7, 0, b'', GP), ('compacted', 7, 1 << 63, b'', GP),
                                       ('compacted-xcomp', 3, 0x8000000000000003, b'', GP), ('xcomp-low', 7, 1, b'', GP),
                                       ('reserved-16', 7, 0, b'\x01' + bytes(47), GP), ('reserved-23', 7, 0, bytes(7) + b'\x80' + bytes(40), GP),
                                       ('reserved-24', 7, 0, bytes(8) + b'\x01' + bytes(39), None), ('bv-bit63', 1 << 63 | 7, 0, b'', GP)):
        d = data(); d[0x1000:0x1000 + 832] = xsave_image(bv, comp, extra)
        case(f'xrstor-{tag}', ['mov eax, 7', 'xor edx, edx', 'xrstor [rdi + 0x1000]'], want, d=d,
             prefix=prelude(xcr0=3 if tag == 'bv-not-in-xcr0' else 7))
    for tag, rfbm, want in (('sse', 2, GP), ('avx', 4, GP), ('x87-only', 1, None), ('none', 0, None)):
        d = data(); d[0x1000:0x1000 + 832] = xsave_image(0, mxcsr=0x11f80)
        case(f'xrstor-mxcsr-reserved-{tag}', [f'mov eax, {rfbm}', 'xor edx, edx', 'xrstor [rdi + 0x1000]'], want, d=d)
    d = data(); d[0x1000:0x11a0] = legacy_image(mxcsr=0x21f80)
    case('fxrstor-mxcsr-reserved', ['fxrstor [rdi + 0x1000]'], GP, d=d)
    # Refusals (unmasked exceptions are not built): before any effect, after every fault check.
    for tag, kw in (('fcw', {'fcw': 0x037e}), ('mxcsr', {'mxcsr': 0x1f00})):
        d = data(); d[0x1000:0x11a0] = legacy_image(**kw)
        case(f'fxrstor-unmasked-{tag}', ['fxrstor [rdi + 0x1000]'], ('stop', 'unsupported_system'), d=d, qemu=False)
        d = data(); d[0x1000:0x1000 + 832] = xsave_image(7, **kw)
        case(f'xrstor-unmasked-{tag}', ['mov eax, 7', 'xor edx, edx', 'xrstor [rdi + 0x1000]'], ('stop', 'unsupported_system'), d=d, qemu=False)
    d = data(); d[0x1000:0x1000 + 832] = xsave_image(6, fcw=0x037e)          # x87 not restored: the FCW is not loaded
    case('xrstor-unmasked-fcw-not-restored', ['mov eax, 7', 'xor edx, edx', 'xrstor [rdi + 0x1000]'], None, d=d)
    # M3: an XSAVE whose stores reach a device page stops `unsupported_mmio` before writing any byte (the RAM part included;
    # the page after the data region maps a device; such a space is not an identity one). An area that does not reach it runs.
    dev = [f'mmio {D + SIZE:#x} 0x90000000']
    for off, rfbm, want in ((0x240, 7, ('stop', 'unsupported_mmio')), (0x100, 1, ('stop', 'unsupported_mmio')), (0x340, 1, None)):
        case(f'mmio-xsave-rfbm{rfbm}-{off:#x}', [f'mov eax, {rfbm}', 'xor edx, edx', f'xsave [rdi + {SIZE - off:#x}]'], want, dev)
    # #AC is never raised: CPL 3 with CR0.AM and RFLAGS.AC, a misaligned operand is #GP(0); an aligned one runs.
    for insn, off, want in (('fxsave', 8, GP), ('fxsave', 0, None), ('xsave', 0x20, GP), ('xrstor', 0x8, GP)):
        d = data(); d[0x1000:0x1000 + 832] = xsave_image(7)
        case(f'ac-{insn}-{off:#x}', full + [f'{insn} [rdi + {0x1000 + off:#x}]'], want, d=d, cpl3='ac')
    return out + segment_cases()


# Segment prefixes in 64-bit mode (CS/DS/ES/SS add no base): the reference's class decides a non-canonical address's
# exception, by Intel's rule (the presented CPU is GenuineIntel; SDM Vol. 3A 4.5.1): #SS(0) for an SS override whatever
# the base, #GP(0) for CS/DS/ES even on an RSP/RBP base. fexc's forms (load, store, read-modify-write, LOCK, BT*, Linux's
# `ds btr` from its SMP alternatives) and the host's (LDMXCSR, FXSAVE, XSAVE; a BT* form under alignment checking at CPL 3),
# canonical (runs) and not. The RSP base is [rsp + rbx] with RSP itself canonical, so QEMU can deliver the exception.
SEGMENT_FORMS = [('load', 'mov rax, qword ptr {s}:[{b}]'), ('store', 'mov qword ptr {s}:[{b}], rcx'), ('rmw', 'add qword ptr {s}:[{b}], 1'),
                 ('lock', 'lock add qword ptr {s}:[{b}], rcx'), ('btr', 'btr qword ptr {s}:[{b}], rdx'), ('lock-bts', 'lock bts qword ptr {s}:[{b}], 5'),
                 ('ldmxcsr', 'ldmxcsr dword ptr {s}:[{b}]'), ('fxsave', 'fxsave {s}:[{b}]'), ('xsave', 'xsave {s}:[{b}]')]
SEGMENT_BASES = [('ds', 'rbp', GP), ('es', 'rbp', GP), ('cs', 'rbp', GP), ('ds', 'rsp + rbx', GP), ('ss', 'rbx', SS), ('ss', 'rsi + 8', SS),
                 ('none', 'rbp', SS), ('none', 'rsp + rbx', SS), ('none', 'rbx', GP)]      # the defaults, as controls


def operand(line, s, b): return line.replace('{s}:', '' if s == 'none' else '{s}:').format(s=s, b=b)


def segment_cases():
    out = []
    def setup(b, target):
        if b == 'rsp + rbx': return [f'mov rbx, {(target - STACK) % 2 ** 64:#x}']
        reg = b.split()[0]
        return [f'mov {reg}, {(target - (8 if "+" in b else 0)) % 2 ** 64:#x}']
    for s, b, nc in SEGMENT_BASES:
        for name, line in SEGMENT_FORMS:
            for tname, target, want in (('canonical', D + 0x1000, None), ('noncanonical', NONCANON, nc), ('straddle', NONCANON - 2, nc)):
                # A straddle: not a split lock, not BT* (its unit), not FX*/X* (misaligned: #GP first); natemu checks the whole
                # range's canonical form before any walk, QEMU (no page below it) takes #PF first: natemu only.
                if tname == 'straddle' and name in ('lock', 'lock-bts', 'btr', 'fxsave', 'xsave'): continue
                lines = ['mov eax, 7', 'xor edx, edx', 'mov ecx, 0x1f80', 'mov dword ptr [rdi + 0x1000], ecx'] + setup(b, target) + \
                        (['mov edx, 0x45'] if name == 'btr' else []) + [operand(line, s, b)]
                out.append({'name': f'segment-{s}-{b.replace(" + ", "-")}-{name}-{tname}', 'lines': lines, 'want': want, 'setup': [], 'd': data(),
                            'cpl3': False, 'qemu': tname != 'straddle', 'prefix': prelude(), 'qemu_differs_if': QEMU_NO_SS if want == SS else None})
        # BT* under alignment checking (CPL 3, the host's BT_ALIGNED cut): #AC when misaligned and canonical, the class's
        # exception when not canonical, the stepped instruction when aligned.
        if b == 'rsp + rbx': continue
        for tname, target, want in (('aligned', D + 0x1000, None), ('misaligned', D + 0x1004, (17, 0, 0)), ('noncanonical', NONCANON, nc)):
            out.append({'name': f'segment-{s}-{b.replace(" + ", "-")}-ac-bts-{tname}', 'lines': setup(b, target) + [operand('bts qword ptr {s}:[{b}], 5', s, b)],
                        'want': want, 'setup': [], 'd': data(), 'cpl3': 'ac', 'qemu': False, 'prefix': prelude()})
    # Linux 6.12's `ds btr %rdx, 0x540(%rbp)` (its SMP alternatives' DS in place of LOCK on one CPU), as the kernel has it.
    for tname, rbp, want in (('canonical', D + 0x1000 - 0x540, None), ('noncanonical', NONCANON - 0x540, GP)):
        out.append({'name': f'segment-linux-ds-btr-{tname}', 'lines': [f'mov rbp, {rbp % 2 ** 64:#x}', 'mov edx, 0x45',
                    '.byte 0x3e, 0x48, 0x0f, 0xb3, 0x95, 0x40, 0x05, 0x00, 0x00'], 'want': want, 'setup': [], 'd': data(), 'cpl3': False,
                    'qemu': True, 'prefix': prelude()})
    return out


def description(prog, setup=(), switch=None, rflags=0):
    lines = [*([] if any(x.startswith('mmio') for x in setup) else ['identity']), f"body {CODE:#x} {prog['bodies'][0]['code']}", *(f"memory {smp.num(r['addr']):#x} {smp.region_bytes(r).hex()}" for r in prog['memory']),
             *setup, 'cpu',
             f'rip {CODE:#x}', f'rflags {rflags:#x}', 'cpl 0', 'gpr ' + ' '.join(prog['cpus'][0]['gpr'])]
    if switch is not None: lines.append(f'switch {switch:#x} 3')
    return '\n'.join(lines) + '\n'


def natemu(a, host, runtime, text, work, limit=None):
    for attempt in range(2):     # concurrent hv_vm_create (another agent's VMs) fails once with 0xfae94007
        rc, out, err = s3.driver(host, runtime, a.fexc, text, work, [limit] if limit else [])
        if rc == 0 or 'fae94007' not in err: break
        time.sleep(2)
    return rc, out, err


def qemu(prog, stub, work, timeout=60):
    img = smp.build_image(prog, stub)
    work.mkdir(parents=True, exist_ok=True); p, dbg = work / 'image.bin', work / 'dbg.out'
    p.write_bytes(img); dbg.unlink(missing_ok=True)
    try:
        r = subprocess.run(smp.qemu_args(1, 'multi', dbg, p, QCPU), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        return None
    return smp.parse_dump(prog, dbg.read_bytes()) if rc == 1 else None


def fxm(off, x87=True):
    """The bytes of a legacy region at `off` QEMU and natemu legitimately differ in: FOP, FIP, FCS, FDP, FDS (QEMU tracks
    them; natemu stores 0, I6 deviation 8) and each ST slot's reserved bytes 10..15 (QEMU leaves them, natemu writes 0)."""
    return [(off + 6, 18)] + [(off + 32 + 16 * k + 10, 6) for k in range(8)] if x87 else []


def envm(off): return [(off + 12, 16)]          # FNSTENV's FIP, FCS, FOP, FDP, FDS (28-byte image)


def masked(state, mask, keep):
    """`state` with the masked byte ranges of the data region replaced from `keep`."""
    data = bytearray(state['memory'][0]['data'])
    for off, n, *_ in mask: data[off:off + n] = keep[off:off + n]
    return {**state, 'memory': [{**state['memory'][0], 'data': bytes(data)}]}


def run_images(a, host, runtime, stub, results):
    bad = 0
    for prog, mask in image_cases():
        if a.only and a.only not in prog['name']: continue
        diffs = []
        want = None if a.no_qemu else qemu(prog, stub, a.out / 'qemu' / prog['name'])
        if want is None and not a.no_qemu: diffs.append('QEMU run failed')
        for limit in (None, 1):
            rc, out, err = natemu(a, host, runtime, description(prog), a.out / 'work' / f"{prog['name']}{'-1' if limit else ''}", limit)
            if rc: diffs.append(f'driver exit {rc}: {err.strip()[-300:]}'); continue
            cpu = out['cpus'][0]
            end = CODE + len(prog['bodies'][0]['code']) // 2
            if cpu['stop'] != 'reached' or smp.num(cpu['rip']) != end:
                diffs.append(f"stopped {cpu['stop']} {cpu['reason']} {cpu.get('fault')} at {cpu['rip']}"); continue
            diffs += s3.per_vcpu_checks(cpu)
            got = s3.vm_state(prog, out)
            if want:
                w = masked(want, mask, got['memory'][0]['data'])
                diffs += [f"max-block {limit}: {x}" for x in smp.compare(prog, got, {**w, 'cpus': [{**w['cpus'][0], 'fault': None}]})]
            for off, n, *value in mask:     # natemu's: 0 (pointers, reserved bytes), or the value I6 deviation 2 gives
                if got['memory'][0]['data'][off:off + n] != (value[0] if value else bytes(n)):
                    diffs.append(f"natemu bytes at +{off:#x}: {got['memory'][0]['data'][off:off + n].hex()}")
        bad += bool(diffs)
        results.append({'group': 'images', 'name': prog['name'], 'ok': not diffs, 'diffs': diffs})
        print(f"{prog['name']}: {'ok' if not diffs else 'FAIL'}", *diffs[:6], sep='\n  ', flush=True)
    return bad


def run_faults(a, host, runtime, stub, results):
    bad = 0
    for c in fault_cases():
        if a.only and a.only not in c['name']: continue
        problems = []
        ac = c['cpl3'] == 'ac'
        pre = list(c['prefix']) + (['mov rax, cr0', 'bts rax, 18', 'mov cr0, rax'] if ac else []) + (['nop'] if c['cpl3'] else [])
        body = pre + c['lines'][:-1]
        cut = ref.assemble('\n'.join(body)) if body else b''
        prog = program(c['name'], body + c['lines'][-1:], c['d'])
        at = CODE + len(cut)
        switch = CODE + len(ref.assemble('\n'.join(pre))) - 1 if c['cpl3'] else None
        rflags = 0x40000 if ac else 0
        pre_prog = {**prog, 'bodies': [{'addr': f'{CODE:#x}', 'code': cut.hex()}]}
        for limit in (None, 1):
            tag = f"{c['name']}{'-1' if limit else ''}"
            rc, out, err = natemu(a, host, runtime, description(prog, c['setup'], switch, rflags), a.out / 'work' / tag, limit)
            rc0, out0, err0 = natemu(a, host, runtime, description(pre_prog, c['setup'], switch, rflags), a.out / 'work' / f'{tag}-prefix', limit)
            if rc or rc0: problems.append(f'driver exit {rc}/{rc0}: {(err or err0).strip()[-300:]}'); continue
            cpu, cpu0 = out['cpus'][0], out0['cpus'][0]
            if cpu0['stop'] != 'reached' or smp.num(cpu0['rip']) != at:
                problems.append(f"prefix stopped {cpu0['stop']} {cpu0['reason']} {cpu0.get('fault')} at {cpu0['rip']}"); continue
            want = c['want']
            if want is None:
                if cpu['stop'] != 'reached': problems.append(f"stopped {cpu['stop']} {cpu['reason']} {cpu.get('fault')} at {cpu['rip']}")
                continue
            if want[0] == 'stop':
                ok = cpu['stop'] == 'resident' and cpu['reason'].startswith(want[1]) and smp.num(cpu['rip']) == at
                if not ok: problems.append(f"stop {cpu['stop']} {cpu['reason']} at {cpu['rip']}, want {want[1]} at {at:#x}")
            else:
                got = s3.vm_fault(cpu)
                exp = (want[0], want[1], at, want[2] if want[0] == 14 else 0)
                if got != exp: problems.append(f"fault {got} ({cpu['stop']} {cpu['reason']}), want {exp}")
            for k in ('gpr', 'xmm', 'mxcsr', 'x87'):
                if cpu.get(k) != cpu0.get(k): problems.append(f'{k} changed at the fault: {cpu.get(k)} != {cpu0.get(k)}')
            m, m0 = (s3.vm_state(p, o)['memory'][0]['data'] for p, o in ((prog, out), (pre_prog, out0)))
            if m != m0:
                j = next(k for k in range(len(m)) if m[k] != m0[k]); problems.append(f'memory written at +{j:#x}')
        if c['qemu'] and not a.no_qemu and c['want'] is not None and c['want'][0] != 'stop':
            full = qemu(prog, stub, a.out / 'qemu' / c['name'], timeout=30)
            qf = full and full['cpus'][0].get('fault')
            exp = (c['want'][0], c['want'][1], at, c['want'][2] if c['want'][0] == 14 else 0)
            differs = QEMU_DIFFERS.get(c['name']) or c.get('qemu_differs_if')
            if qf != exp:
                if differs and (c['name'] in QEMU_DIFFERS or qf == GP_AT(at)): c['qemu_differs'] = f'{qf}: {differs}'
                else: problems.append(f'QEMU raises {qf}, the SDM rule {exp}')
            elif differs: problems.append('QEMU_DIFFERS lists it but QEMU agrees')
        elif c['qemu'] and not a.no_qemu and c['want'] is None:
            full = qemu(prog, stub, a.out / 'qemu' / c['name'], timeout=30)
            if full is None or full['cpus'][0].get('fault') is not None:
                problems.append(f"QEMU: {full and full['cpus'][0].get('fault')} (the SDM rule: no fault)")
        bad += bool(problems)
        results.append({'group': 'faults', 'name': c['name'], 'ok': not problems, 'problems': problems, 'qemu_differs': c.get('qemu_differs')})
        print(f"{c['name']}: {'ok' if not problems else 'FAIL'}" + (f" (QEMU differs: {c['qemu_differs']})" if c.get('qemu_differs') else ''),
              *problems[:6], sep='\n  ', flush=True)
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True); ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only'); ap.add_argument('--no-qemu', action='store_true'); ap.add_argument('--no-build', action='store_true')
    ap.add_argument('--group', choices=('images', 'faults'))
    a = ap.parse_args()
    host, runtime = (ROOT / 'target/release/natemu-host', ROOT / 'target/aarch64-unknown-none/release/natemu-runtime') if a.no_build else vm1.build()
    stub = smp.build_stub(a.out / 'qemu-build')
    results = []
    bad = (run_images(a, host, runtime, stub, results) if a.group != 'faults' else 0) + \
          (run_faults(a, host, runtime, stub, results) if a.group != 'images' else 0)
    (a.out / 'results.json').write_text(json.dumps(results, indent=1, default=str) + '\n')
    print(f'{len(results)} cases, {len(results) - bad} as expected, {bad} not')
    sys.exit(1 if bad else 0)


if __name__ == '__main__': main()
