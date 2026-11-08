#!/usr/bin/env python3
"""Capacity regressions: CPU63 ownership, cross-page atomics, high-GPA code/tables and final RAM page.

Run under natemu-raw/.vm.lock. Large-RAM cases sparsely touch boundary pages;
they do not claim that the host can sustain the full allocation in physical RAM.
"""
import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/reference')]
import ref
import run_slice3_vm as vm

MAIN, CODE, DATA = 0x110000, 0x120000, 0x200000
ROUNDS = 16


def coherence(n):
    bodies, cpus = [], []
    for v in range(n):
        # Every owner translates the shared code before CPU0 changes it. CPUID
        # after publication supplies x86's cross-modifying-code serialization.
        code = ref.assemble(f'''
mov rbx, {DATA}
lea r13, [rip + first]
mov rax, {CODE}
jmp rax
first:
mov r8, rax
lock bts qword ptr [rbx], {v}
mov rdx, {(1 << n) - 1}
ready: cmp [rbx], rdx
jne ready
''' + (f'''mov byte ptr [{CODE + 1}], 2
mov qword ptr [rbx + 8], 1
''' if v == 0 else '''published: cmp qword ptr [rbx + 8], 1
jne published
''') + f'''
xor eax, eax
cpuid
mov rbx, {DATA}
lea r13, [rip + second]
mov rax, {CODE}
jmp rax
second:
mov r9, rax
mov ecx, {ROUNDS}
again: lock inc qword ptr [rbx + 0xffc]
loop again
lock bts qword ptr [rbx + 16], {v}
mov rdx, {(1 << n) - 1}
done: cmp [rbx + 16], rdx
jne done
mov r10, [rbx + 0xffc]
nop
''')
        # Keep the per-CPU bodies separate from the shared translated page.
        at = MAIN + v * 0x1000 if v < 16 else 0x300000 + v * 0x1000
        bodies.append(f'body {at:#x} {code.hex()}')
        cpus.append(f'cpu\nrip {at:#x}\ncpl 0\nif 0')
    return '\n'.join([*bodies, f'body {CODE:#x} ' + ref.assemble('mov eax, 1\njmp r13').hex(), f'region {CODE:#x} 0x1000',
                      f'memory {DATA:#x} ' + bytes(0x2000).hex(), *cpus]) + '\n'


def boundary():
    end = 35 << 30
    code = ref.assemble(f'''
mov rbx, {end - 0x2004}
lock inc qword ptr [rbx]
mov r10, [rbx]
mov rbx, {end - 1}
mov byte ptr [rbx], 0x5a
movzx r9, byte ptr [rbx]
lea r13, [rip + back]
mov rax, {end - 4096}
jmp rax
back: nop
''')
    return '\n'.join(['identity', f'body {MAIN:#x} {code.hex()}',
                      f'body {end - 4096:#x} ' + ref.assemble('mov r8d, 0x1234\njmp r13').hex(),
                      f'region {end - 0x3000:#x} 0x3000',
                      f'cpu\nrip {MAIN:#x}\ncpl 0\nif 0']) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host', type=pathlib.Path, default=ROOT / 'target/release/natemu-host')
    ap.add_argument('--runtime', type=pathlib.Path, default=ROOT / 'target/aarch64-unknown-none/release/natemu-runtime')
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', help='comma-separated case names')
    a = ap.parse_args()
    cases = [('low-n64', 64, 2 << 30, 0x1000),
             ('fault-n64', 64, 2 << 30, 0x1000),
             ('fault-max-n64', 64, 34 << 30, (35 << 30) - (1 << 20)),
             ('hole-crossing-n4', 4, 4 << 30, (3 << 30) - 0x4000),
             ('high-n64', 64, 8 << 30, 4 << 30),
             ('max-n1', 1, 34 << 30, (35 << 30) - (1 << 20)),
             ('last-page', 1, 34 << 30, 4 << 30)]
    rows = []
    for name, n, ram, place in cases:
        if a.only and name not in a.only.split(','): continue
        for block in (0, 1):
            work = a.out / f'{name}-b{block}'
            extra = ([1] if block else []) + ['--ram', hex(ram), '--place', hex(place), '--watchdog-ms', '300000']
            text = boundary() if name == 'last-page' else coherence(n)
            if name.startswith('fault-'):
                text = f'body {MAIN:#x} 0f0b\n' + f'cpu\nrip {MAIN:#x}\ncpl 3\nif 0\n' * n
            work.mkdir(parents=True, exist_ok=False)
            (work / 'program.txt').write_text(text)
            cmd = [str(a.host.resolve()), str(a.runtime.resolve()), 'run-program', str(a.fexc.resolve()),
                   str((work / 'program.txt').resolve()), str(work.resolve()), *map(str, extra)]
            (work / 'command.json').write_text(json.dumps(cmd, indent=2) + '\n')
            with (work / 'stdout.json').open('w') as stdout, (work / 'stderr').open('w') as stderr:
                try:
                    rc = subprocess.run(cmd, stdout=stdout, stderr=stderr, timeout=330).returncode
                except subprocess.TimeoutExpired:
                    rc = 'timeout'
            err = (work / 'stderr').read_text()
            try: out = json.loads((work / 'stdout.json').read_text())
            except ValueError: out = None
            errors = []
            if rc or not out: errors.append(f'driver {rc}: {err[-500:]}')
            else:
                if len(out['cpus']) != n: errors.append('wrong CPU count')
                for v, c in enumerate(out['cpus']):
                    if name.startswith('fault-'):
                        fault = [int(x, 0) for x in c['fault']]
                        if c['stop'] != 'guest_fault' or fault != [6, 0, 0, MAIN]:
                            errors.append(f'cpu{v}: stop={c["stop"]}, fault={fault}')
                        continue
                    want = [0x1234, 0x5a, 1] if name == 'last-page' else [1, 2, n * ROUNDS]
                    got = [int(c['gpr'][i], 0) for i in (8, 9, 10)]
                    if c['stop'] != 'reached' or got != want:
                        errors.append(f'cpu{v}: stop={c["stop"]}, r8/r9/r10={got}, want={want}')
                    errors += [f'cpu{v}: {e}' for e in vm.per_vcpu_checks(c)]
            row = dict(name=name, n=n, ram_bytes=ram, place=place, block=block, rc=rc, errors=errors)
            rows.append(row)
            print(json.dumps(row), flush=True)
            (a.out / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
            if rc == 'timeout': return 1  # Inspect child cleanup before another VM.
    return int(not rows or any(r['errors'] for r in rows))


if __name__ == '__main__':
    sys.exit(main())
