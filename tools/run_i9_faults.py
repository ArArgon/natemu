#!/usr/bin/env python3
"""Precise faults, gating, #AC and refusals of the I9 forms: MOVBE, MMX, AES-NI/PCLMULQDQ, PDEP/PEXT, FFREEP, SHA (advertised
since November 3) and the unadvertised ADX forms (boot-features plan, chunk I9), judged by the SDM rules below and against natemu's own run of the
same program cut before the instruction (run_i8_faults' harness; QEMU user mode cannot set CR0/CR4 and its fault state lacks
the x87/MMX registers).

    tools/with-env python3 tools/run_i9_faults.py --fexc FEXC --out DIR [--max-block N] [--only SUBSTR]

Expected (SDM Vol.2 per instruction; Vol.3 Table 12-1 and 6.15):
  - #PF (CR2 the first faulting byte in address order; error P | W | U), #GP(0) for a non-canonical operand or one straddling
    the canonical boundary, #SS(0) through RSP, with nothing committed: at the fault the general registers, XMM, MXCSR, the
    data region and the x87/MMX state (MM0..7, TOP, the tag byte, the condition codes and exception flags) equal the run cut
    before the instruction (an MMX instruction's TOP = 0 and all-valid tags are written only after its accesses: patch 0024);
    PUNPCKL* mm, m32 reads 4 bytes (no fault on the next page), MOVD 4, PINSRW 2;
  - #GP(0) for a misaligned legacy AESENC/PCLMULQDQ/SHA* m128 and CVTPD2PI m128 (exception class 4 / 2), none for VEX.128 forms;
  - MMX gating: CR0.EM #UD, else CR0.TS #NM (EMMS too), not CR4.OSFXSR; the MMX forms with an XMM operand (CVTPI2PS...) gate
    like SSE (#UD without OSFXSR); MOVBE, PDEP/PEXT are not gated; FFREEP is x87 (#NM under EM or TS);
  - SHA gates like SSE (#NM under CR0.TS, #UD without CR4.OSFXSR);
  - #UD for MOVBE with a register operand and for ADCX/ADOX (not advertised), whatever their operands, and for SHA with a 66
    or F3 prefix (blank cells of Table A-4);
  - #AC(0) at CPL 3 with CR0.AM and RFLAGS.AC for a misaligned MOVBE (2/4/8), MOVQ/MOVD/PINSRW MMX operand (8/4/2);
  - MASKMOVQ refused (`unsupported_form`) before any effect.
"""
import argparse, json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import run_slice1_vm as vm1, ref
import run_i8_faults as i8
from run_i5a_faults import D, CODE, NONCANON

case = i8.case
X87PRE = ['fninit', 'fld1', 'fldpi']   # TOP 6, two valid tags: an MMX instruction must leave them so when it faults

# (name, instruction, bytes accessed, alignment required, store)
FORMS = [('movbe16', 'movbe cx, word ptr [{a}]', 2, 0, False), ('movbe32', 'movbe ecx, dword ptr [{a}]', 4, 0, False),
         ('movbe64', 'movbe rcx, qword ptr [{a}]', 8, 0, False), ('movbe16-st', 'movbe word ptr [{a}], dx', 2, 0, True),
         ('movbe32-st', 'movbe dword ptr [{a}], edx', 4, 0, True), ('movbe64-st', 'movbe qword ptr [{a}], rdx', 8, 0, True),
         ('paddb', 'paddb mm0, qword ptr [{a}]', 8, 0, False), ('punpcklbw', 'punpcklbw mm1, dword ptr [{a}]', 4, 0, False),
         ('movd-mm', 'movd mm2, dword ptr [{a}]', 4, 0, False), ('movq-mm', 'movq mm3, qword ptr [{a}]', 8, 0, False),
         ('pinsrw-mm', 'pinsrw mm4, word ptr [{a}], 1', 2, 0, False), ('pshufw', 'pshufw mm5, qword ptr [{a}], 0x1b', 8, 0, False),
         ('pshufb-mm', 'pshufb mm6, qword ptr [{a}]', 8, 0, False), ('palignr-mm', 'palignr mm7, qword ptr [{a}], 3', 8, 0, False),
         ('cvtpi2ps', 'cvtpi2ps xmm1, qword ptr [{a}]', 8, 0, False), ('cvtps2pi', 'cvtps2pi mm1, qword ptr [{a}]', 8, 0, False),
         ('cvtpd2pi', 'cvtpd2pi mm1, xmmword ptr [{a}]', 16, 16, False),
         ('movq-mm-st', 'movq qword ptr [{a}], mm1', 8, 0, True), ('movd-mm-st', 'movd dword ptr [{a}], mm2', 4, 0, True),
         ('movntq-st', 'movntq qword ptr [{a}], mm3', 8, 0, True),
         ('aesenc', 'aesenc xmm1, xmmword ptr [{a}]', 16, 16, False), ('aeskeygenassist', 'aeskeygenassist xmm1, xmmword ptr [{a}], 7', 16, 16, False),
         ('pclmulqdq', 'pclmulqdq xmm1, xmmword ptr [{a}], 0x11', 16, 16, False),
         ('vaesdec', 'vaesdec xmm1, xmm2, xmmword ptr [{a}]', 16, 0, False), ('vpclmulqdq', 'vpclmulqdq xmm1, xmm2, xmmword ptr [{a}], 1', 16, 0, False),
         ('pdep64', 'pdep rcx, rdx, qword ptr [{a}]', 8, 0, False), ('pext32', 'pext ecx, edx, dword ptr [{a}]', 4, 0, False)] + [
         (g.split()[0], g, 16, 16, False) for g in ['sha1nexte xmm1, xmmword ptr [{a}]', 'sha1msg1 xmm1, xmmword ptr [{a}]',
         'sha1msg2 xmm1, xmmword ptr [{a}]', 'sha256rnds2 xmm1, xmmword ptr [{a}]', 'sha256msg1 xmm1, xmmword ptr [{a}]',
         'sha256msg2 xmm1, xmmword ptr [{a}]', 'sha1rnds4 xmm1, xmmword ptr [{a}], 2']]


def mmx_shift(op, v, c):
    """SDM PSLLW/PSLLD/PSLLQ, PSRLW/PSRLD/PSRLQ, PSRAW/PSRAD on a 64-bit MMX value: each element shifted by the count."""
    w = {'w': 16, 'd': 32, 'q': 64}[op[-1]]
    out = 0
    for i in range(64 // w):
        e = v >> (w * i) & ((1 << w) - 1)
        if op.startswith('psll'): r = 0 if c >= w else e << c & ((1 << w) - 1)
        elif op.startswith('psrl'): r = 0 if c >= w else e >> c
        else:
            s = e - (1 << w) if e >> (w - 1) else e
            r = (s >> min(c, w - 1)) & ((1 << w) - 1)
        out |= r << (w * i)
    return out


def cases():
    out = []
    PF = lambda cr2, write, present: [14, cr2, (1 if present else 0) | (2 if write else 0) | 4]
    GP, SS, UD, NM, AC = [13, 0, 0], [12, 0, 0], [6, 0, 0], [7, 0, 0], [17, 0, 0]
    for name, text, n, align, write in FORMS:
        pre = X87PRE + (['movq mm1, mm0', 'emms'] if name.endswith('-st') and 'mm' in text else [])   # the MMX stores' registers
        c = lambda tag, a, setup=(), want=None, **k: out.append(case(f'{name}-{tag}', text.format(a=a), list(setup), want, prelude=pre, **k))
        c('ok', D + 0x100)
        c('notpresent', D + 0x1020, [f'unmap {D + 0x1000:#x}'], PF(D + 0x1020, write, False))
        if write: c('readonly', D + 0x1020, [f'readonly {D + 0x1000:#x}'], PF(D + 0x1020, True, True))
        for cr in sorted({D + 0x1000 - n // 2, D + 0x1000 - 1} - {D + 0x1000}) if n > 1 else []:
            k = f'cross{D + 0x1000 - cr}'
            if align: c(f'{k}-gp', cr, [f'unmap {D + 0x1000:#x}'], GP); continue
            c(f'{k}-notpresent', cr, [f'unmap {D + 0x1000:#x}'], PF(D + 0x1000, write, False))
            if write:
                c(f'{k}-readonly', cr, [f'readonly {D + 0x1000:#x}'], PF(D + 0x1000, True, True))
                c(f'{k}-firstreadonly', cr, [f'readonly {D:#x}'], PF(cr, True, True))
            c(f'{k}-ok', cr)
        c('end-of-page', D + 0x1000 - n, [f'unmap {D + 0x1000:#x}'])            # the operand's last byte ends the page: no fault
        if align:
            c('mis8', D + 0x108, [], GP)
            c('mis-notpresent', D + 0x1008, [f'unmap {D + 0x1000:#x}'], GP)
        c('noncanonical', 'rdi', [], GP, regs={'rdi': NONCANON})
        c('noncanonical-rsp', 'rsp', [], SS, regs={'rsp': NONCANON})
        if n > 1: c('straddle', 'rdi', [], GP, regs={'rdi': NONCANON - n // 2})
    # PUNPCKLBW mm, m32 reads 4 bytes (FEX loaded 8): the last 4 bytes of a page before an unmapped one run.
    out.append(case('punpckldq-m32-last4', f'punpckldq mm1, dword ptr [{D + 0x1000 - 4}]', [f'unmap {D + 0x1000:#x}'], None, prelude=X87PRE))
    # A fault right after an MMX instruction: its MMn, TOP = 0 and tags FFH are committed (the cut run has them), the faulting
    # instruction's are not; and after an x87 instruction following MMX in one block.
    for k, pre in [('after-paddb', X87PRE + ['paddb mm0, mm1']), ('after-pinsrw', X87PRE + ['pinsrw mm3, ecx, 2', 'pxor mm3, mm2']),
                   ('after-emms-fld', X87PRE + ['paddb mm0, mm1', 'emms', 'fld1'])]:
        out.append(case(f'{k}-movq-notpresent', f'movq mm2, qword ptr [{D + 0x1020}]', [f'unmap {D + 0x1000:#x}'], PF(D + 0x1020, False, False), prelude=pre))
        out.append(case(f'{k}-movq-st-readonly', f'movq qword ptr [{D + 0x1020}], mm0', [f'readonly {D + 0x1000:#x}'], PF(D + 0x1020, True, True), prelude=pre))
    # Gating (CPL 0 preludes set CR0 bits). MMX: EM #UD (with TS too), TS #NM; not CR4.OSFXSR. CVTPI2PS (an XMM operand): SSE's.
    ts = ['mov rax, cr0', 'or rax, 8', 'mov cr0, rax', 'mov eax, 0x200100']
    em = ['mov rax, cr0', 'or rax, 4', 'mov cr0, rax', 'mov eax, 0x200100']
    emts = ['mov rax, cr0', 'or rax, 12', 'mov cr0, rax', 'mov eax, 0x200100']
    mmx = ['paddb mm0, mm1', 'emms', 'movq mm1, qword ptr [rax]', 'movq qword ptr [rax], mm1', 'pshufb mm0, mm1', 'movd ecx, mm2',
           'psllq mm3, 7', 'pmovmskb ecx, mm1', 'movntq qword ptr [rax], mm1', 'maskmovq mm0, mm1']
    for g in mmx:
        tag = g.split()[0] + ('-m' if '[rax]' in g else '')
        out.append(case(f'ts-{tag}', g, [], NM, prelude=ts, cpl=0))
        out.append(case(f'em-{tag}', g, [], UD, prelude=em, cpl=0))
        out.append(case(f'emts-{tag}', g, [], UD, prelude=emts, cpl=0))
        if g.startswith('maskmovq'): continue
        out.append(case(f'noosfxsr-{tag}-runs', g, [], None, prelude=['mov eax, 0x200100'], sse=False, ymmh=False, xcr0=0))
    for g in ['cvtpi2ps xmm1, mm2', 'cvtps2pi mm1, xmm2', 'movq2dq xmm1, mm2']:
        out.append(case(f'ts-{g.split()[0]}', g, [], NM, prelude=ts, cpl=0))
        out.append(case(f'em-{g.split()[0]}', g, [], UD, prelude=em, cpl=0))
        out.append(case(f'noosfxsr-{g.split()[0]}', g, [], UD, sse=False, ymmh=False, xcr0=0))
    for g in ['aesenc xmm1, xmm2', 'pclmulqdq xmm1, xmm2, 0', 'sha256rnds2 xmm1, xmm2', 'sha1rnds4 xmm1, xmm2, 3']:
        out.append(case(f'ts-{g.split()[0]}', g, [], NM, prelude=ts, cpl=0))
        out.append(case(f'noosfxsr-{g.split()[0]}', g, [], UD, sse=False, ymmh=False, xcr0=0))
    out.append(case('em-sha1nexte', 'sha1nexte xmm1, xmm2', [], UD, prelude=em, cpl=0))
    out.append(case('ts-vaesenc', 'vaesenc xmm1, xmm2, xmm3', [], NM, prelude=ts, cpl=0))
    out.append(case('noosxsave-vaesenc', 'vaesenc xmm1, xmm2, xmm3', [], UD, xcr0=0))
    for g in ['movbe ecx, dword ptr [rax]', 'pdep rcx, rdx, rsi', 'pext ecx, edx, esi']:
        out.append(case(f'ts-em-{g.split()[0]}-runs', g, [], None, prelude=emts, cpl=0))
    out.append(case('ts-ffreep', 'ffreep st(0)', [], NM, prelude=['fld1'] + ts, cpl=0))
    out.append(case('em-ffreep', 'ffreep st(0)', [], NM, prelude=['fld1'] + em, cpl=0))
    # #UD: MOVBE with a register operand (REX.W 0f 38 f0 c1, 66 0f 38 f1 c8), ADX (not advertised), SHA with 66 or F3.
    for tag, b in [('movbe-reg', '0x48, 0x0f, 0x38, 0xf0, 0xc1'), ('movbe-reg-st16', '0x66, 0x0f, 0x38, 0xf1, 0xc8'),
                   ('sha256rnds2-66', '0x66, 0x0f, 0x38, 0xcb, 0xca'), ('sha1msg1-f3', '0xf3, 0x0f, 0x38, 0xc9, 0xca'),
                   ('sha256msg2-f2', '0xf2, 0x0f, 0x38, 0xcd, 0xca'),
                   ('sha1rnds4-66', '0x66, 0x0f, 0x3a, 0xcc, 0xca, 0x01')]:
        out.append(case(tag, f'.byte {b}', [], UD))
    for g in ['adcx rcx, rdx', 'adox rcx, qword ptr [rax]']:
        out.append(case(f'ud-{g.split()[0]}', g, [], UD, prelude=['mov eax, 0x200100']))
    # #AC at CPL 3 (CR0.AM, RFLAGS.AC): the operand's size (SDM Vol.3 6.15 Interrupt 17).
    head = ['mov rax, cr0', 'bts rax, 18', 'mov cr0, rax']
    nop_at = CODE + len(ref.assemble('\n'.join(i8.YMMH + head)))
    for tag, insn, ac in [('movbe16-1', 'movbe cx, word ptr [rdi + 1]', True), ('movbe32-2', 'movbe ecx, dword ptr [rdi + 2]', True),
                          ('movbe64-4', 'movbe rcx, qword ptr [rdi + 4]', True), ('movbe64-8', 'movbe rcx, qword ptr [rdi + 8]', False),
                          ('movbe32-st-2', 'movbe dword ptr [rdi + 2], ecx', True), ('movq-mm-4', 'movq mm1, qword ptr [rdi + 4]', True),
                          ('movq-mm-8', 'movq mm1, qword ptr [rdi + 8]', False), ('movd-mm-2', 'movd mm1, dword ptr [rdi + 2]', True),
                          ('paddb-mm-4', 'paddb mm1, qword ptr [rdi + 4]', True), ('pinsrw-1', 'pinsrw mm1, word ptr [rdi + 1], 0', True),
                          ('pinsrw-2', 'pinsrw mm1, word ptr [rdi + 2], 0', False), ('movq-mm-st-4', 'movq qword ptr [rdi + 4], mm1', True)]:
        out.append(case(f'ac-{tag}', insn, ['rflags 0x40202', f'switch {nop_at:#x} 3'], AC if ac else None, prelude=head + ['nop'] + X87PRE, cpl=0))
    # The MMX transition (TOP = 0, all tags valid): SDM CVTPI2PD "For operands xmm, m64: the instruction does not cause a
    # transition to MMX technology"; CVTPI2PS makes it for both operand forms (its page has no such exception; I9 addendum:
    # QEMU 7.2 agrees there). After X87PRE: TOP 6, tag byte C0H (registers 6 and 7). Judged by the SDM.
    for g, top, ftw in [(f'cvtpi2ps xmm1, qword ptr [{D + 0x100}]', 0, 0xff), (f'cvtpi2pd xmm1, qword ptr [{D + 0x100}]', 6, 0xc0),
                        ('cvtpi2ps xmm1, mm2', 0, 0xff), ('cvtpi2pd xmm1, mm2', 0, 0xff), ('cvtps2pi mm1, xmm2', 0, 0xff),
                        (f'cvttpd2pi mm1, xmmword ptr [{D + 0x100}]', 0, 0xff), ('movq2dq xmm1, mm2', 0, 0xff), ('emms', 6, 0)]:
        out.append(case(f'state-{g.split()[0]}-{"m" if "ptr" in g else "r"}', g, [], ['x87', top, ftw], prelude=X87PRE))
    # MMX shifts by a register or imm8 count (SDM PSLLW..PSRAD: a count above the element width less one gives 0, or the sign
    # for PSRA; the count is the whole 64-bit source), mm0 = 8000_7fff_8001_0001H shifted, checked against that rule.
    v = 0x8000_7fff_8001_0001
    for op in ['psllw', 'pslld', 'psllq', 'psrlw', 'psrld', 'psrlq', 'psraw', 'psrad']:
        w = {'w': 16, 'd': 32, 'q': 64}[op[-1]]
        for c in [0, 1, 7, 15, 16, 17, 31, 32, 33, 63, 64, 255, 0x1_0000_0001]:
            out.append(case(f'shift-{op}-{c:#x}', f'{op} mm0, mm1', [], ['mm', 0, mmx_shift(op, v, c)],
                            prelude=[f'mov rax, {v:#x}', 'movq mm0, rax', f'mov rax, {c:#x}', 'movq mm1, rax']))
            if c < 256:
                out.append(case(f'shift-{op}-imm{c}', f'{op} mm0, {c}', [], ['mm', 0, mmx_shift(op, v, c)], prelude=[f'mov rax, {v:#x}', 'movq mm0, rax']))
    out.append(case('maskmovq-refused', 'maskmovq mm0, mm1', [], ['nocode', 'unsupported_form'], prelude=X87PRE))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True); ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int); ap.add_argument('--no-build', action='store_true'); ap.add_argument('--only')
    a = ap.parse_args()
    host, runtime = (ROOT / 'target/release/natemu-host', ROOT / 'target/aarch64-unknown-none/release/natemu-runtime') if a.no_build else vm1.build()
    seen = {}
    run0 = i8.run
    def run(a_, h, r, c, lines, tag):   # keep each run's state: the x87/MMX comparison below
        vm, offsets, err = run0(a_, h, r, c, lines, tag)
        seen[tag] = vm
        return vm, offsets, err
    i8.run = run
    results, bad = [], 0
    for c in cases():
        if a.only and a.only not in c['name']: continue
        seen.clear()
        if isinstance(c['want'], list) and c['want'][0] in ('x87', 'mm'):   # runs to its hlt; the x87/MMX state after it
            lines = c['pre'] + c['lines'] + i8.AFTER
            vm, offsets, err = run(a, host, runtime, c, lines, c['name'])
            if vm is None: problems = [f'driver: {err}']
            elif not vm1.at_hlt(vm, CODE + offsets[-1]): problems = [f"did not reach hlt: {vm['stop']} {vm['reason']} {vm['fault']}"]
            elif c['want'][0] == 'x87':
                got = (int(vm['x87']['top']), int(vm['x87']['ftw'], 0))
                problems = [] if got == tuple(c['want'][1:]) else [f'TOP, tag byte {got}, want {tuple(c["want"][1:])}']
            else:
                got = int(vm['x87']['mm'][c['want'][1]], 0)
                problems = [] if got == c['want'][2] else [f"mm{c['want'][1]} {got:#x}, want {c['want'][2]:#x}"]
        else:
            problems = i8.judge(a, host, runtime, c)
        vm, cut = seen.get(c['name']), seen.get(c['name'] + '-cut')
        if vm and cut and isinstance(c['want'], list) and isinstance(c['want'][0], int) and vm['x87'] != cut['x87']:
            problems.append(f"x87/MMX state changed at the fault: {vm['x87']} vs {cut['x87']}")
        bad += bool(problems)
        results.append({'name': c['name'], 'insn': c['insn'], 'ok': not problems, 'problems': problems})
        if problems: print('FAIL', c['name'], '; '.join(problems)[:400], flush=True)
    (a.out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'{len(results)} cases, {len(results) - bad} as expected, {bad} not')
    sys.exit(1 if bad else 0)

if __name__ == '__main__': main()
