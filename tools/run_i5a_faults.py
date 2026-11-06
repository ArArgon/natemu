#!/usr/bin/env python3
"""Precise faults of SSE forms (boot-features plan, chunk I5a), judged by the SDM rules below (no QEMU run: QEMU user mode
cannot set CR0/CR4 and has no #NM, and its fault state lacks the XMM registers).

    tools/with-env python3 tools/run_i5a_faults.py --fexc FEXC --out DIR [--max-block N]

Each case is a program: a prelude of general-register moves, one SSE instruction, then instructions that must not run; the
data pages may be not present or read-only (`unmap`/`readonly`), the controls set by the prelude at CPL 0 (CR0.TS, CR0.EM)
or by the driver (`sse 0`: CR4.OSFXSR clear). Expected (SDM Vol.2 2.5 exception classes, Vol.3 6.15, LDMXCSR/STMXCSR):
  - #UD with CR0.EM or without CR4.OSFXSR, then #NM with CR0.TS, before anything else (PAUSE, PREFETCHh, the fences,
    MOVNTI and CLFLUSH are not gated: they run);
  - #GP(0) for a legacy SSE 16-byte operand that is not 16-byte aligned (classes 1, 2, 4; not movups/movupd/movdqu/lddqu,
    not a scalar or narrower operand), before any page walk (QEMU's order; an RSP-based one too: #GP, not #SS);
  - #GP(0), or #SS(0) for an RSP/RBP base, for a non-canonical operand (any byte);
  - #PF with CR2 = the first byte's page that faults, in address order (a load or store crossing into a not-present
    second page: CR2 = that page), error P (protection) | W (write intent) | U (CPL 3);
  - LDMXCSR: #GP(0) for a 1 in MXCSR bits 31:16, after its #PF.
At the fault: RIP the SSE instruction, the general registers as the prelude left them, every XMM register and MXCSR as at
the start (the instruction committed nothing), and a store committed no byte (the first page of a crossing store is
unchanged). A case without a fault reaches its `hlt` with the expected registers.
"""
import argparse, json, pathlib, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import run_slice1_vm as vm1, ref

D = 0x200000                      # data: two pages, D and D + 0x1000
CODE = 0x10000
XMM = [(0x1111111111111111 * (i + 1) & (2**64 - 1), 0x0f0e0d0c0b0a0908 + i) for i in range(16)]
GPR0 = {'rax': D + 0x100, 'rbx': D + 0x1000 - 8, 'rcx': 0x1234, 'rdx': 0xabcdef, 'rsi': D + 0x1000, 'rdi': D + 0x200}
NONCANON = 0x0000_8000_0000_0000

# (name, the SSE instruction, intent 'r'/'w', bytes accessed, aligned16 required)
LOADS = [('movaps', 'movaps xmm1, xmmword ptr [{a}]', 16, True), ('movdqa', 'movdqa xmm2, xmmword ptr [{a}]', 16, True),
         ('movups', 'movups xmm1, xmmword ptr [{a}]', 16, False), ('movdqu', 'movdqu xmm3, xmmword ptr [{a}]', 16, False),
         ('lddqu', 'lddqu xmm3, xmmword ptr [{a}]', 16, False), ('addps', 'addps xmm4, xmmword ptr [{a}]', 16, True),
         ('pshufd', 'pshufd xmm5, xmmword ptr [{a}], 27', 16, True), ('punpcklbw', 'punpcklbw xmm6, xmmword ptr [{a}]', 16, True),
         ('ptest', 'ptest xmm7, xmmword ptr [{a}]', 16, True), ('roundps', 'roundps xmm8, xmmword ptr [{a}], 1', 16, True),
         ('movntdqa', 'movntdqa xmm9, xmmword ptr [{a}]', 16, True), ('movss', 'movss xmm1, dword ptr [{a}]', 4, False),
         ('movsd', 'movsd xmm1, qword ptr [{a}]', 8, False), ('movhps', 'movhps xmm1, qword ptr [{a}]', 8, False),
         ('movlpd', 'movlpd xmm1, qword ptr [{a}]', 8, False), ('pinsrd', 'pinsrd xmm1, dword ptr [{a}], 2', 4, False),
         ('pinsrw', 'pinsrw xmm1, word ptr [{a}], 5', 2, False), ('pmovzxbw', 'pmovzxbw xmm1, qword ptr [{a}]', 8, False),
         ('cvtsi2sd', 'cvtsi2sd xmm1, qword ptr [{a}]', 8, False), ('comiss', 'comiss xmm1, dword ptr [{a}]', 4, False),
         ('movq', 'movq xmm1, qword ptr [{a}]', 8, False), ('movd', 'movd xmm1, dword ptr [{a}]', 4, False),
         ('cvtps2pd', 'cvtps2pd xmm1, qword ptr [{a}]', 8, False), ('movddup', 'movddup xmm1, qword ptr [{a}]', 8, False),
         ('crc32', 'crc32 ecx, dword ptr [{a}]', 4, False)]
STORES = [('movaps-st', 'movaps xmmword ptr [{a}], xmm1', 16, True), ('movdqa-st', 'movdqa xmmword ptr [{a}], xmm2', 16, True),
          ('movntdq-st', 'movntdq xmmword ptr [{a}], xmm3', 16, True), ('movntps-st', 'movntps xmmword ptr [{a}], xmm3', 16, True),
          ('movups-st', 'movups xmmword ptr [{a}], xmm1', 16, False), ('movdqu-st', 'movdqu xmmword ptr [{a}], xmm4', 16, False),
          ('movss-st', 'movss dword ptr [{a}], xmm5', 4, False), ('movsd-st', 'movsd qword ptr [{a}], xmm6', 8, False),
          ('movhps-st', 'movhps qword ptr [{a}], xmm7', 8, False), ('movlps-st', 'movlps qword ptr [{a}], xmm7', 8, False),
          ('movq-st', 'movq qword ptr [{a}], xmm8', 8, False), ('movd-st', 'movd dword ptr [{a}], xmm8', 4, False),
          ('pextrw-st', 'pextrw word ptr [{a}], xmm9, 3', 2, False), ('pextrq-st', 'pextrq qword ptr [{a}], xmm9, 1', 8, False),
          ('extractps-st', 'extractps dword ptr [{a}], xmm9, 2', 4, False), ('pextrb-st', 'pextrb byte ptr [{a}], xmm9, 7', 1, False)]
PRELUDE = ['mov ecx, 0x1234', 'mov edx, 0xabcdef']
AFTER = ['mov ecx, 1', 'mov edx, 2', 'hlt']


def case(name, insn, setup=(), want=None, regs=None, mem_check=None, prelude=(), cpl=3, sse=True):
    return {'name': name, 'lines': list(prelude) + PRELUDE + [insn] + AFTER, 'setup': list(setup), 'want': want,
            'regs': {**GPR0, **(regs or {})}, 'mem_check': mem_check, 'cpl': cpl, 'sse': sse, 'insn': insn}


def cases():
    out = []
    PF = lambda cr2, write, present: ['pf', 14, cr2, (1 if present else 0) | (2 if write else 0) | 4]
    for kind, forms in (('r', LOADS), ('w', STORES)):
        write = kind == 'w'
        for name, text, n, aligned in forms:
            # Not present: the operand's page.
            a = D + 0x1010
            out.append(case(f'{name}-notpresent', text.format(a=a), [f'unmap {D + 0x1000:#x}'], PF(a, write, False)))
            if write:
                out.append(case(f'{name}-readonly', text.format(a=a), [f'readonly {D + 0x1000:#x}'], PF(a, True, True),
                                mem_check=(a, n)))
            if n > 1:
                # Crossing into a not-present (or read-only) second page: a misaligned 16-byte one is #GP first.
                c = D + 0x1000 - (n // 2)
                if aligned:
                    out.append(case(f'{name}-crossing-gp', text.format(a=c), [f'unmap {D + 0x1000:#x}'], ['gp', 13, 0, 0]))
                else:
                    out.append(case(f'{name}-crossing-notpresent', text.format(a=c), [f'unmap {D + 0x1000:#x}'],
                                    PF(D + 0x1000, write, False), mem_check=(c, n) if write else None))
                    if write:
                        out.append(case(f'{name}-crossing-readonly', text.format(a=c), [f'readonly {D + 0x1000:#x}'],
                                        PF(D + 0x1000, True, True), mem_check=(c, n)))
                    out.append(case(f'{name}-crossing-ok', text.format(a=c), [], None))
            # Alignment: misaligned 16-byte operands of aligned forms; the same address is fine for the others.
            for off in (1, 8, 15):
                m = D + 0x100 + off
                out.append(case(f'{name}-mis{off}', text.format(a=m), [], ['gp', 13, 0, 0] if aligned else None, mem_check=None))
            if aligned:
                out.append(case(f'{name}-mis-notpresent', text.format(a=D + 0x1008), [f'unmap {D + 0x1000:#x}'], ['gp', 13, 0, 0]))
                # RSP-based, misaligned and non-canonical: #GP(0) (alignment first), not #SS(0).
                out.append(case(f'{name}-mis-rsp-noncanonical', text.format(a='rsp + 8'), [], ['gp', 13, 0, 0],
                                regs={'rsp': NONCANON}))
            # Non-canonical: #GP(0), or #SS(0) through RSP; a canonical straddle (last byte non-canonical).
            out.append(case(f'{name}-noncanonical', text.format(a='rdi'), [], ['gp', 13, 0, 0], regs={'rdi': NONCANON}))
            out.append(case(f'{name}-noncanonical-rsp', text.format(a='rsp'), [], ['ss', 12, 0, 0], regs={'rsp': NONCANON}))
            if n > 1 and not aligned:
                out.append(case(f'{name}-straddle', text.format(a='rdi'), [], ['gp', 13, 0, 0], regs={'rdi': NONCANON - n // 2}))
    # LDMXCSR / STMXCSR: #PF, reserved bits, then the transaction.
    out += [case('ldmxcsr-notpresent', f'ldmxcsr dword ptr [{D + 0x1010}]', [f'unmap {D + 0x1000:#x}'], PF(D + 0x1010, False, False)),
            case('ldmxcsr-reserved', 'ldmxcsr dword ptr [rdi]', [f'memory {D + 0x200:#x} 801f0100'], ['gp', 13, 0, 0]),
            # Clearing an exception mask (I5c: unmasked SIMD exceptions are built, tools/run_i5c.py): it loads.
            case('ldmxcsr-unmasked', 'ldmxcsr dword ptr [rdi]', [f'memory {D + 0x200:#x} 001f0000'], None),
            case('ldmxcsr-masked-ok', 'ldmxcsr dword ptr [rdi]', [f'memory {D + 0x200:#x} c0ff0000'], None),
            case('ldmxcsr-noncanonical-rsp', 'ldmxcsr dword ptr [rsp]', [], ['ss', 12, 0, 0], regs={'rsp': NONCANON}),
            case('stmxcsr-readonly', f'stmxcsr dword ptr [{D + 0x1010}]', [f'readonly {D + 0x1000:#x}'], PF(D + 0x1010, True, True),
                 mem_check=(D + 0x1010, 4)),
            case('stmxcsr-crossing', f'stmxcsr dword ptr [{D + 0xffe}]', [f'unmap {D + 0x1000:#x}'], PF(D + 0x1000, True, False),
                 mem_check=(D + 0xffe, 4))]
    # Gating (CPL 0: the prelude writes CR0; CR4.OSFXSR clear from the driver).
    ts = ['mov rax, cr0', 'or rax, 8', 'mov cr0, rax', 'mov eax, 0x200100']
    em = ['mov rax, cr0', 'or rax, 4', 'mov cr0, rax', 'mov eax, 0x200100']
    both = ['mov rax, cr0', 'or rax, 12', 'mov cr0, rax', 'mov eax, 0x200100']
    gated = ['addps xmm0, xmm1', 'movaps xmm1, xmmword ptr [rax]', 'pshufb xmm2, xmm3', 'ldmxcsr dword ptr [rax]', 'stmxcsr dword ptr [rax]',
             'cvtsi2sd xmm1, rcx', 'movd ecx, xmm2', 'ptest xmm1, xmm2', 'movaps xmm1, xmmword ptr [rax + 1]']
    for g in gated:
        tag = g.split()[0] + ('-mis' if '+ 1' in g else '')
        out.append(case(f'ts-{tag}', g, [], ['nm', 7, 0, 0], prelude=ts, cpl=0))
        out.append(case(f'em-{tag}', g, [], ['ud', 6, 0, 0], prelude=em, cpl=0))
        out.append(case(f'em-ts-{tag}', g, [], ['ud', 6, 0, 0], prelude=both, cpl=0))
        out.append(case(f'noosfxsr-{tag}', g, [], ['ud', 6, 0, 0], sse=False))
        out.append(case(f'noosfxsr-ts-{tag}', g, [], ['ud', 6, 0, 0], prelude=ts, cpl=0, sse=False))
    ungated = ['pause', 'prefetcht0 byte ptr [rax]', 'lfence', 'sfence', 'mfence', 'movnti dword ptr [rax], ecx', 'crc32 ecx, edx',
               'popcnt ecx, edx']
    for g in ungated:
        out.append(case(f'ts-em-noosfxsr-{g.split()[0]}', g, [], None, prelude=both, cpl=0, sse=False))
    # A LOCK prefix on an SSE form: #UD.
    out.append(case('lock-addps', '.byte 0xf0, 0x0f, 0x58, 0xc1', [], ['ud', 6, 0, 0]))   # lock addps xmm0, xmm1
    return out


def description(c, code):
    lines = [f'rip {CODE:#x}', 'rflags 0x202', 'gpr ' + ' '.join(f'{c["regs"].get(r, 0):#x}' for r in ref.REGS), f'code {code.hex()}',
             f'region {D:#x} 0x2000', f'cpl {c["cpl"]}', f'sse {int(c["sse"])}']
    lines += [f'xmm {i:#x} {lo:#x} {hi:#x}' for i, (lo, hi) in enumerate(XMM)]
    lines += [f'memory {D:#x} ' + bytes(range(256)).hex() * 32]          # recognisable bytes in both pages
    return '\n'.join(lines + c['setup']) + '\n'


def judge(c, code, vm, offsets):
    problems = []
    rip = CODE + offsets[len(c['lines']) - len(AFTER) - 1]
    gpr = [int(v, 0) for v in vm['gpr']]
    want_gpr = [c['regs'].get(r, 0) for r in ref.REGS]
    for line in c['lines'][:len(c['lines']) - len(AFTER) - 1]:     # the prelude's register results
        if line == 'mov ecx, 0x1234': want_gpr[1] = 0x1234
        if line == 'mov edx, 0xabcdef': want_gpr[2] = 0xabcdef
        if line.startswith('mov eax, '): want_gpr[0] = int(line.split()[-1], 0)
    if c['want'] and c['want'][0] == 'stop':
        if vm['stop'] != 'resident' or not vm['reason'].startswith(c['want'][1]) or int(vm['rip'], 0) != rip:
            problems.append(f"stop {vm['stop']} {vm['reason']} rip {vm['rip']}, want {c['want'][1]} at {rip:#x}")
    elif c['want']:
        _, vector, cr2, error = c['want']
        got = [int(v, 0) for v in vm['fault']]
        if vm['stop'] != 'guest_fault' or got[:3] != [vector, cr2, error] or got[3] != rip:
            problems.append(f"fault {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}, want {[vector, cr2, error]} at {rip:#x}")
        if 'mov rax, cr0' not in c['lines'] and gpr != want_gpr:
            problems.append(f'gpr {gpr} want {want_gpr}')
        xmm = [int(v, 0) for v in vm['xmm']]
        if xmm != [v for p in XMM for v in p]: problems.append('xmm changed')
        if int(vm['mxcsr'], 0) != 0x1f80: problems.append(f"mxcsr {vm['mxcsr']}")
    elif not vm1.at_hlt(vm, CODE + offsets[-1]):
        problems.append(f"did not reach hlt: {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}")
    if c['mem_check']:
        a, n = c['mem_check']
        mem = bytes.fromhex(vm['memory'][0]['data'])
        if mem[a - D:a - D + n] != (bytes(range(256)) * 32)[a - D:a - D + n]: problems.append(f'bytes at {a:#x} written')
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
        code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' for i, l in enumerate(c['lines'])), labels=True)
        offsets = [0] + [labels[f'l{i}'] for i in range(len(c['lines']) - 1)]
        work = a.out / 'work' / c['name']; work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(description(c, code))
        run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([str(a.max_block)] if a.max_block else []) +
                             vm1.CODE_SHAPE, capture_output=True, text=True)
        if run.returncode:
            problems = [f'driver: {run.stderr.strip()[-300:]}']
        else:
            problems = judge(c, code, json.loads(run.stdout), offsets)
        bad += bool(problems)
        results.append({'name': c['name'], 'insn': c['insn'], 'ok': not problems, 'problems': problems})
        if problems: print('FAIL', c['name'], '; '.join(problems)[:400])
    (a.out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'{len(results)} cases, {len(results) - bad} as expected, {bad} not')
    sys.exit(1 if bad else 0)

if __name__ == '__main__': main()
