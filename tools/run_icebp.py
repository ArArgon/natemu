#!/usr/bin/env python3
"""Bounded MOV SS/watchpoint/ICEBP regression, with ordinary and IST1 #DB stacks.

Run under the shared VM lock. Linux's tools/testing/selftests/x86/mov_ss_trap.c
requires a single ICEBP entry, without a second pending watchpoint exception.
Each handler logs the five frame words, frame address and DR6; a second entry
escapes directly to the program end so a broken IST frame cannot hang the test.
"""
import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools/reference'), str(ROOT / 'tools')]
import ref
from models import delivery_model as dm
from models import segment_model as sm

CODE, IDT, PTR, GDT, TSS, LOG, DATA = 0x100000, 0x300000, 0x300800, 0x301000, 0x302000, 0x310000, 0x400000
STACK, IST = 0x3f0000, 0x3e8000
FIXED = 0xffff0ff0


def program(ist, variant):
    watch = variant in ('watch-icebp', 'watch-nop', 'watch-int3')
    ending = {'watch-nop': 'nop', 'watch-int3': 'int3'}.get(variant, '.byte 0xf1')
    setup = f'''lgdt [{PTR + 16:#x}]
mov eax, 0x30
ltr ax
lidt [{PTR:#x}]
mov ax, ss
mov [{DATA:#x}], ax
mov eax, 4
mov dr6, rax
'''
    if watch:
        setup += f'mov eax, {DATA:#x}\nmov dr0, rax\nmov eax, 0x70002\nmov dr7, rax\n'
    body = '' if variant == 'icebp' else f'mov ss, word ptr [{DATA:#x}]\n'
    asm = setup + body + ending + f'''
after:
nop
jmp done
handler:
push rax
push rbx
mov rbx, [{LOG:#x}]
shl rbx, 6
lea rax, [rsp + 16]
mov [{LOG + 8:#x} + rbx], rax
'''
    for slot in range(5):
        asm += f'mov rax, [rsp + {16 + 8 * slot}]\nmov [{LOG + 16 + 8 * slot:#x} + rbx], rax\n'
    asm += f'''mov rax, dr6
mov [{LOG + 56:#x} + rbx], rax
xor eax, eax
mov dr6, rax
inc qword ptr [{LOG:#x}]
cmp qword ptr [{LOG:#x}], 2
jae done
pop rbx
pop rax
iretq
done:
nop
'''
    code, labels = ref.assemble(asm, labels=True)
    idt = bytearray(64)
    for vector in (1, 3):
        idt[vector * 16:vector * 16 + 16] = dm.gate(CODE + labels['handler'], 8, ist=ist if vector == 1 else 0)
    gdt = bytearray(0x40)
    gdt[8:16] = sm.code().to_bytes(8, 'little')
    gdt[16:24] = sm.data().to_bytes(8, 'little')
    gdt[0x30:0x40] = sm.system_descriptor(TSS, 0x67, 9)
    ptr = (63).to_bytes(2, 'little') + IDT.to_bytes(8, 'little') + bytes(6) + (63).to_bytes(2, 'little') + GDT.to_bytes(8, 'little')
    gpr = [0] * 16
    gpr[4] = STACK
    lines = [f'rip {CODE:#x}', 'rflags 2', 'cpl 0', 'if 0', 'gpr ' + ' '.join(map(hex, gpr)), f'code {code.hex()}', 'region 0x3e0000 0x10000']
    for addr, raw in ((IDT, idt), (PTR, ptr), (GDT, gdt), (TSS, dm.tss(ist=(IST,) + (0,) * 6)), (LOG, bytes(0x100)), (DATA, bytes(16))):
        lines.append(f'memory {addr:#x} {raw.hex()}')
    return '\n'.join(lines) + '\n', labels


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', type=pathlib.Path, default=ROOT / 'target/release/natemu-host')
    p.add_argument('--runtime', type=pathlib.Path, default=ROOT / 'target/aarch64-unknown-none/release/natemu-runtime')
    p.add_argument('--fexc', type=pathlib.Path, required=True)
    p.add_argument('--out', type=pathlib.Path, required=True)
    a = p.parse_args()
    rows = []
    for ist in (0, 1):
        for variant in ('icebp', 'movss-icebp', 'watch-icebp', 'watch-nop', 'watch-int3'):
            for block in (None, 1):
                name = f'{variant}-ist{ist}-block{block}'
                outdir = a.out / name
                outdir.mkdir(parents=True, exist_ok=True)
                text, labels = program(ist, variant)
                source = outdir / 'program.txt'
                source.write_text(text)
                cmd = list(map(str, (a.host, a.runtime, 'run-program', a.fexc, source, outdir))) + ['--watchdog-ms', '5000']
                if block: cmd.append(str(block))
                try:
                    run = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    (outdir / 'stdout').write_text(run.stdout)
                    (outdir / 'stderr').write_text(run.stderr)
                    output = json.loads(run.stdout) if run.returncode == 0 else None
                except subprocess.TimeoutExpired:
                    output = None
                row = {'name': name, 'cmd': cmd, 'labels': labels, 'errors': []}
                if not output:
                    row['errors'].append('driver did not return a result')
                else:
                    raw = bytes.fromhex(next(m['data'] for m in output['memory'] if int(m['addr'], 0) == LOG))
                    q = lambda at: int.from_bytes(raw[at:at + 8], 'little')
                    count = q(0)
                    frames = [[q(8 + k * 64 + j * 8) for j in range(7)] for k in range(min(count, 2))]
                    row.update(stop=output['stop'], entries=count, frames=frames)
                    # INT3 deliberately preserves the deferred trap (before its handler).
                    expected = 2 if variant == 'watch-int3' else 1
                    if output['stop'] != 'reached': row['errors'].append(f"stop {output['stop']}")
                    if count != expected: row['errors'].append(f'{count} entries, expected {expected}')
                    if expected == 1 and frames:
                        frame = frames[0]
                        want = [(IST if ist else STACK) - 40, CODE + labels['after'], 8, 2, STACK, 16]
                        if frame[:6] != want:
                            row['errors'].append(f'frame {frame[:6]}, expected {want}')
                    if variant == 'watch-int3' and frames:
                        if [frame[1] for frame in frames] != [CODE + labels['handler'], CODE + labels['after']]:
                            row['errors'].append('INT3 did not retain the deferred watchpoint before its handler')
                    if variant in ('icebp', 'movss-icebp') and frames and frames[0][6] != FIXED | 4:
                        row['errors'].append('standalone ICEBP changed DR6')
                    if variant == 'watch-nop' and frames and not frames[0][6] & 1:
                        row['errors'].append('ordinary deferred watchpoint lost DR6.B0')
                rows.append(row)
                print(name, 'FAIL' if row['errors'] else 'PASS', row.get('entries'), row['errors'], flush=True)
    (a.out / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
    return int(any(row['errors'] for row in rows))


if __name__ == '__main__':
    sys.exit(main())
