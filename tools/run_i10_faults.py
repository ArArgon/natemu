#!/usr/bin/env python3
"""Precise faults of the 0x67 forms, #UD for the reserved and unadvertised encodings, and the CET hint space (boot-features
plan, chunk I10), judged by the SDM rules below and against natemu's own run of the same program cut before the instruction
(run_i8_faults' harness).

    tools/with-env python3 tools/run_i10_faults.py --fexc FEXC --out DIR [--max-block N] [--only SUBSTR]

Expected:
  - 0x67 (SDM Vol.1 3.3.7, Vol.3 4.5.1): the effective address is computed, truncated to 32 bits and zero-extended, then
    the FS/GS base added. A base register whose upper half is garbage (a non-canonical 64-bit value) is no #GP: the truncated
    address is used, and a #PF on it reports CR2 = the truncated address (error P | W | U), with nothing committed (registers,
    vectors, MXCSR and the data region equal the cut run); base + displacement wrapping past 4 GiB lands low; an address
    truncated to 0xFFFFFxxx (canonical) faults #PF there, never #GP; FS.base + the 32-bit address non-canonical is #GP(0);
    EIP-relative wraps; host-decoded forms (FLDCW, LDMXCSR, FXSAVE, INVLPG) use the same address;
  - a 0x67 REP string faulting midway: ESI/EDI/ECX written zero-extended per element (RSI/RDI/RCX upper halves cleared);
    LOOP with 0x67 writes ECX zero-extended; JECXZ tests ECX only;
  - #AC(0) at CPL 3 (CR0.AM, RFLAGS.AC) for a misaligned 0x67 operand, by its truncated address;
  - BT/BTS/BTR/BTC m, r with 0x67: the signed bit-string displacement participates in the 32-bit address calculation;
  - #UD (SDM Vol.3 6.15 reserved opcodes; Vol.2 per instruction): XOP (8f /1../7), EVEX (62), RTM XBEGIN/XABORT (CPUID RTM 0),
    group 11 /1../6, VIA PadLock (0f a6/a7), the reserved VEX L/W/vvvv/operand-kind encodings and VEX opcodes the profile
    does not define, the MMX/SSE shift groups' reserved encodings, F3 MOVBE, 3DNow!, invalid-in-64-bit opcodes, group 4/5/8
    reserved members, INCSSP/SAVEPREVSSP/RSTORSSP/SETSSBSY (#UD with CR4.CET = 0);
  - the CET hint space 0f 1e: ENDBR32/ENDBR64, RDSSPD/RDSSPQ (destination unchanged) and the other encodings run as NOPs,
    with no memory access even through an unmapped or non-canonical address.
"""
import argparse, json, pathlib, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import run_slice1_vm as vm1, ref
import run_i8_faults as i8
from run_i5a_faults import D, CODE, NONCANON

case = i8.case
GARBAGE = 0xdead_beef_0000_0000
PF = lambda cr2, write, present: [14, cr2, (1 if present else 0) | (2 if write else 0) | 4]
GP, UD, AC = [13, 0, 0], [6, 0, 0], [17, 0, 0]
REGS = ['rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15']

# (name, instruction with {m} the 32-bit memory operand, bytes, write)
FORMS = [('load32', 'mov eax, dword ptr {m}', 4, False), ('load64', 'mov rax, qword ptr {m}', 8, False),
         ('store64', 'mov qword ptr {m}, rdx', 8, True), ('store16', 'mov word ptr {m}, dx', 2, True),
         ('rmw32', 'add dword ptr {m}, ecx', 4, True), ('lock-add', 'lock add dword ptr {m}, ecx', 4, True),
         ('xchg', 'xchg qword ptr {m}, rcx', 8, True), ('inc8', 'inc byte ptr {m}', 1, True),
         ('movzx', 'movzx ecx, word ptr {m}', 2, False), ('cmpxchg16b', 'lock cmpxchg16b xmmword ptr {m}', 16, True),
         ('push-m', 'push qword ptr {m}', 8, False), ('movdqu', 'movdqu xmm1, xmmword ptr {m}', 16, False),
         ('movdqu-st', 'movdqu xmmword ptr {m}, xmm2', 16, True), ('vmovdqu-y', 'vmovdqu ymm3, ymmword ptr {m}', 32, False),
         ('vmovdqu-y-st', 'vmovdqu ymmword ptr {m}, ymm4', 32, True), ('fld', 'fld qword ptr {m}', 8, False),
         ('fstp', 'fstp dword ptr {m}', 4, True), ('bt-imm', 'bt dword ptr {m}, 5', 4, False), ('movbe', 'movbe ecx, dword ptr {m}', 4, False),
         ('fldcw', 'fldcw word ptr {m}', 2, False), ('ldmxcsr', 'ldmxcsr dword ptr {m}', 4, False)]


def gpr_case(name, insn, regs, expect, prelude=(), **k):
    """Runs to its hlt; then the general registers in `expect` must hold those values (`insn` lines split at newlines)."""
    c = case(name, insn, [], None, regs=regs, prelude=prelude, **k)
    # i8.AFTER's `mov ecx, 1` follows: RCX is checked through a copy in R12
    c['lines'] = insn.split('\n') + ['mov r12, rcx']
    c['expect'] = {('r12' if r == 'rcx' else r): v for r, v in expect.items()}
    return c


def cases():
    out = []
    for name, text, n, write in FORMS:
        al = 16 if name == 'cmpxchg16b' else 1
        # a valid MXCSR / FCW (all exceptions masked) where the runs load them
        pre = {'ldmxcsr': [f'mov dword ptr [{D + 0x100:#x}], 0x1f80'], 'fldcw': [f'mov word ptr [{D + 0x100:#x}], 0x37f']}.get(name, [])
        c = lambda tag, m, regs, want, setup=(), **k: out.append(case(f'{name}-{tag}', text.format(m=m), list(setup), want, regs=regs, prelude=pre, **k))
        a = D + 0x1020
        # garbage upper half: the truncated address is used (a 64-bit use would be #GP: non-canonical)
        c('garbage-ok', '[edi]', {'rdi': GARBAGE | (D + 0x100)}, None)
        c('garbage-notpresent', '[edi]', {'rdi': GARBAGE | a}, PF(a, write, False), [f'unmap {D + 0x1000:#x}'])
        if write: c('garbage-readonly', '[edi]', {'rdi': GARBAGE | a}, PF(a, True, True), [f'readonly {D + 0x1000:#x}'])
        # base + displacement wrapping past 4 GiB
        c('wrap-notpresent', '[edi + 0x7ffffff0]', {'rdi': GARBAGE | ((a - 0x7ffffff0) & 0xffffffff)}, PF(a, write, False), [f'unmap {D + 0x1000:#x}'])
        c('wrap-sib-notpresent', '[esi + ebx*8 - 0x10]', {'rsi': (a + 0x10 - 8 * 0xfff00000) & 0xffffffff | 1 << 40, 'rbx': 0xfff00000},
          PF(a, write, False), [f'unmap {D + 0x1000:#x}'])
        # truncated to the top of the 4 GiB space (canonical, unmapped): #PF there, not #GP
        c('top-pf', '[edi]', {'rdi': 0xffff_ffff_ffff_f000}, PF(0xffff_f000, write, False))
        c('noncanon64-ok', '[edi]', {'rdi': NONCANON | (D + 0x100)}, None)
        if n > 1 and al == 1 and name not in ('lock-add', 'xchg', 'bt-imm'):   # (an atomic crossing 16 bytes stops, I3; BT reads a byte)
            cr = D + 0x1000 - n // 2
            c('cross-notpresent', '[edi]', {'rdi': GARBAGE | cr}, PF(D + 0x1000, write, False), [f'unmap {D + 0x1000:#x}'])
            if write: c('cross-firstreadonly', '[edi]', {'rdi': GARBAGE | cr}, PF(cr, True, True), [f'readonly {D:#x}'])
        # FS.base + the 32-bit address non-canonical: #GP(0) (the base is added after the truncation)
        if name not in ('push-m', 'fldcw', 'ldmxcsr'):
            c('fs-noncanonical', 'fs:[edi]', {'rdi': GARBAGE | 0x2000}, GP, ['fsbase 0x7ffffffff000'])
            c('fs-ok', 'fs:[edi]', {'rdi': GARBAGE | ((D + 0x100 - 0xffff_ffff_8000_0000) & 0xffffffff)}, None, ['fsbase 0xffffffff80000000'])
    # EIP-relative: RIP + disp truncated (CODE is 0x10000, so -0x20000 wraps to 0xfffxxxxx)
    out.append(case('eip-wrap-pf', 'mov eax, dword ptr [eip - 0x20000]', [], ['eip', -0x20000, False]))
    out.append(case('eip-ok', f'mov eax, dword ptr [eip + {{D}}]', [], None))
    # moffs32 (67 a1 imm32), xlat (67 d7: EBX + AL)
    out.append(case('moffs-pf', '.byte 0x67, 0xa1, 0x20, 0x10, 0x20, 0x00', [f'unmap {D + 0x1000:#x}'], PF(D + 0x1020, False, False)))
    out.append(case('moffs-st-pf', '.byte 0x67, 0x48, 0xa3, 0x20, 0x10, 0x20, 0x00', [f'readonly {D + 0x1000:#x}'], PF(D + 0x1020, True, True)))
    out.append(case('xlat32-pf', '.byte 0x67, 0xd7', [f'unmap {D + 0x1000:#x}'], PF(D + 0x1010, False, False),
                    regs={'rbx': GARBAGE | (D + 0xff8), 'rax': 0x18}))
    out.append(case('xlat32-top-pf', '.byte 0x67, 0xd7', [], PF(0xffff_fffc, False, False), regs={'rbx': 0xffff_ffff_ffff_fff8, 'rax': 0x4}))
    out.append(case('xlat64-noncanonical', 'xlatb', [], GP, regs={'rbx': GARBAGE | (D + 0x10), 'rax': 0}))
    # host-decoded memory forms with 0x67: INVLPG of a garbage-upper address runs; FXSAVE through it
    out.append(case('invlpg-garbage-ok', 'invlpg byte ptr [edi]', [], None, regs={'rdi': GARBAGE | D}, cpl=0))
    out.append(case('fxsave-garbage-ok', 'fxsave [edi]', [], None, regs={'rdi': GARBAGE | (D + 0x200)}))
    out.append(case('fxsave-garbage-pf', 'fxsave [edi]', [f'readonly {D + 0x1000:#x}'], PF(D + 0x1000, True, True), regs={'rdi': GARBAGE | (D + 0xf00)}))
    # #AC at CPL 3 by the truncated address
    head = ['mov rax, cr0', 'bts rax, 18', 'mov cr0, rax']
    nop_at = CODE + len(ref.assemble('\n'.join(i8.YMMH + head)))
    for tag, insn, ac in [('load32-2', 'mov ecx, dword ptr [edi + 2]', True), ('load32-4', 'mov ecx, dword ptr [edi + 4]', False),
                          ('store64-4', 'mov qword ptr [edi + 4], rcx', True), ('rep-movsd', 'rep movsd dword ptr es:[edi], dword ptr [esi]', True)]:
        out.append(case(f'ac-{tag}', insn, ['rflags 0x40202', f'switch {nop_at:#x} 3'], AC if ac else None, prelude=head + ['nop'], cpl=0,
                        regs={'rdi': GARBAGE | (D + 0x100), 'rsi': GARBAGE | (D + 0x202), 'rcx': GARBAGE | 4}))
    # COR-05: the register bit index selects the operand eight bytes beyond the base; upper address bits are ignored.
    for g in ['bt dword ptr [edi], ecx', 'bts qword ptr [edi], rcx']:
        out.append(case(f'addr32-{g.split()[0]}-index-pf', g, [f'unmap {D + 0x1000:#x}'],
                        PF(D + 0x1028, g.startswith('bts'), False), regs={'rdi': GARBAGE | (D + 0x1020), 'rcx': 64}))
    # strings: a 0x67 REP faulting midway (16 elements done: RSI/RDI/RCX written zero-extended)
    rs = {'rsi': GARBAGE | (D + 0xff0), 'rdi': GARBAGE | (D + 0x100), 'rcx': GARBAGE | 0x20}
    c = case('rep-movsb-midway', 'rep movsb byte ptr es:[edi], byte ptr [esi]', [f'unmap {D + 0x1000:#x}'], PF(D + 0x1000, False, False), regs=rs)
    c['at'], c['expect'] = 0, {'rsi': D + 0x1000, 'rdi': D + 0x110, 'rcx': 0x10}
    out.append(c)
    c = case('rep-stosq-down-midway', 'std\nrep stosq qword ptr es:[edi], rax', [f'readonly {D:#x}'], PF(D + 0xff8, True, True),
             regs={'rdi': GARBAGE | (D + 0x1010), 'rcx': GARBAGE | 8})
    c['lines'], c['at'], c['expect'] = ['std', 'rep stosq qword ptr es:[edi], rax'], 1, {'rdi': D + 0xff8, 'rcx': 5}
    out.append(c)
    out.append(gpr_case('lodsd-zext', 'lodsd eax, dword ptr [esi]', {'rsi': GARBAGE | (D + 0x100)}, {'rsi': D + 0x104}))
    out.append(gpr_case('rep-stosb-zext', 'rep stosb byte ptr es:[edi], al', {'rdi': GARBAGE | (D + 0x100), 'rcx': GARBAGE | 3},
                        {'rdi': D + 0x103, 'rcx': 0}))
    out.append(gpr_case('rep-count-ecx0', 'rep stosb byte ptr es:[edi], al', {'rdi': GARBAGE | (D + 0x100), 'rcx': GARBAGE},
                        {'rdi': GARBAGE | (D + 0x100), 'rcx': GARBAGE}))   # ECX = 0: nothing, no register written
    # LOOP/JECXZ with 0x67 (.byte 0x67 before the 64-bit encodings; targets: the next instruction + 2, skipping `inc r9`)
    out.append(gpr_case('loop-ecx1', '.byte 0x67, 0xe2, 0x03\ninc r9', {'rcx': GARBAGE | 1, 'r9': 0}, {'rcx': 0, 'r9': 1}))
    out.append(gpr_case('loop-ecx2', '.byte 0x67, 0xe2, 0x03\ninc r9', {'rcx': GARBAGE | 2, 'r9': 0}, {'rcx': 1, 'r9': 0}))
    out.append(gpr_case('jecxz-taken', '.byte 0x67, 0xe3, 0x03\ninc r9', {'rcx': GARBAGE, 'r9': 0}, {'rcx': GARBAGE, 'r9': 0}))
    out.append(gpr_case('jrcxz-not-taken', 'jrcxz 2f\ninc r9\n2:', {'rcx': GARBAGE, 'r9': 0}, {'r9': 1}))
    out.append(gpr_case('addr32-call', '.byte 0x67, 0xe8, 0, 0, 0, 0\npop r9', {'r9': 0}, {'r9': 'next'}))
    out.append(gpr_case('lea-zext', 'lea rcx, [edi + 0x7fffffff]', {'rdi': GARBAGE | 0x80000002}, {'rcx': 1}))
    out.append(gpr_case('lea16', 'lea cx, [edi + 2]', {'rdi': GARBAGE | 0xffff, 'rcx': 0x1234_5678}, {'rcx': 0x1234_0001}))
    # #UD
    for tag, b in [('xop-vprotd', '0x8f, 0xe8, 0x78, 0xc2, 0xc0, 0x05'), ('pop-1', '0x8f, 0xc8'), ('evex', '0x62, 0xf1, 0x7d, 0x48, 0xef, 0xc0'),
                   ('xbegin', '0xc7, 0xf8, 0, 0, 0, 0'), ('xabort', '0xc6, 0xf8, 0x05'), ('mov-c7-1', '0xc7, 0x08, 0, 0, 0, 0'),
                   ('xstorerng', '0x0f, 0xa7, 0xc0'), ('rep-xcryptecb', '0xf3, 0x0f, 0xa7, 0xc8'), ('montmul', '0xf3, 0x0f, 0xa6, 0xc0'),
                   ('xtest', '0x0f, 0x01, 0xd6'), ('xend', '0x0f, 0x01, 0xd5'),
                   ('vaesenc-l1', '0xc4, 0xe2, 0x75, 0xdc, 0xc2'), ('vmovd-l1', '0xc5, 0xfd, 0x6e, 0xc6'), ('vpermq-l0', '0xc4, 0xe3, 0xf9, 0x00, 0xc1, 0x1b'),
                   ('vpermq-w0', '0xc4, 0xe3, 0x7d, 0x00, 0xc1, 0x1b'), ('vmovaps-vvvv', '0xc5, 0xf4, 0x28, 0xc1'), ('vpdpbusd', '0xc4, 0xe2, 0x79, 0x50, 0xe1'),
                   ('vmovhlps-l1', '0xc5, 0xfc, 0x12, 0xc1'), ('andn-l1', '0xc4, 0xe2, 0x7c, 0xf2, 0xc1'), ('blsr-0', '0xc4, 0xe2, 0x78, 0xf3, 0xc1'),
                   ('vpgatherdd-reg', '0xc4, 0xe2, 0x7d, 0x90, 0xc1'), ('psllq-mm-3', '0x0f, 0x73, 0xd8, 0x01'), ('psrlw-mm-m', '0x0f, 0x71, 0x10'),
                   ('pi2fd', '0x0f, 0x0f, 0xc1, 0x0d'), ('f3-movbe', '0xf3, 0x0f, 0x38, 0xf0, 0x00'), ('daa', '0x27'), ('pusha', '0x60'),
                   ('aad', '0xd5, 0x0a'), ('op82', '0x82, 0xc0, 0x01'), ('fe-2', '0xfe, 0xd0'), ('ff-7', '0xff, 0xf8'), ('ba-0', '0x0f, 0xba, 0x00, 0x01'),
                   ('incsspq', '0xf3, 0x48, 0x0f, 0xae, 0xe8'), ('incsspd', '0xf3, 0x0f, 0xae, 0xe8'), ('saveprevssp', '0xf3, 0x0f, 0x01, 0xea'),
                   ('setssbsy', '0xf3, 0x0f, 0x01, 0xe8'), ('rstorssp', '0xf3, 0x0f, 0x01, 0x28'), ('lock-endbr64', '0xf0, 0xf3, 0x0f, 0x1e, 0xfa'),
                   ('tpause', '0x66, 0x0f, 0xae, 0xf0'), ('umonitor', '0xf3, 0x0f, 0xae, 0xf0'), ('umwait', '0xf2, 0x0f, 0xae, 0xf1'),
                   ('umonitor-67', '0x67, 0xf3, 0x0f, 0xae, 0xf7')]:
        out.append(case(f'ud-{tag}', f'.byte {b}', [], UD, regs={'rax': D + 0x100}))
    # #UD comes before the AVX gating's #NM (CR0.TS) for a reserved VEX encoding (decode faults first)
    ts = ['mov rax, cr0', 'or rax, 8', 'mov cr0, rax', f'mov eax, {D + 0x100:#x}']
    out.append(case('ud-ts-vmovd-l1', '.byte 0xc5, 0xfd, 0x6e, 0xc6', [], UD, prelude=ts, cpl=0))
    out.append(case('nm-ts-vmovd', '.byte 0xc5, 0xf9, 0x6e, 0xc6', [], [7, 0, 0], prelude=ts, cpl=0))
    out.append(gpr_case('mfence-runs', 'mfence', {'rax': 0x1234}, {'rax': 0x1234}))   # NP 0f ae f0: no WAITPKG prefix
    out.append(case('ud-ts-tpause', '.byte 0x66, 0x0f, 0xae, 0xf0', [], UD, prelude=ts, cpl=0))   # not SSE-gated: #UD
    # CET hint space: NOPs; RDSSP's destination unchanged; no access through an unmapped or non-canonical operand
    for tag, b in [('endbr64', '0xf3, 0x0f, 0x1e, 0xfa'), ('endbr32', '0xf3, 0x0f, 0x1e, 0xfb'), ('rdsspq', '0xf3, 0x48, 0x0f, 0x1e, 0xc8'),
                   ('rdsspd', '0xf3, 0x0f, 0x1e, 0xc8'), ('nop-1e-mem-unmapped', '0x0f, 0x1e, 0x07'), ('nop-1e-noncanonical', '0x66, 0x0f, 0x1e, 0x06'),
                   ('f2-1e', '0xf2, 0x0f, 0x1e, 0xc1')]:
        out.append(gpr_case(f'cet-{tag}', f'.byte {b}', {'rax': 0x1234, 'rdi': D + 0x1020, 'rsi': NONCANON}, {'rax': 0x1234},
                            ))
        out[-1]['setup'] = [f'unmap {D + 0x1000:#x}']
    for c in out:
        c['regs'].setdefault('rsp', D + 0x800)
        if c['regs']['rcx'] != 0x1234: c['pre'].append(f"mov rcx, {c['regs']['rcx']:#x}")   # after PRELUDE's `mov ecx, 0x1234`
    return out


def mmio_cases():
    """M3 completion with 0x67: a MOV load and store on a device page through a base whose upper half is garbage (the site's
    truncated address is the device address; the completion record carries no address). (name, asm, check(out))."""
    DEV, GPA, data = 0x200000, 0xfeb0_0000, bytes(range(0x40, 0x50))
    def text(asm, rbx):
        g = [0] * 16; g[3], g[4], g[0] = rbx, 0x3e8000, 0x1122334455667788
        return (f'rip 0x100000\nrflags 0\ngpr {" ".join(hex(v) for v in g)}\ncode {ref.assemble(asm).hex()}\n'
                f'mmio {DEV:#x} {GPA:#x}\nmemory {DEV:#x} {data.hex()}\nregion 0x3e0000 0x10000\n')
    load = lambda out: [] if out['stop'] == 'reached' and int(out['gpr'][1], 0) == int.from_bytes(data[8:12], 'little') else [f"{out['stop']} {out['reason']} rcx {out['gpr'][1]}"]
    def store(out):
        log = out['devices'][0]['log']
        ok = out['stop'] == 'reached' and len(log) == 1 and int(log[0][4], 0) == 0x55667788 and out['mmio']['completed'] == 1
        return [] if ok else [f"{out['stop']} {out['reason']} log {log}"]
    return [('mmio-load', 'mov ecx, dword ptr [ebx + 8]', GARBAGE | DEV, load, text),
            ('mmio-load-wrap', 'mov ecx, dword ptr [ebx + 0x7ffffff8]', GARBAGE | ((DEV + 8 - 0x7ffffff8) & 0xffffffff), load, text),
            ('mmio-store', 'mov dword ptr [ebx + 4], eax', GARBAGE | DEV, store, text)]


def fetch_cases():
    """I10 review: an undecoded #UD encoding is #UD only once fetched through its ModRM, and over 15 bytes it is #GP(0).
    (name, rip, code hex, setup lines, want [vector, CR2, error]): the code ends in hlt; `unmap 0x101000` cuts the
    instruction at the page end (#PF at 0x101000, a user fetch: error U | I/D = 0x14; SDM Vol.3 Table 6-2, fetch first)."""
    PFX, UDF, GPF = [14, 0x101000, 0x14], [6, 0, 0], [13, 0, 0]
    out = []
    for tag, code in [('vex-vnni', 'c4e27850c0'), ('vex-vpdpbusd', 'c4e27950c0'), ('f3-movbe', 'f30f38f000'), ('xop', '8fe878c2c005'),
                      ('evex', '62f17d48efc0')]:
        rip = 0x101000 - (4 if tag != 'xop' else 3) + (1 if tag == 'evex' else 0)   # at least the ModRM on the next page
        out.append((f'fetch-{tag}-absent', rip, code + 'f4', ['unmap 0x101000'], PFX))
        out.append((f'fetch-{tag}-present', rip, code + 'f4', [], UDF))
    for tag, code in [('evex', '62f17d48efc0'), ('vex-vnni', 'c4e27850c0'), ('xop', '8fe878c2c005'), ('fe2', 'fed0')]:
        n = 15 - len(code) // 2
        out.append((f'length-{tag}-15', 0x100000, '2e' * n + code + 'f4', [], UDF))
        out.append((f'length-{tag}-16', 0x100000, '2e' * (n + 1) + code + 'f4', [], GPF))
    return out


def judge(a, host, runtime, c):
    lines = c['pre'] + c['lines'] + i8.AFTER
    want = c['want']
    if isinstance(want, list) and want[0] == 'eip':     # #PF at the truncated EIP-relative address
        code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' for i, l in enumerate(lines)), labels=True)
        end = CODE + labels[f'l{len(c["pre"])}']
        c = dict(c, want=PF((end + want[1]) & 0xffffffff, want[2], False))
        return i8.judge(a, host, runtime, c)
    if '{D}' in c['insn']:     # EIP-relative to D + 0x100: the displacement from the instruction's end
        code, labels = ref.assemble('\n'.join(f'{l.replace("{D}", "0")}\nl{i}:' for i, l in enumerate(lines)), labels=True)
        end = CODE + labels[f'l{len(c["pre"])}']
        return i8.judge(a, host, runtime, dict(c, lines=[l.replace('{D}', str(D + 0x100 - end)) for l in c['lines']]))
    if 'expect' not in c:
        return i8.judge(a, host, runtime, c)
    vm, offsets, err = i8.run(a, host, runtime, c, lines, c['name'])
    if vm is None: return [f'driver: {err}']
    problems = []
    if c['want'] is None:
        if not vm1.at_hlt(vm, CODE + offsets[-1]): return [f"did not reach hlt: {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}"]
    else:
        rip = CODE + offsets[len(c['pre']) + c['at']]
        got = [int(v, 0) for v in vm['fault']]
        if vm['stop'] != 'guest_fault' or got[:3] != c['want'] or got[3] != rip:
            problems.append(f"fault {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}, want {c['want']} at {rip:#x}")
    for r, v in c['expect'].items():
        if v == 'next': v = CODE + offsets[len(c['pre']) + 1]
        got = int(vm['gpr'][REGS.index(r)], 0)
        if got != v: problems.append(f'{r} {got:#x}, want {v:#x}')
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
    for name, rip, code, setup, want in fetch_cases():
        if a.only and a.only not in name: continue
        work = a.out / 'work' / name; work.mkdir(parents=True, exist_ok=True)
        g = ' '.join(['0x0'] * 4 + ['0x3e8000'] + ['0x0'] * 11)
        (work / 'program.txt').write_text(f'rip {rip:#x}\nrflags 0x202\ngpr {g}\ncode {code}\nregion 0x3e0000 0x10000\n' + ''.join(l + '\n' for l in setup))
        r = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([str(a.max_block)] if a.max_block else []) +
                           vm1.CODE_SHAPE, capture_output=True, text=True)
        if r.returncode: problems = [f'driver: {r.stderr.strip()[-300:]}']
        else:
            vm = json.loads(r.stdout); got = [int(v, 0) for v in vm['fault']]
            problems = [] if vm['stop'] == 'guest_fault' and got[:3] == want and got[3] == rip else [f"{vm['stop']} {vm['reason']} {vm['fault']}, want {want} at {rip:#x}"]
        bad += bool(problems)
        results.append({'name': name, 'insn': code, 'ok': not problems, 'problems': problems})
        if problems: print('FAIL', name, '; '.join(problems)[:400], flush=True)
    for name, asm, rbx, check, text in mmio_cases():
        if a.only and a.only not in name: continue
        work = a.out / 'work' / name; work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(text(asm, rbx))
        r = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([str(a.max_block)] if a.max_block else []) +
                           vm1.CODE_SHAPE, capture_output=True, text=True)
        problems = [f'driver: {r.stderr.strip()[-300:]}'] if r.returncode else check(json.loads(r.stdout))
        bad += bool(problems)
        results.append({'name': name, 'insn': asm, 'ok': not problems, 'problems': problems})
        if problems: print('FAIL', name, '; '.join(problems)[:400], flush=True)
    (a.out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'{len(results)} cases, {len(results) - bad} as expected, {bad} not')
    sys.exit(1 if bad else 0)

if __name__ == '__main__': main()
