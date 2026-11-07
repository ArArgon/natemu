#!/usr/bin/env python3
"""RAM vector access completion with CR0.AM/RFLAGS.AC: compare exact state to AC-off.

Run under the shared VM lock. The AC-off execution is a metamorphic reference:
8-byte-aligned 16/32-byte accesses must have the same effects and faults. Explicit
#AC and #GP controls verify that the fix does not disable alignment enforcement.
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/reference')]
import ref
import run_slice3_vm as vm

CODE, DATA = 0x110000, 0x200000
FORMS = [
    ('movups', 'movups xmm2, [{a}]', 16, False),
    ('movupd', 'movupd xmm2, [{a}]', 16, False),
    ('movdqu', 'movdqu xmm2, [{a}]', 16, False),
    ('lddqu', 'lddqu xmm2, [{a}]', 16, False),
    ('movups-store', 'movups [{a}], xmm1', 16, True),
    ('movdqu-store', 'movdqu [{a}], xmm1', 16, True),
    ('vmovdqu-x', 'vmovdqu xmm2, [{a}]', 16, False),
    ('vmovdqu-y', 'vmovdqu ymm2, [{a}]', 32, False),
    ('vmovups-y-store', 'vmovups [{a}], ymm1', 32, True),
    ('vmovdqu-y-store', 'vmovdqu [{a}], ymm1', 32, True),
    ('vaddps-x', 'vaddps xmm2, xmm1, [{a}]', 16, False),
    ('vaddps-y', 'vaddps ymm2, ymm1, [{a}]', 32, False),
    ('vpxor-y', 'vpxor ymm2, ymm1, [{a}]', 32, False),
    ('vinsertf128', 'vinsertf128 ymm2, ymm1, [{a}], 1', 16, False),
]


def program(insn, address, ac, setup=(), watch=False):
    pre = f'vmovdqu ymm1, [{DATA + 0x200:#x}]\nmov rax, cr0\nbts rax, 18\nmov cr0, rax\n'
    if watch:
        pre += f'mov eax, {address:#x}\nmov dr0, rax\nmov eax, 0xb0001\nmov dr7, rax\n'
    pre += f'mov rdi, {address:#x}\nxor eax, eax\n'
    head = ref.assemble(pre)
    body = ref.assemble(insn.format(a='rdi'))
    code = head + b'\x90' + body + ref.assemble('mov r10d, 0x12345678\nnop')
    raw = bytes((i * 13 + 3) % 256 for i in range(8192))
    return '\n'.join([f'rip {CODE:#x}', 'cpl 0', f'rflags {0x40002 if ac else 2:#x}', 'sse 1', 'xcr0 7',
                      f'switch {CODE + len(head):#x} 3', f'code {code.hex()}', f'memory {DATA:#x} {raw.hex()}', *setup]) + '\n', CODE + len(head) + 1, len(body)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host', type=pathlib.Path, default=ROOT / 'target/release/natemu-host')
    ap.add_argument('--runtime', type=pathlib.Path, default=ROOT / 'target/aarch64-unknown-none/release/natemu-runtime')
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', default='')
    args = ap.parse_args()
    cases = []
    for name, insn, width, store in FORMS:
        for offset in (8, 24, 0xff8):
            cases.append((f'{name}-{offset:x}', insn, DATA + offset, (), False, None))
        for suffix, setup in [('missing', [f'unmap {DATA + 0x1000:#x}']), *([('readonly', [f'readonly {DATA + 0x1000:#x}'])] if store else [])]:
            cases.append((f'{name}-cross-{suffix}', insn, DATA + 0xff8, setup, False, 14))
        cases.append((f'{name}-watch', insn, DATA + 8, (), True, 1))
    for name, insn in [('gp-load', 'movaps xmm2, [{a}]'), ('gp-store', 'vmovaps [{a}], ymm1')]:
        cases.append((name, insn, DATA + 8, (), False, 13))
    cases += [('ac-vector4', 'movups xmm2, [{a}]', DATA + 4, (), False, 17),
              ('ac-after-vector', f'movups xmm2, [{{a}}]\nmov rdx, [{DATA + 1:#x}]', DATA + 8, (), False, 17),
              ('noncanonical', 'movups xmm2, [{a}]', 0x800000000008, (), False, 13)]
    rows = []
    for name, insn, address, setup, watch, vector in cases:
        if args.only not in name:
            continue
        for block in (None, 1):
            row = {'name': name, 'block': block, 'errors': []}
            outputs = []
            for ac in (False, True):
                text, at, length = program(insn, address, ac, setup, watch)
                work = args.out / f'{name}-{block}-ac{int(ac)}'
                rc, output, error = vm.driver(args.host, args.runtime, args.fexc, text, work,
                                              [str(block)] if block else [], timeout=30)
                (work / 'result.json').write_text(json.dumps(output, indent=2) + '\n')
                (work / 'stderr').write_text(error)
                outputs.append(output)
                if not output:
                    row['errors'].append(f'ac={ac} no output: {rc} {error[-200:]}')
            baseline, checked = outputs
            if baseline and checked:
                expected_stop = 'guest_fault' if vector else 'reached'
                for label, output in [('baseline', baseline), ('AC', checked)]:
                    want_stop = 'reached' if vector == 17 and label == 'baseline' else expected_stop
                    if output['stop'] != want_stop:
                        row['errors'].append(f'{label} stop {output["stop"]} {output.get("reason")}')
                    if vector and not (vector == 17 and label == 'baseline') and int(output['fault'][0], 0) != vector:
                        row['errors'].append(f'{label} fault {output["fault"]}, expected vector {vector}')
                keys = ('memory',) if vector == 17 else ('gpr', 'xmm', 'ymmh', 'mxcsr', 'memory', 'rip', 'fault')
                for key in keys:
                    if baseline[key] != checked[key]:
                        row['errors'].append(f'{key} differs')
                if vector == 17:
                    fault_at = at + (len(ref.assemble(insn.split('\n')[0].format(a='rdi'))) if '\n' in insn else 0)
                    if int(checked['fault'][3], 0) != fault_at:
                        row['errors'].append('AC fault not at the expected access')
                if (int(baseline['rflags'], 0) ^ int(checked['rflags'], 0)) & ~0x40000:
                    row['errors'].append('rflags differ beyond AC')
            rows.append(row)
            print(name, block, 'FAIL' if row['errors'] else 'PASS', row['errors'], flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
    return int(any(row['errors'] for row in rows))


if __name__ == '__main__':
    sys.exit(main())
