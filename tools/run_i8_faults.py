#!/usr/bin/env python3
"""Precise faults, gating and refusals of the VEX AVX/AVX2/FMA/F16C forms (boot-features plan, chunk I8), judged by the SDM
rules below and against natemu's own run of the same program cut before the instruction (no QEMU run: QEMU user mode cannot
set CR0/CR4/XCR0 and its fault state lacks the vector registers).

    tools/with-env python3 tools/run_i8_faults.py --fexc FEXC --out DIR [--max-block N] [--only SUBSTR]

Each case: a prelude (YMM0..15's upper halves loaded by VINSERTI128 from the data, so they are not zero; then any control
changes at CPL 0), one VEX instruction, then instructions that must not run. Expected (SDM Vol.2 2.4/2.5/2.8 and each
instruction's exception table; Vol.3 6.15):
  - #UD without CR4.OSXSAVE or with XCR0[2:1] != 11b, then #NM with CR0.TS, before anything else; CR0.EM and CR4.OSFXSR do
    not gate VEX forms; BMI1/BMI2 (VEX general-register forms) are not gated; a 66, F2, F3 or REX prefix before VEX, or
    LOCK, is #UD whatever the gating; XSETBV that clears XCR0[2] makes the next VEX form #UD even in code already run;
  - #GP(0) for VMOVAPS/VMOVAPD/VMOVDQA/VMOVNTPS/VMOVNTPD/VMOVNTDQ/VMOVNTDQA whose operand is not aligned to its size (16 or
    32 bytes), before any page walk; no alignment requirement for any other VEX form (exception class 4);
  - #GP(0), or #SS(0) through RSP, for a non-canonical operand (any byte);
  - #PF with CR2 = the first faulting byte's page in address order (a 32-byte access crossing into a not-present or
    read-only second page: CR2 = that page), error P | W | U;
  - refusals (typed, before any effect): VMASKMOV/VPMASKMOV (FEX's element loop), the gathers, VCVTPS2PH to memory (its
    conversion's MXCSR flags would be committed before a faulting store); an encoding x86 makes #UD is refused under the
    gating too; VLDMXCSR/VSTMXCSR run LDMXCSR/STMXCSR's transaction under the AVX gating;
  - the YMM upper halves exact at a fault right after register-only vector instructions (they stay cached there);
  - HADD/HSUB with two QNaNs: the SDM's operand order (HADD high element first) with the first-source rule.
At a fault: RIP the instruction, and the general registers, XMM, the YMM upper halves, MXCSR and the data region exactly as
in the run cut before the instruction (nothing committed: a crossing store wrote no byte). A case without a fault reaches its
`hlt`.
"""
import argparse, json, pathlib, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import run_slice1_vm as vm1, ref
from run_i5a_faults import D, CODE, XMM, GPR0, NONCANON, PRELUDE, AFTER

YMMH = [f'vinserti128 ymm{i}, ymm{i}, xmmword ptr [{D + 0x600 + 16 * i}], 1' for i in range(16)]

# (name, instruction, bytes accessed, alignment required (0, 16 or 32))
LOADS = [('vmovaps-y', 'vmovaps ymm1, ymmword ptr [{a}]', 32, 32), ('vmovdqa-y', 'vmovdqa ymm2, ymmword ptr [{a}]', 32, 32),
         ('vmovapd-x', 'vmovapd xmm1, xmmword ptr [{a}]', 16, 16), ('vmovdqa-x', 'vmovdqa xmm3, xmmword ptr [{a}]', 16, 16),
         ('vmovntdqa-y', 'vmovntdqa ymm4, ymmword ptr [{a}]', 32, 32), ('vmovntdqa-x', 'vmovntdqa xmm4, xmmword ptr [{a}]', 16, 16),
         ('vmovups-y', 'vmovups ymm1, ymmword ptr [{a}]', 32, 0), ('vmovdqu-y', 'vmovdqu ymm3, ymmword ptr [{a}]', 32, 0),
         ('vmovdqu-x', 'vmovdqu xmm3, xmmword ptr [{a}]', 16, 0), ('vlddqu-y', 'vlddqu ymm3, ymmword ptr [{a}]', 32, 0),
         ('vaddps-y', 'vaddps ymm4, ymm5, ymmword ptr [{a}]', 32, 0), ('vaddps-x', 'vaddps xmm4, xmm5, xmmword ptr [{a}]', 16, 0),
         ('vpcmpeqb-y', 'vpcmpeqb ymm0, ymm7, ymmword ptr [{a}]', 32, 0), ('vpshufb-y', 'vpshufb ymm5, ymm6, ymmword ptr [{a}]', 32, 0),
         ('vptest-y', 'vptest ymm7, ymmword ptr [{a}]', 32, 0), ('vfmadd231ps-y', 'vfmadd231ps ymm8, ymm9, ymmword ptr [{a}]', 32, 0),
         ('vpermq-y', 'vpermq ymm9, ymmword ptr [{a}], 27', 32, 0), ('vroundpd-y', 'vroundpd ymm10, ymmword ptr [{a}], 1', 32, 0),
         ('vpbroadcastb', 'vpbroadcastb ymm7, byte ptr [{a}]', 1, 0), ('vpbroadcastd', 'vpbroadcastd ymm7, dword ptr [{a}]', 4, 0),
         ('vbroadcastsd', 'vbroadcastsd ymm7, qword ptr [{a}]', 8, 0), ('vbroadcastf128', 'vbroadcastf128 ymm7, xmmword ptr [{a}]', 16, 0),
         ('vbroadcasti128', 'vbroadcasti128 ymm7, xmmword ptr [{a}]', 16, 0), ('vinserti128', 'vinserti128 ymm8, ymm9, xmmword ptr [{a}], 1', 16, 0),
         ('vpmovzxbw-y', 'vpmovzxbw ymm1, xmmword ptr [{a}]', 16, 0), ('vcvtph2ps-y', 'vcvtph2ps ymm1, xmmword ptr [{a}]', 16, 0),
         ('vmovss', 'vmovss xmm1, dword ptr [{a}]', 4, 0), ('vmovq', 'vmovq xmm1, qword ptr [{a}]', 8, 0),
         ('vpinsrd', 'vpinsrd xmm1, xmm2, dword ptr [{a}], 2', 4, 0), ('vmovhps', 'vmovhps xmm1, xmm2, qword ptr [{a}]', 8, 0),
         ('vcvtsi2sd', 'vcvtsi2sd xmm1, xmm2, qword ptr [{a}]', 8, 0), ('vcomiss', 'vcomiss xmm1, dword ptr [{a}]', 4, 0),
         ('vfmadd213ss', 'vfmadd213ss xmm1, xmm2, dword ptr [{a}]', 4, 0), ('vcmpps-y', 'vcmpps ymm1, ymm2, ymmword ptr [{a}], 30', 32, 0)]
STORES = [('vmovaps-y-st', 'vmovaps ymmword ptr [{a}], ymm1', 32, 32), ('vmovdqa-y-st', 'vmovdqa ymmword ptr [{a}], ymm2', 32, 32),
          ('vmovntps-y-st', 'vmovntps ymmword ptr [{a}], ymm3', 32, 32), ('vmovntdq-y-st', 'vmovntdq ymmword ptr [{a}], ymm3', 32, 32),
          ('vmovdqa-x-st', 'vmovdqa xmmword ptr [{a}], xmm2', 16, 16), ('vmovntpd-x-st', 'vmovntpd xmmword ptr [{a}], xmm2', 16, 16),
          ('vmovups-y-st', 'vmovups ymmword ptr [{a}], ymm1', 32, 0), ('vmovdqu-y-st', 'vmovdqu ymmword ptr [{a}], ymm4', 32, 0),
          ('vmovdqu-x-st', 'vmovdqu xmmword ptr [{a}], xmm4', 16, 0), ('vextracti128-st', 'vextracti128 xmmword ptr [{a}], ymm5, 1', 16, 0),
          ('vextractf128-st', 'vextractf128 xmmword ptr [{a}], ymm5, 1', 16, 0),
          ('vmovss-st', 'vmovss dword ptr [{a}], xmm5', 4, 0), ('vmovq-st', 'vmovq qword ptr [{a}], xmm8', 8, 0),
          ('vpextrd-st', 'vpextrd dword ptr [{a}], xmm9, 2', 4, 0), ('vmovhps-st', 'vmovhps qword ptr [{a}], xmm7', 8, 0),
          ('vpextrb-st', 'vpextrb byte ptr [{a}], xmm9, 7', 1, 0)]


def case(name, insn, setup=(), want=None, regs=None, prelude=(), cpl=3, xcr0=7, ymmh=True, at=None, lines=None, sse=True):
    pre = (YMMH if ymmh and xcr0 == 7 else []) + list(prelude) + PRELUDE
    return {'name': name, 'pre': pre, 'lines': lines or [insn], 'setup': list(setup), 'want': want, 'regs': {**GPR0, **(regs or {})},
            'cpl': cpl, 'xcr0': xcr0, 'insn': insn, 'at': at, 'sse': sse}


def cases():
    out = []
    PF = lambda cr2, write, present: [14, cr2, (1 if present else 0) | (2 if write else 0) | 4]
    GP, SS, UD, NM = [13, 0, 0], [12, 0, 0], [6, 0, 0], [7, 0, 0]
    for write, forms in ((False, LOADS), (True, STORES)):
        for name, text, n, align in forms:
            a = D + 0x1020
            out.append(case(f'{name}-notpresent', text.format(a=a), [f'unmap {D + 0x1000:#x}'], PF(a, write, False)))
            if write:
                out.append(case(f'{name}-readonly', text.format(a=a), [f'readonly {D + 0x1000:#x}'], PF(a, True, True)))
            if n > 1:
                for c in sorted({D + 0x1000 - n // 2, D + 0x1000 - 1, D + 0x1000 - n + 1}):
                    k = f'{name}-cross{D + 0x1000 - c}'
                    if align:
                        out.append(case(f'{k}-gp', text.format(a=c), [f'unmap {D + 0x1000:#x}'], GP))
                        continue
                    out.append(case(f'{k}-notpresent', text.format(a=c), [f'unmap {D + 0x1000:#x}'], PF(D + 0x1000, write, False)))
                    if write:
                        out.append(case(f'{k}-readonly', text.format(a=c), [f'readonly {D + 0x1000:#x}'], PF(D + 0x1000, True, True)))
                        out.append(case(f'{k}-firstreadonly', text.format(a=c), [f'readonly {D:#x}'], PF(c, True, True)))
                    out.append(case(f'{k}-ok', text.format(a=c)))
            for off in (1, 8, 16):
                m = D + 0x100 + off
                out.append(case(f'{name}-mis{off}', text.format(a=m), [], GP if align and off % align else None))
            if align:
                out.append(case(f'{name}-mis-notpresent', text.format(a=D + 0x1008), [f'unmap {D + 0x1000:#x}'], GP))
                out.append(case(f'{name}-mis-rsp-noncanonical', text.format(a='rsp + 8'), [], GP, regs={'rsp': NONCANON}))
            out.append(case(f'{name}-noncanonical', text.format(a='rdi'), [], GP, regs={'rdi': NONCANON}))
            out.append(case(f'{name}-noncanonical-rsp', text.format(a='rsp'), [], SS, regs={'rsp': NONCANON}))
            if n > 1 and not align:
                out.append(case(f'{name}-straddle', text.format(a='rdi'), [], GP, regs={'rdi': NONCANON - n // 2}))
    # A 32-byte operand at base - 16 (FEX folds a displacement that is a multiple of 16 into the LDP/STP's signed offset): the
    # split check and the resident's completion use base + offset, sign-extended (rsi = the second page's start).
    for name, text, write in [('vmovdqu-y-st-negoff', 'vmovdqu ymmword ptr [rsi - 16], ymm4', True), ('vmovdqu-y-negoff', 'vmovdqu ymm4, ymmword ptr [rsi - 16]', False),
                              ('vmovdqu-y-st-negoff32', 'vmovdqu ymmword ptr [rsi - 32], ymm4', True), ('vmovdqu-y-st-posoff', 'vmovdqu ymmword ptr [rdi + 0xde0], ymm4', True)]:
        a = (D + 0x1000 - 16) if 'negoff' in name and '32' not in name else (D + 0x1000 - 32) if '32' in name else D + 0x200 + 0xde0
        cross = a + 32 > D + 0x1000 and a < D + 0x1000
        out.append(case(f'{name}-notpresent', text, [f'unmap {D + 0x1000:#x}'], PF(D + 0x1000 if cross else a, write, False) if a + 32 > D + 0x1000 else None))
        out.append(case(f'{name}-ok', text))
    # Gating. CPL 0 preludes set CR0 bits; `xcr0` 0 leaves CR4.OSXSAVE clear (XCR0 1), 3 sets it with XCR0 = 3.
    ts = ['mov rax, cr0', 'or rax, 8', 'mov cr0, rax', 'mov eax, 0x200100']
    em = ['mov rax, cr0', 'or rax, 4', 'mov cr0, rax', 'mov eax, 0x200100']
    gated = ['vaddps ymm0, ymm1, ymm2', 'vxorps xmm0, xmm0, xmm0', 'vmovdqu ymm1, ymmword ptr [rax]', 'vmovaps ymm1, ymmword ptr [rax + 1]',
             'vpbroadcastb ymm7, xmm7', 'vmovd xmm7, esi', 'vzeroupper', 'vzeroall', 'vfmadd231ps ymm0, ymm1, ymm2', 'vcvtph2ps ymm1, xmm2',
             'vpmovmskb ecx, ymm0', 'vpgatherdd ymm0, dword ptr [rax + ymm1*4], ymm2', 'vmaskmovps ymm0, ymm1, ymmword ptr [rax]',
             'vmovdqu ymmword ptr [rax], ymm1']
    for g in gated:
        tag = g.split()[0] + ('-mis' if '+ 1' in g else '-st' if g.startswith('vmovdqu ymmword') else '')
        out.append(case(f'ts-{tag}', g, [], NM, prelude=ts, cpl=0))
        out.append(case(f'noosxsave-{tag}', g, [], UD, xcr0=0))
        out.append(case(f'xcr0-3-{tag}', g, [], UD, xcr0=3))
        out.append(case(f'xcr0-3-ts-{tag}', g, [], UD, prelude=ts, cpl=0, xcr0=3))
        out.append(case(f'noosxsave-ts-{tag}', g, [], UD, prelude=ts, cpl=0, xcr0=0))
    # Not gated by CR0.EM or CR4.OSFXSR (the driver's `sse 0`): they run.
    for g in ['vaddps ymm0, ymm1, ymm2', 'vmovdqu ymm1, ymmword ptr [rax]', 'vzeroupper']:
        out.append(case(f'em-runs-{g.split()[0]}', g, [], None, prelude=em, cpl=0))
    # BMI (VEX general-register forms): not gated by OSXSAVE/XCR0/TS.
    for g in ['andn ecx, edx, esi', 'shlx rcx, rdx, rsi', 'rorx ecx, edx, 3', 'bzhi rcx, rdx, rsi']:
        out.append(case(f'bmi-noosxsave-ts-{g.split()[0]}', g, [], None, prelude=ts, cpl=0, xcr0=0))
    # Prefixes before VEX: #UD (also under TS: #UD, not #NM), LOCK too.
    for tag, b in [('66', '0x66'), ('f2', '0xf2'), ('f3', '0xf3'), ('rex', '0x48'), ('lock', '0xf0')]:
        insn = f'.byte {b}, 0xc5, 0xf4, 0x58, 0xc2'          # vaddps ymm0, ymm1, ymm2 after the prefix
        out.append(case(f'prefix-{tag}', insn, [], UD))
        out.append(case(f'prefix-{tag}-ts', insn, [], UD, prelude=ts, cpl=0))
    # A segment prefix is no legacy SSE prefix: DS on vaddps ymm0, ymm1, [rax] runs.
    out.append(case('prefix-ds-runs', '.byte 0x3e, 0xc5, 0xf4, 0x58, 0x00', [], None, prelude=['mov eax, 0x200100']))
    # XSETBV clearing XCR0[2] (CPL 0): the VEX form already run at L becomes #UD there (blocks are keyed by the gating).
    loop = ['xor r8d, r8d', 'L: vaddps ymm0, ymm1, ymm2', 'test r8d, r8d', 'jnz F', 'inc r8d', 'xor ecx, ecx', 'mov eax, 3',
            'xor edx, edx', 'xsetbv', 'jmp L', 'F: nop']
    out.append(case('xsetbv-then-ud', 'vaddps', [], UD, cpl=0, lines=loop, at=1))
    # Refusals: typed stops before any effect (the masked forms would need a non-faulting masked-off element; refused).
    for g in ['vmaskmovps ymm0, ymm1, ymmword ptr [rax]', 'vmaskmovps ymmword ptr [rax], ymm1, ymm2', 'vpmaskmovd ymmword ptr [rax], ymm1, ymm2',
              'vpgatherdd ymm0, dword ptr [rax + ymm1*4], ymm2', 'vmaskmovdqu xmm1, xmm2',
              'vcvtps2ph xmmword ptr [rax], ymm6, 4']:
        out.append(case(f'refused-{g.split()[0]}{"-st" if "ymmword ptr [rax]," in g else ""}', g, [], ['nocode', 'unsupported_form'],
                        prelude=['mov eax, 0x200100']))
    # XSAVE/XRSTOR with live YMM state (component 2 is FEX's avx_high, the same copy the VEX forms use): the image's XMM and
    # YMM_Hi128 areas hold the registers, and after VZEROALL, XRSTOR brings every register back.
    rt = ['mov eax, 7', 'xor edx, edx', f'xsave [{D + 0x800}]', 'vzeroall', f'xrstor [{D + 0x800}]']
    out.append(case('xsave-roundtrip', 'xsave', [f'memory {D + 0xa00:#x} ' + '00' * 64], ['roundtrip'], lines=rt))
    out.append(case('xsave-roundtrip-avx-only', 'xsave', [f'memory {D + 0xa00:#x} ' + '00' * 64], ['roundtrip'],
                    lines=['mov eax, 4', 'xor edx, edx', f'xsave [{D + 0x800}]', 'vzeroupper', f'xrstor [{D + 0x800}]']))
    # Device and unassigned pages (M3, L3): VA 0x210000 mapped to an unassigned page (0xFEB00000: the all-ones shadow, every read
    # form native) or to the I/O APIC's page (a device: no VEX site has a completion record, `unsupported_mmio`); a store to the
    # unassigned page is no MOV form: refused too.
    ones, dev = ['mmio 0x210000 0xfeb00000 unassigned'], ['mmio 0x210000 0xfeb00000']
    for tag, insn in [('vmovdqu-y', 'vmovdqu ymm1, ymmword ptr [0x210000]'), ('vpor-y', 'vpor ymm1, ymm1, ymmword ptr [0x210010]'),
                      ('vbroadcastss', 'vbroadcastss ymm1, dword ptr [0x210004]'), ('vmovdqa-x', 'vmovdqa xmm1, xmmword ptr [0x210000]')]:
        out.append(case(f'ones-{tag}', insn, ones, ['ones', 1]))
        out.append(case(f'device-{tag}', insn, dev, ['stop', 'unsupported_mmio']))
    out.append(case('ones-vmovdqu-y-st', 'vmovdqu ymmword ptr [0x210000], ymm1', ones, ['stop', 'unsupported_mmio']))
    out.append(case('device-vmovdqu-y-st', 'vmovdqu ymmword ptr [0x210000], ymm1', dev, ['stop', 'unsupported_mmio']))
    # I8 review: the YMM upper halves stay cached across register-only vector instructions and are stored before any
    # instruction that can fault or exit: a fault right after such a run (a VEX or GPR memory access, a #DE, an SSE load)
    # reports them exactly (the cut run's state). Twelve registers written (more than the six kept cached).
    chain = ['vpaddd ymm1, ymm1, ymm2', 'vpxor ymm3, ymm1, ymm4', 'vpermq ymm5, ymm3, 0x1b', 'vaddps ymm6, ymm6, ymm7', 'vpshufb ymm8, ymm8, ymm9',
             'vinserti128 ymm10, ymm10, xmm11, 1', 'vperm2i128 ymm12, ymm12, ymm13, 0x21', 'vpunpckhbw ymm14, ymm14, ymm15', 'paddd xmm2, xmm3',
             'vfmadd231ps ymm0, ymm1, ymm2', 'vpbroadcastd ymm7, xmm0', 'vpsravd ymm9, ymm9, ymm4', 'vzeroupper' if False else 'vmovaps ymm11, ymm12',
             'vxorps ymm13, ymm13, ymm13', 'vpmaxub ymm15, ymm15, ymm0']
    for k in (1, 3, len(chain)):
        out.append(case(f'chain{k}-vmovdqu-st-notpresent', f'vmovdqu ymmword ptr [{D + 0x1020}], ymm1', [f'unmap {D + 0x1000:#x}'],
                        PF(D + 0x1020, True, False), prelude=chain[:k]))
        out.append(case(f'chain{k}-mov-notpresent', f'mov rax, qword ptr [{D + 0x1020}]', [f'unmap {D + 0x1000:#x}'], PF(D + 0x1020, False, False),
                        prelude=chain[:k]))
        out.append(case(f'chain{k}-movaps-mis', f'movaps xmm1, xmmword ptr [{D + 0x101}]', [], GP, prelude=chain[:k]))
        out.append(case(f'chain{k}-div0', 'div rbp', [], [0, 0, 0], prelude=chain[:k] + ['xor ebp, ebp']))
        out.append(case(f'chain{k}-vzeroupper-mov-notpresent', f'mov rax, qword ptr [{D + 0x1020}]', [f'unmap {D + 0x1000:#x}'],
                        PF(D + 0x1020, False, False), prelude=chain[:k] + ['vzeroupper']))
    # HADD/HSUB with two QNaNs (SDM: HADDPD DEST[63:0] := SRC1[127:64] + SRC1[63:0], the high element first; HSUBPD
    # DEST[63:0] := SRC1[63:0] - SRC1[127:64]; Vol.1 Table 4-7: two QNaNs give the first source operand's). QEMU 7.2 agrees.
    qa, qb, sa, sb = 0x7ff8000000000aaa, 0x7ff8000000000bbb, 0x7fc00aaa, 0x7fc00bbb
    nan2 = (qa | qb << 64).to_bytes(16, 'little').hex() * 2
    nan4 = b''.join(v.to_bytes(4, 'little') for v in (sa, sb, sa, sb, sa, sb, sa, sb)).hex()
    for name, insn, data, want in [('haddpd', 'haddpd xmm1, xmm1', nan2, [qb, qb]), ('hsubpd', 'hsubpd xmm1, xmm1', nan2, [qa, qa]),
                                   ('vhaddpd-y', 'vhaddpd ymm1, ymm1, ymm1', nan2, [qb, qb, qb, qb]), ('vhsubpd-y', 'vhsubpd ymm1, ymm1, ymm1', nan2, [qa] * 4),
                                   ('haddps', 'haddps xmm1, xmm1', nan4, [sb | sb << 32] * 2), ('hsubps', 'hsubps xmm1, xmm1', nan4, [sa | sa << 32] * 2),
                                   ('vhaddps-y', 'vhaddps ymm1, ymm1, ymm1', nan4, [sb | sb << 32] * 4), ('vhsubps-x', 'vhsubps xmm1, xmm1, xmm1', nan4, [sa | sa << 32] * 2 + [0, 0])]:
        out.append(case(f'nan2-{name}', insn, [f'memory {D + 0x400:#x} {data}'], ['reg', 1, want], ymmh=False,
                        prelude=[f'vmovdqu ymm1, ymmword ptr [{D + 0x400}]']))
    # I8 review: an encoding x86 makes #UD stays an encoding refusal under the gating too (not #NM): VPGATHERDD with a register
    # operand (the reviewer's c4e27d90c1), VAESENC with VEX.L = 1 (VAES not advertised), under TS and without OSXSAVE.
    # (I8 re-review: a gather without a SIB byte, and one whose destination and mask are the same register, likewise.)
    for tag, b in [('gather-reg', '0xc4, 0xe2, 0x7d, 0x90, 0xc1'), ('vaesenc-l1', '0xc4, 0xe2, 0x7d, 0xdc, 0xc2'),
                   ('gather-nosib', '0xc4, 0xe2, 0x7d, 0x90, 0x00'), ('gather-same-dst-mask', '0xc4, 0xe2, 0x7d, 0x90, 0x04, 0x0f'),
                   ('gather-same-dst-index', '0xc4, 0xe2, 0x6d, 0x90, 0x0c, 0x08')]:
        out.append(case(f'ts-invalid-{tag}', f'.byte {b}', [], ['nocode', 'unsupported_form'], prelude=ts, cpl=0))
        out.append(case(f'noosxsave-invalid-{tag}', f'.byte {b}', [], ['nocode', 'unsupported_form'], xcr0=0))
    # VLDMXCSR/VSTMXCSR (I8 review): the LDMXCSR/STMXCSR transaction under the AVX gating (not CR0.EM/CR4.OSFXSR).
    out.append(case('vldmxcsr-ok', f'vldmxcsr dword ptr [{D + 0x400}]', [f'memory {D + 0x400:#x} 805f0000'], ['mxcsr', 0x5f80]))
    out.append(case('vstmxcsr-ok', f'vstmxcsr dword ptr [{D + 0x400}]', [], ['mem', D + 0x400, '801f0000']))
    out.append(case('vldmxcsr-reserved', f'vldmxcsr dword ptr [{D + 0x400}]', [f'memory {D + 0x400:#x} 801f0100'], GP))
    out.append(case('vldmxcsr-unmasked', f'vldmxcsr dword ptr [{D + 0x400}]', [f'memory {D + 0x400:#x} 001f0000'], ['stop', 'unsupported_system']))
    out.append(case('vldmxcsr-notpresent', f'vldmxcsr dword ptr [{D + 0x1010}]', [f'unmap {D + 0x1000:#x}'], PF(D + 0x1010, False, False)))
    out.append(case('vstmxcsr-readonly', f'vstmxcsr dword ptr [{D + 0x1010}]', [f'readonly {D + 0x1000:#x}'], PF(D + 0x1010, True, True)))
    out.append(case('vstmxcsr-noncanonical-rsp', 'vstmxcsr dword ptr [rsp]', [], SS, regs={'rsp': NONCANON}))
    out.append(case('vstmxcsr-fs', f'vstmxcsr dword ptr fs:[{D + 0x400}]', [], ['mem', D + 0x400, '801f0000']))
    for tag, pre, x in [('ts', ts, 7), ('noosxsave', [], 0), ('xcr0-3', [], 3)]:
        out.append(case(f'{tag}-vldmxcsr', f'vldmxcsr dword ptr [{D + 0x400}]', [], NM if tag == 'ts' else UD, prelude=pre, cpl=0 if pre else 3, xcr0=x))
    out.append(case('em-noosfxsr-vstmxcsr-runs', f'vstmxcsr dword ptr [{D + 0x400}]', [], ['mem', D + 0x400, '801f0000'], prelude=em, cpl=0, sse=False))
    # MXCSR flags the corpus cannot judge (QEMU 7.2 never sets DE; SDM tables): ymm1 = 1.0f lanes, ymm2 = the case's data,
    # ymm0 = 0, MXCSR 1F80H before. VCVTPH2PS: "No denormal exception is reported on MXCSR" (a half denormal converts exactly);
    # FMA, VADDPS (a denormal in the upper half) and VCMPPS: DE for a denormal source; VCVTPS2PH: DE, and a tiny inexact
    # result UE and PE ("MXCSR.FTZ is ignored": the half denormal itself, here 0).
    one = '0000803f' * 8
    for name, insn, data, mx in [('vcvtph2ps', 'vcvtph2ps ymm3, xmm2', '0100' + '00' * 30, 0x1f80),
                                 ('vfmadd231ps', 'vfmadd231ps ymm0, ymm1, ymm2', '01000000' + '00' * 28, 0x1f82),
                                 ('vaddps-upper', 'vaddps ymm3, ymm2, ymm0', '00' * 16 + '01000000' + '00' * 12, 0x1f82),
                                 ('vcmpps-eq', 'vcmpps ymm3, ymm2, ymm0, 0', '00' * 20 + '01000000' + '00' * 8, 0x1f82),
                                 ('vcvtps2ph', 'vcvtps2ph xmm3, ymm2, 4', '01000000' + '00' * 28, 0x1fb2)]:
        out.append(case(f'de-{name}', insn, [f'memory {D + 0x400:#x} {one}{data}' + '00' * 32], ['mxcsr', mx], ymmh=False,
                        prelude=[f'vmovdqu ymm1, ymmword ptr [{D + 0x400}]', f'vmovdqu ymm2, ymmword ptr [{D + 0x420}]',
                                 f'vmovdqu ymm0, ymmword ptr [{D + 0x440}]']))
    # The AVX compare predicates' flags (I8 review): ymm2 lane 0 a QNaN beside ymm0 lane 0 a denormal, lane 1 a denormal beside 1.0.
    # Quiet GT_OQ (30) and UNORD_Q (3): no IE; DE only from lane 1 (none beside a NaN). Signalling GT_OS (14) and UNORD_S (19): IE.
    one = '0000803f' * 8
    for pred, mx in [(30, 0x1f82), (3, 0x1f82), (14, 0x1f83), (19, 0x1f83)]:
        out.append(case(f'de-vcmpps-{pred}', f'vcmpps ymm3, ymm2, ymm0, {pred}',
                        [f'memory {D + 0x400:#x} {one}' + '0000c07f' + '01000000' + '00' * 24 + '01000000' + '0000803f' + '00' * 24],
                        ['mxcsr', mx], ymmh=False, prelude=[f'vmovdqu ymm1, ymmword ptr [{D + 0x400}]', f'vmovdqu ymm2, ymmword ptr [{D + 0x420}]',
                                                            f'vmovdqu ymm0, ymmword ptr [{D + 0x440}]']))
    return out


def description(c, code):
    lines = [f'rip {CODE:#x}', 'rflags 0x202', 'gpr ' + ' '.join(f'{c["regs"].get(r, 0):#x}' for r in ref.REGS), f'code {code.hex()}',
             f'region {D:#x} 0x2000', f'cpl {c["cpl"]}', f'sse {int(c["sse"])}'] + ([f'xcr0 {c["xcr0"]}'] if c['xcr0'] else [])
    lines += [f'xmm {i:#x} {lo:#x} {hi:#x}' for i, (lo, hi) in enumerate(XMM)]
    lines += [f'memory {D:#x} ' + bytes(range(256)).hex() * 32]
    return '\n'.join(lines + c['setup']) + '\n'


def run(a, host, runtime, c, lines, tag):
    code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' for i, l in enumerate(lines)), labels=True)
    offsets = [0] + [labels[f'l{i}'] for i in range(len(lines) - 1)]
    work = a.out / 'work' / tag; work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(description(c, code))
    r = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([str(a.max_block)] if a.max_block else []) +
                       vm1.CODE_SHAPE, capture_output=True, text=True)
    if r.returncode: return None, offsets, r.stderr.strip()[-300:]
    return json.loads(r.stdout), offsets, None


def judge(a, host, runtime, c):
    lines = c['pre'] + c['lines'] + AFTER
    vm, offsets, err = run(a, host, runtime, c, lines, c['name'])
    if vm is None: return [f'driver: {err}']
    fault_line = len(c['pre']) + (c['at'] or 0)
    rip = CODE + offsets[fault_line]
    want = c['want']
    if want is None:
        return [] if vm1.at_hlt(vm, CODE + offsets[-1]) else [f"did not reach hlt: {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}"]
    if want[0] == 'stop':
        ok = vm['stop'] == 'resident' and vm['reason'].startswith(want[1]) and int(vm['rip'], 0) == rip
        return [] if ok else [f"stop {vm['stop']} {vm['reason']} rip {vm['rip']}, want {want[1]} at {rip:#x}"]
    if want[0] == 'ones':
        n = want[1]
        got = [int(v, 0) for v in vm['xmm'][2 * n:2 * n + 2]] + ([int(v, 0) for v in vm['ymmh'][2 * n:2 * n + 2]] if 'ymm' in c['insn'] else [])
        ok = vm1.at_hlt(vm, CODE + offsets[-1]) and all(v == 2**64 - 1 for v in got)
        return [] if ok else [f"ymm{n} {got} ({vm['stop']} {vm['reason']}), want all ones"]
    if want[0] == 'reg':
        n, vals = want[1], want[2]
        got = ([int(v, 0) for v in vm['xmm'][2 * n:2 * n + 2]] + [int(v, 0) for v in vm['ymmh'][2 * n:2 * n + 2]])[:len(vals)]
        ok = vm1.at_hlt(vm, CODE + offsets[-1]) and got == vals
        return [] if ok else [f"ymm{n} {[hex(v) for v in got]} ({vm['stop']} {vm['reason']}), want {[hex(v) for v in vals]}"]
    if want[0] == 'mem':
        m = bytes.fromhex(vm['memory'][0]['data'])[want[1] - D:want[1] - D + len(want[2]) // 2].hex()
        ok = vm1.at_hlt(vm, CODE + offsets[-1]) and m == want[2]
        return [] if ok else [f"memory {m} ({vm['stop']} {vm['reason']}), want {want[2]}"]
    if want[0] == 'mxcsr':
        ok = vm1.at_hlt(vm, CODE + offsets[-1]) and int(vm['mxcsr'], 0) == want[1]
        return [] if ok else [f"mxcsr {vm['mxcsr']} ({vm['stop']} {vm['reason']}), want {want[1]:#x}"]
    if want[0] == 'roundtrip':
        cut, _, err = run(a, host, runtime, c, c['pre'] + ['hlt'], c['name'] + '-cut')
        if cut is None: return [f'cut run: {err}']
        problems = [] if vm1.at_hlt(vm, CODE + offsets[-1]) else [f"did not reach hlt: {vm['stop']} {vm['reason']} {vm['fault']}"]
        problems += [f'{k} not restored' for k in ('xmm', 'ymmh') if vm[k] != cut[k]]
        img = bytes.fromhex(vm['memory'][0]['data'])[0x800:0x800 + 832]
        q = lambda b, o: [int.from_bytes(b[o + 8 * i:o + 8 * i + 8], 'little') for i in range(32)]
        if c['lines'][0] == 'mov eax, 7' and q(img, 160) != [int(v, 0) for v in cut['xmm']]: problems.append('image XMM area')
        if q(img, 576) != [int(v, 0) for v in cut['ymmh']]: problems.append('image YMM_Hi128 area')
        if not any(q(img, 576)): problems.append('YMM_Hi128 all zero: the case tests nothing')
        return problems
    if want[0] == 'nocode':
        ok = vm['stop'] == 'no_code' and vm['reason'] == want[1] and int(vm['rip'], 0) == rip
        return [] if ok else [f"stop {vm['stop']} {vm['reason']} rip {vm['rip']}, want no_code {want[1]} at {rip:#x}"]
    problems = []
    got = [int(v, 0) for v in vm['fault']]
    if vm['stop'] != 'guest_fault' or got[:3] != want or got[3] != rip:
        problems.append(f"fault {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}, want {want} at {rip:#x}")
    # The same program cut before the instruction (a hlt there): every register and the data region must be equal.
    if c['at'] is None:
        cut, _, err = run(a, host, runtime, c, c['pre'] + ['hlt'], c['name'] + '-cut')
        if cut is None: return problems + [f'cut run: {err}']
        if not vm1.at_hlt(cut, rip): problems.append(f"cut run did not reach its hlt: {cut['stop']} {cut['reason']} {cut['rip']}")
        for k in ('gpr', 'xmm', 'ymmh', 'mxcsr'):
            if vm[k] != cut[k]: problems.append(f'{k} changed at the fault')
        if vm['memory'][0]['data'] != cut['memory'][0]['data']:
            m, m0 = vm['memory'][0]['data'], cut['memory'][0]['data']
            problems.append(f'memory written at +{next(i for i in range(len(m)) if m[i] != m0[i]) // 2:#x}')
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True); ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int); ap.add_argument('--no-build', action='store_true'); ap.add_argument('--only')
    a = ap.parse_args()
    host, runtime = (ROOT / 'target/release/natemu-host', ROOT / 'target/aarch64-unknown-none/release/natemu-runtime') if a.no_build else vm1.build()
    results, bad = [], 0
    for c in cases():
        if a.only and a.only not in c['name']: continue
        problems = judge(a, host, runtime, c)
        bad += bool(problems)
        results.append({'name': c['name'], 'insn': c['insn'], 'ok': not problems, 'problems': problems})
        if problems: print('FAIL', c['name'], '; '.join(problems)[:400], flush=True)
    (a.out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'{len(results)} cases, {len(results) - bad} as expected, {bad} not')
    sys.exit(1 if bad else 0)

if __name__ == '__main__': main()
